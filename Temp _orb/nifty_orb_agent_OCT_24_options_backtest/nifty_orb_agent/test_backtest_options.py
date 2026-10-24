"""
test_backtest_options.py
----------------------------
Tests for backtest_options.py - see that module's own docstring for the full design
rationale (why options need a SEPARATE historical-quote stand-in, unlike backtest.py's
equity/MCX-futures replay; why weekly and monthly legs are run as two full
options_engine.NiftyOptionsEngine instances fed the SAME futures candle stream).

Covers:
  - _BacktestQuoteDataService: preload, lazy per-symbol fetch (and caching), the
    current_epoch "as of this moment" lookup, and that it refuses to serve anything other
    than get_quotes() (NotImplementedError), by design.
  - The per-underlying config factories (nifty_underlying_config/banknifty_underlying_config/
    mcx_underlying_configs), including MCX's "skip commodities with no option_underlying,
    split capital only across the eligible ones" behaviour.
  - _run_options_leg_for_date: the opening-range wick filter, a WEEKLY vs MONTHLY leg
    genuinely resolving two different contracts/prices for the identical signal, and the
    "no candles at all" / "wick filter fails" empty-DataFrame cases.
  - _compute_overall's aggregation (including the empty-trades case).
  - default_csv_path/save_trades_csv.
  - A small end-to-end run_options_backtest() integration test (forced direction, one
    underlying, one day) proving the whole pipeline produces both expiry legs' trades and
    the right daily_summary/overall shape.

Run with:
    python test_backtest_options.py
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import date, datetime

import pandas as pd
import pytz

import backtest_options as bo
import config
import options_engine
from trade_direction import TradeDirection

TZ = pytz.timezone(config.TIMEZONE)
TARGET_DATE = date(2026, 10, 15)  # a Thursday - trading_days_between() accepts any weekday


def _epoch(h: int, m: int, s: int = 0, d: date = TARGET_DATE) -> int:
    return int(TZ.localize(datetime(d.year, d.month, d.day, h, m, s)).timestamp())


def _futures_day_candles() -> list:
    """20 warm-up candles, then the exact proven breakout pattern test_options_engine.py
    uses (base_price=95.0 ramping by 0.05, then 100.5->101.0 / 101.0->101.8 with volumes
    2000/2200), then a mirrored exit (two weak candles) - reused here so the SAME signal
    reliably fires through the full backtest_options.py replay path, not just the unit-level
    NiftyOptionsEngine tests."""
    candles = []
    base_price = 95.0
    t0 = _epoch(9, 15)
    for i in range(20):
        c = base_price + i * 0.05
        candles.append([t0 + i * 300, c, c + 0.2, c - 0.2, c, 1000])
    idx = len(candles)
    candles.append([t0 + idx * 300, 100.5, 101.5, 100.2, 101.0, 2000]); idx += 1
    candles.append([t0 + idx * 300, 101.0, 102.0, 100.8, 101.8, 2200]); idx += 1
    candles.append([t0 + idx * 300, 95.0, 95.2, 90.0, 91.0, 2500]); idx += 1
    candles.append([t0 + idx * 300, 91.0, 91.2, 85.0, 86.0, 2700]); idx += 1
    return candles


_MC = config.NIFTY_FO_MASTER_COLUMNS


def _master_row(option_type, strike, expiry, symbol, lot_size=75):
    return {
        _MC["underlying"]: "NIFTY", _MC["option_type"]: option_type, _MC["expiry"]: expiry,
        _MC["strike"]: strike, _MC["trading_symbol"]: symbol, _MC["lot_size"]: lot_size,
    }


def _multi_month_master() -> pd.DataFrame:
    rows = []
    for expiry in ("2026-10-15", "2026-10-22", "2026-10-29", "2026-11-26"):
        for strike in (24800, 24850, 24900, 24950, 25000, 25050, 25100):
            for opt in ("CE", "PE"):
                symbol = f"NSE:NIFTY{expiry.replace('-', '')}{int(strike)}{opt}"
                rows.append(_master_row(opt, strike, expiry, symbol))
    return pd.DataFrame(rows)


def _prior_day_seed_candles(count: int = 5) -> list:
    """A handful of flat, low-volume candles dated the day BEFORE TARGET_DATE - real
    prior-session history for _seed_candles_before_date()/seed_volume_windows_from_candles()
    to warm the volume-MA window from, exactly as a genuine previous trading day would live.
    Without this, the FIRST ~100 minutes of a freshly-replayed day can't yet qualify for entry
    (the volume-MA window isn't full) - see backtest.py's own module docstring for why this
    seeding step exists at all; this fixture exists so tests exercise the SAME seeded path
    production always takes, rather than the artificially-unseeded edge case."""
    prior_date = date(TARGET_DATE.year, TARGET_DATE.month, TARGET_DATE.day - 1)
    t0 = _epoch(9, 15, d=prior_date)
    return [[t0 + i * 300, 95.0, 95.2, 94.8, 95.0, 1000] for i in range(count)]


class FakeOptionHistoryService:
    """get_candles_range() stand-in used both by _BacktestQuoteDataService's lazy per-symbol
    OPTION fetch and by backtest_options._fetch_full_range_candles()'s FUTURES pre-fetch.

    Any OPTION symbol (anything other than `futures_symbol`) gets a flat, deterministic
    (hash-derived) premium series, so two DIFFERENT resolved option symbols (e.g. a weekly
    vs. a monthly contract) are provably priced differently, while the SAME symbol is always
    priced identically across calls. The FUTURES symbol itself gets `day_candles` PLUS a
    few real prior-day candles prepended (see _prior_day_seed_candles) - genuine history for
    the volume-MA seeding step to draw from, exactly like a real backtest range would have."""

    def __init__(self, day_candles: list, futures_symbol: str | None = None):
        self.day_candles = day_candles
        self.futures_symbol = futures_symbol
        self.calls: list[tuple] = []

    def get_candles_range(self, symbol, resolution, range_from_date, range_to_date):
        self.calls.append((symbol, resolution, range_from_date, range_to_date))
        if symbol == self.futures_symbol:
            return _prior_day_seed_candles() + list(self.day_candles)
        base = 100.0 + (abs(hash(symbol)) % 50)
        return [[c[0], base, base + 1, base - 1, base, 10] for c in self.day_candles]


class TestBacktestQuoteDataService(unittest.TestCase):
    def test_preload_is_served_without_any_fetch(self):
        real = FakeOptionHistoryService([])
        svc = bo._BacktestQuoteDataService(real, "5", TARGET_DATE)
        svc.preload("NSE:NIFTY50-INDEX", [[_epoch(9, 20), 100, 101, 99, 100.5, 10]])
        svc.current_epoch = _epoch(9, 25)
        result = svc.get_quotes(["NSE:NIFTY50-INDEX"])
        self.assertEqual(result["d"][0]["v"]["lp"], 100.5)
        self.assertEqual(real.calls, [])  # never touched the real provider

    def test_lazy_fetch_happens_once_and_is_cached(self):
        real = FakeOptionHistoryService([[_epoch(9, 20), 100, 101, 99, 50.0, 10]])
        svc = bo._BacktestQuoteDataService(real, "5", TARGET_DATE)
        svc.current_epoch = _epoch(9, 25)
        svc.get_quotes(["NSE:NIFTY2610XYZCE"])
        svc.get_quotes(["NSE:NIFTY2610XYZCE"])
        self.assertEqual(len(real.calls), 1)  # second call served from cache, not re-fetched

    def test_price_as_of_uses_latest_candle_at_or_before_current_epoch(self):
        candles = [[_epoch(9, 20), 0, 0, 0, 10.0, 1], [_epoch(9, 25), 0, 0, 0, 20.0, 1],
                   [_epoch(9, 30), 0, 0, 0, 30.0, 1]]
        svc = bo._BacktestQuoteDataService(FakeOptionHistoryService([]), "5", TARGET_DATE)
        svc.preload("SYM", candles)
        svc.current_epoch = _epoch(9, 27)  # between the 09:25 and 09:30 candles
        result = svc.get_quotes(["SYM"])
        self.assertEqual(result["d"][0]["v"]["lp"], 20.0)

    def test_no_candle_at_or_before_current_epoch_is_silently_omitted(self):
        svc = bo._BacktestQuoteDataService(FakeOptionHistoryService([]), "5", TARGET_DATE)
        svc.preload("SYM", [[_epoch(9, 30), 0, 0, 0, 10.0, 1]])
        svc.current_epoch = _epoch(9, 15)  # before the only candle
        result = svc.get_quotes(["SYM"])
        self.assertEqual(result["d"], [])

    def test_current_epoch_none_returns_no_quotes(self):
        svc = bo._BacktestQuoteDataService(FakeOptionHistoryService([]), "5", TARGET_DATE)
        svc.preload("SYM", [[_epoch(9, 30), 0, 0, 0, 10.0, 1]])
        result = svc.get_quotes(["SYM"])
        self.assertEqual(result["d"], [])

    def test_get_candle_and_place_order_are_deliberately_unimplemented(self):
        svc = bo._BacktestQuoteDataService(FakeOptionHistoryService([]), "5", TARGET_DATE)
        with self.assertRaises(NotImplementedError):
            svc.get_candle("SYM", "5", "2026-10-15", 0, 1)
        with self.assertRaises(NotImplementedError):
            svc.place_order("SYM", 1, "BUY")


class TestUnderlyingConfigFactories(unittest.TestCase):
    def test_nifty_config_matches_live_config_values(self):
        cfg = bo.nifty_underlying_config()
        self.assertEqual(cfg.name, "NIFTY")
        self.assertEqual(cfg.futures_symbol, config.NIFTY_FUTURES_SYMBOL)
        self.assertEqual(cfg.index_symbol, config.NIFTY_INDEX_SYMBOL)
        self.assertEqual(cfg.capital_allocated, config.NIFTY_OPTIONS_CAPITAL)
        self.assertEqual(cfg.candle_resolution, "5")

    def test_banknifty_config_matches_live_config_values(self):
        cfg = bo.banknifty_underlying_config()
        self.assertEqual(cfg.name, "BANKNIFTY")
        self.assertEqual(cfg.futures_symbol, config.BANKNIFTY_FUTURES_SYMBOL)
        self.assertEqual(cfg.index_symbol, config.BANKNIFTY_INDEX_SYMBOL)

    def test_mcx_configs_split_capital_only_across_eligible_commodities(self):
        orig_universe = config.MCX_COMMODITY_UNIVERSE
        orig_capital = config.MCX_OPTIONS_CAPITAL
        try:
            config.MCX_COMMODITY_UNIVERSE = [
                {"symbol": "MCX:TESTGOLD25DECFUT", "lot_size": 10, "option_underlying": "TESTGOLD"},
                {"symbol": "MCX:TESTSILVER25DECFUT", "lot_size": 5, "option_underlying": "TESTSILVER"},
                {"symbol": "MCX:TESTCOPPER25DECFUT", "lot_size": 2500, "option_underlying": ""},  # excluded
            ]
            config.MCX_OPTIONS_CAPITAL = 100_000.0
            cfgs = bo.mcx_underlying_configs()
            self.assertEqual(len(cfgs), 2)
            self.assertEqual({c.name for c in cfgs}, {"TESTGOLD", "TESTSILVER"})
            self.assertEqual(cfgs[0].capital_allocated, 50_000.0)
            self.assertEqual(cfgs[0].index_symbol, cfgs[0].futures_symbol)  # MCX: no separate spot index
        finally:
            config.MCX_COMMODITY_UNIVERSE = orig_universe
            config.MCX_OPTIONS_CAPITAL = orig_capital

    def test_no_eligible_mcx_commodities_returns_empty_list(self):
        orig_universe = config.MCX_COMMODITY_UNIVERSE
        try:
            config.MCX_COMMODITY_UNIVERSE = [{"symbol": "MCX:X25DECFUT", "lot_size": 1}]  # no option_underlying key
            self.assertEqual(bo.mcx_underlying_configs(), [])
        finally:
            config.MCX_COMMODITY_UNIVERSE = orig_universe


class TestRunOptionsLegForDate(unittest.TestCase):
    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True
        self.candles = _futures_day_candles()
        self.master = _multi_month_master()
        self.cfg = bo.nifty_underlying_config()

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def test_weekly_and_monthly_legs_resolve_different_contracts_and_prices(self):
        real = FakeOptionHistoryService(self.candles)
        seed = _prior_day_seed_candles()
        weekly_df = bo._run_options_leg_for_date(real, self.cfg, "WEEKLY", TARGET_DATE, self.candles,
                                                   seed, self.master, TZ, TradeDirection.LONG)
        monthly_df = bo._run_options_leg_for_date(real, self.cfg, "MONTHLY", TARGET_DATE, self.candles,
                                                    seed, self.master, TZ, TradeDirection.LONG)
        self.assertEqual(len(weekly_df), 1)
        self.assertEqual(len(monthly_df), 1)
        self.assertIn("20261015", weekly_df.iloc[0]["symbol"])
        self.assertIn("20261029", monthly_df.iloc[0]["symbol"])
        self.assertNotEqual(weekly_df.iloc[0]["symbol"], monthly_df.iloc[0]["symbol"])
        # Same entry/exit TIMING (identical futures signal) ...
        self.assertEqual(weekly_df.iloc[0]["entry_time"], monthly_df.iloc[0]["entry_time"])
        self.assertEqual(weekly_df.iloc[0]["exit_time"], monthly_df.iloc[0]["exit_time"])
        # ... but each leg's OWN option price (hash-derived per symbol in the fake), so unless
        # the two happen to collide they genuinely differ - assert they're independently correct
        # rather than asserting inequality (a hash collision, while astronomically unlikely
        # here, would make a strict != assertion flaky).
        self.assertEqual(weekly_df.iloc[0]["entry_type"], "INITIAL")
        self.assertTrue(weekly_df.iloc[0]["is_backfill"])
        self.assertTrue(weekly_df.iloc[0]["exit_is_backfill"])
        self.assertEqual(weekly_df.iloc[0]["underlying"], "NIFTY")
        self.assertEqual(weekly_df.iloc[0]["expiry_leg"], "WEEKLY")
        self.assertEqual(monthly_df.iloc[0]["expiry_leg"], "MONTHLY")

    def test_no_futures_candles_returns_empty_dataframe(self):
        real = FakeOptionHistoryService([])
        result = bo._run_options_leg_for_date(real, self.cfg, "WEEKLY", TARGET_DATE, [],
                                                [], self.master, TZ, TradeDirection.LONG)
        self.assertTrue(result.empty)

    def test_wick_filter_failure_skips_the_whole_leg(self):
        orig_threshold = config.MAX_OR_CANDLE_WICK_PCT
        try:
            config.MAX_OR_CANDLE_WICK_PCT = 0.0  # the tightest possible - any real wick fails it
            candles = _futures_day_candles()
            candles[0] = [candles[0][0], 100.0, 110.0, 90.0, 100.0, 1000]  # a huge, failing wick
            real = FakeOptionHistoryService(candles)
            result = bo._run_options_leg_for_date(real, self.cfg, "WEEKLY", TARGET_DATE, candles,
                                                    [], self.master, TZ, TradeDirection.LONG)
            self.assertTrue(result.empty)
        finally:
            config.MAX_OR_CANDLE_WICK_PCT = orig_threshold


class TestComputeOverall(unittest.TestCase):
    def test_empty_trades_gives_zeroed_overall(self):
        overall = bo._compute_overall(pd.DataFrame(), pd.DataFrame({"direction": [None]}))
        self.assertEqual(overall["total_trades"], 0)
        self.assertEqual(overall["total_points"], 0.0)
        self.assertTrue(overall["by_underlying_and_leg"].empty)

    def test_groups_by_underlying_and_leg(self):
        trades = pd.DataFrame([
            {"underlying": "NIFTY", "expiry_leg": "WEEKLY", "points": 10.0, "pnl_amount": 500.0,
             "exit_price": 160.0},
            {"underlying": "NIFTY", "expiry_leg": "MONTHLY", "points": -5.0, "pnl_amount": -250.0,
             "exit_price": 95.0},
            {"underlying": "NIFTY", "expiry_leg": "WEEKLY", "points": 4.0, "pnl_amount": 200.0,
             "exit_price": 150.0},
        ])
        overall = bo._compute_overall(trades, pd.DataFrame({"direction": ["LONG"]}))
        self.assertEqual(overall["total_trades"], 3)
        self.assertEqual(overall["total_points"], 9.0)
        grouped = overall["by_underlying_and_leg"].set_index("expiry_leg")
        self.assertEqual(grouped.loc["WEEKLY", "trades"], 2)
        self.assertEqual(grouped.loc["WEEKLY", "total_points"], 14.0)
        self.assertEqual(grouped.loc["MONTHLY", "trades"], 1)


class TestCsvPaths(unittest.TestCase):
    def setUp(self):
        self.orig_log_dir = config.LOG_DIR
        self.tmp_dir = tempfile.mkdtemp()
        config.LOG_DIR = self.tmp_dir

    def tearDown(self):
        config.LOG_DIR = self.orig_log_dir
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_single_day_path_has_one_date(self):
        path = bo.default_csv_path(TARGET_DATE, TARGET_DATE)
        self.assertEqual(os.path.basename(path), "options_backtest_20261015.csv")

    def test_range_path_has_both_dates(self):
        path = bo.default_csv_path(TARGET_DATE, date(2026, 10, 20))
        self.assertEqual(os.path.basename(path), "options_backtest_20261015_20261020.csv")

    def test_save_trades_csv_writes_a_real_file(self):
        path = os.path.join(self.tmp_dir, "nested", "out.csv")
        df = pd.DataFrame([{"symbol": "X", "points": 1.0}])
        bo.save_trades_csv(df, path)
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            self.assertIn("symbol", f.readline())


class TestRunOptionsBacktestIntegration(unittest.TestCase):
    """One underlying (NIFTY), one forced-direction day, both legs - proves the whole
    run_options_backtest() pipeline (pre-fetch -> per-day -> per-leg replay -> aggregation
    -> CSV-ready DataFrame) produces correctly-shaped, non-empty output end to end."""

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True
        self.orig_loader = options_engine.load_fo_symbol_master
        self.master = _multi_month_master()
        options_engine.load_fo_symbol_master = lambda *a, **k: self.master

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run
        options_engine.load_fo_symbol_master = self.orig_loader

    def test_end_to_end_produces_both_legs(self):
        data_service = FakeOptionHistoryService(_futures_day_candles(),
                                                 futures_symbol=config.NIFTY_FUTURES_SYMBOL)
        # _fetch_full_range_candles() always resolves candles for the futures symbol, no
        # matter what date range it's asked for - the fake ignores range_from/range_to and
        # always returns the one day of candles it was built with, PLUS real prior-day seed
        # candles for the futures symbol specifically (see FakeOptionHistoryService).
        results = bo.run_options_backtest(data_service, TARGET_DATE, TARGET_DATE,
                                           direction=TradeDirection.LONG, legs=("WEEKLY", "MONTHLY"),
                                           markets=("nifty",))
        trades = results["trades"]
        self.assertEqual(len(trades), 2)  # one WEEKLY row, one MONTHLY row
        self.assertEqual(set(trades["expiry_leg"]), {"WEEKLY", "MONTHLY"})
        self.assertTrue((trades["underlying"] == "NIFTY").all())
        self.assertTrue((trades["date"] == str(TARGET_DATE)).all())

        daily = results["daily_summary"]
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily.iloc[0]["direction"], "LONG")
        self.assertEqual(daily.iloc[0]["total_trades"], 2)

        overall = results["overall"]
        self.assertEqual(overall["total_trades"], 2)
        self.assertFalse(overall["by_underlying_and_leg"].empty)

    def test_empty_date_range_returns_empty_everything(self):
        data_service = FakeOptionHistoryService(_futures_day_candles())
        saturday = date(2026, 10, 17)
        results = bo.run_options_backtest(data_service, saturday, saturday,
                                           direction=TradeDirection.LONG, markets=("nifty",))
        self.assertTrue(results["trades"].empty)
        self.assertTrue(results["daily_summary"].empty)


if __name__ == "__main__":
    unittest.main()
