"""
test_opening_range_scanner.py
--------------------------------
Tests for opening_range_scanner.py's direction-aware gain/loss filtering
(_passes_gain_filter, _apply_gain_filter) and the LONG-vs-SHORT sort order.
Universe/sector filtering (load_universe, load_valid_sectors) is tested in
test_updates.py; this file is specifically about the LONG/SHORT screening
logic added on top of it.

Run with:
    python test_opening_range_scanner.py
"""

from __future__ import annotations

import unittest

import config
from opening_range_scanner import _apply_gain_filter, _passes_gain_filter
from trade_direction import TradeDirection


class TestPassesGainFilter(unittest.TestCase):
    def setUp(self):
        self._orig = (config.MIN_GAIN_PCT, config.MAX_GAIN_PCT, config.MIN_LOSS_PCT, config.MAX_LOSS_PCT)
        config.MIN_GAIN_PCT, config.MAX_GAIN_PCT = 0.5, 2.5
        config.MIN_LOSS_PCT, config.MAX_LOSS_PCT = 0.5, 2.5

    def tearDown(self):
        (config.MIN_GAIN_PCT, config.MAX_GAIN_PCT,
         config.MIN_LOSS_PCT, config.MAX_LOSS_PCT) = self._orig

    def test_long_band_boundaries(self):
        self.assertFalse(_passes_gain_filter(0.5, TradeDirection.LONG))   # exclusive lower bound
        self.assertTrue(_passes_gain_filter(0.51, TradeDirection.LONG))
        self.assertTrue(_passes_gain_filter(1.5, TradeDirection.LONG))
        self.assertTrue(_passes_gain_filter(2.5, TradeDirection.LONG))    # inclusive upper bound
        self.assertFalse(_passes_gain_filter(2.51, TradeDirection.LONG))
        self.assertFalse(_passes_gain_filter(0.0, TradeDirection.LONG))
        self.assertFalse(_passes_gain_filter(-1.5, TradeDirection.LONG))  # a decline is never a LONG candidate

    def test_short_band_is_the_mirror_image(self):
        self.assertFalse(_passes_gain_filter(-0.5, TradeDirection.SHORT))   # exclusive bound, mirrored
        self.assertTrue(_passes_gain_filter(-0.51, TradeDirection.SHORT))
        self.assertTrue(_passes_gain_filter(-1.5, TradeDirection.SHORT))
        self.assertTrue(_passes_gain_filter(-2.5, TradeDirection.SHORT))    # inclusive bound, mirrored
        self.assertFalse(_passes_gain_filter(-2.51, TradeDirection.SHORT))
        self.assertFalse(_passes_gain_filter(0.0, TradeDirection.SHORT))
        self.assertFalse(_passes_gain_filter(1.5, TradeDirection.SHORT))    # a gain is never a SHORT candidate

    def test_asymmetric_long_and_short_thresholds_are_independent(self):
        config.MIN_GAIN_PCT, config.MAX_GAIN_PCT = 1.0, 3.0
        config.MIN_LOSS_PCT, config.MAX_LOSS_PCT = 0.2, 1.0
        self.assertFalse(_passes_gain_filter(0.5, TradeDirection.LONG))    # below the (now higher) LONG minimum
        self.assertTrue(_passes_gain_filter(2.0, TradeDirection.LONG))
        self.assertTrue(_passes_gain_filter(-0.5, TradeDirection.SHORT))   # within the (now tighter) SHORT band
        self.assertFalse(_passes_gain_filter(-2.0, TradeDirection.SHORT))  # outside the tighter SHORT band


class TestApplyGainFilter(unittest.TestCase):
    def test_long_sorts_biggest_gainers_first(self):
        rows = [
            {"symbol": "A", "pct_change": 0.8},
            {"symbol": "B", "pct_change": 2.2},
            {"symbol": "C", "pct_change": 1.5},
        ]
        df = _apply_gain_filter(rows, TradeDirection.LONG)
        self.assertEqual(list(df["symbol"]), ["B", "C", "A"])

    def test_short_sorts_biggest_losers_first(self):
        rows = [
            {"symbol": "A", "pct_change": -0.8},
            {"symbol": "B", "pct_change": -2.2},
            {"symbol": "C", "pct_change": -1.5},
        ]
        df = _apply_gain_filter(rows, TradeDirection.SHORT)
        self.assertEqual(list(df["symbol"]), ["B", "C", "A"])  # -2.2 is the "biggest loser"

    def test_default_direction_is_long_for_backward_compatibility(self):
        rows = [{"symbol": "A", "pct_change": 0.8}, {"symbol": "B", "pct_change": 2.2}]
        df = _apply_gain_filter(rows)  # no direction argument at all
        self.assertEqual(list(df["symbol"]), ["B", "A"])

    def test_empty_rows_returns_empty_dataframe_without_crashing(self):
        df = _apply_gain_filter([], TradeDirection.SHORT)
        self.assertTrue(df.empty)

    def test_respects_top_n_stocks_limit(self):
        orig = config.TOP_N_STOCKS
        config.TOP_N_STOCKS = 2
        try:
            rows = [{"symbol": s, "pct_change": pct} for s, pct in
                    [("A", 1.0), ("B", 2.0), ("C", 1.5), ("D", 2.4)]]
            df = _apply_gain_filter(rows, TradeDirection.LONG)
            self.assertEqual(len(df), 2)
            self.assertEqual(list(df["symbol"]), ["D", "B"])
        finally:
            config.TOP_N_STOCKS = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
