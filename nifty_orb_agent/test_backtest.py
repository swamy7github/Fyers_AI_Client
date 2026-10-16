"""
test_backtest.py
-------------------
Tests for backtest.py, in two tiers:

  1. Pure function tests: _seed_candles_before_date (slices prior-session
     candles out of already-fetched data) and
     _compute_historical_sector_leaderboard (the historical counterpart to
     sector_scanner.get_sector_leaderboard).

  2. A full run_backtest() integration test with every Fyers call faked,
     proving three things together: (a) direction is computed FRESH per
     day from (mocked) historical sentiment - a bullish day trades LONG,
     a bearish day trades SHORT, a neutral day is skipped entirely; (b) an
     explicit --direction override forces one fixed side regardless of
     sentiment; (c) prior-session data (fetched via the extended
     INTRADAY_SEED_LOOKBACK_DAYS window) correctly seeds each day's volume-MA
     window, so an entry can fire on just 2 replayed candles within the
     backtested day rather than needing 22.

Run with:
    python test_backtest.py
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytz

import backtest
import config
from trade_direction import TradeDirection
from utilities import get_last_traded_day

    
TZ = pytz.timezone(config.TIMEZONE)


def _epoch(d: date, hour: int, minute: int) -> int:
    return int(TZ.localize(datetime(d.year, d.month, d.day, hour, minute, 0)).timestamp())


class TestSeedCandlesBeforeDate(unittest.TestCase):
    def test_only_candles_strictly_before_target_date_are_returned(self):
        d1, d2 = date(2026, 3, 16), date(2026, 3, 17)
        candles = [[_epoch(d1, 9, 15 + i), 0, 0, 0, 0, 100 + i] for i in range(3)]
        candles += [[_epoch(d2, 9, 20), 0, 0, 0, 0, 999]]  # ON target_date - must be excluded
        result = backtest._seed_candles_before_date(candles, d2, TZ, needed=20)
        self.assertEqual(len(result), 3)
        self.assertTrue(all(c[5] != 999 for c in result))

    def test_capped_at_needed_and_keeps_the_most_recent(self):
        d1 = date(2026, 3, 16)
        candles = [[_epoch(d1, 9, 15) + i * 60, 0, 0, 0, 0, i] for i in range(50)]
        result = backtest._seed_candles_before_date(candles, date(2026, 3, 17), TZ, needed=20)
        self.assertEqual(len(result), 20)
        self.assertEqual([c[5] for c in result], list(range(30, 50)))

    def test_no_prior_candles_returns_empty_list(self):
        result = backtest._seed_candles_before_date([], date(2026, 3, 17), TZ, needed=20)
        self.assertEqual(result, [])


class TestComputeHistoricalSectorLeaderboard(unittest.TestCase):
    def test_ranks_sectors_by_pct_change_descending(self):
        import pandas as pd
        sector_map = pd.DataFrame([
            {"symbol": "NSE:NIFTYIT-INDEX", "index_name": "Nifty IT", "is_sector_index": True},
            {"symbol": "NSE:NIFTYBANK-INDEX", "index_name": "Nifty Bank", "is_sector_index": True},
            {"symbol": "NSE:NIFTY50-INDEX", "index_name": "Nifty 50", "is_sector_index": False},  # excluded
        ])
        d1, d2 = date(2026, 3, 16), date(2026, 3, 17)
        sector_daily = {
            "NSE:NIFTYIT-INDEX": [[_epoch(d1, 0, 0), 0, 0, 0, 100.0, 0], [_epoch(d2, 0, 0), 0, 0, 0, 103.0, 0]],
            "NSE:NIFTYBANK-INDEX": [[_epoch(d1, 0, 0), 0, 0, 0, 100.0, 0], [_epoch(d2, 0, 0), 0, 0, 0, 99.0, 0]],
            "NSE:NIFTY50-INDEX": [[_epoch(d1, 0, 0), 0, 0, 0, 100.0, 0], [_epoch(d2, 0, 0), 0, 0, 0, 101.0, 0]],
        }
        result = backtest._compute_historical_sector_leaderboard(sector_daily, sector_map, d2, TZ)
        self.assertEqual(list(result["index_name"]), ["Nifty IT", "Nifty Bank"])  # Nifty 50 excluded
        self.assertAlmostEqual(result.iloc[0]["pct_change"], 3.0)
        self.assertAlmostEqual(result.iloc[1]["pct_change"], -1.0)

    def test_empty_sector_map_returns_empty_dataframe(self):
        import pandas as pd
        result = backtest._compute_historical_sector_leaderboard({}, pd.DataFrame(), date(2026, 3, 17), TZ)
        self.assertTrue(result.empty)

    def test_symbol_with_no_data_on_target_date_is_skipped_not_a_crash(self):
        import pandas as pd
        sector_map = pd.DataFrame([{"symbol": "NSE:NIFTYIT-INDEX", "index_name": "Nifty IT", "is_sector_index": True}])
        result = backtest._compute_historical_sector_leaderboard(
            {"NSE:NIFTYIT-INDEX": []}, sector_map, date(2026, 3, 17), TZ)
        self.assertTrue(result.empty)


class FakeBacktestDataService:
    """
    Serves canned candles for run_backtest()'s full pipeline: stock
    universe (5-min and daily) and sector-index daily candles, keyed by
    (symbol, resolution) with all requested chunks returning the same
    canned series (test dates are all within one chunk, so this is fine).
    """

    def __init__(self, series_by_symbol_and_resolution: dict):
        self._series = series_by_symbol_and_resolution
        self.calls: list[tuple] = []

    def get_candles_range(self, symbol, resolution, range_from_date, range_to_date):
        self.calls.append((symbol, resolution, range_from_date, range_to_date))
        return self._series.get((symbol, resolution), [])
    
    def get_quotes_batched(self, symbols):
        # Used for sector index quotes (data/sector_index_map.csv rows) and for
        # compute_market_breadth (the stock universe) - same shape either way.
        out = {}
        for s in symbols:
            out[s] = {"lp": 101.0, "prev_close_price": 100.0,
                        "open_price": 100.0, "high_price": 101.5, "low_price": 99.5, "volume": 1000}
        return out


class TestRunBacktestPerDayDirection(unittest.TestCase):
    """
    3 consecutive weekdays: Mon bullish (mocked) -> LONG, Tue bearish -> SHORT,
    Wed neutral -> skipped. One stock each way: WINNER gaps up on Monday,
    LOSER gaps down on Tuesday - and each has enough OWN prior-day 5-min
    history for its volume-MA to seed correctly without extra plumbing.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        sector_csv = self.tmpdir / "sector_index_map.csv"
        universe_csv = self.tmpdir / "ind_nifty500list.csv"
        sector_csv.write_text(
       		"symbol,index_name,category,is_sector_index,sector,sector_url\n"
            "NSE:NIFTYIT-INDEX,Nifty IT,Information Technology,True,IT,https://example.com/it\n"
        )
        universe_csv.write_text(
            "Symbol,Company Name,Industry,sector_url\n"
            "WINNER,Winner Ltd,IT,https://example.com/it\n"
            "LOSER,Loser Ltd,IT,https://example.com/it\n"
        )
        self._orig_sector_csv = config.SECTOR_INDEX_MAP_CSV
        self._orig_universe_csv = config.STOCK_UNIVERSE_CSV
        config.SECTOR_INDEX_MAP_CSV = str(sector_csv)
        config.STOCK_UNIVERSE_CSV = str(universe_csv)

        self._orig_decide = backtest.decide_direction_for_date

    def tearDown(self):
        config.SECTOR_INDEX_MAP_CSV = self._orig_sector_csv
        config.STOCK_UNIVERSE_CSV = self._orig_universe_csv
        backtest.decide_direction_for_date = self._orig_decide
        shutil.rmtree(self.tmpdir)

    def _build_fake_service(self, mon, tue, wed):
        """Flat, no-signal 09:15 candles for each symbol on each day - enough to build a
        shortlist and confirm direction routing, without needing a full breakout sequence
        (that's covered separately in TestRunBacktestVolumeSeedingEnablesQuickEntry)."""
        series = {}
        # WINNER gaps up 1.5% on Monday only; flat other days.
        series[("NSE:WINNER-EQ", "5")] = [
            [_epoch(mon, 9, 15), 100.0, 101.6, 99.8, 101.5, 1000],
            [_epoch(tue, 9, 15), 100.0, 100.2, 99.8, 100.0, 1000],
            [_epoch(wed, 9, 15), 100.0, 100.2, 99.8, 100.0, 1000],
        ]
        series[("NSE:WINNER-EQ", "D")] = [
            [_epoch(mon, 0, 0), 0, 0, 0, 100.0, 0],
            [_epoch(tue, 0, 0), 0, 0, 0, 100.0, 0],
            [_epoch(wed, 0, 0), 0, 0, 0, 100.0, 0],
        ]
        # LOSER gaps down 1.5% on Tuesday only; flat other days.
        series[("NSE:LOSER-EQ", "5")] = [
            [_epoch(mon, 9, 15), 100.0, 100.2, 99.8, 100.0, 1000],
            [_epoch(tue, 9, 15), 100.0, 100.2, 98.4, 98.5, 1000],
            [_epoch(wed, 9, 15), 100.0, 100.2, 99.8, 100.0, 1000],
        ]
        series[("NSE:LOSER-EQ", "D")] = [
            [_epoch(mon, 0, 0), 0, 0, 0, 100.0, 0],
            [_epoch(tue, 0, 0), 0, 0, 0, 100.0, 0],
            [_epoch(wed, 0, 0), 0, 0, 0, 100.0, 0],
        ]
        # A day BEFORE mon too, so the sector leaderboard has a previous-close to compare
        # Monday against - without it, Monday's own leaderboard row would have nothing to
        # diff against and be silently skipped (see _previous_close_on_date's "strictly
        # before" rule), which would make a leaderboard-presence assertion pass vacuously.
        day_before_mon = date(2026, 3, 13)
        series[("NSE:NIFTYIT-INDEX", "D")] = [
            [_epoch(day_before_mon, 0, 0), 0, 0, 0, 99.0, 0],
            [_epoch(mon, 0, 0), 0, 0, 0, 100.0, 0],
            [_epoch(tue, 0, 0), 0, 0, 0, 101.0, 0],
            [_epoch(wed, 0, 0), 0, 0, 0, 102.0, 0],
        ]
        return FakeBacktestDataService(series)

    def test_direction_switches_daily_and_neutral_day_is_skipped(self):
        mon, tue, wed = date(2026, 3, 16), date(2026, 3, 17), date(2026, 3, 18)  # Mon/Tue/Wed
        provider = self._build_fake_service(mon, tue, wed)

        def fake_decide(as_of_date):
            return {
                mon: (TradeDirection.LONG, "BULLISH", 1.2),
                tue: (TradeDirection.SHORT, "BEARISH", -1.2),
                wed: (None, "NEUTRAL", 0.1),
            }[as_of_date]

        backtest.decide_direction_for_date = fake_decide
        results = backtest.run_backtest(provider, mon, wed)

        summary = results["daily_summary"].set_index("date")
        self.assertEqual(summary.loc[str(mon), "direction"], "LONG")
        self.assertEqual(summary.loc[str(tue), "direction"], "SHORT")
        # pandas stores a column mixing strings and None as NaN, not a literal None -
        # pd.isna() is the correct check here, not assertIsNone.
        self.assertTrue(pd.isna(summary.loc[str(wed), "direction"]))
        self.assertEqual(results["overall"]["days_skipped_no_direction"], 1)

    def test_forced_direction_override_ignores_sentiment_every_day(self):
        mon, tue, wed = date(2026, 3, 16), date(2026, 3, 17), date(2026, 3, 18)
        provider = self._build_fake_service(mon, tue, wed)

        call_count = {"n": 0}

        def fake_decide(as_of_date):
            call_count["n"] += 1
            return (TradeDirection.SHORT, "BEARISH", -1.0)  # would ALWAYS say SHORT if consulted

        backtest.decide_direction_for_date = fake_decide
        results = backtest.run_backtest(provider, mon, wed, direction=TradeDirection.LONG)

        # Forced direction means decide_direction_for_date should never even be CALLED.
        self.assertEqual(call_count["n"], 0)
        summary = results["daily_summary"].set_index("date")
        self.assertEqual(summary.loc[str(mon), "direction"], "LONG")
        self.assertEqual(summary.loc[str(tue), "direction"], "LONG")
        self.assertEqual(summary.loc[str(wed), "direction"], "LONG")

    def test_sector_leaderboard_is_present_and_correctly_computed(self):
        mon, tue, wed = date(2026, 3, 16), date(2026, 3, 17), date(2026, 3, 18)
        provider = self._build_fake_service(mon, tue, wed)
        backtest.decide_direction_for_date = lambda d: (TradeDirection.LONG, "BULLISH", 1.0)
        results = backtest.run_backtest(provider, mon, mon)  # just Monday, keep it simple
        self.assertIn(str(mon), results["sector_leaderboards"])
        board = results["sector_leaderboards"][str(mon)]
        self.assertFalse(board.empty)  # must actually have data, not just an empty placeholder
        self.assertEqual(list(board["index_name"]), ["Nifty IT"])
        self.assertAlmostEqual(board.iloc[0]["pct_change"], 1.01)  # (100-99)/99*100, rounded to 2dp


