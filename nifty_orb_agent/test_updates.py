"""
test_updates.py
-----------------
Lightweight tests for:

  1. Sector-map loading and stock-universe filtering
     (sector_scanner.load_sector_map, opening_range_scanner.load_valid_sectors
     / load_universe) - using temporary CSVs, never your real data/ files.

  2. Market-phase detection primitives, now living in bootstrap.py rather
     than main.py (bootstrap.detect_market_phase / _is_market_open_now /
     _within_signal_window) - the fuller picture, including the actual
     catch-up orchestration (bootstrap_and_catchup itself, historical
     replay, broker reconciliation), is in test_bootstrap.py; this file
     keeps just the phase-detection unit tests plus main.py's remaining
     fast-vs-accurate opening-range path selector.

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
import pandas as pd
import pytz

import config
import main
import bootstrap
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
            "symbol,index_name,category,is_sector_index,sector,sector_url\n"
            "NSE:NIFTYIT-INDEX,Nifty IT,Information Technology,True,IT,n\n"
            "NSE:NIFTYBANK-INDEX,Nifty Bank,Banking,True,Banks,m\n"
            "NSE:NIFTY50-INDEX,Nifty 50,Broad Market,False\n"
        )

        # Universe: 5 stocks - 2 IT (one with mixed casing/whitespace, to test
        # normalization), 2 Banks (one differently-cased), 1 Pharma (no matching
        # sector -> should be dropped).
        self.universe_csv.write_text(
            "Symbol,Company Name,Industry,sector_url\n"
            "TCS,Tata Consultancy Services,IT,m\n"
            "INFY,Infosys Limited,  it ,n \n"
            "HDFCBANK,HDFC Bank,Banks,s\n"
            "ICICIBANK,ICICI Bank,BANKS,fd\n"
            "SUNPHARMA,Sun Pharma,Pharma,df\n"
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
            "symbol,index_name,category,is_sector_index,Sector,sector_url\n"  # capital "Sector"
            "NSE:NIFTYIT-INDEX,Nifty IT,Information Technology,True,  IT  \n"  # extra whitespace too
        )
        df = load_sector_map()
        self.assertEqual(list(df["sector"]), ["IT"])  # found the column despite casing, and trimmed it

    def test_load_valid_sectors_excludes_blank_rows(self):
        valid = load_valid_sectors()
        self.assertEqual(valid, {"IT", "BANKS"})

    def test_universe_filtering_matches_case_insensitively_and_drops_unmatched(self):
        symbols = load_universe(pd.DataFrame([{"sector":"Information Technology"},{"sector": "Banking"}]))
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
        self.assertEqual(bootstrap.detect_market_phase(now), bootstrap.MarketPhase.PRE_OPEN)
        self.assertFalse(bootstrap._is_market_open_now(now))
        self.assertFalse(bootstrap._within_signal_window(now))

    def test_mid_session_start_at_10am(self):
        now = self._dt(10, 0)
        self.assertEqual(bootstrap.detect_market_phase(now), bootstrap.MarketPhase.POST_OPEN)
        self.assertTrue(bootstrap._is_market_open_now(now))
        self.assertTrue(bootstrap._within_signal_window(now))

    def test_after_market_close(self):
        now = self._dt(16, 0)
        self.assertEqual(bootstrap.detect_market_phase(now), bootstrap.MarketPhase.POST_CLOSE)
        self.assertFalse(bootstrap._is_market_open_now(now))
        self.assertFalse(bootstrap._within_signal_window(now))

    def test_open_but_past_signal_cutoff_boundary_case(self):
        # 15:20 IST: market is open (close is 15:30) but past the 15:10 signal-checking
        # cutoff - phase is still POST_OPEN, but _within_signal_window (the narrower gate
        # bootstrap_and_catchup uses to decide whether starting anything NEW is worthwhile)
        # is False. See _within_signal_window's docstring for why.
        now = self._dt(15, 20)
        self.assertEqual(bootstrap.detect_market_phase(now), bootstrap.MarketPhase.POST_OPEN)
        self.assertTrue(bootstrap._is_market_open_now(now))
        self.assertFalse(bootstrap._within_signal_window(now))

    def test_weekend_is_post_close(self):
        saturday = self._dt(10, 0, day=26)  # 26 Sep 2026 is a Saturday
        self.assertEqual(bootstrap.detect_market_phase(saturday), bootstrap.MarketPhase.POST_CLOSE)

    def test_opening_range_fast_vs_accurate_path_selection(self):
        on_time = self._dt(9, 20, 2)
        self.assertTrue(main._should_use_fast_opening_range_path(on_time))

        slightly_late = self._dt(9, 22, 0)  # 118s late, within the 180s tolerance
        self.assertTrue(main._should_use_fast_opening_range_path(slightly_late))

        very_late = self._dt(10, 0, 0)  # well outside tolerance
        self.assertFalse(main._should_use_fast_opening_range_path(very_late))


if __name__ == "__main__":
    unittest.main(verbosity=2)
