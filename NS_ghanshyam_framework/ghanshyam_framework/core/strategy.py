"""Ghanshyam opening-range breakout strategy for index and commodity options.

The strategy works on the UNDERLYING's 5-minute candles and emits one of
``BUY_CE``, ``BUY_PE``, ``EXIT`` or ``HOLD``. Option contract selection (strike,
expiry, Fyers symbol) and position-level risk checks live here too so that the
backtester and the paper trader share exactly the same logic.

Rules implemented (from the publicly described 09:20 setup, scaled to percent):

1. The first N 5-minute candles of the session (default 1, i.e. 09:15-09:20)
   define the opening range.
2. Skip the day if the open gaps too far from the previous close.
3. Only take a breakout early in the session (default: first 30 minutes after
   the range forms). A 5-minute close above the range high -> BUY_CE; below the
   range low -> BUY_PE. Only the first (fresh) breakout of each side counts.
4. Stop = the opposite side of the range, but never tighter than a minimum
   distance; if the range is very wide the minimum distance is used instead.
5. Target = fixed percentage of spot. Stop moves to cost after a favourable
   move; the trade is dropped if it stalls; everything is closed at square-off.

The original rules are discretionary and incomplete. Treat every threshold in
``StrategyConfig`` as a hypothesis to backtest, not as the "true" Ghanshyam rule.
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Mapping, Optional, Sequence, Tuple

import pandas as pd

from config.settings import (CANDLE_MINUTES, EXPIRY_OVERRIDES, HOLIDAYS, InstrumentConfig,
                             RiskConfig, StrategyConfig)

LOGGER = logging.getLogger(__name__)

MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
WEEKLY_MONTH_CODE = {m: str(m) for m in range(1, 10)} | {10: "O", 11: "N", 12: "D"}


class Signal(str, Enum):
    """Signal states returned by the strategy."""

    BUY_CE = "BUY_CE"
    BUY_PE = "BUY_PE"
    EXIT = "EXIT"
    HOLD = "HOLD"


@dataclass(frozen=True)
class SignalResult:
    """A signal plus the levels and explanation that came with it."""

    signal: Signal
    reason: str = ""
    ref_spot: Optional[float] = None
    stop_spot: Optional[float] = None
    target_spot: Optional[float] = None


@dataclass
class Position:
    """Underlying-level state of an open trade (mutated as bars arrive)."""

    side: Signal                    # BUY_CE or BUY_PE
    entry_time: datetime
    entry_spot: float
    stop_spot: float
    target_spot: float
    bars_held: int = 0
    mfe_pct: float = 0.0            # best favourable move so far, % of entry
    stop_at_cost: bool = False

    @property
    def is_call(self) -> bool:
        return self.side == Signal.BUY_CE


@dataclass(frozen=True)
class ExitEvent:
    """An intrabar stop/target hit: why, and the underlying price it happened at."""

    reason: str
    spot: float


@dataclass(frozen=True)
class OpeningRange:
    """High/low of the opening candles and when the range completed."""

    high: float
    low: float
    end_time: datetime

    @property
    def width(self) -> float:
        return self.high - self.low


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=span, adjust=False).mean()


def vwap(frame: pd.DataFrame) -> pd.Series:
    """Session VWAP. Falls back to a running mean of the typical price when the
    feed carries no volume (index candles usually have none)."""
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    volume = frame["volume"]
    if volume.sum() > 0:
        return (typical * volume).cumsum() / volume.cumsum()
    return typical.expanding().mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI. Returns 100 when there are no losses and 50 for a flat series."""
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    out = 100 - 100 / (1 + gain / loss)
    out = out.where(loss != 0, 100.0)
    return out.where(~((loss == 0) & (gain == 0)), 50.0)


# --------------------------------------------------------------------------- #
# Contract selection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptionContract:
    """A specific option contract to trade."""

    instrument: str
    strike: float
    option_type: str          # "CE" or "PE"
    expiry: date
    symbol: str


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    return last_day - timedelta(days=(last_day.weekday() - weekday) % 7)


def _previous_trading_day(day: date, holidays: Sequence[date]) -> date:
    while day.weekday() >= 5 or day in holidays:
        day -= timedelta(days=1)
    return day


