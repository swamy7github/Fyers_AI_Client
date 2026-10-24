"""
test_mcx_options_engine.py
------------------------------
Tests for mcx_options_engine.py: the MCX commodity OPTIONS counterpart to commodity_engine.py's
FUTURES engine (and the multi-commodity counterpart to options_engine.py's single-index
NiftyOptionsEngine - see that module's own module docstring for the full rule set this reuses
verbatim, unchanged, per commodity).

Covers: load_mcx_fo_symbol_master() (a thin options_engine.load_fo_symbol_master() wrapper
pointed at MCX's own CSV/column config), MCXOptionsEngine's construction (one NiftyOptionsEngine
per commodity, sharing ONE TradeBook, seeded VWAP, a commodity with no option_underlying
skipped rather than crashing), its duck-typed surface (.trackers/.feed_candle()/.check_all()/
.square_off_all()/.open_positions_count()), an end-to-end entry/exit proving the underlying
filter genuinely separates TWO commodities sharing ONE master file (not just happening to work
because only one was ever present), and run_mcx_options_engine()'s own wiring (reuses
commodity_engine.fetch_mcx_universe_opening_ranges() verbatim, capital split per tracked
commodity, bias_direction only gates the quality filter).

config.MCX_COMMODITY_UNIVERSE is monkeypatched to a small, deterministic fake universe for
every test here, mirroring test_commodity_engine.py's own convention.

Run with:
    python test_mcx_options_engine.py
"""

from __future__ import annotations

import unittest

import pandas as pd

import commodity_engine
import config
import mcx_options_engine
from mcx_options_engine import MCXOptionsEngine
from trade_direction import TradeDirection

_MASTER_COLUMNS = config.MCX_FO_MASTER_COLUMNS  # {logical: actual column name}

_TEST_UNIVERSE = [
    {"symbol": "MCX:TESTGOLD", "lot_size": 10, "option_underlying": "TESTGOLD"},
    {"symbol": "MCX:TESTSILVER", "lot_size": 5, "option_underlying": "TESTSILVER"},
]


def _master_row(underlying, option_type, strike, expiry, lot_size=10, symbol=None):
    symbol = symbol or f"MCX:{underlying}25O14{int(strike)}{option_type}"
    return {
        _MASTER_COLUMNS["underlying"]: underlying,
        _MASTER_COLUMNS["option_type"]: option_type,
        _MASTER_COLUMNS["expiry"]: expiry,
        _MASTER_COLUMNS["strike"]: strike,
        _MASTER_COLUMNS["trading_symbol"]: symbol,
        _MASTER_COLUMNS["lot_size"]: lot_size,
    }


def _sample_master(underlyings=("TESTGOLD", "TESTSILVER")) -> pd.DataFrame:
    """One shared master covering BOTH test commodities, several strikes each - lets tests
    confirm the underlying filter genuinely separates them rather than happening to work
    because only one was ever present (mirrors test_options_engine.py's own NIFTY/BANKNIFTY
    shared-master precedent)."""
    rows = []
    for underlying in underlyings:
        base = 4800 if underlying == "TESTGOLD" else 9800
        for strike in (base, base + 50, base + 100, base + 150, base + 200):
            for opt_type in ("CE", "PE"):
                rows.append(_master_row(underlying, opt_type, strike, "2026-10-14"))
    return pd.DataFrame(rows)


class FakeMcxOptionsDataService:
    """Implements get_candles_bulk (fetch_mcx_universe_opening_ranges), get_quotes (strike
    resolution spot + option-price fetch - `index_symbol` IS the futures symbol for MCX, see
    mcx_options_engine.py's module docstring, so `spot_by_symbol` is keyed by futures symbol),
    and get_candle (NiftyOptionsEngine.check_all's live loop)."""

    def __init__(self, or_candles_by_symbol: dict[str, list] | None = None,
                 spot_by_symbol: dict[str, float] | None = None,
                 option_price: float = 50.0,
                 live_candles_by_symbol: dict[str, list] | None = None):
        self._or_candles = or_candles_by_symbol or {}
        self._spot_by_symbol = spot_by_symbol or {}
        self._option_price = option_price
        self._live_candles = live_candles_by_symbol or {}

    def get_candles_bulk(self, symbols, resolution, range_from_date, range_to_date):
        return {s: self._or_candles.get(s, []) for s in symbols}

    def get_quotes(self, symbols):
        symbol = symbols[0]
        if symbol in self._spot_by_symbol:
            return {"d": [{"n": symbol, "v": {"lp": self._spot_by_symbol[symbol]}}]}
        return {"d": [{"n": symbol, "v": {"lp": self._option_price}}]}

    def get_candle(self, symbol, resolution, date_str, from_epoch, to_epoch):
        candle = self._live_candles.get(symbol)
        return [candle] if candle else []


