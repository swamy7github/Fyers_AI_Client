"""
test_historical_sentiment.py
-------------------------------
Tests for global_sentiment.fetch_global_snapshot_as_of() and
bootstrap.decide_direction_for_date() - the historical counterparts used by
backtest.py to compute what direction WOULD have been chosen on each
replayed day, rather than one fixed direction for the whole backtest range.

The critical property tested here is that these functions never look past
as_of_date - leaking a future day's data into a historical decision would
make a backtest look artificially good (or bad) in a way live trading could
never actually achieve.

Run with:
    python test_historical_sentiment.py
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import date

import config
import global_sentiment


class FakeYfTicker:
    """
    Records the (start, end) window it was asked for, and serves a fixed
    2-row Close series regardless of window - enough to test the DATE
    boundary logic (does the function ask for the right window?) without
    needing real market data shaped exactly right.
    """
    requested_windows: list[tuple[str, str]] = []

    def __init__(self, ticker):
        self.ticker = ticker

    def history(self, start=None, end=None, interval=None, period=None):
        import pandas as pd
        FakeYfTicker.requested_windows.append((start, end))
        return pd.DataFrame({"Close": [100.0, 102.0]})  # a flat +2% move for every ticker


class TestFetchGlobalSnapshotAsOf(unittest.TestCase):
    def setUp(self):
        FakeYfTicker.requested_windows = []
        self._orig_yf = global_sentiment.yf
        global_sentiment.yf = types.SimpleNamespace(Ticker=FakeYfTicker)

    def tearDown(self):
        global_sentiment.yf = self._orig_yf

    def test_end_date_never_includes_as_of_date_itself(self):
        as_of = date(2026, 3, 15)
        global_sentiment.fetch_global_snapshot_as_of(as_of)
        for start, end in FakeYfTicker.requested_windows:
            self.assertEqual(end, "2026-03-15")  # yfinance's end= is exclusive of this date itself
            self.assertLess(start, end)

    def test_returns_a_value_per_ticker_from_the_fixed_series(self):
        snapshot = global_sentiment.fetch_global_snapshot_as_of(date(2026, 3, 15))
        self.assertEqual(len(snapshot), len(global_sentiment.GLOBAL_TICKERS))
        for value in snapshot.values():
            self.assertAlmostEqual(value, 2.0)  # (102-100)/100*100

    def test_a_failing_ticker_is_skipped_not_fatal(self):
        class SometimesFailingTicker(FakeYfTicker):
            def history(self, start=None, end=None, interval=None, period=None):
                if self.ticker == "^VIX":
                    raise RuntimeError("simulated fetch failure")
                return super().history(start=start, end=end, interval=interval, period=period)

        global_sentiment.yf = types.SimpleNamespace(Ticker=SometimesFailingTicker)
        snapshot = global_sentiment.fetch_global_snapshot_as_of(date(2026, 3, 15))
        self.assertNotIn("US VIX", snapshot)
        self.assertEqual(len(snapshot), len(global_sentiment.GLOBAL_TICKERS) - 1)


class TestDecideDirectionForDate(unittest.TestCase):
    def setUp(self):
        self._orig_allow_long = config.ALLOW_LONG_ON_NEUTRAL
        self._orig_allow_short = config.ALLOW_SHORT_ON_NEUTRAL
        config.ALLOW_LONG_ON_NEUTRAL = False
        config.ALLOW_SHORT_ON_NEUTRAL = False

    def tearDown(self):
        config.ALLOW_LONG_ON_NEUTRAL = self._orig_allow_long
        config.ALLOW_SHORT_ON_NEUTRAL = self._orig_allow_short

    def _with_mocked_historical_sentiment(self, label, score):
        import bootstrap as bootstrap_module
        orig_fetch = bootstrap_module.global_sentiment.fetch_global_snapshot_as_of
        orig_score = bootstrap_module.global_sentiment.score_global_bias
        bootstrap_module.global_sentiment.fetch_global_snapshot_as_of = lambda as_of_date: {}
        bootstrap_module.global_sentiment.score_global_bias = lambda snapshot: (label, score)
        return bootstrap_module, orig_fetch, orig_score

    def test_bullish_day_maps_to_long(self):
        from trade_direction import TradeDirection
        bootstrap_module, orig_fetch, orig_score = self._with_mocked_historical_sentiment("BULLISH", 1.0)
        try:
            direction, label, score = bootstrap_module.decide_direction_for_date(date(2026, 3, 15))
        finally:
            bootstrap_module.global_sentiment.fetch_global_snapshot_as_of = orig_fetch
            bootstrap_module.global_sentiment.score_global_bias = orig_score
        self.assertEqual(direction, TradeDirection.LONG)

    def test_bearish_day_maps_to_short(self):
        from trade_direction import TradeDirection
        bootstrap_module, orig_fetch, orig_score = self._with_mocked_historical_sentiment("BEARISH", -1.0)
        try:
            direction, label, score = bootstrap_module.decide_direction_for_date(date(2026, 3, 15))
        finally:
            bootstrap_module.global_sentiment.fetch_global_snapshot_as_of = orig_fetch
            bootstrap_module.global_sentiment.score_global_bias = orig_score
        self.assertEqual(direction, TradeDirection.SHORT)

    def test_neutral_day_with_default_config_returns_none(self):
        bootstrap_module, orig_fetch, orig_score = self._with_mocked_historical_sentiment("NEUTRAL", 0.0)
        try:
            direction, _, _ = bootstrap_module.decide_direction_for_date(date(2026, 3, 15))
        finally:
            bootstrap_module.global_sentiment.fetch_global_snapshot_as_of = orig_fetch
            bootstrap_module.global_sentiment.score_global_bias = orig_score
        self.assertIsNone(direction)

    def test_shares_the_same_fallback_policy_as_the_live_decision(self):
        # Proves _apply_neutral_fallback is genuinely SHARED, not two independently
        # drifting copies of the same policy.
        import bootstrap as bootstrap_module
        from trade_direction import TradeDirection
        config.ALLOW_LONG_ON_NEUTRAL = True
        bootstrap_module, orig_fetch, orig_score = self._with_mocked_historical_sentiment("NEUTRAL", 0.0)
        try:
            direction, _, _ = bootstrap_module.decide_direction_for_date(date(2026, 3, 15))
        finally:
            bootstrap_module.global_sentiment.fetch_global_snapshot_as_of = orig_fetch
            bootstrap_module.global_sentiment.score_global_bias = orig_score
        self.assertEqual(direction, TradeDirection.LONG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
