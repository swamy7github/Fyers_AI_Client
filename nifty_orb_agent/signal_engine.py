"""
signal_engine.py
------------------
Tracks the shortlisted stocks from the opening-range scan candle by candle
(every 5 minutes) until 15:10, looking for:

  BUY  - two CONSECUTIVE 5-min candles that each close:
           (a) above the opening-range high (the 09:15 candle's high), AND
           (b) above the running session VWAP, AND
           (c) with volume > the 20-period volume moving average.
         Entry is taken at the close of the 2nd confirming candle.

         Because two candles are required *after* the 09:15 one (09:20-09:25
         and 09:25-09:30), the earliest possible entry is at the close of
         the 09:30 candle - which is exactly the "wait till 9:30 candle
         closed" checkpoint in the brief.

  WEAKNESS EVENT (shared detector for both the add-on and the stop-loss) -
         two CONSECUTIVE candles that each close below VWAP AND each have
         volume above a fast 2-period volume average (SMA(2)). This is
         intentionally quicker to react than the 20-period average used for
         entries: getting into a trade should need sustained conviction,
         getting out (or adding) should react fast.

         - 1st weakness event on an open position (no square-off has fired
           yet) -> BUY MORE: add to the position at that candle's close,
           instead of exiting. Up to config.MAX_POSITION_ADDS times.
         - Any weakness event after that (or immediately, if
           MAX_POSITION_ADDS = 0) -> STOP-LOSS / SQUARE OFF: close the
           ENTIRE position (the original entry plus every add-on) at that
           candle's close.

  EOD  - any position still open at SQUARE_OFF_TIME (15:10:30) is closed at
         the last traded price, so nothing is left open overnight.

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
from fyers_service import FyersDataService

logger = logging.getLogger("orb_agent.signals_engine")


@dataclass
class StockTracker:
    symbol: str
    or_high: float  # opening range (09:15 candle) high - the breakout level
    candles: list = field(default_factory=list)  # raw OHLCV history for the day
    cum_pv: float = 0.0   # cumulative typical_price * volume, for VWAP
    cum_vol: float = 0.0

    # -- entry (buy) tracking: needs sustained conviction, so a slow 20-period volume MA --
    volume_window: deque = field(default_factory=lambda: deque(maxlen=config.VOLUME_MA_PERIOD))
    consecutive_qualifying: int = 0
    position_open: bool = False

    # -- weakness (add-on / stop-loss) tracking: reacts fast, so a 2-period volume MA --
    volume_window_fast: deque = field(default_factory=lambda: deque(maxlen=config.VOLUME_SMA_STOPLOSS_PERIOD))
    consecutive_weak: int = 0
    weakness_events_on_position: int = 0  # how many confirmed weakness events since the last flat state

    def vwap(self) -> float:
        return self.cum_pv / self.cum_vol if self.cum_vol else 0.0

    def volume_ma(self) -> Optional[float]:
        """20-period volume MA used for BUY entries. None until the window is full -
        we skip signals rather than fake an average with too little history."""
        if len(self.volume_window) < config.VOLUME_MA_PERIOD:
            return None
        return sum(self.volume_window) / len(self.volume_window)

    def volume_sma_fast(self) -> Optional[float]:
        """2-period volume MA used for the weakness/stop-loss detector. Same None-until-full rule."""
        if len(self.volume_window_fast) < config.VOLUME_SMA_STOPLOSS_PERIOD:
            return None
        return sum(self.volume_window_fast) / len(self.volume_window_fast)


class TradeBook:
    """
    Records every entry/exit and derives points captured (profit/loss) and
    P&L%. Each BUY (the original entry, or a later add-on) is its own row,
    tagged via `entry_type` ("INITIAL" or "ADD") so the position-sizing
    history stays visible in the printed trade book. A square-off closes
    every still-open row for that symbol at the same exit price/time, since
    the whole position (initial + adds) always exits together.
    """

    def __init__(self):
        self.trades: list[dict] = []

    def record_entry(self, symbol: str, entry_time: str, entry_price: float, entry_type: str = "INITIAL"):
        self.trades.append({
            "symbol": symbol, "entry_type": entry_type,
            "entry_time": entry_time, "entry_price": entry_price,
            "exit_time": None, "exit_price": None, "points": None, "pnl_pct": None,
        })

    def close_all_open(self, symbol: str, exit_time: str, exit_price: float) -> int:
        """Closes every unmatched (still-open) row for `symbol` at once. Returns how many rows were closed."""
        closed_count = 0
        for trade in self.trades:
            if trade["symbol"] == symbol and trade["exit_time"] is None:
                trade["exit_time"] = exit_time
                trade["exit_price"] = exit_price
                trade["points"] = round(exit_price - trade["entry_price"], 2)
                trade["pnl_pct"] = round((exit_price - trade["entry_price"]) / trade["entry_price"] * 100, 2)
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
            "win_rate_pct": round(float((closed["points"] > 0).mean() * 100), 2) if not closed.empty else 0.0,
        }


class SignalEngine:
    def __init__(self, data_service: FyersDataService, shortlist_df: pd.DataFrame):
        self.data_service = data_service
        self.trackers = {
            row["symbol"]: StockTracker(symbol=row["symbol"], or_high=row["high"])
            for _, row in shortlist_df.iterrows()
        }
        self.trade_book = TradeBook()

    def _fetch_latest_candle(self, symbol: str, window_start_epoch: int, window_end_epoch: int):
        candles = self.data_service.get_5min_candle(
            symbol, datetime.now().strftime("%Y-%m-%d"), window_start_epoch, window_end_epoch
        )
        return candles[0] if candles else None

    def check_all(self, window_start_epoch: int, window_end_epoch: int):
        """Called every 5 minutes by the scheduler between ~09:25 and 15:10."""
        now_str = datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%H:%M:%S")
        for symbol, tracker in self.trackers.items():
            try:
                candle = self._fetch_latest_candle(symbol, window_start_epoch, window_end_epoch)
                if not candle:
                    continue
                self._update_tracker_and_check_signal(tracker, candle, now_str)
            except Exception as exc:
                logger.error("Signal check failed for %s: %s", symbol, exc)

    def feed_candle(self, symbol: str, candle: list, now_str: str):
        """
        Public entry point for feeding ONE already-fetched candle for ONE
        symbol directly, bypassing check_all()'s live data fetch entirely.

        This exists so backtest.py can replay historical candles through
        exactly the same signal logic used live, without either duplicating
        that logic or reaching into a private method. If you change the
        strategy in this file, both live trading and any backtest built on
        this method pick up the change automatically - there's only one
        place the rules live.
        """
        tracker = self.trackers.get(symbol)
        if tracker is None:
            raise KeyError(f"{symbol!r} is not one of this engine's tracked symbols "
                            f"({list(self.trackers)}).")
        self._update_tracker_and_check_signal(tracker, candle, now_str)

    def _update_tracker_and_check_signal(self, tracker: StockTracker, candle: list, now_str: str):
        _, o, h, l, c, v = candle
        typical_price = (h + l + c) / 3
        tracker.candles.append(candle)
        tracker.cum_pv += typical_price * v
        tracker.cum_vol += v

        vwap = tracker.vwap()

        # Both volume averages are computed BEFORE this candle joins their window, so each
        # is a trailing average that excludes the current bar - matches "volume > its average".
        vol_ma_slow = tracker.volume_ma()
        vol_ma_fast = tracker.volume_sma_fast()
        tracker.volume_window.append(v)
        tracker.volume_window_fast.append(v)

        if tracker.position_open:
            self._check_weakness_and_act(tracker, c, vwap, v, vol_ma_fast, now_str)
            return

        # ---- BUY check (only while flat) ----
        qualifies = (vol_ma_slow is not None) and (c > tracker.or_high) and (c > vwap) and (v > vol_ma_slow)
        tracker.consecutive_qualifying = tracker.consecutive_qualifying + 1 if qualifies else 0

        if tracker.consecutive_qualifying >= config.CONSECUTIVE_CANDLES_FOR_BUY:
            tracker.position_open = True
            tracker.weakness_events_on_position = 0
            tracker.consecutive_weak = 0
            self.trade_book.record_entry(tracker.symbol, now_str, c, entry_type="INITIAL")
            logger.info("BUY   %s @ %.2f (2 consecutive candles above OR high %.2f, VWAP & volume MA)",
                         tracker.symbol, c, tracker.or_high)
            tracker.consecutive_qualifying = 0  # reset so it doesn't immediately re-fire

    def _check_weakness_and_act(self, tracker: StockTracker, c: float, vwap: float, v: float,
                                 vol_ma_fast: Optional[float], now_str: str):
        """
        Runs only while a position is open. Detects the shared "weakness event"
        (2 consecutive candles closing below VWAP with volume > volume SMA(2))
        and either adds to the position or squares it off, per
        config.MAX_POSITION_ADDS - see the module docstring for the full rule.
        """
        is_weak_candle = (vol_ma_fast is not None) and (c < vwap) and (v > vol_ma_fast)
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
            self.trade_book.record_entry(tracker.symbol, now_str, c, entry_type="ADD")
            logger.info(
                "ADD   %s @ %.2f (weakness event #%d: 2 consecutive candles below VWAP %.2f "
                "with volume > fast volSMA(%d) - adding to position, not exiting)",
                tracker.symbol, c, tracker.weakness_events_on_position, vwap,
                config.VOLUME_SMA_STOPLOSS_PERIOD,
            )
        else:
            # A further weakness event after we've already added -> full stop-loss.
            closed_count = self.trade_book.close_all_open(tracker.symbol, now_str, c)
            logger.info(
                "STOP-LOSS / SQUARE OFF %s @ %.2f (weakness event #%d: closing all %d open "
                "row(s) for this position - close < VWAP %.2f, vol > fast volSMA(%d) on both "
                "confirming candles)",
                tracker.symbol, c, tracker.weakness_events_on_position, closed_count, vwap,
                config.VOLUME_SMA_STOPLOSS_PERIOD,
            )
            tracker.position_open = False
            tracker.weakness_events_on_position = 0

    def square_off_all(self, at_time_str: str | None = None):
        """
        Force-close any still-open paper positions (initial + any adds) at
        the last known close.

        at_time_str defaults to the real current wall-clock time, which is
        correct for live use (main.py's EOD job calls this with no
        argument). backtest.py passes the replayed day's own last-seen
        candle time instead - without this, a backtest would stamp every
        exit with whenever the backtest script happened to actually run,
        not the historical trading day being replayed.
        """
        now_str = at_time_str or datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%H:%M:%S")
        for symbol, tracker in self.trackers.items():
            if tracker.position_open and tracker.candles:
                last_close = tracker.candles[-1][4]
                closed_count = self.trade_book.close_all_open(symbol, now_str, last_close)
                logger.info("EOD SQUARE-OFF %s @ %.2f (%d row(s) closed)", symbol, last_close, closed_count)
                tracker.position_open = False
                tracker.weakness_events_on_position = 0