def _or_candle(open_close: tuple[float, float], volume=1000):
    from_epoch, _ = commodity_engine._mcx_915_window_epochs()
    o, c = open_close
    h, l = max(o, c), min(o, c)
    return [from_epoch, o, h, l, c, volume]


class TestLoadMcxFoSymbolMaster(unittest.TestCase):
    def test_missing_file_returns_empty_dataframe(self):
        df = mcx_options_engine.load_mcx_fo_symbol_master("/does/not/exist.csv")
        self.assertTrue(df.empty)

    def test_uses_config_mcx_csv_path_and_columns_by_default(self):
        # No file at the (also-nonexistent, by default) configured path either - just confirms
        # this reads MCX_FO_SYMBOL_MASTER_CSV/MCX_FO_MASTER_COLUMNS, not NIFTY's, without
        # raising.
        df = mcx_options_engine.load_mcx_fo_symbol_master()
        self.assertTrue(df.empty)


class TestMCXOptionsEngineConstruction(unittest.TestCase):
    def setUp(self):
        self._orig_universe = config.MCX_COMMODITY_UNIVERSE
        config.MCX_COMMODITY_UNIVERSE = _TEST_UNIVERSE
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True
        self.master = _sample_master()

    def tearDown(self):
        config.MCX_COMMODITY_UNIVERSE = self._orig_universe
        config.DRY_RUN = self.orig_dry_run

    def _universe_df(self, symbols=("MCX:TESTGOLD", "MCX:TESTSILVER")):
        return pd.DataFrame([
            {"symbol": s, "open": 95.0, "high": 100.0, "low": 100.0, "close": 95.0, "volume": 1000}
            for s in symbols
        ])

    def test_builds_one_sub_engine_per_commodity(self):
        ds = FakeMcxOptionsDataService()
        engine = MCXOptionsEngine(ds, self._universe_df(), 200_000.0, symbol_master=self.master)
        self.assertEqual(set(engine.trackers), {"MCX:TESTGOLD", "MCX:TESTSILVER"})

    def test_every_sub_engine_shares_one_trade_book(self):
        ds = FakeMcxOptionsDataService()
        engine = MCXOptionsEngine(ds, self._universe_df(), 200_000.0, symbol_master=self.master)
        for sub in engine._engines.values():
            self.assertIs(sub.trade_book, engine.trade_book)

    def test_capital_is_split_evenly_across_tracked_commodities(self):
        ds = FakeMcxOptionsDataService()
        engine = MCXOptionsEngine(ds, self._universe_df(), 200_000.0, symbol_master=self.master)
        for sub in engine._engines.values():
            self.assertEqual(sub.capital_allocated, 100_000.0)

    def test_vwap_is_seeded_from_the_opening_range_candle(self):
        ds = FakeMcxOptionsDataService()
        engine = MCXOptionsEngine(ds, self._universe_df(("MCX:TESTGOLD",)), 200_000.0,
                                   symbol_master=self.master)
        tracker = engine.trackers["MCX:TESTGOLD"]
        self.assertGreater(tracker.cum_vol, 0)  # seed_opening_range_candle fed the OR candle in

    def test_commodity_missing_option_underlying_is_skipped_not_crashed(self):
        self._orig_universe_copy = list(config.MCX_COMMODITY_UNIVERSE)
        config.MCX_COMMODITY_UNIVERSE = [
            {"symbol": "MCX:TESTGOLD", "lot_size": 10, "option_underlying": "TESTGOLD"},
            {"symbol": "MCX:TESTSILVER", "lot_size": 5},  # no option_underlying at all
        ]
        ds = FakeMcxOptionsDataService()
        engine = MCXOptionsEngine(ds, self._universe_df(), 200_000.0, symbol_master=self.master)
        self.assertEqual(set(engine.trackers), {"MCX:TESTGOLD"})  # silver skipped, gold unaffected

    def test_feed_candle_routes_to_the_right_commodity(self):
        ds = FakeMcxOptionsDataService()
        engine = MCXOptionsEngine(ds, self._universe_df(), 200_000.0, symbol_master=self.master)
        # Construction already seeded each tracker's own opening-range candle (1 each) - only
        # GOLD should pick up a SECOND candle from this feed_candle() call; SILVER stays at 1.
        gold_before = len(engine.trackers["MCX:TESTGOLD"].candles)
        silver_before = len(engine.trackers["MCX:TESTSILVER"].candles)
        engine.feed_candle("MCX:TESTGOLD", [1, 95.0, 95.2, 94.8, 95.1, 500], "09:20:00")
        self.assertEqual(len(engine.trackers["MCX:TESTGOLD"].candles), gold_before + 1)
        self.assertEqual(len(engine.trackers["MCX:TESTSILVER"].candles), silver_before)

    def test_feed_candle_raises_for_an_unknown_symbol(self):
        ds = FakeMcxOptionsDataService()
        engine = MCXOptionsEngine(ds, self._universe_df(), 200_000.0, symbol_master=self.master)
        with self.assertRaises(KeyError):
            engine.feed_candle("MCX:NOT_TRACKED", [1, 1, 1, 1, 1, 1], "09:20:00")

    def test_open_positions_count_starts_at_zero(self):
        ds = FakeMcxOptionsDataService()
        engine = MCXOptionsEngine(ds, self._universe_df(), 200_000.0, symbol_master=self.master)
        self.assertEqual(engine.open_positions_count(), 0)

    def test_square_off_all_is_a_no_op_when_nothing_is_open(self):
        ds = FakeMcxOptionsDataService()
        engine = MCXOptionsEngine(ds, self._universe_df(), 200_000.0, symbol_master=self.master)
        engine.square_off_all()  # should not raise
        self.assertEqual(engine.trade_book.trades, [])


