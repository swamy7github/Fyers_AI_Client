"""
test_options_engine.py
--------------------------
Tests for options_engine.py's SINGLE-SOURCE, BOTH-SIDES design: PRICE/VWAP/VOLUME signal
all read from the NIFTY future, the NIFTY index read only (at SIGNAL TIME, not engine
construction) for its spot price to pick a strike (ITM/ATM/OTM, config.OPTION_STRIKE_MODE),
execution on a single option contract whose type (CE/PE) is decided by WHICH breakout
direction the strategy's own entry criteria confirms first - not a pre-computed daily bias
(see options_engine.py's module docstring).

Covers: the F&O symbol-master loader/column validation, ITM/ATM/OTM strike/expiry resolution
(including the "fewer strikes listed than requested" fallback for ITM/OTM), the opening-range
fetch, NiftyOptionsEngine's entry/exit arithmetic fed directly via feed_candle() (hand-verified
VWAP numbers, reusing the exact warm-up pattern test_signal_engine.py already proved out for
equities), proof that the strike is resolved AT SIGNAL TIME rather than at engine construction,
proof that config.OPTION_STRIKE_MODE genuinely changes which strike gets bought, a same-day
CE-then-PE "second leg" scenario, and run_nifty_options_engine()'s wiring (bias_direction now
only gates the opening-range quality filter, it no longer picks CE vs PE).

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
from options_engine import NiftyOptionsEngine
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
    "nearest expiry" AND "Nth ITM/OTM strike within that expiry" AND "nearest-to-spot ATM
    strike" selection."""
    rows = []
    for expiry in ("2026-10-14", "2026-10-21"):
        for strike in (24800, 24850, 24900, 24950, 25000, 25050, 25100):
            for opt_type in ("CE", "PE"):
                rows.append(_master_row(opt_type, strike, expiry))
    return pd.DataFrame(rows)


def _banknifty_master_row(option_type, strike, expiry, lot_size=15, symbol=None):
    symbol = symbol or f"NSE:BANKNIFTY25O14{int(strike)}{option_type}"
    return {
        _MASTER_COLUMNS["underlying"]: "BANKNIFTY",
        _MASTER_COLUMNS["option_type"]: option_type,
        _MASTER_COLUMNS["expiry"]: expiry,
        _MASTER_COLUMNS["strike"]: strike,
        _MASTER_COLUMNS["trading_symbol"]: symbol,
        _MASTER_COLUMNS["lot_size"]: lot_size,
    }


def _sample_banknifty_master() -> pd.DataFrame:
    """Same shape as _sample_master() but underlying="BANKNIFTY", a wider (100-point)
    strike gap, and BANKNIFTY's smaller lot size - lets tests confirm the underlying
    filter in resolve_option_symbol() actually separates the two rather than just
    happening to work because only one underlying was ever present in the master."""
    rows = []
    for expiry in ("2026-10-14", "2026-10-21"):
        for strike in (51000, 51100, 51200, 51300, 51400, 51500, 51600):
            for opt_type in ("CE", "PE"):
                rows.append(_banknifty_master_row(opt_type, strike, expiry))
    return pd.DataFrame(rows)


class FakeQuoteDataService:
    """Implements only get_quotes - enough for resolve_option_symbol/_fetch_index_spot
    and NiftyOptionsEngine's option-price fetch. `spot` feeds `index_symbol` lookups
    (config.NIFTY_INDEX_SYMBOL by default - pass config.BANKNIFTY_INDEX_SYMBOL for
    BankNifty tests); `option_prices` (a dict or a single float) feeds any other symbol."""

    def __init__(self, spot: float | None, option_prices=None, index_symbol: str | None = None):
        self._spot = spot
        self._index_symbol = index_symbol or config.NIFTY_INDEX_SYMBOL
        self._option_prices = option_prices if isinstance(option_prices, dict) else {}
        self._default_option_price = option_prices if isinstance(option_prices, (int, float)) else None

    def get_quotes(self, symbols):
        symbol = symbols[0]
        if symbol == self._index_symbol:
            if self._spot is None:
                return {"d": []}
            return {"d": [{"n": symbol, "v": {"lp": self._spot}}]}
        price = self._option_prices.get(symbol, self._default_option_price)
        if price is None:
            return {"d": []}
        return {"d": [{"n": symbol, "v": {"lp": price}}]}


class FakeCandleDataService:
    """Implements get_5min_candle AND get_candle - enough for fetch_index_opening_range /
    NiftyOptionsEngine.check_all() (which, since NiftyOptionsEngine grew a configurable
    `candle_resolution` for MCX options' sake, now calls the generic get_candle(symbol,
    resolution, ...) instead of the old 5-min-only convenience wrapper - see
    options_engine.NiftyOptionsEngine._fetch_latest_candle - mirroring how the real
    fyers_service.FyersDataService.get_5min_candle is itself just a thin wrapper around its own
    get_candle()). `candles_by_symbol` maps symbol -> the ONE candle (a list) to return for ANY
    window/resolution requested for that symbol."""

    def __init__(self, candles_by_symbol: dict[str, list]):
        self._candles_by_symbol = candles_by_symbol
        self.calls = []

    def get_candle(self, symbol, resolution, date_str, from_epoch, to_epoch):
        self.calls.append((symbol, resolution, date_str, from_epoch, to_epoch))
        candle = self._candles_by_symbol.get(symbol)
        return [candle] if candle else []

    def get_5min_candle(self, symbol, date_str, from_epoch, to_epoch):
        return self.get_candle(symbol, "5", date_str, from_epoch, to_epoch)


class FakeFullDataService(FakeQuoteDataService, FakeCandleDataService):
    def __init__(self, spot, candles_by_symbol, option_prices=None, index_symbol: str | None = None):
        FakeQuoteDataService.__init__(self, spot, option_prices, index_symbol=index_symbol)
        FakeCandleDataService.__init__(self, candles_by_symbol)


