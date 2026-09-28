"""
test_bootstrap.py
--------------------
Tests for bootstrap.py, in two tiers:

  1. Focused, isolated tests for the two pieces of real new logic:
     _replay_intraday_history() (does it correctly reconstruct VWAP/volume-MA
     state and detect a signal that would have fired earlier today, without
     feeding a still-forming candle?) and _reconcile_broker_state() (does
     broker truth correctly override replay in both directions?).

  2. A full end-to-end integration test of bootstrap_and_catchup() itself,
     with every external call faked (quotes, candle history, positions),
     confirming the whole pipeline - sector/breadth snapshot, gap-up screen,
     replay, broker reconciliation - produces a correctly-populated
     SignalEngine from a single call, for a realistic mid-session start.

Market-phase detection itself (detect_market_phase / _is_market_open_now /
_within_signal_window) is tested in test_updates.py, not duplicated here.

Run with:
    python test_bootstrap.py
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pytz

import config
import bootstrap
from signal_engine import SignalEngine, StockTracker, TradeBook

TZ = pytz.timezone(config.TIMEZONE)


def _epoch(hour: int, minute: int, day=(2026, 9, 21)) -> int:
    return int(TZ.localize(datetime(day[0], day[1], day[2], hour, minute, 0)).timestamp())


class FakeReplayDataService:
    """Only implements get_candles_bulk - enough for _replay_intraday_history in isolation."""

    def __init__(self, candles_by_symbol: dict[str, list]):
        self._candles_by_symbol = candles_by_symbol
        self.calls: list[tuple] = []

    def get_candles_bulk(self, symbols, resolution, range_from_date, range_to_date):
        self.calls.append((tuple(symbols), resolution, range_from_date, range_to_date))
        return {s: self._candles_by_symbol.get(s, []) for s in symbols}


class FakePositionsDataService:
    def __init__(self, positions=None, raise_on_positions=False):
        self._positions = positions or []
        self._raise = raise_on_positions

    def get_positions(self):
        if self._raise:
            raise RuntimeError("simulated broker API failure")
        return self._positions


def _bare_engine(symbol="NSE:TEST-EQ", or_high=100.0) -> SignalEngine:
    engine = SignalEngine.__new__(SignalEngine)
    engine.data_service = None
    engine.trade_book = TradeBook()
    engine.trackers = {symbol: StockTracker(symbol=symbol, or_high=or_high)}
    return engine


class TestReplayIntradayHistory(unittest.TestCase):
    def test_replays_only_fully_closed_candles_up_to_now(self):
        symbol = "NSE:TEST-EQ"
        engine = _bare_engine(symbol, or_high=100.0)
        # Candles at 09:20, 09:25, 09:30 (each fully closed by 09:35 "now"), plus one at
        # 09:35 whose 5-min window (09:35-09:40) has NOT closed yet as of "now"=09:35:00 -
        # that one must NOT be replayed.
        candles = [
            [_epoch(9, 20), 99, 99.5, 98.5, 99.0, 1000],
            [_epoch(9, 25), 99, 100.2, 98.8, 100.0, 1000],
            [_epoch(9, 30), 100, 101.5, 99.5, 101.0, 1500],
            [_epoch(9, 35), 101, 101.2, 100.8, 101.1, 1000],  # still forming as of "now" below
        ]
        provider = FakeReplayDataService({symbol: candles})
        now = TZ.localize(datetime(2026, 9, 21, 9, 35, 0))  # 09:35-09:40 candle not closed yet

        replayed = bootstrap._replay_intraday_history(engine, provider, [symbol], now)

        self.assertEqual(replayed, 3)  # NOT the still-forming 4th candle
        self.assertEqual(len(engine.trackers[symbol].candles), 3)

    def test_replay_reconstructs_a_buy_signal_as_backfill_without_placing_a_live_order(self):
        symbol = "NSE:TEST-EQ"
        engine = _bare_engine(symbol, or_high=100.0)

        # 20 neutral warm-up candles (fills the volume MA window), spanning 09:20 through
        # 10:55 at 5-min spacing, then 2 qualifying candles placed safely AFTER that range
        # (11:00, 11:05) - trigger a BUY, replayed as if this morning's history.
        candles = []
        for i in range(20):
            t_minute = 20 + i * 5
            hour, minute = 9 + t_minute // 60, t_minute % 60
            c = 95 + i * 0.05
            candles.append([_epoch(hour, minute), c, c + 0.2, c - 0.2, c, 1000])
        candles.append([_epoch(11, 0), 100.5, 101.5, 100.2, 101.0, 2000])
        candles.append([_epoch(11, 5), 101.0, 102.0, 100.8, 101.8, 2200])

        provider = FakeReplayDataService({symbol: candles})
        now = TZ.localize(datetime(2026, 9, 21, 11, 10, 0))  # well after the last candle closed

        orig_dry_run = config.DRY_RUN
        config.DRY_RUN = False  # to prove is_backfill (not DRY_RUN) is what blocks the live order here
        try:
            bootstrap._replay_intraday_history(engine, provider, [symbol], now)
        finally:
            config.DRY_RUN = orig_dry_run

        tracker = engine.trackers[symbol]
        self.assertTrue(tracker.position_open)
        trade = engine.trade_book.trades[0]
        self.assertTrue(trade["is_backfill"])
        self.assertEqual(trade["entry_price"], 101.8)

    def test_no_symbols_makes_zero_calls(self):
        provider = FakeReplayDataService({})
        engine = _bare_engine()
        replayed = bootstrap._replay_intraday_history(engine, provider, [], datetime.now(TZ))
        self.assertEqual(replayed, 0)
        self.assertEqual(provider.calls, [])

    def test_symbol_with_no_candles_is_skipped_without_crashing(self):
        engine = _bare_engine("NSE:TEST-EQ")
        provider = FakeReplayDataService({})  # no data for anything
        now = TZ.localize(datetime(2026, 9, 21, 10, 0, 0))
        replayed = bootstrap._replay_intraday_history(engine, provider, ["NSE:TEST-EQ"], now)
        self.assertEqual(replayed, 0)


class TestReconcileBrokerState(unittest.TestCase):
    def test_adopts_a_broker_position_replay_missed(self):
        engine = _bare_engine("NSE:TEST-EQ")
        # replay found nothing (position_open stays False), but the broker reports one open
        provider = FakePositionsDataService(positions=[
            {"symbol": "NSE:TEST-EQ", "netQty": 10, "netAvg": 123.45},
        ])
        bootstrap._reconcile_broker_state(engine, provider)
        tracker = engine.trackers["NSE:TEST-EQ"]
        self.assertTrue(tracker.position_open)
        self.assertEqual(tracker.entry_price_for_risk, 123.45)

    def test_clears_a_replay_reconstructed_position_the_broker_does_not_confirm(self):
        engine = _bare_engine("NSE:TEST-EQ")
        tracker = engine.trackers["NSE:TEST-EQ"]
        tracker.position_open = True
        tracker.entry_price_for_risk = 100.0
        tracker.highest_price_since_entry = 105.0
        tracker.weakness_events_on_position = 1

        provider = FakePositionsDataService(positions=[])  # broker reports nothing open
        bootstrap._reconcile_broker_state(engine, provider)

        self.assertFalse(tracker.position_open)
        self.assertIsNone(tracker.entry_price_for_risk)
        self.assertIsNone(tracker.highest_price_since_entry)
        self.assertEqual(tracker.weakness_events_on_position, 0)

    def test_agreement_between_replay_and_broker_leaves_state_untouched(self):
        engine = _bare_engine("NSE:TEST-EQ")
        tracker = engine.trackers["NSE:TEST-EQ"]
        tracker.position_open = True
        tracker.entry_price_for_risk = 100.0
        tracker.highest_price_since_entry = 100.0

        provider = FakePositionsDataService(positions=[
            {"symbol": "NSE:TEST-EQ", "netQty": 10, "netAvg": 99.5},  # slightly different avg - still "confirmed"
        ])
        bootstrap._reconcile_broker_state(engine, provider)

        # Confirmed, not overwritten - replay's own entry price is left as-is (see the
        # module docstring: this only ADOPTS when replay found nothing, it doesn't second-
        # guess a replay-reconstructed entry price it already agrees is open).
        self.assertTrue(tracker.position_open)
        self.assertEqual(tracker.entry_price_for_risk, 100.0)

    def test_broker_api_failure_leaves_replay_state_untouched_and_does_not_raise(self):
        engine = _bare_engine("NSE:TEST-EQ")
        tracker = engine.trackers["NSE:TEST-EQ"]
        tracker.position_open = True
        tracker.entry_price_for_risk = 100.0

        provider = FakePositionsDataService(raise_on_positions=True)
        bootstrap._reconcile_broker_state(engine, provider)  # should not raise

        self.assertTrue(tracker.position_open)  # untouched - replay's own view is kept as a fallback
        self.assertEqual(tracker.entry_price_for_risk, 100.0)

    def test_position_in_a_symbol_outside_todays_shortlist_is_logged_not_adopted(self):
        engine = _bare_engine("NSE:TEST-EQ")  # only tracking NSE:TEST-EQ today
        provider = FakePositionsDataService(positions=[
            {"symbol": "NSE:OTHER-EQ", "netQty": 5, "netAvg": 50.0},
        ])
        bootstrap._reconcile_broker_state(engine, provider)  # should not raise or affect NSE:TEST-EQ
        self.assertFalse(engine.trackers["NSE:TEST-EQ"].position_open)

    def test_flat_position_lines_are_ignored(self):
        engine = _bare_engine("NSE:TEST-EQ")
        provider = FakePositionsDataService(positions=[
            {"symbol": "NSE:TEST-EQ", "netQty": 0, "netAvg": 0},  # a flat/closed line, not an open position
        ])
        bootstrap._reconcile_broker_state(engine, provider)
        self.assertFalse(engine.trackers["NSE:TEST-EQ"].position_open)


class TestBootstrapAndCatchupRouting(unittest.TestCase):
    """The three early-return phases never touch data_service at all - passing a bare
    sentinel object with no methods proves this: any attempt to call something on it
    raises AttributeError immediately, failing the test loudly."""

    def _freeze_now(self, monkeypatch_dt):
        pass  # see individual tests - bootstrap.py reads datetime.now(TZ) internally

    def test_pre_open_returns_immediately_without_touching_data_service(self):
        import bootstrap as bootstrap_module

        class Sentinel:
            def __getattr__(self, name):
                raise AssertionError(f"PRE_OPEN path should never call {name}() on data_service")

        orig_now = bootstrap_module.datetime

        class FrozenDatetime(orig_now):
            @classmethod
            def now(cls, tz=None):
                return TZ.localize(datetime(2026, 9, 21, 8, 0, 0))

        bootstrap_module.datetime = FrozenDatetime
        try:
            result = bootstrap_module.bootstrap_and_catchup(Sentinel())
            self.assertEqual(result.phase, bootstrap_module.MarketPhase.PRE_OPEN)
            self.assertIsNone(result.signal_engine)
        finally:
            bootstrap_module.datetime = orig_now

    def test_post_close_returns_immediately_without_touching_data_service(self):
        import bootstrap as bootstrap_module

        class Sentinel:
            def __getattr__(self, name):
                raise AssertionError(f"POST_CLOSE path should never call {name}() on data_service")

        orig_now = bootstrap_module.datetime

        class FrozenDatetime(orig_now):
            @classmethod
            def now(cls, tz=None):
                return TZ.localize(datetime(2026, 9, 21, 16, 0, 0))

        bootstrap_module.datetime = FrozenDatetime
        try:
            result = bootstrap_module.bootstrap_and_catchup(Sentinel())
            self.assertEqual(result.phase, bootstrap_module.MarketPhase.POST_CLOSE)
            self.assertIsNone(result.signal_engine)
        finally:
            bootstrap_module.datetime = orig_now

    def test_post_open_past_signal_cutoff_returns_immediately(self):
        import bootstrap as bootstrap_module

        class Sentinel:
            def __getattr__(self, name):
                raise AssertionError(f"past-cutoff path should never call {name}() on data_service")

        orig_now = bootstrap_module.datetime

        class FrozenDatetime(orig_now):
            @classmethod
            def now(cls, tz=None):
                return TZ.localize(datetime(2026, 9, 21, 15, 20, 0))  # open, but past 15:10 cutoff

        bootstrap_module.datetime = FrozenDatetime
        try:
            result = bootstrap_module.bootstrap_and_catchup(Sentinel())
            self.assertEqual(result.phase, bootstrap_module.MarketPhase.POST_OPEN)
            self.assertIsNone(result.signal_engine)
        finally:
            bootstrap_module.datetime = orig_now


class FakeFullPipelineDataService:
    """
    Implements everything bootstrap_and_catchup()'s full mid-session path needs:
    get_quotes_batched (sector snapshot + market breadth), get_candles_range +
    get_previous_close (the gap-up screen's accurate path), get_candles_bulk (replay),
    get_positions (reconciliation).
    """

    def __init__(self):
        self.gap_up_symbol = "NSE:WINNER-EQ"

    def get_quotes_batched(self, symbols):
        # Used for sector index quotes (data/sector_index_map.csv rows) and for
        # compute_market_breadth (the stock universe) - same shape either way.
        out = {}
        for s in symbols:
            out[s] = {"lp": 101.0, "prev_close_price": 100.0,
                      "open_price": 100.0, "high_price": 101.5, "low_price": 99.5, "volume": 1000}
        return out

    def get_candles_range(self, symbol, resolution, range_from_date, range_to_date):
        # The 09:15 opening-range candle for scan_opening_range_accurate: WINNER gaps up
        # 1.5% (within the 0.5%-2.5% band), everything else is flat (filtered out).
        if symbol == self.gap_up_symbol:
            return [[_epoch(9, 15), 100.0, 101.6, 99.8, 101.5, 5000]]
        return [[_epoch(9, 15), 100.0, 100.2, 99.8, 100.0, 500]]

    def get_previous_close(self, symbol):
        return 100.0

    def get_candles_bulk(self, symbols, resolution, range_from_date, range_to_date):
        # No further movement after the opening range in this fixture - replay just
        # confirms the position stays flat (no BUY yet), which is enough to prove the
        # pipeline actually wires replay in, without needing a full breakout sequence here
        # (that path is covered directly in TestReplayIntradayHistory above). Epoch must
        # fall within [9:15, 9:20) to also serve as the opening-range candle itself for
        # scan_opening_range_accurate's window check (from_epoch <= c[0] < to_epoch).
        out = {}
        for s in symbols:
            out[s] = [[_epoch(9, 15), 100.0, 101.6 if s == self.gap_up_symbol else 100.2,
                       99.8, 101.5 if s == self.gap_up_symbol else 100.0, 1000]]
        return out

    def get_positions(self):
        return []


class TestBootstrapAndCatchupFullPipeline(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        sector_csv = self.tmpdir / "sector_index_map.csv"
        universe_csv = self.tmpdir / "ind_nifty500list.csv"
        sector_csv.write_text(
            "symbol,index_name,category,is_sector_index,sector\n"
            "NSE:NIFTYIT-INDEX,Nifty IT,Information Technology,True,IT\n"
        )
        universe_csv.write_text(
            "Symbol,Company Name,Industry\n"
            "WINNER,Winner Ltd,IT\n"
            "FLAT,Flat Ltd,IT\n"
        )
        self._orig_sector_csv = config.SECTOR_INDEX_MAP_CSV
        self._orig_universe_csv = config.STOCK_UNIVERSE_CSV
        config.SECTOR_INDEX_MAP_CSV = str(sector_csv)
        config.STOCK_UNIVERSE_CSV = str(universe_csv)

    def tearDown(self):
        config.SECTOR_INDEX_MAP_CSV = self._orig_sector_csv
        config.STOCK_UNIVERSE_CSV = self._orig_universe_csv
        shutil.rmtree(self.tmpdir)

    def test_full_mid_session_catchup_produces_a_populated_engine(self):
        import bootstrap as bootstrap_module
        import opening_range_scanner as ors_module

        FROZEN_NOW = TZ.localize(datetime(2026, 9, 21, 10, 0, 0))  # mid-session start

        # Both modules independently `from datetime import datetime` and call
        # datetime.now() internally - patching only one leaves the other using the
        # sandbox's REAL current date, which won't match this fixture's 2026-09-21
        # epochs at all. Both must be frozen to the SAME instant for this to work.
        orig_bootstrap_dt = bootstrap_module.datetime
        orig_ors_dt = ors_module.datetime

        class FrozenDatetime(orig_bootstrap_dt):
            @classmethod
            def now(cls, tz=None):
                return FROZEN_NOW

        bootstrap_module.datetime = FrozenDatetime
        ors_module.datetime = FrozenDatetime
        try:
            result = bootstrap_module.bootstrap_and_catchup(FakeFullPipelineDataService())
        finally:
            bootstrap_module.datetime = orig_bootstrap_dt
            ors_module.datetime = orig_ors_dt

        self.assertEqual(result.phase, bootstrap_module.MarketPhase.POST_OPEN)
        self.assertIsNotNone(result.signal_engine)
        # Only the gap-up symbol should have made it through the 0.5%-2.5% screen.
        self.assertEqual(set(result.signal_engine.trackers.keys()), {"NSE:WINNER-EQ"})
        self.assertIn("advance_decline_ratio", result.market_breadth)


if __name__ == "__main__":
    unittest.main(verbosity=2)