class TestMCXOptionsEngineEntryExit(unittest.TestCase):
    """End-to-end: two commodities sharing ONE master file, each fired independently, proving
    (a) the underlying filter genuinely separates them and (b) each resolves against its OWN
    futures LTP (index_symbol=futures_symbol - no separate spot index for MCX)."""

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True
        self._orig_universe = config.MCX_COMMODITY_UNIVERSE
        config.MCX_COMMODITY_UNIVERSE = _TEST_UNIVERSE
        self.master = _sample_master()
        universe_df = pd.DataFrame([
            {"symbol": "MCX:TESTGOLD", "open": 95.0, "high": 100.0, "low": 100.0, "close": 95.0, "volume": 1000},
            {"symbol": "MCX:TESTSILVER", "open": 95.0, "high": 100.0, "low": 100.0, "close": 95.0, "volume": 1000},
        ])
        ds = FakeMcxOptionsDataService(
            spot_by_symbol={"MCX:TESTGOLD": 4_910.0, "MCX:TESTSILVER": 9_910.0},
            option_price=50.0,
        )
        self.engine = MCXOptionsEngine(ds, universe_df, 200_000.0, symbol_master=self.master)

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run
        config.MCX_COMMODITY_UNIVERSE = self._orig_universe

    def _warmup(self, symbol, count=20, base_price=95.0, volume=1000):
        for i in range(count):
            c = base_price + i * 0.05
            self.engine.feed_candle(symbol, [1700000000 + i, c, c + 0.2, c - 0.2, c, volume], f"09:{20+i}:00")

    def test_gold_breakout_buys_a_gold_ce_not_silver(self):
        self._warmup("MCX:TESTGOLD")
        self.engine.feed_candle("MCX:TESTGOLD", [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00")
        self.engine.feed_candle("MCX:TESTGOLD", [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00")

        gold_sub = self.engine._engines["MCX:TESTGOLD"]
        silver_sub = self.engine._engines["MCX:TESTSILVER"]
        self.assertTrue(gold_sub.option.is_open)
        self.assertIn("TESTGOLD", gold_sub.option.symbol)
        self.assertIn("CE", gold_sub.option.symbol)
        self.assertFalse(silver_sub.option.is_open)  # untouched - silver got no candles

    def test_silver_breakdown_buys_a_silver_pe_resolved_against_its_own_spot(self):
        self._warmup("MCX:TESTSILVER")
        self.engine.feed_candle("MCX:TESTSILVER", [1, 89.5, 89.8, 88.5, 89.0, 2000], "09:45:00")
        self.engine.feed_candle("MCX:TESTSILVER", [2, 89.0, 89.2, 87.8, 88.2, 2200], "09:50:00")

        silver_sub = self.engine._engines["MCX:TESTSILVER"]
        self.assertTrue(silver_sub.option.is_open)
        self.assertIn("TESTSILVER", silver_sub.option.symbol)
        self.assertIn("PE", silver_sub.option.symbol)
        self.assertEqual(silver_sub.option.entry_direction, TradeDirection.SHORT)
        # spot=9910 -> nearest OTM PE strike BELOW spot among (9800..10000 step 50) is 9900
        self.assertIn("9900", silver_sub.option.symbol)

    def test_trades_from_both_commodities_land_in_the_same_shared_trade_book(self):
        self._warmup("MCX:TESTGOLD")
        self.engine.feed_candle("MCX:TESTGOLD", [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00")
        self.engine.feed_candle("MCX:TESTGOLD", [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00")
        self.assertEqual(len(self.engine.trade_book.trades), 1)
        self.assertEqual(self.engine.open_positions_count(), 1)

        self.engine.square_off_all("15:30:00")
        self.assertEqual(self.engine.open_positions_count(), 0)
        self.assertIsNotNone(self.engine.trade_book.trades[0]["exit_price"])


class TestRunMcxOptionsEngine(unittest.TestCase):
    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True
        self._orig_universe = config.MCX_COMMODITY_UNIVERSE
        config.MCX_COMMODITY_UNIVERSE = _TEST_UNIVERSE
        self._orig_capital = config.MCX_OPTIONS_CAPITAL
        config.MCX_OPTIONS_CAPITAL = 200_000.0
        self.master = _sample_master()

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run
        config.MCX_COMMODITY_UNIVERSE = self._orig_universe
        config.MCX_OPTIONS_CAPITAL = self._orig_capital

    def test_builds_an_engine_tracking_every_available_commodity(self):
        ds = FakeMcxOptionsDataService(
            or_candles_by_symbol={
                "MCX:TESTGOLD": [_or_candle((100.0, 102.0))],
                "MCX:TESTSILVER": [_or_candle((100.0, 98.0))],
            },
            spot_by_symbol={"MCX:TESTGOLD": 4_910.0, "MCX:TESTSILVER": 9_910.0},
        )
        engine = mcx_options_engine.run_mcx_options_engine(ds, TradeDirection.LONG, symbol_master=self.master)
        self.assertIsNotNone(engine)
        self.assertEqual(set(engine.trackers), {"MCX:TESTGOLD", "MCX:TESTSILVER"})
        for sub in engine._engines.values():
            self.assertEqual(sub.capital_allocated, 100_000.0)  # split across 2 tracked commodities

    def test_bias_direction_does_not_pick_ce_or_pe(self):
        """Mirrors run_nifty_options_engine's own equivalent test - bias_direction only gates
        the opening-range quality filter here, same as every options engine in this agent."""
        ds = FakeMcxOptionsDataService(
            or_candles_by_symbol={"MCX:TESTGOLD": [_or_candle((100.0, 102.0))]},
            spot_by_symbol={"MCX:TESTGOLD": 4_910.0},
        )
        config.MCX_COMMODITY_UNIVERSE = [_TEST_UNIVERSE[0]]
        engine = mcx_options_engine.run_mcx_options_engine(ds, TradeDirection.SHORT, symbol_master=self.master)
        self.assertIsNotNone(engine)
        sub = engine._engines["MCX:TESTGOLD"]
        self.assertIsNone(sub.option.symbol)  # nothing pre-resolved regardless of bias

    def test_strike_mode_threads_through_to_every_sub_engine(self):
        ds = FakeMcxOptionsDataService(
            or_candles_by_symbol={"MCX:TESTGOLD": [_or_candle((100.0, 102.0))]},
            spot_by_symbol={"MCX:TESTGOLD": 4_910.0},
        )
        config.MCX_COMMODITY_UNIVERSE = [_TEST_UNIVERSE[0]]
        engine = mcx_options_engine.run_mcx_options_engine(ds, TradeDirection.LONG,
                                                             symbol_master=self.master, strike_mode="ATM")
        self.assertEqual(engine._engines["MCX:TESTGOLD"].strike_mode, "ATM")

    def test_empty_master_returns_none(self):
        ds = FakeMcxOptionsDataService(
            or_candles_by_symbol={"MCX:TESTGOLD": [_or_candle((100.0, 102.0))]},
        )
        engine = mcx_options_engine.run_mcx_options_engine(ds, TradeDirection.LONG, symbol_master=pd.DataFrame())
        self.assertIsNone(engine)

    def test_no_opening_range_candle_for_any_commodity_returns_none(self):
        ds = FakeMcxOptionsDataService()  # no OR candles at all
        engine = mcx_options_engine.run_mcx_options_engine(ds, TradeDirection.LONG, symbol_master=self.master)
        self.assertIsNone(engine)

    def test_a_data_gap_on_one_commodity_still_tracks_the_other(self):
        ds = FakeMcxOptionsDataService(
            or_candles_by_symbol={"MCX:TESTGOLD": [_or_candle((100.0, 102.0))]},  # silver missing
            spot_by_symbol={"MCX:TESTGOLD": 4_910.0},
        )
        engine = mcx_options_engine.run_mcx_options_engine(ds, TradeDirection.LONG, symbol_master=self.master)
        self.assertIsNotNone(engine)
        self.assertEqual(set(engine.trackers), {"MCX:TESTGOLD"})
        self.assertEqual(engine._engines["MCX:TESTGOLD"].capital_allocated, 200_000.0)  # only 1 slot now


if __name__ == "__main__":
    unittest.main()
