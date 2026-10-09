"""
test_commodity_engine.py
----------------------------
Tests for commodity_engine.py: fetch_mcx_universe_opening_ranges() (fetches EVERY symbol in
config.MCX_COMMODITY_UNIVERSE's own opening-range candle, UNFILTERED - no gain/loss-band
shortlisting, unlike equities) and run_mcx_commodity_engine()'s per-tracker lot_size and
capital_allocated wiring (equal split across whichever universe symbols actually had a
candle available today, not a fixed assumption that all of them did).

config.MCX_COMMODITY_UNIVERSE is monkeypatched to a small, deterministic set of fake
symbols for every test here so these never depend on - or need updating for - whatever
real contracts happen to be in config.py.

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
    """Implements only get_candles_bulk - enough for fetch_mcx_universe_opening_ranges()."""

    def __init__(self, candles_by_symbol: dict[str, list]):
        self._candles_by_symbol = candles_by_symbol

    def get_candles_bulk(self, symbols, resolution, range_from_date, range_to_date):
        return {s: self._candles_by_symbol.get(s, []) for s in symbols}


def _window_candle(open_close: tuple[float, float], volume=1000):
    """Builds one 5-min candle starting at commodity_engine._mcx_915_window_epochs()'s own
    from_epoch, so the window match finds it regardless of whatever MCX_MARKET_OPEN_TIME
    happens to be configured to in this test run."""
    from_epoch, _ = commodity_engine._mcx_915_window_epochs()
    o, c = open_close
    h, l = max(o, c), min(o, c)
    return [from_epoch, o, h, l, c, volume]


class TestFetchMcxUniverseOpeningRanges(unittest.TestCase):
    def setUp(self):
        self._orig_universe = config.MCX_COMMODITY_UNIVERSE
        config.MCX_COMMODITY_UNIVERSE = _TEST_UNIVERSE

    def tearDown(self):
        config.MCX_COMMODITY_UNIVERSE = self._orig_universe

    def test_every_universe_symbol_is_included_regardless_of_its_price_move(self):
        """The whole point of this round's change: NO gain/loss-band filtering - a flat
        or even a losing open still gets tracked, unlike equities' shortlist scan."""
        candles = {
            "MCX:TESTGOLD": [_window_candle((100.0, 102.0))],   # +2% - would have passed a gain filter
            "MCX:TESTSILVER": [_window_candle((100.0, 100.1))],  # +0.1% - would have been excluded before
            "MCX:TESTCRUDE": [_window_candle((100.0, 100.0))],   # flat - would have been excluded before
        }
        data_service = FakeCommodityDataService(candles)

        df = commodity_engine.fetch_mcx_universe_opening_ranges(data_service)
        self.assertEqual(set(df["symbol"]), {"MCX:TESTGOLD", "MCX:TESTSILVER", "MCX:TESTCRUDE"})

    def test_a_losing_open_is_still_included(self):
        candles = {s["symbol"]: [_window_candle((100.0, 97.0))] for s in _TEST_UNIVERSE}  # -3% each
        data_service = FakeCommodityDataService(candles)

        df = commodity_engine.fetch_mcx_universe_opening_ranges(data_service)
        self.assertEqual(set(df["symbol"]), {"MCX:TESTGOLD", "MCX:TESTSILVER", "MCX:TESTCRUDE"})

    def test_a_symbol_with_no_candle_yet_is_excluded_but_others_still_included(self):
        candles = {
            "MCX:TESTGOLD": [_window_candle((100.0, 102.0))],
            # MCX:TESTSILVER has no candle at all today (data gap)
            "MCX:TESTCRUDE": [_window_candle((100.0, 100.0))],
        }
        data_service = FakeCommodityDataService(candles)

        df = commodity_engine.fetch_mcx_universe_opening_ranges(data_service)
        self.assertEqual(set(df["symbol"]), {"MCX:TESTGOLD", "MCX:TESTCRUDE"})

    def test_no_candles_available_for_anyone_returns_empty_dataframe(self):
        data_service = FakeCommodityDataService({})
        df = commodity_engine.fetch_mcx_universe_opening_ranges(data_service)
        self.assertTrue(df.empty)

    def test_returned_high_low_feed_directly_into_tracker_opening_range(self):
        candles = {"MCX:TESTGOLD": [_window_candle((100.0, 103.0))]}  # o=100, c=103 -> h=103, l=100
        data_service = FakeCommodityDataService(candles)
        df = commodity_engine.fetch_mcx_universe_opening_ranges(data_service)
        row = df[df["symbol"] == "MCX:TESTGOLD"].iloc[0]
        self.assertEqual(row["high"], 103.0)
        self.assertEqual(row["low"], 100.0)


