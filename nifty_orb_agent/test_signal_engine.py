"""
test_signal_engine.py
------------------------
Tests for signal_engine.py, focused on the parts that had NO test coverage
before this file: broker position adoption (adopt_broker_position), and
the is_backfill gate on live order placement (_maybe_place_live_order).
The candle-level BUY/weakness-event logic itself was covered by earlier
conversational testing; is_backfill was added on top of it without changing
that logic, and a couple of tests here re-confirm that specifically.

There is no tick-level stop-loss/target/trailing-SL any more - that was
removed (stop-loss is now purely candle/VWAP-driven, see signal_engine.py's
module docstring), so the tests that used to hand-verify that arithmetic
are gone too. TestAdoptBrokerPosition below instead confirms a
broker-adopted position is managed by the SAME candle-level weakness
detector as any other open position.

Run with:
    python test_signal_engine.py
"""

from __future__ import annotations

import unittest

import config
from signal_engine import SignalEngine, StockTracker, TradeBook, seed_volume_windows_from_candles
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
    engine.trade_book.record_entry(tracker.symbol, entry_time, entry_price, entry_type="INITIAL",
                                    is_backfill=False, direction=tracker.direction)


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

    def test_unknown_symbol_returns_false_and_does_not_raise(self):
        engine, _ = _make_engine_with_tracker()
        adopted = engine.adopt_broker_position("NSE:NOT-TRACKED-EQ", 150.0, 10, "10:00:00")
        self.assertFalse(adopted)

    def _seed(self, engine, tracker, volume=1000):
        # Mirrors what bootstrap.bootstrap_and_catchup() actually does in production:
        # fetch_and_seed_volume_windows() runs BEFORE _reconcile_broker_state()/
        # adopt_broker_position(), so an adopted position's volume MA is ready
        # immediately, not after ~100 minutes of live-only warm-up.
        seed_volume_windows_from_candles(
            engine.trackers, {tracker.symbol: [[i, 100, 100.2, 99.8, 100.0, volume] for i in range(20)]})

    def test_adopted_short_direction_correction_drives_the_mirrored_weakness_detector(self):
        # There's no tick-level risk check any more (removed - see signal_engine.py's
        # module docstring); this instead confirms the corrected SHORT direction drives
        # the right (mirrored) candle-level math: closing ABOVE vwap counts against a
        # short, not below it.
        engine, tracker = _make_engine_with_tracker(direction=TradeDirection.LONG)
        engine.adopt_broker_position(tracker.symbol, 100.0, -10, "10:00:00")  # negative qty -> SHORT
        self.assertEqual(tracker.direction, TradeDirection.SHORT)
        self._seed(engine, tracker)

        # Warm-up candle establishes a VWAP baseline (~95) without itself counting as weak.
        engine.feed_candle(tracker.symbol, [1, 95, 95.2, 94.8, 95.0, 1000], "10:05:00", is_backfill=False)
        self.assertTrue(tracker.position_open)
        # Two consecutive candles closing ABOVE that VWAP, with volume above the seeded MA -
        # a weakness event for a SHORT. 1st event -> ADD, not exit.
        engine.feed_candle(tracker.symbol, [2, 100, 100.2, 99.8, 100.0, 2000], "10:10:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [3, 103, 103.2, 102.8, 103.0, 2200], "10:15:00", is_backfill=False)
        self.assertTrue(tracker.position_open)  # added, not squared off
        self.assertEqual(engine.trade_book.trades[-1]["entry_type"], "ADD")
        self.assertEqual(engine.trade_book.trades[-1]["direction"], "SHORT")

    def test_adopted_position_is_managed_by_the_candle_level_weakness_detector(self):
        # Mirror of the test above for a plain (uncorrected) LONG adoption - confirms an
        # adopted position is managed exactly like any other open position from the next
        # candle onward, with no special-casing left over from the old tick-level path.
        engine, tracker = _make_engine_with_tracker()  # defaults to LONG
        engine.adopt_broker_position(tracker.symbol, 100.0, 10, "10:00:00")
        self.assertTrue(tracker.position_open)
        self._seed(engine, tracker)

        # Warm-up candle establishes a VWAP baseline (~105) without itself counting as weak.
        engine.feed_candle(tracker.symbol, [1, 105, 105.2, 104.8, 105.0, 1000], "10:05:00", is_backfill=False)
        # Two consecutive candles closing BELOW that VWAP, with volume above the seeded MA -
        # a weakness event for a LONG. 1st event -> ADD, not exit.
        engine.feed_candle(tracker.symbol, [2, 100, 100.2, 99.8, 100.0, 2000], "10:10:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [3, 97, 97.2, 96.8, 97.0, 2200], "10:15:00", is_backfill=False)
        self.assertTrue(tracker.position_open)  # added, not squared off
        self.assertEqual(engine.trade_book.trades[-1]["entry_type"], "ADD")
        self.assertEqual(engine.trade_book.trades[-1]["direction"], "LONG")


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
        # Recorded the moment a BUY fires, not just on broker-adopted positions - general
        # bookkeeping (see adopt_broker_position), even though nothing computes tick-level
        # risk off it any more.
        engine, tracker = _make_engine_with_tracker()
        self._feed_warmup_candles(engine, tracker, is_backfill=False)
        engine.feed_candle(tracker.symbol, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=False)
        self.assertEqual(tracker.entry_price_for_risk, 101.8)


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

        # Two consecutive candles ABOVE vwap with volume > the 20-period volume MA -
        # weakness for a SHORT (price rising against the position) - 1st event -> ADD,
        # not exit. VWAP at
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


class TestCapitalBasedPositionSizing(unittest.TestCase):
    """
    Confirms entries/adds/exits now size orders from tracker.capital_allocated /
    tracker.lot_size via position_sizing.calculate_quantity() (see config.TRADING_CAPITAL),
    instead of the old flat config.DEFAULT_ORDER_QTY - and that TradeBook rows carry a
    real qty and a correctly-derived rupee pnl_amount.
    """

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def _feed_warmup_candles(self, engine, tracker, count=20, base_price=95.0):
        for i in range(count):
            c = base_price + i * 0.05
            engine.feed_candle(tracker.symbol, [1700000000 + i, c, c + 0.2, c - 0.2, c, 1000],
                                f"09:{20 + i}:00", is_backfill=False)

    def test_entry_qty_is_computed_from_capital_allocated_and_price(self):
        engine, tracker = _make_engine_with_tracker()
        tracker.capital_allocated = 10_000.0
        self._feed_warmup_candles(engine, tracker)
        engine.feed_candle(tracker.symbol, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=False)

        self.assertTrue(tracker.position_open)
        expected_qty = int(10_000.0 // 101.8)  # floor(capital / entry price), lot_size=1
        self.assertGreater(expected_qty, 0)
        self.assertEqual(tracker.total_qty_open, expected_qty)
        self.assertEqual(engine.trade_book.trades[0]["qty"], expected_qty)

    def test_lot_size_rounds_quantity_down_to_whole_lots(self):
        engine, tracker = _make_engine_with_tracker()
        tracker.capital_allocated = 10_000.0
        tracker.lot_size = 75
        self._feed_warmup_candles(engine, tracker)
        engine.feed_candle(tracker.symbol, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=False)

        self.assertTrue(tracker.position_open)
        self.assertGreater(tracker.total_qty_open, 0)
        self.assertEqual(tracker.total_qty_open % 75, 0)

    def test_entry_is_skipped_entirely_when_capital_cant_afford_one_lot(self):
        engine, tracker = _make_engine_with_tracker()
        tracker.capital_allocated = 10.0  # can't afford even 1 share at ~100
        self._feed_warmup_candles(engine, tracker)
        engine.feed_candle(tracker.symbol, [1, 100.5, 101.5, 100.2, 101.0, 2000], "09:45:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [2, 101.0, 102.0, 100.8, 101.8, 2200], "09:50:00", is_backfill=False)

        self.assertFalse(tracker.position_open)  # skipped entirely, never opened at qty=0
        self.assertEqual(engine.trade_book.trades, [])

    def test_close_all_open_computes_rupee_pnl_amount_from_qty(self):
        book = TradeBook()
        book.record_entry("NSE:A-EQ", "09:30:00", 100.0, direction=TradeDirection.LONG, qty=50)
        book.close_all_open("NSE:A-EQ", "10:00:00", 105.0)
        trade = book.trades[0]
        self.assertEqual(trade["points"], 5.0)
        self.assertEqual(trade["pnl_amount"], 250.0)  # 5 points x 50 qty

    def test_weakness_add_accumulates_qty_and_full_squareoff_closes_the_total(self):
        # Reuses TestAdoptBrokerPosition's hand-verified LONG adopt + weakness-ADD candle
        # sequence (see that class's test_adopted_position_is_managed_by_the_candle_level_
        # weakness_detector), then continues it two more weak candles to also exercise the
        # full square-off path's qty bookkeeping.
        engine, tracker = _make_engine_with_tracker()  # defaults to LONG
        tracker.capital_allocated = 40_000.0
        engine.adopt_broker_position(tracker.symbol, 100.0, 10, "10:00:00")
        initial_qty = tracker.total_qty_open
        self.assertEqual(initial_qty, 10)  # broker-adopted qty, NOT a fresh capital-based calc
        seed_volume_windows_from_candles(
            engine.trackers, {tracker.symbol: [[i, 100, 100.2, 99.8, 100.0, 1000] for i in range(20)]})

        engine.feed_candle(tracker.symbol, [1, 105, 105.2, 104.8, 105.0, 1000], "10:05:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [2, 100, 100.2, 99.8, 100.0, 2000], "10:10:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [3, 97, 97.2, 96.8, 97.0, 2200], "10:15:00", is_backfill=False)
        self.assertTrue(tracker.position_open)  # 1st weakness event -> ADD, not exit
        add_qty = engine.trade_book.trades[-1]["qty"]
        self.assertGreater(add_qty, 0)
        self.assertEqual(tracker.total_qty_open, initial_qty + add_qty)

        # A second weakness event -> full square-off, closing the TOTAL accumulated qty.
        engine.feed_candle(tracker.symbol, [4, 90, 90.2, 89.8, 90.0, 2500], "10:20:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [5, 85, 85.2, 84.8, 85.0, 2700], "10:25:00", is_backfill=False)
        self.assertFalse(tracker.position_open)
        self.assertEqual(tracker.total_qty_open, 0)
        self.assertEqual(engine.trade_book.trades[0]["qty"], initial_qty)
        self.assertEqual(engine.trade_book.trades[1]["qty"], add_qty)


class TestSeedVolumeWindowsFromCandles(unittest.TestCase):
    """
    seed_volume_windows_from_candles() in isolation - the pure function
    both bootstrap.fetch_and_seed_volume_windows() (live) and backtest.py
    (from its own pre-fetched data) call. See the module docstring for
    exactly what bug this fixes: without it, no entry can fire before
    ~100 minutes of live-only warm-up.
    """

    def _candle(self, epoch, volume):
        return [epoch, 100.0, 101.0, 99.0, 100.0, volume]

    def test_seeds_exactly_20_candles_makes_volume_ma_ready_immediately(self):
        engine, tracker = _make_engine_with_tracker()
        candles = [self._candle(i, 1000 + i) for i in range(20)]
        seed_volume_windows_from_candles(engine.trackers, {tracker.symbol: candles})
        self.assertEqual(len(tracker.volume_window), 20)
        self.assertIsNotNone(tracker.volume_ma())

    def test_seeding_with_fewer_than_needed_leaves_it_partially_warmed(self):
        engine, tracker = _make_engine_with_tracker()
        candles = [self._candle(i, 1000) for i in range(5)]  # only 5, need 20
        seed_volume_windows_from_candles(engine.trackers, {tracker.symbol: candles})
        self.assertEqual(len(tracker.volume_window), 5)
        self.assertIsNone(tracker.volume_ma())  # still not ready - correctly not faked

    def test_only_the_most_recent_needed_candles_are_kept_even_if_more_are_given(self):
        engine, tracker = _make_engine_with_tracker()
        candles = [self._candle(i, i) for i in range(50)]  # far more than the 20 needed
        seed_volume_windows_from_candles(engine.trackers, {tracker.symbol: candles})
        self.assertEqual(len(tracker.volume_window), 20)
        # should be the LAST 20 (volumes 30..49), not the first 20
        self.assertEqual(list(tracker.volume_window), list(range(30, 50)))

    def test_empty_candle_list_is_a_safe_no_op(self):
        engine, tracker = _make_engine_with_tracker()
        seed_volume_windows_from_candles(engine.trackers, {tracker.symbol: []})
        self.assertEqual(len(tracker.volume_window), 0)
        self.assertIsNone(tracker.volume_ma())

    def test_symbol_missing_from_candles_by_symbol_is_a_safe_no_op(self):
        engine, tracker = _make_engine_with_tracker()
        seed_volume_windows_from_candles(engine.trackers, {})  # no entry for tracker.symbol at all
        self.assertEqual(len(tracker.volume_window), 0)

    def test_multiple_trackers_are_seeded_independently(self):
        engine, tracker_a = _make_engine_with_tracker(symbol="NSE:A-EQ")
        tracker_b = StockTracker(symbol="NSE:B-EQ", or_high=100.0, or_low=100.0)
        engine.trackers["NSE:B-EQ"] = tracker_b
        seed_volume_windows_from_candles(engine.trackers, {
            "NSE:A-EQ": [self._candle(i, 1000) for i in range(20)],
            "NSE:B-EQ": [self._candle(i, 2000) for i in range(3)],
        })
        self.assertEqual(len(tracker_a.volume_window), 20)
        self.assertEqual(len(tracker_b.volume_window), 3)


class TestSeedingFixesTheEntryTimingBug(unittest.TestCase):
    """
    Direct proof that seeding restores the documented "earliest entry at
    the 09:30 candle close" behaviour: WITHOUT seeding, an entry needs 22
    live candles (20 to warm up volMA + 2 qualifying) - roughly 11:10 AM.
    WITH seeding, only the 2 qualifying candles are needed, since the
    volume MA is already warm from the prior session.
    """

    def setUp(self):
        self.orig_dry_run = config.DRY_RUN
        config.DRY_RUN = True

    def tearDown(self):
        config.DRY_RUN = self.orig_dry_run

    def test_buy_fires_on_just_2_live_candles_when_seeded(self):
        engine, tracker = _make_engine_with_tracker(or_high=100.0)
        # Seed with 20 candles of realistic volume - stands in for "yesterday's last 20 bars".
        seed_candles = [[i, 95.0, 95.2, 94.8, 95.0, 1000] for i in range(20)]
        seed_volume_windows_from_candles(engine.trackers, {tracker.symbol: seed_candles})
        self.assertIsNotNone(tracker.volume_ma())  # ready before a single LIVE candle is fed

        # Now feed exactly 2 LIVE candles - nothing more - and confirm a BUY fires.
        engine.feed_candle(tracker.symbol, [100, 100.5, 101.5, 100.2, 101.0, 2000], "09:25:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [101, 101.0, 102.0, 100.8, 101.8, 2200], "09:30:00", is_backfill=False)

        self.assertTrue(tracker.position_open)
        self.assertEqual(engine.trade_book.trades[0]["entry_price"], 101.8)

    def test_without_seeding_the_same_2_candles_do_not_trigger_a_buy(self):
        # Control case: same 2 live candles, NO seeding - confirms the fix is what made the
        # difference above, not some other change to the entry logic itself.
        engine, tracker = _make_engine_with_tracker(or_high=100.0)
        engine.feed_candle(tracker.symbol, [100, 100.5, 101.5, 100.2, 101.0, 2000], "09:25:00", is_backfill=False)
        engine.feed_candle(tracker.symbol, [101, 101.0, 102.0, 100.8, 101.8, 2200], "09:30:00", is_backfill=False)
        self.assertFalse(tracker.position_open)  # volume MA still None - correctly withheld


if __name__ == "__main__":
    unittest.main(verbosity=2)