class ExpiryResolver:
    """Finds the nearest option expiry on or after a date.

    Uses explicit calendars from ``EXPIRY_OVERRIDES`` when present, otherwise the
    instrument's weekday rule (weekly, or last-weekday-of-month), shifted back to
    the previous trading day when it falls on a weekend or holiday.
    """

    def __init__(self, instrument: InstrumentConfig,
                 overrides: Optional[Mapping[str, Sequence[date]]] = None,
                 holidays: Optional[Sequence[date]] = None) -> None:
        self.instrument = instrument
        self.overrides = EXPIRY_OVERRIDES if overrides is None else overrides
        self.holidays = list(HOLIDAYS if holidays is None else holidays)

    def next_expiry(self, on: date) -> date:
        """Nearest expiry >= ``on`` (an expiry day itself counts)."""
        explicit = sorted(self.overrides.get(self.instrument.name, []))
        for candidate in explicit:
            if candidate >= on:
                return candidate
        weekday = self.instrument.expiry_weekday
        if self.instrument.expiry_cycle == "weekly":
            raw = on + timedelta(days=(weekday - on.weekday()) % 7)
            for week in range(8):
                adjusted = _previous_trading_day(raw + timedelta(weeks=week), self.holidays)
                if adjusted >= on:
                    return adjusted
        else:
            year, month = on.year, on.month
            for _ in range(14):
                adjusted = _previous_trading_day(
                    _last_weekday_of_month(year, month, weekday), self.holidays)
                if adjusted >= on:
                    return adjusted
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        raise ValueError(f"Could not resolve expiry for {self.instrument.name} on {on}")

    def is_monthly(self, expiry: date) -> bool:
        """True if ``expiry`` is the month's final expiry (uses the monthly symbol format)."""
        if self.instrument.expiry_cycle == "monthly":
            return True
        last = _previous_trading_day(
            _last_weekday_of_month(expiry.year, expiry.month, self.instrument.expiry_weekday),
            self.holidays)
        return expiry == last


def build_option_symbol(instrument: InstrumentConfig, expiry: date, strike: float,
                        option_type: str, monthly: bool) -> str:
    """Build a Fyers option symbol, e.g. ``NSE:BANKNIFTY26OCT55000CE`` (monthly)
    or ``NSE:NIFTY26O2724000PE`` (weekly: YY + month code + DD).

    Always check generated symbols against the Fyers symbol master before
    trusting them for real quotes.
    """
    strike_text = str(int(strike)) if float(strike).is_integer() else f"{strike:g}"
    yy = f"{expiry.year % 100:02d}"
    if monthly:
        body = f"{yy}{MONTH_ABBR[expiry.month - 1]}"
    else:
        body = f"{yy}{WEEKLY_MONTH_CODE[expiry.month]}{expiry.day:02d}"
    return f"{instrument.option_prefix}{body}{strike_text}{option_type}"


def resolve_underlying_symbol(instrument: InstrumentConfig, on: date) -> str:
    """Fill ``{yy}``/``{mon}`` placeholders (MCX futures) for the month of ``on``."""
    template = instrument.underlying_symbol
    if "{" not in template:
        return template
    return template.format(yy=f"{on.year % 100:02d}", mon=MONTH_ABBR[on.month - 1])