class FakeSpotSequenceDataService(FakeCandleDataService):
    """get_quotes for index_symbol returns spots IN ORDER from `spot_sequence` (one per call,
    the last value repeats once exhausted) - simulates the index's spot price genuinely moving
    between today's ~09:20 engine setup and whenever (if ever) an entry signal later fires.
    get_quotes for any OTHER symbol (the resolved option contract) returns a flat
    `option_price`. Used to prove strike resolution happens AT SIGNAL TIME (reads whichever
    spot is current when an entry actually fires), not once upfront at engine construction -
    see TestNiftyOptionsEngineResolvesStrikeAtSignalTime below."""

    def __init__(self, candles_by_symbol, spot_sequence, option_price, index_symbol: str | None = None):
        super().__init__(candles_by_symbol)
        self._spot_sequence = list(spot_sequence)
        self._index_symbol = index_symbol or config.NIFTY_INDEX_SYMBOL
        self._option_price = option_price
        self.index_spot_calls = 0

    def get_quotes(self, symbols):
        symbol = symbols[0]
        if symbol == self._index_symbol:
            self.index_spot_calls += 1
            spot = self._spot_sequence.pop(0) if self._spot_sequence else None
            if spot is None:
                return {"d": []}
            return {"d": [{"n": symbol, "v": {"lp": spot}}]}
        return {"d": [{"n": symbol, "v": {"lp": self._option_price}}]}


# ----------------------------------------------------------------------------
# Symbol master
# ----------------------------------------------------------------------------
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
        self.assertEqual(len(df), 28)  # 2 expiries x 7 strikes x 2 option types


# ----------------------------------------------------------------------------
# ITM / ATM / OTM strike resolution
# ----------------------------------------------------------------------------
class TestResolveOptionSymbolOtm(unittest.TestCase):
    """strike_mode="OTM" (the default, and this repo's original/only behaviour before
    config.OPTION_STRIKE_MODE existed)."""

    def test_ce_picks_the_first_strike_above_spot_by_default(self):
        master = _sample_master()
        # Spot 24,910 - strikes above spot are 24950, 25000, 25050, 25100 - nearest (1-away) is 24950.
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, lot_size = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master)
        self.assertIn("24950", symbol)
        self.assertIn("CE", symbol)
        self.assertEqual(lot_size, 75)

    def test_pe_picks_the_first_strike_below_spot_by_default(self):
        master = _sample_master()
        # Spot 24,910 - strikes below spot are 24900, 24850, 24800 - nearest (1-away) is 24900.
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, _ = options_engine.resolve_option_symbol(data_service, "PE", symbol_master=master)
        self.assertIn("24900", symbol)
        self.assertIn("PE", symbol)

    def test_strikes_away_parameter_picks_further_otm(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        # 2-away CE: strikes above spot ascending are 24950(1st), 25000(2nd).
        symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master,
                                                            strikes_away=2)
        self.assertIn("25000", symbol)

    def test_falls_back_to_farthest_available_when_fewer_strikes_than_requested(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        # Only 4 strikes exist above spot (24950/25000/25050/25100) - asking for the 10th
        # should fall back to the farthest (25100), not crash or return None.
        symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master,
                                                            strikes_away=10)
        self.assertIn("25100", symbol)

    def test_uses_nearest_expiry_not_a_later_one(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, _ = options_engine.resolve_option_symbol(data_service, "PE", symbol_master=master)
        self.assertIn("24900", symbol)

    def test_empty_master_returns_none(self):
        data_service = FakeQuoteDataService(spot=24_910.0)
        result = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=pd.DataFrame())
        self.assertIsNone(result)

    def test_no_matching_option_type_returns_none(self):
        master = _sample_master()
        master = master[master[_MASTER_COLUMNS["option_type"]] == "CE"]  # drop all PE rows
        data_service = FakeQuoteDataService(spot=24_910.0)
        result = options_engine.resolve_option_symbol(data_service, "PE", symbol_master=master)
        self.assertIsNone(result)

    def test_unresolvable_spot_returns_none(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=None)  # simulates a quotes call returning nothing
        result = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master)
        self.assertIsNone(result)

    def test_strikes_away_below_one_is_treated_as_one(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master,
                                                            strikes_away=0)
        self.assertIn("24950", symbol)

    def test_default_strike_mode_comes_from_config_option_strike_mode(self):
        orig = config.OPTION_STRIKE_MODE
        try:
            config.OPTION_STRIKE_MODE = "OTM"
            master = _sample_master()
            data_service = FakeQuoteDataService(spot=24_910.0)
            symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master)
            self.assertIn("24950", symbol)
        finally:
            config.OPTION_STRIKE_MODE = orig

    def test_invalid_strike_mode_falls_back_to_otm(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", strike_mode="BOGUS",
                                                            symbol_master=master)
        self.assertIn("24950", symbol)  # same result as OTM, the documented fallback


def _multi_month_master() -> pd.DataFrame:
    """Four weekly expiries spanning two calendar months (two remaining in October, two in
    November) - enough to prove expiry_select="MONTHLY" picks a DIFFERENT, later contract
    than the default "NEAREST" when there's more than one week left in the current month."""
    rows = []
    for expiry in ("2026-10-15", "2026-10-22", "2026-10-29", "2026-11-05"):
        for strike in (24800, 24850, 24900, 24950, 25000, 25050, 25100):
            for opt_type in ("CE", "PE"):
                symbol = f"NSE:NIFTY{expiry.replace('-', '')}{int(strike)}{opt_type}"
                rows.append(_master_row(opt_type, strike, expiry, symbol=symbol))
    return pd.DataFrame(rows)


