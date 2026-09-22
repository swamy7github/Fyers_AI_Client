"""
test_updates.py
-----------------
Lightweight tests for:

  1. Sector-map loading and stock-universe filtering
     (sector_scanner.load_sector_map, opening_range_scanner.load_valid_sectors
     / load_universe) - using temporary CSVs, never your real data/ files.

  2. The late-startup catch-up decision helpers in main.py
     (_is_market_open_now, _within_signal_window, _should_catch_up_*,
     _should_run_immediate_signal_check, _should_use_fast_opening_range_path)
     for the three startup scenarios called out in the task - before market
     hours, mid-session (10:00), after market close - plus one boundary case
     worth knowing about explicitly (market open, but past the signal-
     checking cutoff).

These are pure-function / pure-data tests: no live Fyers connection, no
real clock dependency, no actual APScheduler run loop. That's deliberate -
testing "did the scheduler wait until the right wall-clock second" is
inherently flaky and slow; testing "given this timestamp, does the code
decide to catch up or not" is fast, deterministic, and exercises exactly
the logic that matters.

Uses only the standard library's unittest (no pytest dependency, to avoid
adding one just for this). Run with:
    python test_updates.py
or:
    python -m unittest test_updates -v
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pytz

import config
import main
from opening_range_scanner import load_universe, load_valid_sectors
from sector_scanner import load_sector_map


class TestSectorMapAndUniverseFiltering(unittest.TestCase):
    """Uses temporary CSVs so it never touches your real data/ files."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sector_csv = self.tmpdir / "sector_index_map.csv"
        self.universe_csv = self.tmpdir / "ind_nifty500list.csv"

        # Small sector reference: two real sectors (IT, Banks) plus one blank-sector row
        # (mimicking a broad-market row that should never match anything).
        self.sector_csv.write_text(
            "symbol,index_name,category,is_sector_index,sector\n"
            "NSE:NIFTYIT-INDEX,Nifty IT,Information Technology,True,IT\n"
            "NSE:NIFTYBANK-INDEX,Nifty Bank,Banking,True,Banks\n"
            "NSE:NIFTY50-INDEX,Nifty 50,Broad Market,False,\n"
        )

        # Universe: 5 stocks - 2 IT (one with mixed casing/whitespace, to test
        # normalization), 2 Banks (one differently-cased), 1 Pharma (no matching
        # sector -> should be dropped).
        self.universe_csv.write_text(
            "Symbol,Company Name,Industry\n"
            "TCS,Tata Consultancy Services,IT\n"
            "INFY,Infosys Limited,  it  \n"
            "HDFCBANK,HDFC Bank,Banks\n"
            "ICICIBANK,ICICI Bank,BANKS\n"
            "SUNPHARMA,Sun Pharma,Pharma\n"
        )

        # Point config at the temp files for the duration of this test only.
        self._orig_sector_csv = config.SECTOR_INDEX_MAP_CSV
        self._orig_universe_csv = config.STOCK_UNIVERSE_CSV
        config.SECTOR_INDEX_MAP_CSV = str(self.sector_csv)
        config.STOCK_UNIVERSE_CSV = str(self.universe_csv)

    def tearDown(self):
        config.SECTOR_INDEX_MAP_CSV = self._orig_sector_csv
        config.STOCK_UNIVERSE_CSV = self._orig_universe_csv
        shutil.rmtree(self.tmpdir)

    def test_load_sector_map_extracts_sector_column_case_insensitively(self):
        df = load_sector_map()
        self.assertIn("sector", df.columns)
        self.assertEqual(set(df["sector"]), {"IT", "Banks", ""})

    def test_load_sector_map_handles_capitalized_header(self):
        self.sector_csv.write_text(
            "symbol,index_name,category,is_sector_index,Sector\n"  # capital "Sector"
            "NSE:NIFTYIT-INDEX,Nifty IT,Information Technology,True,  IT  \n"  # extra whitespace too
        )
        df = load_sector_map()
        self.assertEqual(list(df["sector"]), ["IT"])  # found the column despite casing, and trimmed it

    def test_load_valid_sectors_excludes_blank_rows(self):
        valid = load_valid_sectors()
        self.assertEqual(valid, {"IT", "BANKS"})

    def test_universe_filtering_matches_case_insensitively_and_drops_unmatched(self):
        symbols = load_universe()
        self.assertEqual(set(symbols), {
            "NSE:TCS-EQ", "NSE:INFY-EQ", "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ",
        })
        self.assertNotIn("NSE:SUNPHARMA-EQ", symbols)

    def test_missing_industry_column_falls_back_to_full_universe(self):
        self.universe_csv.write_text("Symbol,Company Name\nTCS,Tata Consultancy Services\n")
        symbols = load_universe()
        self.assertEqual(symbols, ["NSE:TCS-EQ"])  # nothing filtered out - no Industry column to filter on

    def test_no_valid_sectors_falls_back_to_full_universe(self):
        self.sector_csv.write_text(
            "symbol,index_name,category,is_sector_index,sector\n"
            "NSE:NIFTY50-INDEX,Nifty 50,Broad Market,False,\n"
        )
        symbols = load_universe()
        self.assertEqual(len(symbols), 5)  # all 5 rows retained, filtering skipped entirely

    def test_missing_sector_csv_does_not_crash(self):
        config.SECTOR_INDEX_MAP_CSV = str(self.tmpdir / "does_not_exist.csv")
        df = load_sector_map()
        self.assertTrue(df.empty)
        symbols = load_universe()
        self.assertEqual(len(symbols), 5)  # falls back to the full, unfiltered universe


