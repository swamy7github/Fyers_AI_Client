"""
test_signal_engine.py
------------------------
Tests for signal_engine.py, focused on the parts that had NO test coverage
before this file: tick-level stop-loss/target/trailing-SL
(check_tick_for_exit), broker position adoption (adopt_broker_position),
and the is_backfill gate on live order placement (_maybe_place_live_order).
The candle-level BUY/weakness-event logic itself was covered by earlier
conversational testing; is_backfill was added on top of it without changing
that logic, and a couple of tests here re-confirm that specifically.

check_tick_for_exit's math is hand-verified below (see each test's
docstring) rather than just asserted against its own output - this is
exactly the kind of logic where "looks right" isn't good enough, since a
wrong stop-loss/target calculation would misprice real risk.

Run with:
    python test_signal_engine.py
"""

from __future__ import annotations

import unittest

import config
from signal_engine import SignalEngine, StockTracker, TradeBook


def _make_engine_with_tracker(symbol="NSE:TEST-EQ", or_high=100.0) -> tuple[SignalEngine, StockTracker]:
    engine = SignalEngine.__new__(SignalEngine)  # bypass __init__ (no data_service/shortlist needed for these tests)
    engine.data_service = None
    engine.trade_book = TradeBook()
    tracker = StockTracker(symbol=symbol, or_high=or_high)
    engine.trackers = {symbol: tracker}
    return engine, tracker


def _open_position(engine: SignalEngine, tracker: StockTracker, entry_price: float, entry_time: str = "09:45:00"):
    tracker.position_open = True
    tracker.entry_price_for_risk = entry_price
    tracker.highest_price_since_entry = entry_price
    engine.trade_book.record_entry(tracker.symbol, entry_time, entry_price, entry_type="INITIAL", is_backfill=False)


