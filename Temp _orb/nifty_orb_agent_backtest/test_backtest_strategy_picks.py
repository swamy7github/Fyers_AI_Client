"""
test_backtest_strategy_picks.py
----------------------------------
Tests for backtest_strategy_picks.py, split into two concerns:

  1. TestTearSheetMath - the tear-sheet statistics (Sharpe, drawdown, profit
     factor, win rate) against values calculated independently by hand (see
     the module docstring's comments for the arithmetic), not just
     re-derived from the same code under test. Getting this wrong would
     mean wrong conclusions handed to a real trader, so these are asserted
     to several decimal places, not just "doesn't crash".

  2. TestBacktestEngineAndCsvRoundTrip - the execution engine (equal-weight
     allocation, cost model, missing/illiquid-candle handling, duplicate-date
     row safety) and the CSV round-trip (new columns appended correctly,
     JSON field re-parses, original columns/row order preserved), using a
     fake HistoryProvider so nothing here touches a real Fyers connection.

Uses only the standard library's unittest. Run with:
    python test_backtest_strategy_picks.py
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

import backtest_strategy_picks as bp


# ----------------------------------------------------------------------------
# A minimal fake HistoryProvider: serves pre-canned daily candles from a dict
# keyed by (symbol, date) -> (open, high, low, close, volume), and counts how
# many times get_candles_range() is actually called per symbol, so the
# caching tests can assert on call counts directly.
# ----------------------------------------------------------------------------
class FakeHistoryProvider:
    def __init__(self, bars: dict[tuple[str, str], tuple[float, float, float, float, float]]):
        self.bars = bars
        self.call_log: list[tuple[str, str, str]] = []  # (symbol, range_from, range_to)

    def get_candles_range(self, symbol: str, resolution: str, range_from_date: str, range_to_date: str) -> list:
        self.call_log.append((symbol, range_from_date, range_to_date))
        from datetime import datetime, timedelta
        start = datetime.strptime(range_from_date, "%Y-%m-%d").date()
        end = datetime.strptime(range_to_date, "%Y-%m-%d").date()
        out = []
        d = start
        while d <= end:
            key = (symbol, d.strftime("%Y-%m-%d"))
            if key in self.bars:
                o, h, l, c, v = self.bars[key]
                # A daily candle's epoch, interpreted in IST, must land on this calendar date -
                # 09:00 IST comfortably avoids any UTC-conversion edge case in the test itself.
                import pytz
                tz = pytz.timezone("Asia/Kolkata")
                epoch = int(tz.localize(datetime(d.year, d.month, d.day, 9, 0, 0)).timestamp())
                out.append([epoch, o, h, l, c, v])
            d += timedelta(days=1)
        return out


def _write_picks_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    """rows: list of (date_str, stocks_str)."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "stocks"])
        for date_str, stocks_str in rows:
            writer.writerow([date_str, stocks_str])


