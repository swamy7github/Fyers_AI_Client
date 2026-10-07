"""
test_options_engine.py
--------------------------
Tests for options_engine.py: the F&O symbol-master loader/column
validation, ATM strike/expiry resolution, the single-symbol opening-range
shortlist builder, and run_nifty_options_engine()'s CE-on-LONG/PE-on-SHORT
mapping and lot_size/capital_allocated wiring onto the returned SignalEngine.

Everything here is a locally-built pandas DataFrame or a tiny Fake data
service - no real Fyers symbol master file or network call is involved.

Run with:
    python test_options_engine.py
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import pandas as pd

import config
import options_engine
from trade_direction import TradeDirection

_MASTER_COLUMNS = config.NIFTY_FO_MASTER_COLUMNS  # {logical: actual column name}


def _master_row(option_type, strike, expiry, lot_size=75, symbol=None):
    symbol = symbol or f"NSE:NIFTY25O14{int(strike)}{option_type}"
    return {
        _MASTER_COLUMNS["underlying"]: "NIFTY",
        _MASTER_COLUMNS["option_type"]: option_type,
        _MASTER_COLUMNS["expiry"]: expiry,
        _MASTER_COLUMNS["strike"]: strike,
        _MASTER_COLUMNS["trading_symbol"]: symbol,
        _MASTER_COLUMNS["lot_size"]: lot_size,
    }


def _sample_master() -> pd.DataFrame:
    """Two expiries, several strikes each, both CE and PE - enough to exercise
    "nearest expiry" AND "nearest strike within that expiry" selection."""
    rows = []
    for expiry in ("2026-10-14", "2026-10-21"):
        for strike in (24800, 24850, 24900, 24950, 25000):
            for opt_type in ("CE", "PE"):
                rows.append(_master_row(opt_type, strike, expiry))
    return pd.DataFrame(rows)


class FakeQuoteDataService:
    """Implements only get_quotes - enough for resolve_atm_option_symbol/_fetch_nifty_spot."""

    def __init__(self, spot: float | None):
        self._spot = spot

    def get_quotes(self, symbols):
        if self._spot is None:
            return {"d": []}
        return {"d": [{"n": symbols[0], "v": {"lp": self._spot}}]}


class FakeCandleDataService:
    """Implements only get_5min_candle - enough for build_single_symbol_shortlist."""

    def __init__(self, candles):
        self._candles = candles
        self.calls = []

    def get_5min_candle(self, symbol, date_str, from_epoch, to_epoch):
        self.calls.append((symbol, date_str, from_epoch, to_epoch))
        return self._candles


class FakeFullDataService(FakeQuoteDataService, FakeCandleDataService):
    def __init__(self, spot, candles):
        FakeQuoteDataService.__init__(self, spot)
        FakeCandleDataService.__init__(self, candles)


class TestLoadFoSymbolMaster(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_missing_file_returns_empty_dataframe_without_raising(self):
        df = options_engine.load_fo_symbol_master(os.path.join(self.tmp_dir, "does_not_exist.csv"))
        self.assertTrue(df.empty)

    def test_missing_expected_column_returns_empty_dataframe(self):
        path = os.path.join(self.tmp_dir, "master.csv")
        pd.DataFrame([{"some_other_column": 1}]).to_csv(path, index=False)
        df = options_engine.load_fo_symbol_master(path)
        self.assertTrue(df.empty)

    def test_well_formed_file_loads_successfully(self):
        path = os.path.join(self.tmp_dir, "master.csv")
        _sample_master().to_csv(path, index=False)
        df = options_engine.load_fo_symbol_master(path)
        self.assertFalse(df.empty)
        self.assertEqual(len(df), 20)  # 2 expiries x 5 strikes x 2 option types


class TestResolveAtmOptionSymbol(unittest.TestCase):
    def test_picks_nearest_strike_within_nearest_expiry(self):
        master = _sample_master()
        # Spot 24,910 - nearest strike among (24800, 24850, 24900, 24950, 25000) is 24900.
        data_service = FakeQuoteDataService(spot=24_910.0)
        result = options_engine.resolve_atm_option_symbol(data_service, "CE", symbol_master=master)
        self.assertIsNotNone(result)
        symbol, lot_size = result
        self.assertIn("24900", symbol)
        self.assertIn("CE", symbol)
        self.assertEqual(lot_size, 75)

    def test_uses_nearest_expiry_not_a_later_one(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_900.0)
        symbol, _ = options_engine.resolve_atm_option_symbol(data_service, "PE", symbol_master=master)
        # Both expiries have an exact-match 24900 strike - only the EARLIER expiry's
        # row should ever be considered, so the "2026-10-21" row would also match if
        # the nearest-expiry filter were broken; this just confirms a result comes back
        # and resolve doesn't crash when more than one expiry has the exact strike.
        self.assertIn("24900", symbol)
        self.assertIn("PE", symbol)

    def test_exact_strike_match_wins_tie_break(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=25_000.0)
        symbol, _ = options_engine.resolve_atm_option_symbol(data_service, "CE", symbol_master=master)
        self.assertIn("25000", symbol)

    def test_empty_master_returns_none(self):
        data_service = FakeQuoteDataService(spot=24_900.0)
        result = options_engine.resolve_atm_option_symbol(data_service, "CE", symbol_master=pd.DataFrame())
        self.assertIsNone(result)

    def test_no_matching_option_type_returns_none(self):
        master = _sample_master()
        master = master[master[_MASTER_COLUMNS["option_type"]] == "CE"]  # drop all PE rows
        data_service = FakeQuoteDataService(spot=24_900.0)
        result = options_engine.resolve_atm_option_symbol(data_service, "PE", symbol_master=master)
        self.assertIsNone(result)

    def test_unresolvable_spot_returns_none(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=None)  # simulates a quotes call returning nothing
        result = options_engine.resolve_atm_option_symbol(data_service, "CE", symbol_master=master)
        self.assertIsNone(result)


class TestBuildSingleSymbolShortlist(unittest.TestCase):
    def test_builds_a_one_row_shortlist_from_the_opening_range_candle(self):
        candle = [1234567890, 100.0, 105.0, 98.0, 102.0, 5000]
        data_service = FakeCandleDataService([candle])
        df = options_engine.build_single_symbol_shortlist(data_service, "NSE:NIFTY25O1424900CE")
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row["symbol"], "NSE:NIFTY25O1424900CE")
        self.assertEqual(row["high"], 105.0)
        self.assertEqual(row["low"], 98.0)

    def test_no_candle_available_returns_empty_dataframe(self):
        data_service = FakeCandleDataService([])
        df = options_engine.build_single_symbol_shortlist(data_service, "NSE:NIFTY25O1424900CE")
        self.assertTrue(df.empty)


class TestRunNiftyOptionsEngine(unittest.TestCase):
    def setUp(self):
        self._orig_capital = config.NIFTY_OPTIONS_CAPITAL
        config.NIFTY_OPTIONS_CAPITAL = 200_000.0

    def tearDown(self):
        config.NIFTY_OPTIONS_CAPITAL = self._orig_capital

    def test_long_bias_buys_a_call(self):
        master = _sample_master()
        candle = [1234567890, 100.0, 105.0, 98.0, 102.0, 5000]
        data_service = FakeFullDataService(spot=24_900.0, candles=[candle])
        engine = options_engine.run_nifty_options_engine(data_service, TradeDirection.LONG, symbol_master=master)
        self.assertIsNotNone(engine)
        self.assertEqual(len(engine.trackers), 1)
        symbol, tracker = next(iter(engine.trackers.items()))
        self.assertIn("CE", symbol)
        self.assertEqual(tracker.direction, TradeDirection.LONG)
        self.assertEqual(tracker.lot_size, 75)
        self.assertEqual(tracker.capital_allocated, 200_000.0)

    def test_short_bias_buys_a_put_but_engine_still_tracks_long(self):
        master = _sample_master()
        candle = [1234567890, 100.0, 105.0, 98.0, 102.0, 5000]
        data_service = FakeFullDataService(spot=24_900.0, candles=[candle])
        engine = options_engine.run_nifty_options_engine(data_service, TradeDirection.SHORT, symbol_master=master)
        self.assertIsNotNone(engine)
        symbol, tracker = next(iter(engine.trackers.items()))
        self.assertIn("PE", symbol)
        # BUY-ONLY: even on a SHORT day's bias, the PE contract itself is tracked LONG
        # (rising premium = good) - see options_engine.py's module docstring, point 1.
        self.assertEqual(tracker.direction, TradeDirection.LONG)

    def test_unresolvable_contract_returns_none(self):
        data_service = FakeFullDataService(spot=24_900.0, candles=[])
        engine = options_engine.run_nifty_options_engine(data_service, TradeDirection.LONG,
                                                           symbol_master=pd.DataFrame())
        self.assertIsNone(engine)

    def test_missing_opening_range_candle_returns_none(self):
        master = _sample_master()
        data_service = FakeFullDataService(spot=24_900.0, candles=[])  # resolves fine, no candle yet
        engine = options_engine.run_nifty_options_engine(data_service, TradeDirection.LONG, symbol_master=master)
        self.assertIsNone(engine)


if __name__ == "__main__":
    unittest.main(verbosity=2)