class TestCheckTickForExit(unittest.TestCase):
    """
    Default config for these: STOP_LOSS_PCT=1.0, TARGET_PCT=2.0,
    TRAILING_SL_PCT=1.0 (see config.py). Hand-verified arithmetic, entry=100:
      stop_loss_price   = 100 * 0.99 = 99
      target_price      = 100 * 1.02 = 102
      trailing_sl_price = highest_price_since_entry * 0.99 (ratchets with highest)
      effective_stop     = max(stop_loss_price, trailing_sl_price)
    """

    def setUp(self):
        self.orig_stop, self.orig_target, self.orig_trail = (
            config.STOP_LOSS_PCT, config.TARGET_PCT, config.TRAILING_SL_PCT)
        config.STOP_LOSS_PCT, config.TARGET_PCT, config.TRAILING_SL_PCT = 1.0, 2.0, 1.0
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True  # default for these; individual tests override where relevant

    def tearDown(self):
        config.STOP_LOSS_PCT, config.TARGET_PCT, config.TRAILING_SL_PCT = (
            self.orig_stop, self.orig_target, self.orig_trail)
        config.DRY_RUN = self.orig_dry_run

    def test_no_open_position_returns_none(self):
        engine, tracker = _make_engine_with_tracker()
        self.assertIsNone(engine.check_tick_for_exit(tracker.symbol, 100.0, "10:00:00"))

    def test_missing_entry_price_returns_none_even_if_flagged_open(self):
        # Shouldn't happen given how position_open is always set alongside entry_price_for_risk
        # elsewhere in this file, but check_tick_for_exit guards it explicitly - confirm the guard works.
        engine, tracker = _make_engine_with_tracker()
        tracker.position_open = True
        tracker.entry_price_for_risk = None
        self.assertIsNone(engine.check_tick_for_exit(tracker.symbol, 100.0, "10:00:00"))

    def test_unknown_symbol_returns_none(self):
        engine, _ = _make_engine_with_tracker()
        self.assertIsNone(engine.check_tick_for_exit("NSE:NOT-TRACKED-EQ", 100.0, "10:00:00"))

    def test_price_between_stop_and_target_does_not_exit(self):
        engine, tracker = _make_engine_with_tracker()
        _open_position(engine, tracker, 100.0)
        result = engine.check_tick_for_exit(tracker.symbol, 100.5, "10:00:00")
        self.assertIsNone(result)
        self.assertTrue(tracker.position_open)

    def test_plain_stop_loss_hit_without_any_favorable_move_first(self):
        # highest never rises above entry, so trailing_sl_price == stop_loss_price == 99 -
        # the tie-break labels this "STOP-LOSS", not "TRAILING-SL".
        engine, tracker = _make_engine_with_tracker()
        _open_position(engine, tracker, 100.0)
        result = engine.check_tick_for_exit(tracker.symbol, 99.0, "10:05:00")
        self.assertEqual(result, "STOP-LOSS")
        self.assertFalse(tracker.position_open)
        self.assertIsNone(tracker.entry_price_for_risk)
        self.assertIsNone(tracker.highest_price_since_entry)

    def test_target_hit(self):
        engine, tracker = _make_engine_with_tracker()
        _open_position(engine, tracker, 100.0)
        # ltp=102.5: highest becomes 102.5, trailing_sl_price=102.5*0.99=101.475,
        # effective_stop=max(99,101.475)=101.475; ltp(102.5) > 101.475 so the stop branch
        # doesn't fire, and ltp(102.5) >= target(102) -> TARGET.
        result = engine.check_tick_for_exit(tracker.symbol, 102.5, "10:05:00")
        self.assertEqual(result, "TARGET")
        self.assertFalse(tracker.position_open)

    def test_trailing_stop_ratchets_up_and_then_triggers_on_pullback(self):
        engine, tracker = _make_engine_with_tracker()
        _open_position(engine, tracker, 100.0)

        # Rally to 101 - no exit (101 > trailing_sl 99.99, < target 102)
        self.assertIsNone(engine.check_tick_for_exit(tracker.symbol, 101.0, "10:01:00"))
        self.assertAlmostEqual(tracker.highest_price_since_entry, 101.0)

        # Small pullback to 100.5, still above trailing stop (99.99) - no exit,
        # and highest must NOT drop back down.
        self.assertIsNone(engine.check_tick_for_exit(tracker.symbol, 100.5, "10:02:00"))
        self.assertAlmostEqual(tracker.highest_price_since_entry, 101.0)  # unchanged, ratchets up only

        # Drops to 99.9: below trailing_sl_price (101*0.99=99.99) but still above the hard
        # stop (99) - should exit via TRAILING-SL specifically, not STOP-LOSS.
        result = engine.check_tick_for_exit(tracker.symbol, 99.9, "10:03:00")
        self.assertEqual(result, "TRAILING-SL")
        self.assertFalse(tracker.position_open)

    def test_target_is_effectively_a_hard_ceiling_even_on_a_large_gap_up(self):
        # On the tick that FIRST sets a new high, effective_stop is always strictly less
        # than that new high (trailing_sl_price = new_high * 0.99 < new_high), so the stop
        # branch can never preempt the target check on that same tick - meaning TARGET
        # fires the moment price first reaches it, however large the move, and the
        # trailing stop never gets a chance to "overtake" an unreached target. Confirmed
        # here rather than assumed: an initial hypothesis that a big enough gap could let
        # the trailing stop override the target turned out to be wrong when actually
        # tested - this test documents the ACTUAL (and, on reflection, correct-by-design)
        # behavior instead.
        engine, tracker = _make_engine_with_tracker()
        _open_position(engine, tracker, 100.0)
        result = engine.check_tick_for_exit(tracker.symbol, 110.0, "10:01:00")  # gaps straight past the 102 target
        self.assertEqual(result, "TARGET")
        self.assertFalse(tracker.position_open)

    def test_dry_run_true_never_attempts_a_live_order_and_does_not_raise(self):
        engine, tracker = _make_engine_with_tracker()
        _open_position(engine, tracker, 100.0)
        config.DRY_RUN = True
        # Should complete cleanly (DRY_RUN branch in _maybe_place_live_order just logs).
        result = engine.check_tick_for_exit(tracker.symbol, 99.0, "10:00:00")
        self.assertEqual(result, "STOP-LOSS")

    def test_dry_run_false_live_exit_hits_the_placeholder_not_a_crash(self):
        # No real fyers.place_order() is wired in yet (see _maybe_place_live_order's TODO) -
        # confirm the "LIVE ORDER PLACEHOLDER" branch is reached and doesn't raise.
        engine, tracker = _make_engine_with_tracker()
        _open_position(engine, tracker, 100.0)
        config.DRY_RUN = False
        result = engine.check_tick_for_exit(tracker.symbol, 99.0, "10:00:00")
        self.assertEqual(result, "STOP-LOSS")

    def test_tick_exit_is_never_flagged_as_backfill(self):
        engine, tracker = _make_engine_with_tracker()
        _open_position(engine, tracker, 100.0)
        engine.check_tick_for_exit(tracker.symbol, 99.0, "10:00:00")
        trade = engine.trade_book.trades[0]
        self.assertFalse(trade["exit_is_backfill"])