class TestTearSheetMath(unittest.TestCase):
    """
    3 days, 1 stock/day, 0% cost - matches the hand/numpy-verified scenario:
      day1: open=100 close=110 (+10%)  -> capital 100,000 -> 110,000
      day2: open=100 close=99  (-1%)   -> capital 110,000 -> 108,900
      day3: open=100 close=105 (+5%)   -> capital 108,900 -> 114,345

    Independently computed expected values (see the conversation this test
    was written alongside for the full by-hand arithmetic, cross-checked
    with numpy.mean/std):
      final_capital=114345.0, net_gain=14345.0, total_return_pct=14.345
      total_trades=3, win_rate_pct=66.666...7
      gross_profit=15445.0, gross_loss=-1100.0, profit_factor=14.0409090909...
      max_drawdown_pct=-1.0
      sharpe_ratio=13.45076491962732 (annualized, rf=0, sample std ddof=1, sqrt(252))
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.csv_path = self.tmpdir / "picks.csv"
        _write_picks_csv(self.csv_path, [
            ("2024-01-01", "NSE:A-EQ"),
            ("2024-01-02", "NSE:A-EQ"),
            ("2024-01-03", "NSE:A-EQ"),
        ])
        self.provider = FakeHistoryProvider({
            ("NSE:A-EQ", "2024-01-01"): (100.0, 111.0, 99.0, 110.0, 10000.0),
            ("NSE:A-EQ", "2024-01-02"): (100.0, 101.0, 98.0, 99.0, 10000.0),
            ("NSE:A-EQ", "2024-01-03"): (100.0, 106.0, 99.0, 105.0, 10000.0),
        })
        self.cache_dir = self.tmpdir / "cache"

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_tear_sheet_matches_hand_calculated_values(self):
        ts = bp.run_backtest_from_csv(
            str(self.csv_path), self.provider,
            initial_capital=100_000.0, cost_pct_per_side=0.0, cache_dir=str(self.cache_dir),
        )
        self.assertAlmostEqual(ts.final_capital, 114345.0, places=2)
        self.assertAlmostEqual(ts.net_gain, 14345.0, places=2)
        self.assertAlmostEqual(ts.total_return_pct, 14.345, places=3)
        self.assertEqual(ts.total_trades, 3)
        self.assertAlmostEqual(ts.win_rate_pct, 200 / 3, places=2)  # 66.67%
        self.assertAlmostEqual(ts.gross_profit, 15445.0, places=2)
        self.assertAlmostEqual(ts.gross_loss, -1100.0, places=2)
        self.assertAlmostEqual(ts.profit_factor, 15445.0 / 1100.0, places=4)
        self.assertAlmostEqual(ts.max_drawdown_pct, -1.0, places=2)
        self.assertAlmostEqual(ts.sharpe_ratio, 13.45076491962732, places=3)

    def test_profit_factor_is_inf_when_there_are_no_losing_trades(self):
        provider = FakeHistoryProvider({
            ("NSE:A-EQ", "2024-01-01"): (100.0, 111.0, 99.0, 110.0, 10000.0),
        })
        _write_picks_csv(self.csv_path, [("2024-01-01", "NSE:A-EQ")])
        ts = bp.run_backtest_from_csv(str(self.csv_path), provider, cache_dir=str(self.cache_dir))
        self.assertTrue(math.isinf(ts.profit_factor))

    def test_single_day_sharpe_is_zero_not_a_crash(self):
        provider = FakeHistoryProvider({("NSE:A-EQ", "2024-01-01"): (100.0, 101.0, 99.0, 101.0, 1000.0)})
        _write_picks_csv(self.csv_path, [("2024-01-01", "NSE:A-EQ")])
        ts = bp.run_backtest_from_csv(str(self.csv_path), provider, cache_dir=str(self.cache_dir))
        self.assertEqual(ts.sharpe_ratio, 0.0)


class TestBacktestEngineAndCsvRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.csv_path = self.tmpdir / "picks.csv"
        self.cache_dir = self.tmpdir / "cache"

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_cost_model_deducted_on_both_sides(self):
        # open=100, close=110, 0.05% per side:
        #   effective_entry = 100 * 1.0005 = 100.05
        #   effective_exit  = 110 * 0.9995 = 109.945
        #   return_pct = (109.945 - 100.05) / 100.05 * 100
        provider = FakeHistoryProvider({("NSE:A-EQ", "2024-01-01"): (100.0, 111.0, 99.0, 110.0, 1000.0)})
        _write_picks_csv(self.csv_path, [("2024-01-01", "NSE:A-EQ")])
        ts = bp.run_backtest_from_csv(str(self.csv_path), provider, initial_capital=100_000.0,
                                       cost_pct_per_side=0.05, cache_dir=str(self.cache_dir))
        expected_entry = 100.0 * 1.0005
        expected_exit = 110.0 * 0.9995
        expected_return_pct = (expected_exit - expected_entry) / expected_entry * 100
        expected_pnl = round(100_000.0 * expected_return_pct / 100, 2)
        self.assertAlmostEqual(ts.net_gain, expected_pnl, places=2)
        self.assertLess(ts.net_gain, 10_000.0)  # strictly less than the zero-cost +10% case

    def test_equal_weight_allocation_across_two_active_stocks(self):
        provider = FakeHistoryProvider({
            ("NSE:A-EQ", "2024-01-01"): (100.0, 111.0, 99.0, 110.0, 1000.0),  # +10%
            ("NSE:B-EQ", "2024-01-01"): (100.0, 101.0, 89.0, 90.0, 1000.0),   # -10%
        })
        _write_picks_csv(self.csv_path, [("2024-01-01", "NSE:A-EQ, NSE:B-EQ")])
        ts = bp.run_backtest_from_csv(str(self.csv_path), provider, initial_capital=100_000.0,
                                       cost_pct_per_side=0.0, cache_dir=str(self.cache_dir))
        # 50,000 * +10% + 50,000 * -10% = 5,000 - 5,000 = 0 net
        self.assertAlmostEqual(ts.net_gain, 0.0, places=2)
        self.assertEqual(ts.total_trades, 2)

    def test_missing_candle_is_skipped_and_capital_reallocated(self):
        # B has no candle at all on this date -> A alone gets the FULL day's capital,
        # not half of it.
        provider = FakeHistoryProvider({
            ("NSE:A-EQ", "2024-01-01"): (100.0, 111.0, 99.0, 110.0, 1000.0),  # +10%
        })
        _write_picks_csv(self.csv_path, [("2024-01-01", "NSE:A-EQ, NSE:B-EQ")])
        ts = bp.run_backtest_from_csv(str(self.csv_path), provider, initial_capital=100_000.0,
                                       cost_pct_per_side=0.0, cache_dir=str(self.cache_dir))
        self.assertAlmostEqual(ts.net_gain, 10_000.0, places=2)  # full 100k * +10%, not 50k
        self.assertEqual(ts.total_trades, 1)

    def test_illiquid_zero_volume_candle_is_skipped(self):
        provider = FakeHistoryProvider({
            ("NSE:A-EQ", "2024-01-01"): (100.0, 111.0, 99.0, 110.0, 0.0),  # zero volume -> illiquid
        })
        _write_picks_csv(self.csv_path, [("2024-01-01", "NSE:A-EQ")])
        ts = bp.run_backtest_from_csv(str(self.csv_path), provider, cache_dir=str(self.cache_dir))
        self.assertEqual(ts.total_trades, 0)
        self.assertAlmostEqual(ts.net_gain, 0.0, places=2)

    def test_day_with_zero_active_stocks_contributes_no_pnl_and_does_not_crash(self):
        provider = FakeHistoryProvider({})  # no candles for anything
        _write_picks_csv(self.csv_path, [("2024-01-01", "NSE:A-EQ")])
        ts = bp.run_backtest_from_csv(str(self.csv_path), provider, initial_capital=50_000.0,
                                       cache_dir=str(self.cache_dir))
        self.assertEqual(ts.final_capital, 50_000.0)
        self.assertEqual(ts.total_trades, 0)

    def test_csv_gets_three_new_columns_and_json_round_trips(self):
        provider = FakeHistoryProvider({("NSE:A-EQ", "2024-01-01"): (100.0, 111.0, 99.0, 110.0, 1000.0)})
        _write_picks_csv(self.csv_path, [("2024-01-01", "NSE:A-EQ")])
        bp.run_backtest_from_csv(str(self.csv_path), provider, cache_dir=str(self.cache_dir))

        reread = bp.load_picks_csv(str(self.csv_path))
        self.assertEqual(list(reread.columns), [
            "date", "stocks", "daily_net_profit", "daily_return_pct", "stock_performance_details",
        ])
        self.assertEqual(reread.loc[0, "date"], "2024-01-01")   # original columns untouched
        self.assertEqual(reread.loc[0, "stocks"], "NSE:A-EQ")

        details = json.loads(reread.loc[0, "stock_performance_details"])
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["symbol"], "NSE:A-EQ")
        self.assertEqual(details[0]["entry"], 100.0)
        self.assertEqual(details[0]["exit"], 110.0)
        self.assertIn("return_pct", details[0])
        self.assertIn("net_pnl", details[0])

    def test_multi_symbol_cell_with_commas_survives_round_trip(self):
        # The `stocks` cell itself contains commas - this is exactly the case that would
        # "corrupt formatting" if not quoted correctly on write-back.
        provider = FakeHistoryProvider({
            ("NSE:A-EQ", "2024-01-01"): (100.0, 111.0, 99.0, 110.0, 1000.0),
            ("NSE:B-EQ", "2024-01-01"): (100.0, 101.0, 99.0, 100.0, 1000.0),
        })
        _write_picks_csv(self.csv_path, [("2024-01-01", "NSE:A-EQ, NSE:B-EQ")])
        bp.run_backtest_from_csv(str(self.csv_path), provider, cache_dir=str(self.cache_dir))

        reread = bp.load_picks_csv(str(self.csv_path))
        self.assertEqual(len(reread), 1)  # NOT split into two rows by the embedded comma
        self.assertEqual(reread.loc[0, "stocks"], "NSE:A-EQ, NSE:B-EQ")
        details = json.loads(reread.loc[0, "stock_performance_details"])
        self.assertEqual({d["symbol"] for d in details}, {"NSE:A-EQ", "NSE:B-EQ"})

    def test_duplicate_dates_do_not_cross_contaminate_results(self):
        # Two rows share the same date but different picks - a date-keyed lookup dict
        # would let one overwrite the other; row-position keying (see BacktestEngine.run)
        # must keep them separate.
        provider = FakeHistoryProvider({
            ("NSE:A-EQ", "2024-01-01"): (100.0, 111.0, 99.0, 110.0, 1000.0),  # +10%
            ("NSE:B-EQ", "2024-01-01"): (100.0, 101.0, 89.0, 95.0, 1000.0),   # -5%
        })
        _write_picks_csv(self.csv_path, [
            ("2024-01-01", "NSE:A-EQ"),
            ("2024-01-01", "NSE:B-EQ"),
        ])
        bp.run_backtest_from_csv(str(self.csv_path), provider, initial_capital=100_000.0,
                                  cost_pct_per_side=0.0, cache_dir=str(self.cache_dir))
        reread = bp.load_picks_csv(str(self.csv_path))
        row0_details = json.loads(reread.loc[0, "stock_performance_details"])
        row1_details = json.loads(reread.loc[1, "stock_performance_details"])
        self.assertEqual(row0_details[0]["symbol"], "NSE:A-EQ")
        self.assertEqual(row1_details[0]["symbol"], "NSE:B-EQ")
        self.assertAlmostEqual(row0_details[0]["return_pct"], 10.0, places=2)
        self.assertAlmostEqual(row1_details[0]["return_pct"], -5.0, places=2)

    def test_caching_avoids_a_second_api_call_on_rerun(self):
        provider = FakeHistoryProvider({
            ("NSE:A-EQ", "2024-01-01"): (100.0, 111.0, 99.0, 110.0, 1000.0),
        })
        _write_picks_csv(self.csv_path, [("2024-01-01", "NSE:A-EQ")])

        bp.run_backtest_from_csv(str(self.csv_path), provider, cache_dir=str(self.cache_dir))
        first_run_calls = len(provider.call_log)
        self.assertGreaterEqual(first_run_calls, 1)

        # Re-run over the SAME date range: the on-disk cache already covers it, so
        # get_candles_range should not be called again at all.
        bp.run_backtest_from_csv(str(self.csv_path), provider, cache_dir=str(self.cache_dir))
        self.assertEqual(len(provider.call_log), first_run_calls, "second run should make zero new API calls")


if __name__ == "__main__":
    unittest.main(verbosity=2)