class TestRunMcxCommodityEngine(unittest.TestCase):
    def setUp(self):
        self._orig_universe = config.MCX_COMMODITY_UNIVERSE
        self._orig_capital = config.MCX_CAPITAL
        config.MCX_COMMODITY_UNIVERSE = _TEST_UNIVERSE
        config.MCX_CAPITAL = 200_000.0

    def tearDown(self):
        config.MCX_COMMODITY_UNIVERSE = self._orig_universe
        config.MCX_CAPITAL = self._orig_capital

    def test_the_whole_universe_is_tracked_with_an_equal_capital_split(self):
        candles = {
            "MCX:TESTGOLD": [_window_candle((100.0, 102.0))],    # +2%
            "MCX:TESTSILVER": [_window_candle((100.0, 103.0))],  # +3%
            "MCX:TESTCRUDE": [_window_candle((100.0, 100.0))],   # flat - still tracked (no filter)
        }
        data_service = FakeCommodityDataService(candles)

        engine = commodity_engine.run_mcx_commodity_engine(data_service, TradeDirection.LONG)
        self.assertIsNotNone(engine)
        self.assertEqual(set(engine.trackers), {"MCX:TESTGOLD", "MCX:TESTSILVER", "MCX:TESTCRUDE"})

        gold = engine.trackers["MCX:TESTGOLD"]
        silver = engine.trackers["MCX:TESTSILVER"]
        crude = engine.trackers["MCX:TESTCRUDE"]
        self.assertEqual(gold.lot_size, 10)
        self.assertEqual(silver.lot_size, 5)
        self.assertEqual(crude.lot_size, 100)
        # all 3 universe symbols tracked today -> 200,000 / 3 each.
        expected_per_slot = 200_000.0 / 3
        self.assertAlmostEqual(gold.capital_allocated, expected_per_slot)
        self.assertAlmostEqual(silver.capital_allocated, expected_per_slot)
        self.assertAlmostEqual(crude.capital_allocated, expected_per_slot)
        self.assertEqual(gold.direction, TradeDirection.LONG)

    def test_short_bias_is_traded_as_is_not_forced_long(self):
        candles = {s["symbol"]: [_window_candle((100.0, 97.0))] for s in _TEST_UNIVERSE}
        data_service = FakeCommodityDataService(candles)

        engine = commodity_engine.run_mcx_commodity_engine(data_service, TradeDirection.SHORT)
        self.assertIsNotNone(engine)
        gold = engine.trackers["MCX:TESTGOLD"]
        self.assertEqual(gold.direction, TradeDirection.SHORT)

    def test_a_symbol_missing_its_candle_is_excluded_but_the_rest_still_trade(self):
        candles = {
            "MCX:TESTGOLD": [_window_candle((100.0, 102.0))],
            "MCX:TESTSILVER": [_window_candle((100.0, 101.0))],
            # MCX:TESTCRUDE has no candle today
        }
        data_service = FakeCommodityDataService(candles)

        engine = commodity_engine.run_mcx_commodity_engine(data_service, TradeDirection.LONG)
        self.assertIsNotNone(engine)
        self.assertEqual(set(engine.trackers), {"MCX:TESTGOLD", "MCX:TESTSILVER"})
        # 2 symbols tracked today -> 200,000 / 2 each, NOT /3 (the full universe's length).
        self.assertEqual(engine.trackers["MCX:TESTGOLD"].capital_allocated, 100_000.0)

    def test_no_candle_available_for_anyone_returns_none(self):
        data_service = FakeCommodityDataService({})
        engine = commodity_engine.run_mcx_commodity_engine(data_service, TradeDirection.LONG)
        self.assertIsNone(engine)

    def test_lot_size_lookup_has_no_entry_for_an_unlisted_symbol(self):
        # commodity_engine.run_mcx_commodity_engine() falls back to lot_size=1 (with a loud
        # error log) for any tracked symbol missing from the lookup built here - this
        # confirms the lookup itself is exactly config.MCX_COMMODITY_UNIVERSE's own
        # {symbol: lot_size} mapping, with nothing invented for symbols outside it.
        lot_sizes = commodity_engine._lot_size_lookup()
        self.assertEqual(lot_sizes, {"MCX:TESTGOLD": 10, "MCX:TESTSILVER": 5, "MCX:TESTCRUDE": 100})
        self.assertNotIn("MCX:NOT-IN-UNIVERSE-AT-ALL", lot_sizes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