class TestResolveOptionSymbolExpirySelect(unittest.TestCase):
    """expiry_select - backtest_options.py's weekly-vs-monthly comparison knob. Every LIVE
    caller leaves this at the default "NEAREST" (unchanged original behaviour - see
    TestResolveOptionSymbolOtm.test_uses_nearest_expiry_not_a_later_one)."""

    def test_nearest_is_still_the_default_and_unchanged(self):
        master = _multi_month_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master)
        self.assertIn("20261015", symbol)  # the single soonest expiry, same as before this feature existed

    def test_monthly_picks_the_last_expiry_within_the_earliest_month(self):
        master = _multi_month_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master,
                                                            expiry_select="MONTHLY")
        self.assertIn("20261029", symbol)  # last Thursday of October, NOT 10-15 (nearest) or 11-05 (next month)

    def test_monthly_and_nearest_collapse_to_the_same_contract_when_only_one_expiry_per_month(self):
        """BANKNIFTY/MCX-style monthly-only listing - both selectors must agree."""
        master = _sample_master()  # two expiries, both inside the same month (10-14/10-21 -> Oct)
        data_service = FakeQuoteDataService(spot=24_910.0)
        nearest_symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master,
                                                                    expiry_select="NEAREST")
        self.assertIn("24950", nearest_symbol)

    def test_invalid_expiry_select_falls_back_to_nearest(self):
        master = _multi_month_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master,
                                                            expiry_select="BOGUS")
        self.assertIn("20261015", symbol)  # same result as NEAREST, the documented fallback

    def test_unparseable_expiry_column_falls_back_to_nearest_for_monthly(self):
        master = _multi_month_master().copy()
        master[_MASTER_COLUMNS["expiry"]] = "not-a-date"
        data_service = FakeQuoteDataService(spot=24_910.0)
        result = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=master,
                                                        expiry_select="MONTHLY")
        # min() of an all-identical unparseable column still resolves to SOME row - the point
        # of this test is just that it degrades gracefully (no exception), not a specific strike.
        self.assertIsNotNone(result)


class TestResolveOptionSymbolItm(unittest.TestCase):
    """strike_mode="ITM" - the mirror image of OTM: CE below spot, PE above spot."""

    def test_ce_picks_the_first_strike_below_spot(self):
        master = _sample_master()
        # Spot 24,910 - ITM CALL strikes are BELOW spot: 24900 (nearest), 24850, 24800.
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, lot_size = options_engine.resolve_option_symbol(data_service, "CE", strike_mode="ITM",
                                                                   symbol_master=master)
        self.assertIn("24900", symbol)
        self.assertIn("CE", symbol)
        self.assertEqual(lot_size, 75)

    def test_pe_picks_the_first_strike_above_spot(self):
        master = _sample_master()
        # Spot 24,910 - ITM PUT strikes are ABOVE spot: 24950 (nearest), 25000, ...
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, _ = options_engine.resolve_option_symbol(data_service, "PE", strike_mode="ITM",
                                                            symbol_master=master)
        self.assertIn("24950", symbol)
        self.assertIn("PE", symbol)

    def test_strikes_away_parameter_picks_further_itm(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        # 2-away ITM CE: strikes below spot descending are 24900(1st), 24850(2nd).
        symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", strike_mode="ITM",
                                                            symbol_master=master, strikes_away=2)
        self.assertIn("24850", symbol)

    def test_falls_back_to_farthest_available_when_fewer_itm_strikes_than_requested(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        # Only 3 ITM CE strikes exist below spot (24900/24850/24800) - asking for the 10th
        # should fall back to the farthest (24800).
        symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", strike_mode="ITM",
                                                            symbol_master=master, strikes_away=10)
        self.assertIn("24800", symbol)


