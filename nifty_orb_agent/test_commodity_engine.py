"""
test_commodity_engine.py
----------------------------
Tests for commodity_engine.py: the MCX opening-range scan against a fixed
commodity universe, and run_mcx_commodity_engine()'s per-tracker lot_size
and capital_allocated wiring (equal split across whatever actually made
today's shortlist, not a fixed len(MCX_COMMODITY_UNIVERSE) split).

config.MCX_COMMODITY_UNIVERSE is monkeypatched to a small, deterministic
set of fake symbols for every test here so these never depend on - or need
updating for - whatever real contracts happen to be in config.py.

Run with:
    python test_commodity_engine.py
"""

from __future__ import annotations

import unittest

import config
import commodity_engine
from trade_direction import TradeDirection


_TEST_UNIVERSE = [
    {"symbol": "MCX:TESTGOLD", "lot_size": 10},
    {"symbol": "MCX:TESTSILVER", "lot_size": 5},
    {"symbol": "MCX:TESTCRUDE", "lot_size": 100},
]


class FakeCommodityDataService:
    """Implements get_candles_bulk + get_previous_close - enough for
    opening_range_scanner.scan_symbols_accurate(), which scan_mcx_opening_range()
    delegates to."""

    def __init__(self, candles_by_symbol: dict[str, list], prev_close_by_symbol: dict[str, float]):
        self._candles_by_symbol = candles_by_symbol
        self._prev_close_by_symbol = prev_close_by_symbol

    def get_candles_bulk(self, symbols, resolution, range_from_date, range_to_date):
        return {s: self._candles_by_symbol.get(s, []) for s in symbols}

    def get_previous_close(self, symbol):
        return self._prev_close_by_symbol.get(symbol)


def _window_candle(symbol_open_close: tuple[float, float], volume=1000):
    """Builds one 5-min candle starting at commodity_engine._mcx_915_window_epochs()'s
    own from_epoch, so scan_symbols_accurate's window match finds it regardless of
    whatever MCX_MARKET_OPEN_TIME happens to be configured to in this test run."""
    from_epoch, _ = commodity_engine._mcx_915_window_epochs()
    o, c = symbol_open_close
    h, l = max(o, c), min(o, c)
    return [from_epoch, o, h, l, c, volume]


class TestScanMcxOpeningRange(unittest.TestCase):
    def setUp(self):
        self._orig_universe = config.MCX_COMMODITY_UNIVERSE
        self._orig_bounds = (config.MIN_GAIN_PCT, config.MAX_GAIN_PCT, config.MIN_LOSS_PCT, config.MAX_LOSS_PCT)
        config.MCX_COMMODITY_UNIVERSE = _TEST_UNIVERSE
        config.MIN_GAIN_PCT, config.MAX_GAIN_PCT = 0.5, 5.0
        config.MIN_LOSS_PCT, config.MAX_LOSS_PCT = 0.5, 5.0

    def tearDown(self):
        config.MCX_COMMODITY_UNIVERSE = self._orig_universe
        (config.MIN_GAIN_PCT, config.MAX_GAIN_PCT,
         config.MIN_LOSS_PCT, config.MAX_LOSS_PCT) = self._orig_bounds

    def test_only_symbols_passing_the_long_gain_band_are_shortlisted(self):
        candles = {
            "MCX:TESTGOLD": [_window_candle((100.0, 102.0))],    # +2% vs prev close 100 -> in band
            "MCX:TESTSILVER": [_window_candle((100.0, 100.1))],  # +0.1% -> below MIN_GAIN_PCT, excluded
            "MCX:TESTCRUDE": [_window_candle((100.0, 100.0))],   # flat -> excluded
        }
        prev_close = {"MCX:TESTGOLD": 100.0, "MCX:TESTSILVER": 100.0, "MCX:TESTCRUDE": 100.0}
        data_service = FakeCommodityDataService(candles, prev_close)

        df = commodity_engine.scan_mcx_opening_range(data_service, TradeDirection.LONG)
        self.assertEqual(list(df["symbol"]), ["MCX:TESTGOLD"])

    def test_short_direction_shortlists_decliners(self):
        candles = {
            "MCX:TESTGOLD": [_window_candle((100.0, 97.0))],     # -3% -> in the SHORT band
            "MCX:TESTSILVER": [_window_candle((100.0, 100.1))],  # a gain -> excluded from SHORT
            "MCX:TESTCRUDE": [_window_candle((100.0, 100.0))],
        }
        prev_close = {"MCX:TESTGOLD": 100.0, "MCX:TESTSILVER": 100.0, "MCX:TESTCRUDE": 100.0}
        data_service = FakeCommodityDataService(candles, prev_close)

        df = commodity_engine.scan_mcx_opening_range(data_service, TradeDirection.SHORT)
        self.assertEqual(list(df["symbol"]), ["MCX:TESTGOLD"])

    def test_no_matches_returns_empty_dataframe(self):
        candles = {s["symbol"]: [_window_candle((100.0, 100.0))] for s in _TEST_UNIVERSE}
        prev_close = {s["symbol"]: 100.0 for s in _TEST_UNIVERSE}
        data_service = FakeCommodityDataService(candles, prev_close)

        df = commodity_engine.scan_mcx_opening_range(data_service, TradeDirection.LONG)
        self.assertTrue(df.empty)