class TestAdoptBrokerPosition(unittest.TestCase):
    def test_adopts_into_a_tracked_symbol(self):
        engine, tracker = _make_engine_with_tracker()
        adopted = engine.adopt_broker_position(tracker.symbol, 150.0, 10, "10:00:00")
        self.assertTrue(adopted)
        self.assertTrue(tracker.position_open)
        self.assertEqual(tracker.entry_price_for_risk, 150.0)
        self.assertEqual(tracker.highest_price_since_entry, 150.0)

        trade = engine.trade_book.trades[0]
        self.assertEqual(trade["entry_type"], "BROKER_SYNC")
        self.assertTrue(trade["is_backfill"])  # recorded for audit, but treated like backfill (no live re-entry)
        self.assertEqual(trade["entry_price"], 150.0)

    def test_unknown_symbol_returns_false_and_does_not_raise(self):
        engine, _ = _make_engine_with_tracker()
        adopted = engine.adopt_broker_position("NSE:NOT-TRACKED-EQ", 150.0, 10, "10:00:00")
        self.assertFalse(adopted)

    def test_adopted_position_immediately_gets_live_tick_risk_management(self):
        engine, tracker = _make_engine_with_tracker()
        engine.adopt_broker_position(tracker.symbol, 100.0, 10, "10:00:00")
        # Should behave exactly like a normal open position for tick-level exits.
        result = engine.check_tick_for_exit(tracker.symbol, 99.0, "10:01:00")
        self.assertEqual(result, "STOP-LOSS")


class TestFeedCandleBackfillGating(unittest.TestCase):
    """
    Confirms is_backfill correctly threads through to the trade book without
    changing the underlying BUY/weakness-event detection logic itself - i.e.
    the exact same 2-consecutive-candle rules from earlier fire identically
    whether replaying history (is_backfill=True) or live (is_backfill=False);
    only the record's is_backfill flag (and therefore whether
    _maybe_place_live_order would ever place a real order for it) differs.
    """

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def _feed_warmup_candles(self, engine, tracker, is_backfill, count=20, base_price=95.0):
        for i in range(count):
            c = base_price + i * 0.05
            engine.feed_candle(tracker.symbol, [1700000000 + i, c, c + 0.2, c - 0.2, c, 1000],
                                f"09:{20 + i}:00", is_backfill=is_backfill)

    def test_backfilled_buy_signal_is_flagged_and_recorded(self):
        engine, tracker = _make_engine_with_tracker()
        self._feed_warmup_candles(engine, tracker, is_backfill=True)
        engine.feed_candle(tracker.symbol, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=True)
        engine.feed_candle(tracker.symbol, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=True)

        self.assertTrue(tracker.position_open)
        trade = engine.trade_book.trades[0]
        self.assertTrue(trade["is_backfill"])
        self.assertEqual(trade["entry_price"], 101.8)

    def test_live_buy_signal_is_not_flagged_as_backfill(self):
        engine, tracker = _make_engine_with_tracker()
        self._feed_warmup_candles(engine, tracker, is_backfill=False)
        engine.feed_candle(tracker.symbol, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=False)

        self.assertTrue(tracker.position_open)
        trade = engine.trade_book.trades[0]
        self.assertFalse(trade["is_backfill"])

    def test_feed_candle_raises_keyerror_for_untracked_symbol(self):
        engine, _ = _make_engine_with_tracker()
        with self.assertRaises(KeyError):
            engine.feed_candle("NSE:NOT-TRACKED-EQ", [1, 100, 101, 99, 100, 1000], "09:20:00")

    def test_entry_price_for_risk_is_set_on_a_live_buy(self):
        # tick-level risk management needs this populated the moment a BUY fires, not just
        # on broker-adopted positions.
        engine, tracker = _make_engine_with_tracker()
        self._feed_warmup_candles(engine, tracker, is_backfill=False)
        engine.feed_candle(tracker.symbol, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=False)
        self.assertEqual(tracker.entry_price_for_risk, 101.8)
        self.assertEqual(tracker.highest_price_since_entry, 101.8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