class StrikeSelector:
    """Chooses the strike from spot: ATM, or ITM/OTM by a number of strike steps."""

    def __init__(self, instrument: InstrumentConfig, mode: str = "ATM", steps: int = 0) -> None:
        if mode not in {"ATM", "ITM", "OTM"}:
            raise ValueError("mode must be ATM, ITM or OTM")
        self.instrument = instrument
        self.mode = mode
        self.steps = steps

    def atm(self, spot: float) -> float:
        step = self.instrument.strike_step
        return float(int(spot / step + 0.5) * step)

    def select(self, spot: float, side: Signal) -> float:
        """Strike for a CE (``BUY_CE``) or PE (``BUY_PE``) at ``spot``."""
        if side not in (Signal.BUY_CE, Signal.BUY_PE):
            raise ValueError(f"No strike for signal {side}")
        atm = self.atm(spot)
        shift = self.steps * self.instrument.strike_step
        if self.mode == "ATM" or shift == 0:
            return atm
        into_money = -shift if side == Signal.BUY_CE else shift   # ITM direction
        return atm + into_money if self.mode == "ITM" else atm - into_money

    def contract(self, spot: float, side: Signal, on: date,
                 resolver: ExpiryResolver) -> OptionContract:
        """Full contract (strike + nearest expiry + symbol) for a signal."""
        strike = self.select(spot, side)
        expiry = resolver.next_expiry(on)
        option_type = "CE" if side == Signal.BUY_CE else "PE"
        symbol = build_option_symbol(self.instrument, expiry, strike, option_type,
                                     resolver.is_monthly(expiry))
        return OptionContract(self.instrument.name, strike, option_type, expiry, symbol)


# --------------------------------------------------------------------------- #
# Risk management
# --------------------------------------------------------------------------- #
@dataclass
class DayRiskState:
    """Running risk counters for one trading day."""

    trades: int = 0
    realized_pnl: float = 0.0
    consecutive_losses: int = 0


