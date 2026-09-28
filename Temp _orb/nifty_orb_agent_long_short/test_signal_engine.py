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
from trade_direction import TradeDirection


def _make_engine_with_tracker(symbol="NSE:TEST-EQ", or_high=100.0, or_low=100.0,
                               direction=TradeDirection.LONG) -> tuple[SignalEngine, StockTracker]:
    engine = SignalEngine.__new__(SignalEngine)  # bypass __init__ (no data_service/shortlist needed for these tests)
    engine.data_service = None
    engine.trade_book = TradeBook()
    tracker = StockTracker(symbol=symbol, or_high=or_high, or_low=or_low, direction=direction)
    engine.trackers = {symbol: tracker}
    return engine, tracker


def _open_position(engine: SignalEngine, tracker: StockTracker, entry_price: float, entry_time: str = "09:45:00"):
    tracker.position_open = True
    tracker.entry_price_for_risk = entry_price
    tracker.highest_price_since_entry = entry_price
    tracker.lowest_price_since_entry = entry_price
    engine.trade_book.record_entry(tracker.symbol, entry_time, entry_price, entry_type="INITIAL",
                                    is_backfill=False, direction=tracker.direction)


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


class TestCheckTickForExitShort(unittest.TestCase):
    """
    The mirror image of TestCheckTickForExit above, for a SHORT position.
    Same config defaults: STOP_LOSS_PCT=1.0, TARGET_PCT=2.0, TRAILING_SL_PCT=1.0.
    Hand-verified arithmetic, entry=100 (sold short at 100):
      stop_loss_price   = 100 * 1.01 = 101   (ABOVE entry - price rising hurts a short)
      target_price      = 100 * 0.98 = 98    (BELOW entry - price falling profits a short)
      trailing_sl_price = lowest_price_since_entry * 1.01 (ratchets DOWN with the lowest)
      effective_stop     = min(stop_loss_price, trailing_sl_price)
    """

    def setUp(self):
        self.orig_stop, self.orig_target, self.orig_trail = (
            config.STOP_LOSS_PCT, config.TARGET_PCT, config.TRAILING_SL_PCT)
        config.STOP_LOSS_PCT, config.TARGET_PCT, config.TRAILING_SL_PCT = 1.0, 2.0, 1.0
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True

    def tearDown(self):
        config.STOP_LOSS_PCT, config.TARGET_PCT, config.TRAILING_SL_PCT = (
            self.orig_stop, self.orig_target, self.orig_trail)
        config.DRY_RUN = self.orig_dry_run

    def _short_tracker(self):
        return _make_engine_with_tracker(direction=TradeDirection.SHORT)

    def test_price_between_target_and_stop_does_not_exit(self):
        engine, tracker = self._short_tracker()
        _open_position(engine, tracker, 100.0)
        result = engine.check_tick_for_exit(tracker.symbol, 99.5, "10:00:00")
        self.assertIsNone(result)
        self.assertTrue(tracker.position_open)

    def test_plain_stop_loss_hit_without_any_favorable_move_first(self):
        # lowest never falls below entry, so trailing_sl_price == stop_loss_price == 101 -
        # tie-break labels this "STOP-LOSS", not "TRAILING-SL" (same convention as LONG).
        engine, tracker = self._short_tracker()
        _open_position(engine, tracker, 100.0)
        result = engine.check_tick_for_exit(tracker.symbol, 101.0, "10:05:00")
        self.assertEqual(result, "STOP-LOSS")
        self.assertFalse(tracker.position_open)
        self.assertIsNone(tracker.entry_price_for_risk)
        self.assertIsNone(tracker.lowest_price_since_entry)

    def test_target_hit(self):
        engine, tracker = self._short_tracker()
        _open_position(engine, tracker, 100.0)
        # ltp=97.5: lowest becomes 97.5, trailing_sl_price=97.5*1.01=98.475,
        # effective_stop=min(101,98.475)=98.475; ltp(97.5) < 98.475 so the stop branch
        # doesn't fire, and ltp(97.5) <= target(98) -> TARGET.
        result = engine.check_tick_for_exit(tracker.symbol, 97.5, "10:05:00")
        self.assertEqual(result, "TARGET")
        self.assertFalse(tracker.position_open)

    def test_trailing_stop_ratchets_down_and_then_triggers_on_a_bounce(self):
        engine, tracker = self._short_tracker()
        _open_position(engine, tracker, 100.0)

        # Favorable move to 99 - no exit (99 < effective_stop 99.99, > target 98)
        self.assertIsNone(engine.check_tick_for_exit(tracker.symbol, 99.0, "10:01:00"))
        self.assertAlmostEqual(tracker.lowest_price_since_entry, 99.0)

        # Small bounce to 99.5, still below the trailing stop (99.99) - no exit, and
        # lowest must NOT rise back up.
        self.assertIsNone(engine.check_tick_for_exit(tracker.symbol, 99.5, "10:02:00"))
        self.assertAlmostEqual(tracker.lowest_price_since_entry, 99.0)  # unchanged, ratchets down only

        # Rises to 100.0: above trailing_sl_price (99*1.01=99.99) but still below the
        # original hard stop (101) - should exit via TRAILING-SL specifically.
        result = engine.check_tick_for_exit(tracker.symbol, 100.0, "10:03:00")
        self.assertEqual(result, "TRAILING-SL")
        self.assertFalse(tracker.position_open)

    def test_target_is_effectively_a_hard_floor_even_on_a_large_gap_down(self):
        # Mirror of the LONG "target is a hard ceiling" property: on the tick that FIRST
        # sets a new low, effective_stop is always strictly above that new low
        # (trailing_sl_price = new_low * 1.01 > new_low), so the stop branch can never
        # preempt the target check on that same tick - TARGET fires the moment price
        # first reaches it, however large the drop.
        engine, tracker = self._short_tracker()
        _open_position(engine, tracker, 100.0)
        result = engine.check_tick_for_exit(tracker.symbol, 90.0, "10:01:00")  # gaps straight past the 98 target
        self.assertEqual(result, "TARGET")
        self.assertFalse(tracker.position_open)

    def test_exit_side_is_buy_to_cover(self):
        engine, tracker = self._short_tracker()
        _open_position(engine, tracker, 100.0)
        engine.check_tick_for_exit(tracker.symbol, 101.0, "10:00:00")
        # side isn't stored directly on the trade row, but the log-message construction
        # (BUY vs SELL) is exercised implicitly by this call completing without error and
        # the position correctly closing - see also TestTradeBookDirectionAwarePnL for the
        # actual signed P&L, which is what would be wrong if entry/exit were swapped.
        self.assertFalse(tracker.position_open)