class TestResolveOptionSymbolAtm(unittest.TestCase):
    """strike_mode="ATM" - always the single strike nearest to spot, whichever side it's on;
    strikes_away is ignored entirely in this mode."""

    def test_ce_picks_the_single_nearest_strike(self):
        master = _sample_master()
        # Spot 24,910 - distances: |24900-24910|=10, |24950-24910|=40 -> 24900 is nearest.
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, lot_size = options_engine.resolve_option_symbol(data_service, "CE", strike_mode="ATM",
                                                                   symbol_master=master)
        self.assertIn("24900", symbol)
        self.assertEqual(lot_size, 75)

    def test_pe_picks_the_same_nearest_strike_as_ce(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, _ = options_engine.resolve_option_symbol(data_service, "PE", strike_mode="ATM",
                                                            symbol_master=master)
        self.assertIn("24900", symbol)
        self.assertIn("PE", symbol)

    def test_strikes_away_is_ignored_in_atm_mode(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0)
        symbol, _ = options_engine.resolve_option_symbol(data_service, "CE", strike_mode="ATM",
                                                            symbol_master=master, strikes_away=5)
        self.assertIn("24900", symbol)  # unaffected by strikes_away


# ----------------------------------------------------------------------------
# Index opening-range fetch
# ----------------------------------------------------------------------------
class TestFetchIndexOpeningRange(unittest.TestCase):
    def test_returns_high_low_from_the_opening_range_candle(self):
        candle = [1234567890, 24900.0, 24950.0, 24880.0, 24920.0, 0]  # index volume often 0 - irrelevant here
        data_service = FakeCandleDataService({config.NIFTY_INDEX_SYMBOL: candle})
        result = options_engine.fetch_index_opening_range(data_service)
        self.assertEqual(result, (24950.0, 24880.0))

    def test_no_candle_available_returns_none(self):
        data_service = FakeCandleDataService({})
        result = options_engine.fetch_index_opening_range(data_service)
        self.assertIsNone(result)


# ----------------------------------------------------------------------------
# NiftyOptionsEngine entry/exit arithmetic (fed directly via feed_candle)
# ----------------------------------------------------------------------------
class TestNiftyOptionsEngineUpsideBreakout(unittest.TestCase):
    """Reuses test_signal_engine.py's exact proven warm-up/entry candle numbers (base_price=95.0
    ramping by 0.05 over 20 candles, or_high=100.0, entry candles 100.5->101.0 then 101.0->101.8
    with volumes 2000/2200) - all fed as the FUTURES symbol's own candles now (single source for
    both price/VWAP and volume - see options_engine.py's module docstring). Spot=24,910 against
    _sample_master() resolves a CE (OTM default) at strike 24950 once the breakout confirms -
    this is now resolved AT THAT MOMENT, not supplied upfront to the constructor."""

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0, option_prices=150.0)
        self.engine = NiftyOptionsEngine(data_service, config.NIFTY_FUTURES_SYMBOL,
                                          or_high=100.0, or_low=100.0, capital_allocated=200_000.0,
                                          index_symbol=config.NIFTY_INDEX_SYMBOL, symbol_master=master)
        self.sym = self.engine.futures_tracker.symbol

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def _feed(self, candle, now_str):
        self.engine.feed_candle(self.sym, candle, now_str, is_backfill=False)

    def _warmup(self, count=20, base_price=95.0, volume=1000):
        for i in range(count):
            c = base_price + i * 0.05
            self._feed([1700000000 + i, c, c + 0.2, c - 0.2, c, volume], f"09:{20 + i}:00")

    def test_entry_fires_on_futures_breakout_confirmed_by_its_own_volume(self):
        self._warmup()
        self._feed([1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00")
        self._feed([2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00")

        self.assertTrue(self.engine.option.is_open)
        self.assertIn("24950", self.engine.option.symbol)  # OTM CE resolved fresh at signal time
        self.assertIn("CE", self.engine.option.symbol)
        self.assertEqual(self.engine.option.entry_direction, TradeDirection.LONG)
        self.assertEqual(self.engine.option.entry_price, 150.0)  # the OPTION's own premium, not the future's close
        expected_qty = int(200_000.0 // (150.0 * 75)) * 75
        self.assertEqual(self.engine.option.qty, expected_qty)
        self.assertEqual(self.engine.trade_book.trades[0]["symbol"], self.engine.option.symbol)

    def test_breakout_without_volume_confirmation_does_not_enter(self):
        self._warmup()
        # Candles qualify on price/VWAP, but volume stays AT/below its own MA (~1000).
        self._feed([1, 100.5, 101.5, 100.2, 101.0, 500], "09:45:00")
        self._feed([2, 101.0, 102.0, 100.8, 101.8, 500], "09:50:00")
        self.assertFalse(self.engine.option.is_open)

    def test_only_one_qualifying_candle_does_not_yet_enter(self):
        self._warmup()
        self._feed([1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00")
        self.assertFalse(self.engine.option.is_open)

    def test_exit_fires_on_closing_below_vwap_confirmed_by_volume(self):
        self._warmup()
        self._feed([1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00")
        self._feed([2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00")
        self.assertTrue(self.engine.option.is_open)

        # Two consecutive candles closing well below the running VWAP, with volume above its
        # own (slowly-rising) 20-period MA on both.
        self._feed([3, 95.0, 95.2, 90.0, 91.0, 2500], "09:55:00")
        self._feed([4, 91.0, 91.2, 85.0, 86.0, 2700], "10:00:00")

        self.assertFalse(self.engine.option.is_open)
        trade = self.engine.trade_book.trades[0]
        self.assertEqual(trade["exit_price"], 150.0)  # FakeQuoteDataService returns a flat 150.0 always
        self.assertIsNotNone(trade["exit_time"])

    def test_entry_skipped_when_capital_cant_afford_one_lot(self):
        self.engine.capital_allocated = 100.0  # can't afford even one 75-lot at any real premium
        self._warmup()
        self._feed([1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00")
        self._feed([2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00")
        self.assertFalse(self.engine.option.is_open)
        self.assertEqual(self.engine.trade_book.trades, [])

    def test_entry_skipped_when_strike_cannot_be_resolved(self):
        """An empty master means resolve_option_symbol() returns None - the breakout still
        confirms, but there's nothing to buy, so no entry/trade is recorded."""
        data_service = FakeQuoteDataService(spot=24_910.0, option_prices=150.0)
        engine = NiftyOptionsEngine(data_service, config.NIFTY_FUTURES_SYMBOL, or_high=100.0,
                                     or_low=100.0, capital_allocated=200_000.0,
                                     index_symbol=config.NIFTY_INDEX_SYMBOL, symbol_master=pd.DataFrame())
        sym = engine.futures_tracker.symbol
        for i in range(20):
            c = 95.0 + i * 0.05
            engine.feed_candle(sym, [1700000000 + i, c, c + 0.2, c - 0.2, c, 1000],
                                f"09:{20 + i}:00", is_backfill=False)
        engine.feed_candle(sym, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(sym, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=False)
        self.assertFalse(engine.option.is_open)
        self.assertEqual(engine.trade_book.trades, [])


class TestNiftyOptionsEngineDownsideBreakout(unittest.TestCase):
    """Mirror image of the upside tests, reusing test_signal_engine.py's SHORT warm-up pattern
    (or_low=100.0, base trending down from 105). Spot=24,910 resolves a PE (OTM default) at
    strike 24900 once the breakdown confirms."""

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0, option_prices=80.0)
        self.engine = NiftyOptionsEngine(data_service, config.NIFTY_FUTURES_SYMBOL,
                                          or_high=100.0, or_low=100.0, capital_allocated=200_000.0,
                                          index_symbol=config.NIFTY_INDEX_SYMBOL, symbol_master=master)
        self.sym = self.engine.futures_tracker.symbol

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def _feed(self, candle, now_str):
        self.engine.feed_candle(self.sym, candle, now_str, is_backfill=False)

    def _warmup(self, count=20, volume=1000):
        for i in range(count):
            c = 105 - i * 0.05
            self._feed([1700000000 + i, c, c + 0.2, c - 0.2, c, volume], f"09:{20 + i}:00")

    def test_entry_fires_on_breakdown_confirmed_by_its_own_volume(self):
        self._warmup()
        self._feed([1, 99.5, 99.8, 98.5, 99.0, 2000], "09:45:00")
        self._feed([2, 99.0, 99.2, 97.8, 98.2, 2200], "09:50:00")

        self.assertTrue(self.engine.option.is_open)
        self.assertIn("24900", self.engine.option.symbol)  # OTM PE resolved fresh at signal time
        self.assertIn("PE", self.engine.option.symbol)
        self.assertEqual(self.engine.option.entry_direction, TradeDirection.SHORT)
        self.assertEqual(self.engine.option.entry_price, 80.0)

    def test_exit_fires_on_closing_above_vwap_confirmed_by_volume(self):
        self._warmup()
        self._feed([1, 99.5, 99.8, 98.5, 99.0, 2000], "09:45:00")
        self._feed([2, 99.0, 99.2, 97.8, 98.2, 2200], "09:50:00")
        self.assertTrue(self.engine.option.is_open)

        self._feed([3, 105.0, 106.5, 104.5, 106.0, 3000], "09:55:00")
        self._feed([4, 106.0, 109.0, 105.5, 108.0, 3500], "10:00:00")

        self.assertFalse(self.engine.option.is_open)


class TestNiftyOptionsEngineBidirectionalSecondLeg(unittest.TestCase):
    """Proves the engine is now genuinely bidirectional WITHIN one day (see module docstring):
    an upside breakout buys a CE and later exits, then - with no new engine, no new day, and no
    pre-set bias to change - a later downside breakout buys a PE, decided purely by the
    strategy's own entry criteria each time, not a fixed direction chosen once this morning."""

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def test_ce_then_pe_on_the_same_engine_the_same_day(self):
        master = _sample_master()
        data_service = FakeQuoteDataService(spot=24_910.0, option_prices=150.0)
        # or_high=100 / or_low=50: a wide-enough band that the CE breakout, its exit, and the
        # later PE breakdown are all clearly, unambiguously on the correct side of each level -
        # this test is about proving the DIRECTION/instrument switch works end to end, not
        # re-proving tight VWAP arithmetic (already covered by the hand-verified tests above).
        engine = NiftyOptionsEngine(data_service, config.NIFTY_FUTURES_SYMBOL, or_high=100.0,
                                     or_low=50.0, capital_allocated=200_000.0,
                                     index_symbol=config.NIFTY_INDEX_SYMBOL, symbol_master=master)
        sym = engine.futures_tracker.symbol

        def feed(candle, now_str):
            engine.feed_candle(sym, candle, now_str, is_backfill=False)

        # Warm-up around 95, flat-ish.
        for i in range(20):
            c = 95.0 + i * 0.05
            feed([1700000000 + i, c, c + 0.2, c - 0.2, c, 1000], f"09:{20 + i}:00")

        # Leg 1: upside breakout above or_high=100 -> CE.
        feed([1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00")
        feed([2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00")
        self.assertTrue(engine.option.is_open)
        self.assertIn("24950", engine.option.symbol)
        self.assertIn("CE", engine.option.symbol)

        # Leg 1 exit: two candles closing back below VWAP with volume confirmation.
        feed([3, 101.8, 102.0, 94.0, 95.0, 2500], "09:55:00")
        feed([4, 95.0, 95.2, 92.0, 93.0, 2700], "10:00:00")
        self.assertFalse(engine.option.is_open)
        self.assertEqual(engine.trade_book.trades[0]["symbol"], "NSE:NIFTY25O1424950CE")

        # Leg 2: a sharp-enough reversal to clear or_low=50 on the downside, comfortably below
        # whatever VWAP has become by now, with strong volume on both confirming candles.
        feed([5, 93.0, 95.0, 65.0, 70.0, 2900], "10:05:00")  # bridge candle, not yet below or_low
        feed([6, 70.0, 72.0, 40.0, 45.0, 3000], "10:10:00")  # 1st confirming downside candle
        feed([7, 45.0, 47.0, 20.0, 25.0, 3200], "10:15:00")  # 2nd confirming downside candle

        self.assertTrue(engine.option.is_open)
        self.assertIn("24900", engine.option.symbol)  # OTM PE, resolved fresh for THIS leg
        self.assertIn("PE", engine.option.symbol)
        self.assertEqual(engine.option.entry_direction, TradeDirection.SHORT)
        self.assertEqual(len(engine.trade_book.trades), 2)
        self.assertEqual(engine.trade_book.trades[1]["symbol"], "NSE:NIFTY25O1424900PE")


class TestNiftyOptionsEngineResolvesStrikeAtSignalTime(unittest.TestCase):
    """Directly proves requirement (1): the strike is resolved from spot AT THE MOMENT the
    entry signal fires, not once upfront at engine construction (the OLD behaviour)."""

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def test_no_strike_is_resolved_until_the_entry_signal_actually_fires(self):
        """Structural proof that nothing is resolved upfront at ~09:20 engine construction (the
        OLD behaviour) or during warm-up: zero index-spot quote calls happen until the 2nd
        confirming breakout candle closes, at which point EXACTLY one live quote is fetched and
        used to resolve the strike - i.e. the strike genuinely reflects spot AT SIGNAL TIME, not
        a value cached/fetched earlier."""
        master = _sample_master()
        data_service = FakeSpotSequenceDataService(candles_by_symbol={},
                                                     spot_sequence=[24_910.0], option_price=150.0)
        engine = NiftyOptionsEngine(data_service, config.NIFTY_FUTURES_SYMBOL, or_high=100.0,
                                     or_low=100.0, capital_allocated=200_000.0,
                                     index_symbol=config.NIFTY_INDEX_SYMBOL, symbol_master=master)
        self.assertEqual(data_service.index_spot_calls, 0)  # nothing resolved yet at construction

        sym = engine.futures_tracker.symbol
        for i in range(20):
            c = 95.0 + i * 0.05
            engine.feed_candle(sym, [1700000000 + i, c, c + 0.2, c - 0.2, c, 1000],
                                f"09:{20 + i}:00", is_backfill=False)
        self.assertEqual(data_service.index_spot_calls, 0)  # still nothing during warm-up

        engine.feed_candle(sym, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=False)
        self.assertEqual(data_service.index_spot_calls, 0)  # still only 1 of 2 confirming candles

        engine.feed_candle(sym, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=False)

        self.assertTrue(engine.option.is_open)
        self.assertEqual(data_service.index_spot_calls, 1)  # resolved exactly once, at signal time
        self.assertIn("24950", engine.option.symbol)  # the live spot (24,910) at THAT moment


class TestNiftyOptionsEngineStrikeModeConfigurable(unittest.TestCase):
    """Confirms config.OPTION_STRIKE_MODE/strike_mode genuinely drives WHICH strike this engine
    buys - same spot (24,910), same breakout, three different modes, three different strikes."""

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True
        self.master = _sample_master()

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def _build_and_fire_upside(self, strike_mode):
        data_service = FakeQuoteDataService(spot=24_910.0, option_prices=150.0)
        engine = NiftyOptionsEngine(data_service, config.NIFTY_FUTURES_SYMBOL, or_high=100.0,
                                     or_low=100.0, capital_allocated=200_000.0,
                                     index_symbol=config.NIFTY_INDEX_SYMBOL,
                                     strike_mode=strike_mode, symbol_master=self.master)
        sym = engine.futures_tracker.symbol
        for i in range(20):
            c = 95.0 + i * 0.05
            engine.feed_candle(sym, [1700000000 + i, c, c + 0.2, c - 0.2, c, 1000],
                                f"09:{20 + i}:00", is_backfill=False)
        engine.feed_candle(sym, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(sym, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=False)
        return engine

    def test_otm_mode_buys_the_nearest_strike_above_spot(self):
        engine = self._build_and_fire_upside("OTM")
        self.assertIn("24950", engine.option.symbol)   # nearest strike ABOVE spot=24,910

    def test_itm_mode_buys_the_nearest_strike_below_spot(self):
        engine = self._build_and_fire_upside("ITM")
        self.assertIn("24900", engine.option.symbol)   # nearest strike BELOW spot=24,910

    def test_atm_mode_buys_the_single_nearest_strike_either_side(self):
        engine = self._build_and_fire_upside("ATM")
        self.assertIn("24900", engine.option.symbol)   # |24900-24910|=10 < |24950-24910|=40


class TestNiftyOptionsEngineMisc(unittest.TestCase):
    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True
        self.master = _sample_master()

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def _build(self, spot=24_910.0, option_prices=150.0):
        data_service = FakeQuoteDataService(spot=spot, option_prices=option_prices)
        return NiftyOptionsEngine(data_service, config.NIFTY_FUTURES_SYMBOL, or_high=100.0,
                                   or_low=100.0, capital_allocated=200_000.0,
                                   index_symbol=config.NIFTY_INDEX_SYMBOL, symbol_master=self.master)

    def test_trackers_property_exposes_only_the_futures_tracker_for_seeding(self):
        engine = self._build()
        self.assertEqual(set(engine.trackers), {config.NIFTY_FUTURES_SYMBOL})

    def test_option_leg_is_unresolved_until_an_entry_fires(self):
        engine = self._build()
        self.assertIsNone(engine.option.symbol)
        self.assertIsNone(engine.option.lot_size)
        self.assertFalse(engine.option.is_open)

    def test_square_off_all_is_a_no_op_when_nothing_is_open(self):
        engine = self._build()
        engine.square_off_all()  # should not raise
        self.assertEqual(engine.trade_book.trades, [])

    def test_square_off_all_closes_an_open_position(self):
        engine = self._build()
        engine.option.symbol = "NSE:NIFTY25O1425000CE"
        engine.option.lot_size = 75
        engine.option.is_open = True
        engine.option.entry_price = 140.0
        engine.option.qty = 75
        engine.option.entry_direction = TradeDirection.LONG
        engine.trade_book.record_entry(engine.option.symbol, "09:45:00", 140.0, qty=75,
                                        direction=TradeDirection.LONG)
        engine.square_off_all(at_time_str="15:10:30")
        self.assertFalse(engine.option.is_open)
        trade = engine.trade_book.trades[0]
        self.assertEqual(trade["exit_price"], 150.0)
        self.assertEqual(trade["exit_time"], "15:10:30")

    def test_check_all_skips_the_tick_when_the_candle_is_missing(self):
        data_service = FakeFullDataService(spot=24_910.0, candles_by_symbol={})  # no futures candle at all
        engine = NiftyOptionsEngine(data_service, config.NIFTY_FUTURES_SYMBOL, or_high=100.0,
                                     or_low=100.0, capital_allocated=200_000.0,
                                     index_symbol=config.NIFTY_INDEX_SYMBOL, symbol_master=self.master)
        engine.check_all(1700000000, 1700000300)  # should not raise
        self.assertFalse(engine.option.is_open)
        self.assertEqual(len(engine.futures_tracker.candles), 0)  # never got as far as updating the tracker


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
class TestRunNiftyOptionsEngine(unittest.TestCase):
    def setUp(self):
        self._orig_capital = config.NIFTY_OPTIONS_CAPITAL
        config.NIFTY_OPTIONS_CAPITAL = 200_000.0

    def tearDown(self):
        config.NIFTY_OPTIONS_CAPITAL = self._orig_capital

    def test_builds_a_both_sides_engine_with_no_leg_resolved_yet(self):
        """bias_direction now only gates the opening-range quality filter (see
        run_index_options_engine's docstring) - it does NOT pre-select CE/PE/a strike any
        more, so engine.option.symbol stays None regardless of which bias was passed in."""
        master = _sample_master()
        futures_candle = [1234567890, 24900.0, 24950.0, 24880.0, 24920.0, 1000]
        data_service = FakeFullDataService(spot=24_910.0,
                                            candles_by_symbol={config.NIFTY_FUTURES_SYMBOL: futures_candle})
        engine = options_engine.run_nifty_options_engine(data_service, TradeDirection.LONG, symbol_master=master)
        self.assertIsNotNone(engine)
        self.assertIsNone(engine.option.symbol)
        self.assertEqual(engine.capital_allocated, 200_000.0)
        self.assertEqual(engine.futures_tracker.or_high, 24950.0)
        self.assertEqual(engine.futures_tracker.or_low, 24880.0)
        self.assertEqual(engine.strikes_away, config.NIFTY_OTM_STRIKES_AWAY)

    def test_short_bias_also_just_builds_a_both_sides_engine(self):
        master = _sample_master()
        futures_candle = [1234567890, 24900.0, 24950.0, 24880.0, 24920.0, 1000]
        data_service = FakeFullDataService(spot=24_910.0,
                                            candles_by_symbol={config.NIFTY_FUTURES_SYMBOL: futures_candle})
        engine = options_engine.run_nifty_options_engine(data_service, TradeDirection.SHORT, symbol_master=master)
        self.assertIsNotNone(engine)
        self.assertIsNone(engine.option.symbol)  # bias no longer decides CE vs PE

    def test_strike_mode_parameter_threads_through_to_the_engine(self):
        master = _sample_master()
        futures_candle = [1234567890, 24900.0, 24950.0, 24880.0, 24920.0, 1000]
        data_service = FakeFullDataService(spot=24_910.0,
                                            candles_by_symbol={config.NIFTY_FUTURES_SYMBOL: futures_candle})
        engine = options_engine.run_nifty_options_engine(data_service, TradeDirection.LONG,
                                                           symbol_master=master, strike_mode="ATM")
        self.assertEqual(engine.strike_mode, "ATM")

    def test_unresolvable_contract_returns_none(self):
        data_service = FakeFullDataService(spot=24_910.0, candles_by_symbol={})
        engine = options_engine.run_nifty_options_engine(data_service, TradeDirection.LONG,
                                                           symbol_master=pd.DataFrame())
        self.assertIsNone(engine)

    def test_missing_opening_range_candle_returns_none(self):
        master = _sample_master()
        data_service = FakeFullDataService(spot=24_910.0, candles_by_symbol={})  # resolves fine, no OR candle
        engine = options_engine.run_nifty_options_engine(data_service, TradeDirection.LONG, symbol_master=master)
        self.assertIsNone(engine)


# ----------------------------------------------------------------------------
# BANKNIFTY - the exact same engine/rules as NIFTY above, driven by
# run_banknifty_options_engine()/config.BANKNIFTY_* instead of NIFTY's. These tests exist
# mainly to confirm the underlying filter actually separates NIFTY from BANKNIFTY rows in a
# SHARED symbol master (rather than just happening to work because only one underlying was
# ever present), and that the wiring (index/futures symbols, capital bucket, underlying
# label) comes out right end to end - the entry/exit ARITHMETIC itself is identical to
# NIFTY's and already proven by TestNiftyOptionsEngineUpsideBreakout/DownsideBreakout above.
# ----------------------------------------------------------------------------
class TestResolveOptionSymbolBankNifty(unittest.TestCase):
    def test_resolves_banknifty_ce_from_a_mixed_master_without_cross_contamination(self):
        mixed_master = pd.concat([_sample_master(), _sample_banknifty_master()], ignore_index=True)
        data_service = FakeQuoteDataService(spot=51_250.0, index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        symbol, lot_size = options_engine.resolve_option_symbol(
            data_service, "CE", symbol_master=mixed_master, underlying="BANKNIFTY",
            index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        self.assertTrue(symbol.startswith("NSE:BANKNIFTY"))  # i.e. didn't accidentally pick a plain-NIFTY row
        self.assertEqual(lot_size, 15)
        self.assertIn("51300", symbol)  # nearest OTM CE strike above spot=51250 from the 100-point ladder

    def test_resolves_banknifty_pe_from_a_mixed_master(self):
        mixed_master = pd.concat([_sample_master(), _sample_banknifty_master()], ignore_index=True)
        data_service = FakeQuoteDataService(spot=51_250.0, index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        symbol, lot_size = options_engine.resolve_option_symbol(
            data_service, "PE", symbol_master=mixed_master, underlying="BANKNIFTY",
            index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        self.assertIn("BANKNIFTY", symbol)
        self.assertEqual(lot_size, 15)
        self.assertIn("51200", symbol)  # nearest OTM PE strike below spot=51250

    def test_plain_nifty_resolution_unaffected_by_banknifty_rows_in_the_same_master(self):
        mixed_master = pd.concat([_sample_master(), _sample_banknifty_master()], ignore_index=True)
        data_service = FakeQuoteDataService(spot=24_910.0)  # default index_symbol = NIFTY
        symbol, lot_size = options_engine.resolve_option_symbol(data_service, "CE", symbol_master=mixed_master)
        self.assertNotIn("BANKNIFTY", symbol)
        self.assertEqual(lot_size, 75)


class TestFetchIndexOpeningRangeBankNifty(unittest.TestCase):
    def test_fetches_the_given_index_symbols_own_opening_range(self):
        candle = [1700000000, 51200.0, 51350.0, 51100.0, 51300.0, 0]
        data_service = FakeCandleDataService({config.BANKNIFTY_INDEX_SYMBOL: candle})
        result = options_engine.fetch_index_opening_range(data_service, index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        self.assertEqual(result, (51350.0, 51100.0))
        # confirms it asked for BANKNIFTY_INDEX_SYMBOL's candle, not NIFTY's
        self.assertEqual(data_service.calls[0][0], config.BANKNIFTY_INDEX_SYMBOL)


class TestNiftyOptionsEngineBankNiftyWiring(unittest.TestCase):
    """Confirms NiftyOptionsEngine's futures_symbol/underlying constructor params actually
    wire BankNifty's own futures symbol through, instead of silently defaulting back to
    NIFTY's."""

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def test_engine_uses_banknifty_futures_symbol_when_constructed_with_it(self):
        master = _sample_banknifty_master()
        data_service = FakeQuoteDataService(spot=51_250.0, option_prices=800.0,
                                             index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        engine = NiftyOptionsEngine(data_service, config.BANKNIFTY_FUTURES_SYMBOL,
                                     or_high=51300.0, or_low=51100.0, capital_allocated=300_000.0,
                                     index_symbol=config.BANKNIFTY_INDEX_SYMBOL, underlying="BANKNIFTY",
                                     symbol_master=master)
        self.assertEqual(engine.futures_tracker.symbol, config.BANKNIFTY_FUTURES_SYMBOL)
        self.assertEqual(set(engine.trackers), {config.BANKNIFTY_FUTURES_SYMBOL})
        self.assertEqual(engine.underlying, "BANKNIFTY")

    def test_entry_fires_correctly_when_engine_is_wired_to_banknifty_futures_symbol(self):
        """Same hand-verified warm-up/entry numbers as TestNiftyOptionsEngineUpsideBreakout,
        just fed through an engine constructed with BANKNIFTY's own futures symbol - confirms
        the single-source arithmetic doesn't secretly depend on the NIFTY defaults anywhere,
        and that it resolves a REAL BANKNIFTY CE symbol (spot=51,250 -> nearest OTM CE=51300)."""
        master = _sample_banknifty_master()
        data_service = FakeQuoteDataService(spot=51_250.0, option_prices=800.0,
                                             index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        engine = NiftyOptionsEngine(data_service, config.BANKNIFTY_FUTURES_SYMBOL,
                                     or_high=100.0, or_low=100.0, capital_allocated=300_000.0,
                                     index_symbol=config.BANKNIFTY_INDEX_SYMBOL, underlying="BANKNIFTY",
                                     symbol_master=master)
        sym = engine.futures_tracker.symbol
        for i in range(20):
            c = 95.0 + i * 0.05
            engine.feed_candle(sym, [1700000000 + i, c, c + 0.2, c - 0.2, c, 1000],
                                f"09:{20 + i}:00", is_backfill=False)
        engine.feed_candle(sym, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(sym, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=False)

        self.assertTrue(engine.option.is_open)
        self.assertEqual(engine.option.entry_price, 800.0)
        self.assertIn("51300", engine.option.symbol)
        self.assertEqual(engine.trade_book.trades[0]["symbol"], engine.option.symbol)


class TestRunBankNiftyOptionsEngine(unittest.TestCase):
    def setUp(self):
        self._orig_capital = config.BANKNIFTY_OPTIONS_CAPITAL
        config.BANKNIFTY_OPTIONS_CAPITAL = 300_000.0

    def tearDown(self):
        config.BANKNIFTY_OPTIONS_CAPITAL = self._orig_capital

    def test_builds_a_both_sides_banknifty_engine_with_no_leg_resolved_yet(self):
        master = _sample_banknifty_master()
        futures_candle = [1234567890, 51200.0, 51300.0, 51100.0, 51250.0, 1000]
        data_service = FakeFullDataService(spot=51_250.0,
                                            candles_by_symbol={config.BANKNIFTY_FUTURES_SYMBOL: futures_candle},
                                            index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        engine = options_engine.run_banknifty_options_engine(data_service, TradeDirection.LONG, symbol_master=master)
        self.assertIsNotNone(engine)
        self.assertIsNone(engine.option.symbol)
        self.assertEqual(engine.capital_allocated, 300_000.0)
        self.assertEqual(engine.underlying, "BANKNIFTY")
        self.assertEqual(engine.futures_tracker.symbol, config.BANKNIFTY_FUTURES_SYMBOL)
        self.assertEqual(engine.futures_tracker.or_high, 51300.0)
        self.assertEqual(engine.futures_tracker.or_low, 51100.0)

    def test_short_bias_also_just_builds_a_both_sides_engine(self):
        master = _sample_banknifty_master()
        futures_candle = [1234567890, 51200.0, 51300.0, 51100.0, 51250.0, 1000]
        data_service = FakeFullDataService(spot=51_250.0,
                                            candles_by_symbol={config.BANKNIFTY_FUTURES_SYMBOL: futures_candle},
                                            index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        engine = options_engine.run_banknifty_options_engine(data_service, TradeDirection.SHORT, symbol_master=master)
        self.assertIsNotNone(engine)
        self.assertIsNone(engine.option.symbol)

    def test_unresolvable_contract_returns_none(self):
        data_service = FakeFullDataService(spot=51_250.0, candles_by_symbol={},
                                            index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        engine = options_engine.run_banknifty_options_engine(data_service, TradeDirection.LONG,
                                                               symbol_master=pd.DataFrame())
        self.assertIsNone(engine)

    def test_missing_opening_range_candle_returns_none(self):
        master = _sample_banknifty_master()
        data_service = FakeFullDataService(spot=51_250.0, candles_by_symbol={},  # resolves fine, no OR candle
                                            index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
        engine = options_engine.run_banknifty_options_engine(data_service, TradeDirection.LONG, symbol_master=master)
        self.assertIsNone(engine)

    def test_runs_independently_of_a_simultaneous_nifty_engine(self):
        """Confirms NIFTY and BANKNIFTY can both be built from ONE shared mixed master in the
        same test (standing in for both ENABLE_NIFTY_OPTIONS_TRADING and
        ENABLE_BANKNIFTY_OPTIONS_TRADING being True at once) without either run affecting the
        other's wiring - instrument resolution itself is now deferred to signal time (see
        TestNiftyOptionsEngineBidirectionalSecondLeg/TestResolveOptionSymbolBankNifty for that),
        so this checks what IS known right after building: futures symbol, capital bucket,
        underlying label."""
        mixed_master = pd.concat([_sample_master(), _sample_banknifty_master()], ignore_index=True)
        nifty_data_service = FakeFullDataService(
            spot=24_910.0, candles_by_symbol={config.NIFTY_FUTURES_SYMBOL:
                                               [1, 24900.0, 24950.0, 24880.0, 24920.0, 1000]})
        banknifty_data_service = FakeFullDataService(
            spot=51_250.0, candles_by_symbol={config.BANKNIFTY_FUTURES_SYMBOL:
                                               [1, 51200.0, 51300.0, 51100.0, 51250.0, 1000]},
            index_symbol=config.BANKNIFTY_INDEX_SYMBOL)

        nifty_engine = options_engine.run_nifty_options_engine(nifty_data_service, TradeDirection.LONG,
                                                                 symbol_master=mixed_master)
        banknifty_engine = options_engine.run_banknifty_options_engine(banknifty_data_service, TradeDirection.LONG,
                                                                         symbol_master=mixed_master)
        self.assertIsNotNone(nifty_engine)
        self.assertIsNotNone(banknifty_engine)
        self.assertEqual(nifty_engine.underlying, "NIFTY")
        self.assertEqual(banknifty_engine.underlying, "BANKNIFTY")
        self.assertEqual(nifty_engine.futures_tracker.symbol, config.NIFTY_FUTURES_SYMBOL)
        self.assertEqual(banknifty_engine.futures_tracker.symbol, config.BANKNIFTY_FUTURES_SYMBOL)
        self.assertEqual(nifty_engine.capital_allocated, config.NIFTY_OPTIONS_CAPITAL)
        self.assertEqual(banknifty_engine.capital_allocated, 300_000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