class TestRunMcxCommodityEngine(unittest.TestCase):
    def setUp(self):
        self._orig_universe = config.MCX_COMMODITY_UNIVERSE
        self._orig_capital = config.MCX_CAPITAL
        self._orig_bounds = (config.MIN_GAIN_PCT, config.MAX_GAIN_PCT, config.MIN_LOSS_PCT, config.MAX_LOSS_PCT)
        config.MCX_COMMODITY_UNIVERSE = _TEST_UNIVERSE
        config.MCX_CAPITAL = 200_000.0
        config.MIN_GAIN_PCT, config.MAX_GAIN_PCT = 0.5, 5.0
        config.MIN_LOSS_PCT, config.MAX_LOSS_PCT = 0.5, 5.0

    def tearDown(self):
        config.MCX_COMMODITY_UNIVERSE = self._orig_universe
        config.MCX_CAPITAL = self._orig_capital
        (config.MIN_GAIN_PCT, config.MAX_GAIN_PCT,
         config.MIN_LOSS_PCT, config.MAX_LOSS_PCT) = self._orig_bounds

    def test_each_tracker_gets_its_own_lot_size_and_an_equal_capital_split(self):
        candles = {
            "MCX:TESTGOLD": [_window_candle((100.0, 102.0))],    # +2%
            "MCX:TESTSILVER": [_window_candle((100.0, 103.0))],  # +3%
            "MCX:TESTCRUDE": [_window_candle((100.0, 100.0))],   # flat - excluded
        }
        prev_close = {s: 100.0 for s in ("MCX:TESTGOLD", "MCX:TESTSILVER", "MCX:TESTCRUDE")}
        data_service = FakeCommodityDataService(candles, prev_close)

        engine = commodity_engine.run_mcx_commodity_engine(data_service, TradeDirection.LONG)
        self.assertIsNotNone(engine)
        self.assertEqual(set(engine.trackers), {"MCX:TESTGOLD", "MCX:TESTSILVER"})

        gold = engine.trackers["MCX:TESTGOLD"]
        silver = engine.trackers["MCX:TESTSILVER"]
        self.assertEqual(gold.lot_size, 10)
        self.assertEqual(silver.lot_size, 5)
        # 2 symbols shortlisted today -> 200,000 / 2 = 100,000 each, NOT /3 (the full
        # universe's length) - see position_sizing.capital_per_slot.
        self.assertEqual(gold.capital_allocated, 100_000.0)
        self.assertEqual(silver.capital_allocated, 100_000.0)
        self.assertEqual(gold.direction, TradeDirection.LONG)

    def test_short_bias_is_traded_as_is_not_forced_long(self):
        candles = {
            "MCX:TESTGOLD": [_window_candle((100.0, 97.0))],  # -3%
            "MCX:TESTSILVER": [_window_candle((100.0, 100.0))],
            "MCX:TESTCRUDE": [_window_candle((100.0, 100.0))],
        }
        prev_close = {s: 100.0 for s in ("MCX:TESTGOLD", "MCX:TESTSILVER", "MCX:TESTCRUDE")}
        data_service = FakeCommodityDataService(candles, prev_close)

        engine = commodity_engine.run_mcx_commodity_engine(data_service, TradeDirection.SHORT)
        self.assertIsNotNone(engine)
        gold = engine.trackers["MCX:TESTGOLD"]
        self.assertEqual(gold.direction, TradeDirection.SHORT)

    def test_no_shortlisted_commodity_returns_none(self):
        candles = {s["symbol"]: [_window_candle((100.0, 100.0))] for s in _TEST_UNIVERSE}
        prev_close = {s["symbol"]: 100.0 for s in _TEST_UNIVERSE}
        data_service = FakeCommodityDataService(candles, prev_close)

        engine = commodity_engine.run_mcx_commodity_engine(data_service, TradeDirection.LONG)
        self.assertIsNone(engine)

    def test_lot_size_lookup_has_no_entry_for_an_unlisted_symbol(self):
        # commodity_engine.run_mcx_commodity_engine() falls back to lot_size=1 (with a loud
        # error log) for any shortlisted symbol missing from the lookup built here - this
        # confirms the lookup itself is exactly config.MCX_COMMODITY_UNIVERSE's own
        # {symbol: lot_size} mapping, with nothing invented for symbols outside it.
        lot_sizes = commodity_engine._lot_size_lookup()
        self.assertEqual(lot_sizes, {"MCX:TESTGOLD": 10, "MCX:TESTSILVER": 5, "MCX:TESTCRUDE": 100})
        self.assertNotIn("MCX:NOT-IN-UNIVERSE-AT-ALL", lot_sizes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