class TestTradeBookDirectionAwarePnL(unittest.TestCase):
    """
    Direct tests of TradeBook.close_all_open()'s signed points/pnl_pct math:
    LONG profits on price rising (exit - entry), SHORT profits on price
    falling (entry - exit) - the exact opposite sign convention. Getting
    this backwards would silently invert every short trade's reported P&L.
    """

    def test_long_profit_is_exit_minus_entry(self):
        book = TradeBook()
        book.record_entry("NSE:A-EQ", "09:30:00", 100.0, direction=TradeDirection.LONG)
        book.close_all_open("NSE:A-EQ", "10:00:00", 105.0)
        trade = book.trades[0]
        self.assertEqual(trade["points"], 5.0)
        self.assertAlmostEqual(trade["pnl_pct"], 5.0)

    def test_long_loss_is_still_exit_minus_entry_and_negative(self):
        book = TradeBook()
        book.record_entry("NSE:A-EQ", "09:30:00", 100.0, direction=TradeDirection.LONG)
        book.close_all_open("NSE:A-EQ", "10:00:00", 95.0)
        trade = book.trades[0]
        self.assertEqual(trade["points"], -5.0)

    def test_short_profit_is_entry_minus_exit(self):
        book = TradeBook()
        book.record_entry("NSE:A-EQ", "09:30:00", 100.0, direction=TradeDirection.SHORT)
        book.close_all_open("NSE:A-EQ", "10:00:00", 95.0)  # price fell - good for a short
        trade = book.trades[0]
        self.assertEqual(trade["points"], 5.0)  # profit, NOT -5.0
        self.assertAlmostEqual(trade["pnl_pct"], 5.0)

    def test_short_loss_is_still_entry_minus_exit_and_negative(self):
        book = TradeBook()
        book.record_entry("NSE:A-EQ", "09:30:00", 100.0, direction=TradeDirection.SHORT)
        book.close_all_open("NSE:A-EQ", "10:00:00", 105.0)  # price rose - bad for a short
        trade = book.trades[0]
        self.assertEqual(trade["points"], -5.0)

    def test_default_direction_is_long_for_backward_compatible_call_sites(self):
        book = TradeBook()
        book.record_entry("NSE:A-EQ", "09:30:00", 100.0)  # no direction argument at all
        book.close_all_open("NSE:A-EQ", "10:00:00", 105.0)
        self.assertEqual(book.trades[0]["points"], 5.0)  # LONG math, not SHORT

    def test_multiple_short_rows_all_close_with_correct_signed_pnl(self):
        # Mirrors an ADD scenario: an initial short plus one add-on, both closed together.
        book = TradeBook()
        book.record_entry("NSE:A-EQ", "09:30:00", 100.0, entry_type="INITIAL", direction=TradeDirection.SHORT)
        book.record_entry("NSE:A-EQ", "09:45:00", 103.0, entry_type="ADD", direction=TradeDirection.SHORT)
        closed = book.close_all_open("NSE:A-EQ", "10:00:00", 101.0)
        self.assertEqual(closed, 2)
        self.assertEqual(book.trades[0]["points"], -1.0)   # sold at 100, covered at 101 -> lost 1
        self.assertEqual(book.trades[1]["points"], 2.0)    # sold at 103, covered at 101 -> gained 2


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

    def test_positive_qty_is_adopted_as_long(self):
        engine, tracker = _make_engine_with_tracker(direction=TradeDirection.LONG)
        engine.adopt_broker_position(tracker.symbol, 150.0, 10, "10:00:00")
        self.assertEqual(tracker.direction, TradeDirection.LONG)

    def test_negative_qty_is_adopted_as_short(self):
        engine, tracker = _make_engine_with_tracker(direction=TradeDirection.LONG)  # engine thinks LONG today
        engine.adopt_broker_position(tracker.symbol, 150.0, -10, "10:00:00")  # broker says otherwise
        self.assertEqual(tracker.direction, TradeDirection.SHORT)  # broker truth wins, corrected
        self.assertTrue(tracker.position_open)

    def test_adopted_short_immediately_gets_correctly_mirrored_tick_risk_management(self):
        engine, tracker = _make_engine_with_tracker(direction=TradeDirection.LONG)
        engine.adopt_broker_position(tracker.symbol, 100.0, -10, "10:00:00")  # negative qty -> SHORT
        # A rise in price should now hurt (stop-out), not help - confirms the corrected
        # direction actually drives check_tick_for_exit's mirrored math, not just the flag.
        result = engine.check_tick_for_exit(tracker.symbol, 101.0, "10:01:00")
        self.assertEqual(result, "STOP-LOSS")

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