class RiskManager:
    """Daily loss limits, trade-count limits and position sizing."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def can_enter(self, state: DayRiskState) -> Tuple[bool, str]:
        """Whether a new trade is allowed, with the blocking reason if not."""
        cfg = self.config
        if state.trades >= cfg.max_trades_per_day:
            return False, f"max trades per day reached ({cfg.max_trades_per_day})"
        if state.realized_pnl <= -abs(cfg.max_daily_loss_inr):
            return False, f"daily loss limit hit ({state.realized_pnl:,.0f})"
        if state.consecutive_losses >= cfg.max_consecutive_losses:
            return False, f"{state.consecutive_losses} consecutive losses"
        return True, ""

    def register_entry(self, state: DayRiskState) -> None:
        state.trades += 1

    def register_exit(self, state: DayRiskState, net_pnl: float) -> None:
        """Update counters after a trade closes."""
        state.realized_pnl += net_pnl
        state.consecutive_losses = state.consecutive_losses + 1 if net_pnl < 0 else 0

    def lots_for(self, entry_spot: float, stop_spot: float, lot_size: int) -> int:
        """Number of lots to trade.

        With ``risk_per_trade_inr`` set, lots are sized so the estimated loss at
        the stop (distance x ``assumed_delta`` x lot size) fits the budget; 0 means
        even one lot is too risky. Otherwise the fixed ``lots`` setting is used.
        """
        cfg = self.config
        if cfg.risk_per_trade_inr is None:
            return max(1, min(cfg.lots, cfg.max_lots))
        loss_per_lot = abs(entry_spot - stop_spot) * cfg.assumed_delta * lot_size
        if loss_per_lot <= 0:
            return 0
        return max(0, min(int(cfg.risk_per_trade_inr // loss_per_lot), cfg.max_lots))


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
class GhanshyamStrategy:
    """Stateless signal generator; position state is passed in by the caller."""

    def __init__(self, instrument: InstrumentConfig,
                 config: Optional[StrategyConfig] = None) -> None:
        self.instrument = instrument
        self.config = config or StrategyConfig()
        self.minutes = CANDLE_MINUTES

    # ---- opening range ------------------------------------------------------
    def opening_range(self, day_df: pd.DataFrame) -> Optional[OpeningRange]:
        """Opening range of the day, or None if the opening candles are missing."""
        n = self.config.opening_range_candles
        if len(day_df) < n:
            return None
        if day_df.index[0].time() != self.instrument.session_open:
            LOGGER.debug("First candle %s is not the session open", day_df.index[0])
            return None
        head = day_df.iloc[:n]
        end_time = head.index[-1].to_pydatetime() + timedelta(minutes=self.minutes)
        return OpeningRange(float(head["high"].max()), float(head["low"].min()), end_time)

    # ---- main entry point -----------------------------------------------------
    def evaluate(self, day_df: pd.DataFrame, prev_close: Optional[float] = None,
                 position: Optional[Position] = None) -> SignalResult:
        """Evaluate the latest COMPLETED candle in ``day_df`` (today's bars only)."""
        if day_df.empty:
            return SignalResult(Signal.HOLD, "no data")
        candle_end = day_df.index[-1].to_pydatetime() + timedelta(minutes=self.minutes)
        if position is not None:
            return self._manage_position(day_df, position, candle_end)
        return self._look_for_entry(day_df, prev_close, candle_end)

    # ---- exits ----------------------------------------------------------------
    def _manage_position(self, day_df: pd.DataFrame, position: Position,
                         candle_end: datetime) -> SignalResult:
        cfg = self.config
        close = float(day_df["close"].iloc[-1])
        if candle_end.time() >= self.instrument.square_off:
            return SignalResult(Signal.EXIT, "square_off", ref_spot=close)
        # Close-based backstop; intrabar hits are caught by check_intrabar().
        if position.is_call:
            if close <= position.stop_spot:
                return SignalResult(Signal.EXIT, self._stop_reason(position), ref_spot=close)
            if close >= position.target_spot:
                return SignalResult(Signal.EXIT, "target", ref_spot=close)
        else:
            if close >= position.stop_spot:
                return SignalResult(Signal.EXIT, self._stop_reason(position), ref_spot=close)
            if close <= position.target_spot:
                return SignalResult(Signal.EXIT, "target", ref_spot=close)
        if position.bars_held >= cfg.stall_candles and position.mfe_pct < cfg.stall_min_progress_pct:
            return SignalResult(Signal.EXIT, "stalled", ref_spot=close)
        return SignalResult(Signal.HOLD, "in position", ref_spot=close)

    @staticmethod
    def _stop_reason(position: Position) -> str:
        return "breakeven_stop" if position.stop_at_cost else "stop_loss"

    def check_intrabar(self, position: Position, bar: Mapping[str, float]) -> Optional[ExitEvent]:
        """Check one bar (or a single tick shaped as a bar) against stop and target.

        The stop is tested first, so when a bar touches both levels the loss is
        assumed to come first (conservative). A bar that opens beyond a level is
        filled at its open, not at the level.
        """
        o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
        reason = self._stop_reason(position)
        if position.is_call:
            if o <= position.stop_spot:
                return ExitEvent(reason, o)
            if l <= position.stop_spot:
                return ExitEvent(reason, position.stop_spot)
            if o >= position.target_spot:
                return ExitEvent("target", o)
            if h >= position.target_spot:
                return ExitEvent("target", position.target_spot)
        else:
            if o >= position.stop_spot:
                return ExitEvent(reason, o)
            if h >= position.stop_spot:
                return ExitEvent(reason, position.stop_spot)
            if o <= position.target_spot:
                return ExitEvent("target", o)
            if l <= position.target_spot:
                return ExitEvent("target", position.target_spot)
        return None

    def update_position(self, position: Position, bar: Mapping[str, float]) -> None:
        """Update bars held, best excursion and the move-to-cost stop after a bar."""
        position.bars_held += 1
        if position.is_call:
            favourable = (float(bar["high"]) - position.entry_spot) / position.entry_spot * 100
        else:
            favourable = (position.entry_spot - float(bar["low"])) / position.entry_spot * 100
        position.mfe_pct = max(position.mfe_pct, favourable)
        if not position.stop_at_cost and position.mfe_pct >= self.config.trail_to_cost_pct:
            if position.is_call:
                position.stop_spot = max(position.stop_spot, position.entry_spot)
            else:
                position.stop_spot = min(position.stop_spot, position.entry_spot)
            position.stop_at_cost = True

    def open_position(self, result: SignalResult, entry_time: datetime) -> Position:
        """Create the position state for an entry signal."""
        if result.signal not in (Signal.BUY_CE, Signal.BUY_PE) or result.ref_spot is None \
                or result.stop_spot is None or result.target_spot is None:
            raise ValueError("open_position needs an entry SignalResult with levels")
        return Position(result.signal, entry_time, result.ref_spot,
                        result.stop_spot, result.target_spot)

    # ---- entries --------------------------------------------------------------
    def _look_for_entry(self, day_df: pd.DataFrame, prev_close: Optional[float],
                        candle_end: datetime) -> SignalResult:
        cfg = self.config
        n = cfg.opening_range_candles
        rng = self.opening_range(day_df)
        if rng is None:
            return SignalResult(Signal.HOLD, "opening range unavailable")
        if len(day_df) <= n:
            return SignalResult(Signal.HOLD, "building opening range")

        first_open = float(day_df["open"].iloc[0])
        if prev_close:
            gap_pct = abs(first_open - prev_close) / prev_close * 100
            if gap_pct > cfg.max_gap_pct:
                return SignalResult(Signal.HOLD, f"gap {gap_pct:.2f}% exceeds {cfg.max_gap_pct}%")

        last_ts = day_df.index[-1].to_pydatetime()
        window_end = (datetime.combine(last_ts.date(), self.instrument.session_open)
                      + timedelta(minutes=n * self.minutes + cfg.entry_window_minutes))
        if last_ts >= window_end:
            return SignalResult(Signal.HOLD, "entry window closed")
        if candle_end.time() >= self.instrument.square_off:
            return SignalResult(Signal.HOLD, "too close to square-off")

        after = day_df.iloc[n:]
        last = after.iloc[-1]
        prior = after.iloc[:-1]
        up_col, down_col = ("close", "close") if cfg.confirm_on_close else ("high", "low")
        broke_up = float(last[up_col]) > rng.high
        broke_down = float(last[down_col]) < rng.low
        if broke_up and broke_down:
            return SignalResult(Signal.HOLD, "ambiguous bar broke both sides")
        if not (broke_up or broke_down):
            return SignalResult(Signal.HOLD, "inside opening range")
        if broke_up and (prior[up_col] > rng.high).any():
            return SignalResult(Signal.HOLD, "stale breakout (up)")
        if broke_down and (prior[down_col] < rng.low).any():
            return SignalResult(Signal.HOLD, "stale breakout (down)")

        side = Signal.BUY_CE if broke_up else Signal.BUY_PE
        blocked = self._filter_block(day_df, side)
        if blocked:
            return SignalResult(Signal.HOLD, blocked)

        spot = float(last["close"])
        stop, target = self._levels(spot, rng, side)
        word = "above" if broke_up else "below"
        level = rng.high if broke_up else rng.low
        return SignalResult(side, f"close {word} opening range {level:.2f}",
                            ref_spot=spot, stop_spot=stop, target_spot=target)

    def _levels(self, spot: float, rng: OpeningRange, side: Signal) -> Tuple[float, float]:
        """Stop and target for an entry at ``spot``."""
        cfg = self.config
        min_dist = spot * cfg.min_sl_pct / 100
        too_wide = rng.width > spot * cfg.large_range_pct / 100
        target_dist = spot * cfg.target_pct / 100
        if side == Signal.BUY_CE:
            stop = spot - min_dist if too_wide else min(rng.low, spot - min_dist)
            return stop, spot + target_dist
        stop = spot + min_dist if too_wide else max(rng.high, spot + min_dist)
        return stop, spot - target_dist

    def _filter_block(self, day_df: pd.DataFrame, side: Signal) -> str:
        """Optional VWAP / EMA / RSI confirmation. Returns a reason if blocked."""
        cfg = self.config
        close = day_df["close"]
        bullish = side == Signal.BUY_CE
        if cfg.use_vwap_filter:
            level = float(vwap(day_df).iloc[-1])
            if (bullish and close.iloc[-1] <= level) or (not bullish and close.iloc[-1] >= level):
                return "blocked by VWAP filter"
        if cfg.use_ema_filter:
            fast, slow = ema(close, cfg.ema_fast).iloc[-1], ema(close, cfg.ema_slow).iloc[-1]
            if (bullish and fast <= slow) or (not bullish and fast >= slow):
                return "blocked by EMA filter"
        if cfg.use_rsi_filter:
            value = float(rsi(close, cfg.rsi_period).iloc[-1])
            if (bullish and value < cfg.rsi_bull) or (not bullish and value > cfg.rsi_bear):
                return f"blocked by RSI filter ({value:.1f})"
        return ""