class TestRunBacktestVolumeSeedingEnablesQuickEntry(unittest.TestCase):
    """
    Direct proof that backtest.py's own prior-session seeding (from the
    INTRADAY_SEED_LOOKBACK_DAYS-extended pre-fetch, at zero extra API cost)
    lets an entry fire on just 2 replayed candles within the backtested
    day - the same fix, and the same proof pattern, as
    test_signal_engine.TestSeedingFixesTheEntryTimingBug, now confirmed
    wired correctly for the backtest path specifically.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        sector_csv = self.tmpdir / "sector_index_map.csv"
        universe_csv = self.tmpdir / "ind_nifty500list.csv"
        sector_csv.write_text(
            "symbol,index_name,category,is_sector_index,sector,sector_url\n"
            "NSE:NIFTYIT-INDEX,Nifty IT,Information Technology,True,IT,https://example.com/it\n"
        )
        universe_csv.write_text(
            "Symbol,Company Name,Industry,sector_url\n"
            "WINNER,Winner Ltd,IT,https://example.com/it\n"
            "LOSER,Loser Ltd,IT,https://example.com/it\n"
        )
        #sector_csv.write_text("symbol,index_name,category,is_sector_index,sector,sector_url\n")  # empty is fine here
        #universe_csv.write_text("Symbol,Company Name,Industry,sector_url\nWINNER,Winner Ltd,IT,https://example.com/it\n")
        self._orig_sector_csv = config.SECTOR_INDEX_MAP_CSV
        self._orig_universe_csv = config.STOCK_UNIVERSE_CSV
        config.SECTOR_INDEX_MAP_CSV = str(sector_csv)
        config.STOCK_UNIVERSE_CSV = str(universe_csv)
        self._orig_decide = backtest.decide_direction_for_date
        backtest.decide_direction_for_date = lambda d: (TradeDirection.LONG, "BULLISH", 1.0)

    def tearDown(self):
        config.SECTOR_INDEX_MAP_CSV = self._orig_sector_csv
        config.STOCK_UNIVERSE_CSV = self._orig_universe_csv
        backtest.decide_direction_for_date = self._orig_decide
        shutil.rmtree(self.tmpdir)

    def test_buy_fires_on_2_candles_within_the_day_when_prior_day_data_exists(self):
        d = get_last_traded_day()  # Tuesday
        d=d.split("-")
        target_day = date(int(d[0]), int(d[1]), int(d[2]))
        prior_day = date(int(d[0]), int(d[1]), int(d[2])-1)  # 2026-03-17
        #prior_day = date(2026, 3, 16)   # Monday - within the 7-day lookback window

        series = {}
        # Prior day: 20 flat candles for volume-MA seeding.
        series[("NSE:WINNER-EQ", "5")] = [
            [_epoch(prior_day, 9, 15) + i * 300, 95.0, 95.2, 94.8, 95.0, 1000] for i in range(20)
        ]
        # Target day: opening-range candle (index 0, gap-up +1.5%), then exactly 2 more
        # candles that should trigger a BUY given the seeded volume MA.
        series[("NSE:WINNER-EQ", "5")] += [
            [_epoch(target_day, 9, 15), 100.0, 101.6, 99.8, 101.5, 5000],   # OR candle: +1.5% gap
            [_epoch(target_day, 9, 20), 101.5, 102.5, 101.2, 102.0, 3000],  # qualifying candle 1
            [_epoch(target_day, 9, 25), 102.0, 103.0, 101.8, 102.8, 3200],  # qualifying candle 2 -> BUY
        ]
        series[("NSE:WINNER-EQ", "D")] = [
            [_epoch(prior_day, 0, 0), 0, 0, 0, 100.0, 0],
            [_epoch(target_day, 0, 0), 0, 0, 0, 100.0, 0],
        ]

        provider = FakeBacktestDataService(series)
        results = backtest.run_backtest(provider, target_day, target_day, direction=TradeDirection.LONG)

        trades = results["trades"]
        self.assertFalse(trades.empty, "expected a BUY to fire from just 2 in-day candles")
        self.assertEqual(trades.iloc[0]["entry_price"], 102.8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