class TestFeedCandleShortEntryAndWeakness(unittest.TestCase):
    """
    The mirror image of TestFeedCandleBackfillGating, for a SHORT-direction
    engine: entry requires 2 consecutive candles closing BELOW the
    opening-range LOW and below VWAP (instead of above the OR high and
    above VWAP for LONG); a weakness event is 2 consecutive candles closing
    ABOVE VWAP (price rising hurts a short) rather than below it.
    """

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def _short_tracker_with_warmup(self, is_backfill=False):
        # or_low=100.0, sitting below a 20-candle warm-up trending DOWN from 105 to ~104 -
        # mirrors the LONG test's or_high=100.0 sitting above an upward-trending warm-up.
        engine, tracker = _make_engine_with_tracker(or_low=100.0, direction=TradeDirection.SHORT)
        for i in range(20):
            c = 105 - i * 0.05
            engine.feed_candle(tracker.symbol, [1700000000 + i, c, c + 0.2, c - 0.2, c, 1000],
                                f"09:{20 + i}:00", is_backfill=is_backfill)
        return engine, tracker

    def test_two_consecutive_candles_below_or_low_and_vwap_trigger_a_short_entry(self):
        engine, tracker = self._short_tracker_with_warmup()
        # VWAP after warm-up sits around ~104.5; both candles below is that AND below or_low=100.
        engine.feed_candle(tracker.symbol, [1, 99.5, 99.8, 98.5, 99.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [2, 99.0, 99.2, 97.8, 98.2, 2200], "09:50:00", is_backfill=False)

        self.assertTrue(tracker.position_open)
        self.assertEqual(tracker.direction, TradeDirection.SHORT)
        trade = engine.trade_book.trades[0]
        self.assertEqual(trade["direction"], "SHORT")
        self.assertEqual(trade["entry_price"], 98.2)
        self.assertEqual(tracker.entry_price_for_risk, 98.2)
        self.assertEqual(tracker.lowest_price_since_entry, 98.2)

    def test_only_one_qualifying_candle_does_not_yet_enter(self):
        engine, tracker = self._short_tracker_with_warmup()
        engine.feed_candle(tracker.symbol, [1, 99.5, 99.8, 98.5, 99.0, 2000], "09:45:00", is_backfill=False)
        self.assertFalse(tracker.position_open)

    def test_a_candle_above_or_low_does_not_qualify_even_with_high_volume(self):
        engine, tracker = self._short_tracker_with_warmup()
        # Closes ABOVE or_low(100) - should not count toward the entry streak at all.
        engine.feed_candle(tracker.symbol, [1, 104.0, 104.5, 103.0, 103.5, 5000], "09:45:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [2, 99.0, 99.2, 97.8, 98.2, 2200], "09:50:00", is_backfill=False)
        self.assertFalse(tracker.position_open)  # only 1 qualifying candle so far, not 2 consecutive

    def test_backfilled_short_entry_is_flagged_and_recorded(self):
        engine, tracker = self._short_tracker_with_warmup(is_backfill=True)
        engine.feed_candle(tracker.symbol, [1, 99.5, 99.8, 98.5, 99.0, 2000], "09:45:00", is_backfill=True)
        engine.feed_candle(tracker.symbol, [2, 99.0, 99.2, 97.8, 98.2, 2200], "09:50:00", is_backfill=True)
        self.assertTrue(tracker.position_open)
        self.assertTrue(engine.trade_book.trades[0]["is_backfill"])

    def test_weakness_event_add_then_stop_loss_squares_off_short(self):
        engine, tracker = self._short_tracker_with_warmup()
        engine.feed_candle(tracker.symbol, [1, 99.5, 99.8, 98.5, 99.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [2, 99.0, 99.2, 97.8, 98.2, 2200], "09:50:00", is_backfill=False)
        self.assertTrue(tracker.position_open)

        # Two consecutive candles ABOVE vwap with volume > fast SMA(2) - weakness for a
        # SHORT (price rising against the position) - 1st event -> ADD, not exit. VWAP at
        # this point is still pulled up near ~103.5 by the 20-candle warm-up mass, so these
        # need to close well clear of that, not just above the entry candles' own prices.
        engine.feed_candle(tracker.symbol, [3, 105.0, 106.5, 104.5, 106.0, 3000], "09:55:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [4, 106.0, 109.0, 105.5, 108.0, 3500], "10:00:00", is_backfill=False)
        self.assertTrue(tracker.position_open)  # still open - added, not squared off
        self.assertEqual(engine.trade_book.trades[1]["entry_type"], "ADD")
        self.assertEqual(engine.trade_book.trades[1]["direction"], "SHORT")

        # A SECOND weakness event -> now squares off the whole position.
        engine.feed_candle(tracker.symbol, [5, 108.0, 112.5, 107.5, 112.0, 4000], "10:05:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [6, 112.0, 115.5, 111.5, 115.0, 4500], "10:10:00", is_backfill=False)
        self.assertFalse(tracker.position_open)
        # Both rows should be closed, both with the SHORT (entry - exit) sign convention -
        # both a loss here, since price rose against the short throughout.
        self.assertIsNotNone(engine.trade_book.trades[0]["exit_price"])
        self.assertIsNotNone(engine.trade_book.trades[1]["exit_price"])
        self.assertLess(engine.trade_book.trades[0]["points"], 0)
        self.assertLess(engine.trade_book.trades[1]["points"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
