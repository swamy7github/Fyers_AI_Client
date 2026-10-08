"""
signal_engine.py
------------------
Tracks the shortlisted stocks from the opening-range scan candle by candle
(every 5 minutes) until 15:10, in EITHER direction (LONG or SHORT - see
trade_direction.py). Which direction a given SignalEngine trades is fixed
for its whole lifetime (set once at construction, from that day's
shortlist and sentiment-derived direction - see bootstrap.py/main.py); a
single engine never trades both directions at once.

  ENTRY - two CONSECUTIVE 5-min candles that each close:
         LONG:  (a) above the opening-range HIGH (the 09:15 candle's high),
                (b) above the running session VWAP, (c) with volume > the
                20-period volume moving average.
         SHORT: the mirror image - (a) below the opening-range LOW, (b)
                below VWAP, (c) volume > the 20-period volume MA.
         Entry is taken at the close of the 2nd confirming candle.

         Because two candles are required *after* the 09:15 one (09:20-09:25
         and 09:25-09:30), the earliest possible entry is at the close of
         the 09:30 candle - which is exactly the "wait till 9:30 candle
         closed" checkpoint in the original brief.

  WEAKNESS EVENT (shared detector for both the add-on and the stop-loss) -
         two CONSECUTIVE candles moving AGAINST the position, each with
         volume above the SAME 20-period volume moving average used for
         entries:
         LONG:  closing below VWAP (price falling hurts a long).
         SHORT: closing above VWAP (price rising hurts a short).
         (Earlier revisions checked volume against a separate, faster
         2-period average here; exit now uses the identical 20-period
         volume MA as entry, so the volume-conviction bar is the same on
         the way in and the way out.)

         - 1st weakness event on an open position (no square-off has fired
           yet) -> ADD to the position at that candle's close (BUY more for
           LONG, sell more for SHORT), instead of exiting. Up to
           config.MAX_POSITION_ADDS times.
         - Any weakness event after that (or immediately, if
           MAX_POSITION_ADDS = 0) -> STOP-LOSS / SQUARE OFF: close the
           ENTIRE position (the original entry plus every add-on) at that
           candle's close.

  EOD  - any position still open at SQUARE_OFF_TIME (15:10:30) is closed at
         the last traded price, so nothing is left open overnight.

The WEAKNESS EVENT detector and EOD square-off are the ONLY two ways a
position closes - there is no separate tick-level stop-loss, target, or
trailing-stop. Stop-loss is purely dynamic/candle-driven: a position (LONG
or SHORT) only closes once 2 consecutive 5-min candles confirm the move
against it, exactly like entry requires 2 consecutive candles to confirm
the move in its favor. (Earlier revisions also had a fixed-%, tick-by-tick
stop-loss/target/trailing-SL in check_tick_for_exit, fed by a live
WebSocket in live_tick_stream.py - that has been removed; see this
module's git history or README for why.)

All trades are PAPER trades recorded in a TradeBook; nothing is sent to the
broker unless you explicitly wire up order placement (see README) and set
config.DRY_RUN = False.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd
import pytz

import config
import position_sizing
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fyersclient
from trade_direction import TradeDirection

logger = logging.getLogger("orb_agent.signals_engine")


@dataclass
class StockTracker:
    symbol: str
    or_high: Optional[float] = None  # opening range (09:15 candle) high - LONG breakout level
    or_low: Optional[float] = None   # opening range (09:15 candle) low - SHORT breakdown level
    direction: TradeDirection = TradeDirection.LONG
    candles: list = field(default_factory=list)  # raw OHLCV history for the day
    cum_pv: float = 0.0   # cumulative typical_price * volume, for VWAP
    cum_vol: float = 0.0

    # -- volume MA (20-period): shared by BOTH entry qualification and the
    #    weakness/exit detector below, so the volume-conviction bar is the
    #    same in both directions --
    volume_window: deque = field(default_factory=lambda: deque(maxlen=config.VOLUME_MA_PERIOD))
    consecutive_qualifying: int = 0
    position_open: bool = False

    # -- weakness (add-on / stop-loss) tracking --
    consecutive_weak: int = 0
    weakness_events_on_position: int = 0  # how many confirmed weakness events since the last flat state

    # -- entry price bookkeeping --
    # The INITIAL entry price for the currently open position. There is no tick-level
    # stop-loss/target/trailing-SL any more (see the module docstring) - the ONLY way a
    # position closes before EOD is the candle-level weakness detector above - but this is
    # still recorded for broker reconciliation (adopt_broker_position) and general bookkeeping.
    entry_price_for_risk: Optional[float] = None

    # -- capital-based position sizing (see position_sizing.py) --
    # lot_size: 1 for an NSE equity (any whole share is its own "lot"); >1 for an
    # options contract or commodity future, which only transacts in fixed lot multiples
    # (options_engine.py / commodity_engine.py set this explicitly per tracker when they
    # build a non-equity SignalEngine - the default of 1 here is correct for plain equities
    # and is what every existing equity call site gets unchanged).
    lot_size: int = 1
    # capital_allocated: rupees this ONE tracker is allowed to spend per BUY/SELL (an
    # entry, or any later ADD - see _check_weakness_and_act) - defaults to the equity
    # per-slot share of config.TRADING_CAPITAL, but options_engine.py/commodity_engine.py
    # override it per tracker with their own dedicated capital bucket
    # (config.NIFTY_OPTIONS_CAPITAL / config.MCX_CAPITAL).
    capital_allocated: float = field(default_factory=lambda: config.CAPITAL_PER_TRADE)
    # total_qty_open: running total of shares/units currently held for this position
    # (initial entry + every ADD since), so an exit (weakness stop-loss or EOD square-off)
    # closes the EXACT quantity that was actually bought, not a guess. Reset to 0 once the
    # position is fully closed.
    total_qty_open: int = 0

    def vwap(self) -> float:
        return self.cum_pv / self.cum_vol if self.cum_vol else 0.0

    def volume_ma(self) -> Optional[float]:
        """20-period volume MA, shared by both the ENTRY qualification check and the
        WEAKNESS/exit detector. None until the window is full - we skip signals rather
        than fake an average with too little history."""
        if len(self.volume_window) < config.VOLUME_MA_PERIOD:
            return None
        return sum(self.volume_window) / len(self.volume_window)


def seed_volume_windows_from_candles(trackers: dict[str, StockTracker], candles_by_symbol: dict[str, list]) -> None:
    """
    Pre-fills each tracker's 20-period volume window with volume from
    already-fetched candles - typically the prior trading session's - so
    the volume MA used for BOTH entries and the weakness/exit detector is
    ready from the very first candle of a new session instead of needing
    ~100 minutes of live warm-up (20 candles x 5 min after the
    opening-range candle). Without this, no entry (or exit) could fire
    before roughly 11:00 AM every single day, silently contradicting the
    documented "earliest entry at 09:30" behaviour.

    Pure and side-effect-free beyond mutating the trackers' deques - has no
    opinion on HOW candles_by_symbol was obtained. Live callers
    (bootstrap.fetch_and_seed_volume_windows) fetch a fresh prior-session
    bulk candle set; backtest.py slices its own already-pre-fetched
    full-range data instead, at zero extra API cost.

    candles_by_symbol values are expected in chronological order (oldest
    first) - only the volume field (index 5) is used, and only the most
    recent VOLUME_MA_PERIOD candles of each list matter (older ones are
    trimmed here so a very long candle list doesn't matter). A tracker with
    no entry, or an empty list, is left alone: its window starts empty and
    warms up live exactly as before this existed - this never raises.
    """
    needed = config.VOLUME_MA_PERIOD
    for symbol, tracker in trackers.items():
        candles = candles_by_symbol.get(symbol) or []
        if not candles:
            logger.info("No prior-session volume history available for %s - volume MA will "
                        "warm up live over the first %d candles today, as before.", symbol, needed)
            continue
        volumes = [float(c[5]) for c in candles[-needed:]]
        tracker.volume_window.extend(volumes)
        ready = ("immediately" if len(tracker.volume_window) >= needed
                  else f"after {needed - len(tracker.volume_window)} more live candle(s)")
        logger.info("Seeded %s's volume windows with %d prior-session candle(s) - volume MA ready %s.",
                    symbol, len(volumes), ready)


class TradeBook:
    """
    Records every entry/exit and derives points captured (profit/loss),
    P&L%, and now (with capital-based sizing - see position_sizing.py) a
    rupee P&L amount too, direction-aware: for a SHORT, profit is
    entry-minus-exit (you sell first, buy back later), the mirror image of
    a LONG's exit-minus-entry. Each entry (the original, or a later add-on)
    is its own row, tagged via `entry_type` ("INITIAL" or "ADD") so the
    position-sizing history stays visible in the printed trade book, and
    now carries its own `qty` (how many shares/lots THAT row bought) so
    pnl_amount can be computed per-row even when an ADD's size differs from
    the INITIAL entry's. A square-off closes every still-open row for that
    symbol at the same exit price/time, since the whole position (initial +
    adds) always exits together - pnl_amount is still computed per-row
    using THAT row's own qty, then summed in summary().
    """

    def __init__(self):
        self.trades: list[dict] = []

    def record_entry(self, symbol: str, entry_time: str, entry_price: float, entry_type: str = "INITIAL",
                      is_backfill: bool = False, direction: TradeDirection = TradeDirection.LONG,
                      qty: int = 0):
        self.trades.append({
            "symbol": symbol, "entry_type": entry_type, "direction": direction.value,
            "entry_time": entry_time, "entry_price": entry_price, "qty": qty,
            "exit_time": None, "exit_price": None, "points": None, "pnl_pct": None,
            "pnl_amount": None,
            # is_backfill marks an entry the engine reconstructed from historical replay
            # (or seeded from the broker's own reported state) rather than detected live.
            # This is the flag SignalEngine._maybe_place_live_order() checks before ever
            # placing a real order - it's what makes "don't enter late at a stale price"
            # a structural guarantee instead of something that has to be remembered later
            # when live order placement gets wired up.
            "is_backfill": is_backfill, "exit_is_backfill": None,
        })

    def close_all_open(self, symbol: str, exit_time: str, exit_price: float, exit_is_backfill: bool = False) -> int:
        """Closes every unmatched (still-open) row for `symbol` at once. Returns how many rows were closed."""
        closed_count = 0
        for trade in self.trades:
            if trade["symbol"] == symbol and trade["exit_time"] is None:
                trade["exit_time"] = exit_time
                trade["exit_price"] = exit_price
                if trade["direction"] == TradeDirection.SHORT.value:
                    trade["points"] = round(trade["entry_price"] - exit_price, 2)
                    trade["pnl_pct"] = round((trade["entry_price"] - exit_price) / trade["entry_price"] * 100, 2)
                else:
                    trade["points"] = round(exit_price - trade["entry_price"], 2)
                    trade["pnl_pct"] = round((exit_price - trade["entry_price"]) / trade["entry_price"] * 100, 2)
                trade["pnl_amount"] = round(trade["points"] * (trade.get("qty") or 0), 2)
                trade["exit_is_backfill"] = exit_is_backfill
                closed_count += 1
        return closed_count

    def as_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.trades)

    def summary(self) -> dict:
        df = self.as_dataframe()
        closed = df[df["exit_price"].notna()] if not df.empty else df
        return {
            "total_trades": int(len(closed)),
            "total_points": round(float(closed["points"].sum()), 2) if not closed.empty else 0.0,
            "total_pnl_amount": round(float(closed["pnl_amount"].sum()), 2) if not closed.empty else 0.0,
            "win_rate_pct": round(float((closed["points"] > 0).mean() * 100), 2) if not closed.empty else 0.0,
        }


class SignalEngine:
    def __init__(self, data_service: FyersDataService, shortlist_df: pd.DataFrame,
                 direction: TradeDirection = TradeDirection.LONG):
        self.data_service = data_service
        self.direction = direction
        self.trackers = {
            row["symbol"]: StockTracker(symbol=row["symbol"], or_high=row["high"], or_low=row["low"],
                                         direction=direction)
            for _, row in shortlist_df.iterrows()
        }
        self.trade_book = TradeBook()

    def _fetch_latest_candle(self, symbol: str, window_start_epoch: int, window_end_epoch: int):
        candles = self.data_service.get_5min_candle(
            symbol, datetime.now().strftime("%Y-%m-%d"), window_start_epoch, window_end_epoch
        )
        return candles[0] if candles else None

    def check_all(self, window_start_epoch: int, window_end_epoch: int):
        """Called every 5 minutes by the scheduler between ~09:25 and 15:10. Always a LIVE call
        (is_backfill=False) - historical replay goes through feed_candle() directly instead,
        see bootstrap.py."""
        now_str = datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%H:%M:%S")
        for symbol, tracker in self.trackers.items():
            try:
                candle = self._fetch_latest_candle(symbol, window_start_epoch, window_end_epoch)
                if not candle:
                    continue
                self._update_tracker_and_check_signal(tracker, candle, now_str, is_backfill=False)
            except Exception as exc:
                logger.error("Signal check failed for %s: %s", symbol, exc)

    def feed_candle(self, symbol: str, candle: list, now_str: str, is_backfill: bool = False):
        """
        Public entry point for feeding ONE already-fetched candle for ONE
        symbol directly, bypassing check_all()'s live data fetch entirely.

        Two callers use this: backtest.py replays a full historical day
        (is_backfill left at its default False, since a backtest has no
        "live order" concept to gate - every trade in a backtest is
        equally hypothetical). bootstrap.py's catch-up pipeline replays
        TODAY's candles from market open up to "now" on a late start, with
        is_backfill=True - see _maybe_place_live_order for exactly what
        that flag changes.

        If you change the strategy in this file, live trading, backtest.py
        AND bootstrap.py's catch-up replay all pick up the change
        automatically - there's only one place the rules live.
        """
        tracker = self.trackers.get(symbol)
        if tracker is None:
            raise KeyError(f"{symbol!r} is not one of this engine's tracked symbols "
                            f"({list(self.trackers)}).")
        self._update_tracker_and_check_signal(tracker, candle, now_str, is_backfill=is_backfill)

    def _update_tracker_and_check_signal(self, tracker: StockTracker, candle: list, now_str: str,
                                          is_backfill: bool = False):
        _, o, h, l, c, v = candle
        typical_price = (h + l + c) / 3
        tracker.candles.append(candle)
        tracker.cum_pv += typical_price * v
        tracker.cum_vol += v

        vwap = tracker.vwap()

        # The volume MA is computed BEFORE this candle joins its window, so it's a
        # trailing average that excludes the current bar - matches "volume > its average".
        # Shared by both the entry check below and the weakness/exit check.
        vol_ma_slow = tracker.volume_ma()
        tracker.volume_window.append(v)

        if tracker.position_open:
            self._check_weakness_and_act(tracker, c, vwap, v, vol_ma_slow, now_str, is_backfill)
            return

        # ---- ENTRY check (only while flat), direction-aware ----
        if tracker.direction == TradeDirection.LONG:
            qualifies = (vol_ma_slow is not None and tracker.or_high is not None
                         and c > tracker.or_high and c > vwap and v > vol_ma_slow)
        else:  # SHORT - mirror image: break BELOW the opening-range low, BELOW VWAP
            qualifies = (vol_ma_slow is not None and tracker.or_low is not None
                         and c < tracker.or_low and c < vwap and v > vol_ma_slow)
        tracker.consecutive_qualifying = tracker.consecutive_qualifying + 1 if qualifies else 0

        if tracker.consecutive_qualifying >= config.CONSECUTIVE_CANDLES_FOR_BUY:
            qty = self._size_order(tracker, c)
            if qty == 0:
                logger.warning("Entry signal for %s @ %.2f fired, but capital_allocated=%.2f (lot_size=%d) "
                                "can't afford even one lot - SKIPPING this entry entirely (not placing an "
                                "order, not opening a tracked position).",
                                tracker.symbol, c, tracker.capital_allocated, tracker.lot_size)
                tracker.consecutive_qualifying = 0
                return
            tracker.position_open = True
            tracker.weakness_events_on_position = 0
            tracker.consecutive_weak = 0
            tracker.entry_price_for_risk = c
            tracker.total_qty_open = qty
            self.trade_book.record_entry(tracker.symbol, now_str, c, entry_type="INITIAL",
                                          is_backfill=is_backfill, direction=tracker.direction, qty=qty)
            if tracker.direction == TradeDirection.LONG:
                logger.info("BUY   %s qty=%d @ %.2f (2 consecutive candles above OR high %.2f, VWAP & volume MA)%s",
                             tracker.symbol, qty, c, tracker.or_high, " [BACKFILL]" if is_backfill else "")
                self._maybe_place_live_order(tracker, "BUY", c, is_backfill, now_str, qty)
            else:
                logger.info("SHORT %s qty=%d @ %.2f (2 consecutive candles below OR low %.2f, VWAP & volume MA)%s",
                             tracker.symbol, qty, c, tracker.or_low, " [BACKFILL]" if is_backfill else "")
                self._maybe_place_live_order(tracker, "SELL (SHORT)", c, is_backfill, now_str, qty)
            tracker.consecutive_qualifying = 0  # reset so it doesn't immediately re-fire

    def _check_weakness_and_act(self, tracker: StockTracker, c: float, vwap: float, v: float,
                                 vol_ma_slow: Optional[float], now_str: str, is_backfill: bool = False):
        """
        Runs only while a position is open. Detects the shared "weakness event"
        - 2 CONSECUTIVE candles closed AGAINST the position, each with volume
        above the same 20-period volume MA used for entries: closing below
        VWAP for a LONG, above VWAP for a SHORT - and either adds to the
        position or squares it off, per config.MAX_POSITION_ADDS - see the
        module docstring for the full rule.
        """
        if tracker.direction == TradeDirection.LONG:
            is_weak_candle = (vol_ma_slow is not None) and (c < vwap) and (v > vol_ma_slow)
            add_side, exit_side = "BUY (ADD)", "SELL (weakness stop-loss)"
            vs_vwap = "below"
        else:
            is_weak_candle = (vol_ma_slow is not None) and (c > vwap) and (v > vol_ma_slow)
            add_side, exit_side = "SELL (ADD)", "BUY (weakness stop-loss)"
            vs_vwap = "above"

        tracker.consecutive_weak = tracker.consecutive_weak + 1 if is_weak_candle else 0

        if tracker.consecutive_weak < config.CONSECUTIVE_CANDLES_FOR_STOPLOSS:
            return  # not (yet) two consecutive weak candles - keep holding

        # A weakness event has fired. Reset the streak so a FRESH pair of weak
        # candles is required before this can fire again.
        tracker.consecutive_weak = 0
        tracker.weakness_events_on_position += 1

        if tracker.weakness_events_on_position <= config.MAX_POSITION_ADDS:
            # First (or Nth, up to MAX_POSITION_ADDS) weakness event on this position,
            # and no square-off has happened yet -> add to the position instead of exiting.
            add_qty = self._size_order(tracker, c)
            if add_qty == 0:
                logger.warning("Weakness-ADD signal for %s @ %.2f fired, but capital_allocated=%.2f "
                                "(lot_size=%d) can't afford another lot - SKIPPING the add (position "
                                "stays open at its current size; this does NOT count as a square-off).",
                                tracker.symbol, c, tracker.capital_allocated, tracker.lot_size)
                return
            tracker.total_qty_open += add_qty
            self.trade_book.record_entry(tracker.symbol, now_str, c, entry_type="ADD",
                                          is_backfill=is_backfill, direction=tracker.direction, qty=add_qty)
            logger.info(
                "ADD   %s qty=%d @ %.2f (weakness event #%d: 2 consecutive candles %s VWAP %.2f "
                "with volume > 20-period volume MA(%d) - adding to position, not exiting)%s",
                tracker.symbol, add_qty, c, tracker.weakness_events_on_position, vs_vwap, vwap,
                config.VOLUME_MA_PERIOD, " [BACKFILL]" if is_backfill else "",
            )
            self._maybe_place_live_order(tracker, add_side, c, is_backfill, now_str, add_qty)
        else:
            # A further weakness event after we've already added -> full stop-loss.
            exit_qty = tracker.total_qty_open
            closed_count = self.trade_book.close_all_open(tracker.symbol, now_str, c, exit_is_backfill=is_backfill)
            logger.info(
                "STOP-LOSS / SQUARE OFF %s qty=%d @ %.2f (weakness event #%d: closing all %d open "
                "row(s) for this position - close %s VWAP %.2f, vol > 20-period volume MA(%d) "
                "on both confirming candles)%s",
                tracker.symbol, exit_qty, c, tracker.weakness_events_on_position, closed_count, vs_vwap, vwap,
                config.VOLUME_MA_PERIOD, " [BACKFILL]" if is_backfill else "",
            )
            self._maybe_place_live_order(tracker, exit_side, c, is_backfill, now_str, exit_qty)
            tracker.position_open = False
            tracker.weakness_events_on_position = 0
            tracker.entry_price_for_risk = None
            tracker.total_qty_open = 0

    def _size_order(self, tracker: StockTracker, price: float) -> int:
        """
        Capital-based quantity for ONE order (an entry or an ADD) on `tracker`,
        via position_sizing.calculate_quantity(tracker.capital_allocated, price,
        tracker.lot_size) - see position_sizing.py for the rounding rule. Falls
        back to config.DEFAULT_ORDER_QTY ONLY if that comes back 0 for a reason
        OTHER than "capital can't afford a lot" (i.e. tracker.capital_allocated
        itself is misconfigured as <= 0) - a genuine "can't afford one lot" 0 is
        returned as-is so callers skip the trade instead of silently overspending.
        """
        qty = position_sizing.calculate_quantity(tracker.capital_allocated, price, tracker.lot_size)
        if qty == 0 and tracker.capital_allocated <= 0:
            logger.error("%s has no capital_allocated (%.2f) - falling back to "
                          "config.DEFAULT_ORDER_QTY=%d. Fix capital_allocated instead of relying "
                          "on this fallback.", tracker.symbol, tracker.capital_allocated,
                          config.DEFAULT_ORDER_QTY)
            return config.DEFAULT_ORDER_QTY
        return qty

    def _maybe_place_live_order(self, tracker: StockTracker, side: str, price: float,
                                 is_backfill: bool, now_str: str, qty: int):
        """
        Central gate for EVERY real order this engine could place - entries,
        adds, and weakness-event exits, for BOTH directions, all route
        through here. Two independent, always-checked reasons this no-ops
        instead of touching the broker:

          - config.DRY_RUN is True (the default): paper-trading only,
            unconditionally, regardless of is_backfill.
          - is_backfill is True: this signal was reconstructed from
            historical candle replay (bootstrap.py's catch-up pipeline on
            a late start), not detected live. Placing a fresh order for it
            now would mean transacting at whatever price prevails AT
            CATCH-UP TIME, not the price that actually justified the
            signal when it first fired - "entering late at a stale price."
            The trade is still recorded in the paper trade book either
            way (see the two call sites above) so you can see what the
            engine found; it just never reaches the broker.

        A broker-ADOPTED real position (see adopt_broker_position()) never
        calls this method for its entry at all - there's nothing to place,
        the position already exists for real - but it IS managed by the
        same candle-level weakness detector as any other open position from
        the next candle check onward, and an exit for it routes through
        here exactly like any other live exit (is_backfill=False),
        regardless of config.DRY_RUN.

        `qty` is the caller's already-computed capital-based order size (see
        _size_order) - this method itself has no sizing opinion, it's purely
        a gate plus (once DRY_RUN=False) the actual broker call. `side`
        already encodes BUY vs SELL correctly for both LONG and SHORT
        positions; the raw side Fyers needs is just its first word (e.g.
        "SELL (SHORT)" -> "SELL") - see fyers_service.FyersDataService.place_order.
        """
        if config.DRY_RUN:
            logger.info("[DRY_RUN] Would place %s order: %s qty=%d @ ~%.2f",
                        side, tracker.symbol, qty, price)
            return
        if is_backfill:
            logger.info("[BACKFILL] Skipping live %s order for %s (signal reconstructed from "
                        "history at catch-up, not a live decision) - price has likely moved on.",
                        side, tracker.symbol)
            return
        raw_side = side.split()[0]  # "BUY" or "SELL" - every `side` string above starts with one of these
        try:
            self.data_service.place_order(tracker.symbol, qty, raw_side)
        except Exception as exc:
            # A failed broker call must not crash the whole candle-check loop (other symbols'
            # signals still need evaluating) - it's logged loudly instead; the paper trade_book
            # row above was already recorded either way, so what the engine INTENDED is never
            # lost even if the broker leg itself failed and needs manual reconciliation.
            logger.error("LIVE %s order for %s qty=%d @ ~%.2f FAILED: %s - this position may now "
                         "be out of sync with the broker; check manually.",
                         side, tracker.symbol, qty, price, exc)

    def adopt_broker_position(self, symbol: str, avg_price: float, qty: int, now_str: str) -> bool:
        """
        Adopts a position the broker reports as ALREADY open for `symbol`
        into this engine's local tracking. Used by bootstrap.py after a
        late or crashed-and-restarted start, so a REAL position never goes
        unmanaged just because the process that originally opened it isn't
        the process running now. Returns True if adopted, False if `symbol`
        isn't one this engine is tracking (logged, not raised - a broker
        position outside today's shortlist is a real thing you should know
        about, but this engine has no opinion on it).

        `qty` follows the broker's own sign convention (Fyers: positive
        netQty = long, negative netQty = short) - the ADOPTED direction is
        taken from that sign directly, which can differ from
        tracker.direction (the direction THIS engine was built to trade
        today from sentiment). Broker truth always wins: if the broker says
        you're short a stock this engine was built to trade long today (or
        vice versa - e.g. a manual trade, or yesterday's position that
        didn't get squared off), the tracker's direction is corrected to
        match reality, because managing a real position under the wrong
        direction's mirrored weakness-event logic would be actively
        dangerous, not just inconsistent.

        Unlike a normal entry, this does NOT go through
        _maybe_place_live_order (there is nothing to place - the position
        already exists). From this point on it's managed exactly like any
        other open position: the next candle check (job_check_signals,
        every 5 minutes) runs the same weakness-event detector against it.
        There is no faster, tick-level check any more - see the module
        docstring - so a real, broker-adopted position's first protection
        after being adopted is up to CANDLE_INTERVAL_MINUTES away, not
        immediate. Know that trade-off before relying on this in production.
        """
        tracker = self.trackers.get(symbol)
        if tracker is None:
            logger.warning("Broker reports an open position in %s, which isn't in today's "
                            "shortlist - NOT adopting it into this engine's tracking (out of "
                            "scope here). You still hold it for real; manage it separately.",
                            symbol)
            return False

        actual_direction = TradeDirection.SHORT if qty < 0 else TradeDirection.LONG
        if actual_direction != tracker.direction:
            logger.warning("Broker position in %s is %s (qty=%d), but this engine was tracking "
                            "it as %s today - correcting to match the broker's real position.",
                            symbol, actual_direction.value, qty, tracker.direction.value)
            tracker.direction = actual_direction

        tracker.position_open = True
        tracker.entry_price_for_risk = avg_price
        tracker.weakness_events_on_position = 0
        tracker.consecutive_weak = 0
        tracker.total_qty_open = abs(qty)  # broker's own reported size, not a fresh capital-based calc
        self.trade_book.record_entry(symbol, now_str, avg_price, entry_type="BROKER_SYNC",
                                      is_backfill=True, direction=tracker.direction, qty=abs(qty))
        logger.warning("ADOPTED real broker %s position: %s qty=%d avg_price=%.2f - now managed by "
                        "the candle-level weakness detector from the next check onward (every %d "
                        "min; no tick-level protection - see adopt_broker_position's docstring).",
                        tracker.direction.value, symbol, qty, avg_price, config.CANDLE_INTERVAL_MINUTES)
        return True

    def square_off_all(self, at_time_str: str | None = None):
        """
        Force-close any still-open paper positions (initial + any adds) at
        the last known close, direction-aware (BUY to cover a SHORT, SELL
        to exit a LONG).

        at_time_str defaults to the real current wall-clock time, which is
        correct for live use (main.py's EOD job calls this with no
        argument). backtest.py passes the replayed day's own last-seen
        candle time instead - without this, a backtest would stamp every
        exit with whenever the backtest script happened to actually run,
        not the historical trading day being replayed.

        Always routes through _maybe_place_live_order with is_backfill=False:
        a real EOD square-off is a live, necessary action in production (get
        out before close, no exceptions), not a reconstructed historical
        one - config.DRY_RUN is what actually gates whether it touches the
        broker, same as everywhere else.
        """
        now_str = at_time_str or datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%H:%M:%S")
        for symbol, tracker in self.trackers.items():
            if tracker.position_open and tracker.candles:
                last_close = tracker.candles[-1][4]
                exit_qty = tracker.total_qty_open
                closed_count = self.trade_book.close_all_open(symbol, now_str, last_close, exit_is_backfill=False)
                exit_side = "BUY" if tracker.direction == TradeDirection.SHORT else "SELL"
                logger.info("EOD SQUARE-OFF %s qty=%d @ %.2f (%d row(s) closed) [%s]",
                            symbol, exit_qty, last_close, closed_count, tracker.direction.value)
                self._maybe_place_live_order(tracker, f"{exit_side} (EOD square-off)", last_close,
                                              is_backfill=False, now_str=now_str, qty=exit_qty)
                tracker.position_open = False
                tracker.weakness_events_on_position = 0
                tracker.entry_price_for_risk = None
                tracker.total_qty_open = 0