class TestSchedulerLateStartupDecisions(unittest.TestCase):
    """Pure-function tests for main.py's startup catch-up gating, using injected
    timestamps instead of a real clock or a running APScheduler instance."""

    @classmethod
    def setUpClass(cls):
        cls.tz = pytz.timezone(config.TIMEZONE)

    def _dt(self, hour, minute, second=0, day=21):  # 21 Sep 2026 is a Monday
        return self.tz.localize(datetime(2026, 9, day, hour, minute, second))

    def test_before_market_hours(self):
        now = self._dt(8, 0)
        self.assertFalse(main._is_market_open_now(now))
        self.assertFalse(main._should_catch_up_sector_postopen(now))
        self.assertFalse(main._should_catch_up_opening_range(now))
        self.assertFalse(main._should_run_immediate_signal_check(now))

    def test_mid_session_start_at_10am(self):
        now = self._dt(10, 0)
        self.assertTrue(main._is_market_open_now(now))
        self.assertTrue(main._within_signal_window(now))
        self.assertTrue(main._should_catch_up_sector_postopen(now))
        self.assertTrue(main._should_catch_up_opening_range(now))
        self.assertTrue(main._should_run_immediate_signal_check(now))

    def test_after_market_close(self):
        now = self._dt(16, 0)
        self.assertFalse(main._is_market_open_now(now))
        self.assertFalse(main._should_catch_up_sector_postopen(now))
        self.assertFalse(main._should_catch_up_opening_range(now))
        self.assertFalse(main._should_run_immediate_signal_check(now))

    def test_open_but_past_signal_cutoff_boundary_case(self):
        # 15:20 IST: market is open (close is 15:30) but past the 15:10 signal-checking
        # cutoff. Fresh opening-range/sector catch-up should NOT run (see
        # _within_signal_window's docstring - no point opening a new position with no
        # square-off safety net left today), but the immediate signal-check job is still
        # scheduled per the literal spec ("if started within trading hours...") - it just
        # no-ops internally once it actually runs, since job_check_signals() has its own
        # cutoff check too.
        now = self._dt(15, 20)
        self.assertTrue(main._is_market_open_now(now))
        self.assertFalse(main._within_signal_window(now))
        self.assertFalse(main._should_catch_up_sector_postopen(now))
        self.assertFalse(main._should_catch_up_opening_range(now))
        self.assertTrue(main._should_run_immediate_signal_check(now))

    def test_weekend_is_never_market_open(self):
        saturday = self._dt(10, 0, day=26)  # 26 Sep 2026 is a Saturday
        self.assertFalse(main._is_market_open_now(saturday))

    def test_opening_range_fast_vs_accurate_path_selection(self):
        on_time = self._dt(9, 20, 2)
        self.assertTrue(main._should_use_fast_opening_range_path(on_time))

        slightly_late = self._dt(9, 22, 0)  # 118s late, within the 180s tolerance
        self.assertTrue(main._should_use_fast_opening_range_path(slightly_late))

        very_late = self._dt(10, 0, 0)  # well outside tolerance
        self.assertFalse(main._should_use_fast_opening_range_path(very_late))


if __name__ == "__main__":
    unittest.main(verbosity=2)
