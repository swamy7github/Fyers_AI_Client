"""Domain models shared by every layer of ``ghanshyam_options``.

All timestamps are *naive* ``datetime`` objects expressed in IST (Asia/Kolkata).
The feed layer is responsible for converting broker epochs to IST once; nothing
downstream ever touches time zones again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class Exchange(str, Enum):
    NSE = "NSE"
    MCX = "MCX"


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class Bias(str, Enum):
    """Directional bias derived from the LDCHL / PDC filter (guide p3)."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TARGET = "TARGET"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_STOP = "TIME_STOP"
    SQUARE_OFF = "SQUARE_OFF"
    RISK_LOCKOUT = "RISK_LOCKOUT"
    MANUAL = "MANUAL"


@dataclass(frozen=True)
class Candle:
    """One closed OHLCV bar."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: Optional[float] = None

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_green(self) -> bool:
        return self.close > self.open

    @property
    def is_red(self) -> bool:
        return self.close < self.open

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass
class DayLevels:
    """Pre-market structural map (guide p2, p9): LDCHL plus opening range."""

    trade_date: date
    pdh: float
    pdl: float
    pdc: float
    prev_open: Optional[float] = None
    opening_range_high: Optional[float] = None
    opening_range_low: Optional[float] = None
    support_levels: List[float] = field(default_factory=list)
    resistance_levels: List[float] = field(default_factory=list)
    daily_atr: Optional[float] = None
    is_expiry_day: bool = False

    @property
    def opening_range_ready(self) -> bool:
        return self.opening_range_high is not None and self.opening_range_low is not None

    def all_supports(self) -> List[float]:
        levels = [self.pdl, self.pdc] + list(self.support_levels)
        if self.opening_range_low is not None:
            levels.append(self.opening_range_low)
        return levels

    def all_resistances(self) -> List[float]:
        levels = [self.pdh, self.pdc] + list(self.resistance_levels)
        if self.opening_range_high is not None:
            levels.append(self.opening_range_high)
        return levels


@dataclass
class Signal:
    """A fully specified trade idea emitted by a strategy at the close of a bar.

    ``spot_level`` is the underlying level at which the entry triggers (stop
    entry at a pattern extreme); ``stop_loss`` / ``target`` are underlying
    levels unless ``target_premium_pct`` is set (premium-based target, used by
    the 15:00 squeeze).
    """

    instrument: str
    strategy: str
    option_type: OptionType
    ts: datetime
    spot_level: float
    stop_loss: float
    target: Optional[float]
    reason: str
    confidence: float
    components: Dict[str, Optional[float]] = field(default_factory=dict)
    timeframe: int = 5
    max_hold_minutes: Optional[int] = None
    target_premium_pct: Optional[float] = None
    strike_mode: Optional[str] = None
    counter_trend: bool = False
    bar_ts: Optional[datetime] = None
    entry_type: str = "stop"          # "stop": fill when spot_level is touched; "market": fill at next bar open
    fill_same_bar: bool = False       # stop entry already breached inside the signal bar (09:20 setup)

    @property
    def risk_points(self) -> float:
        return abs(self.spot_level - self.stop_loss)

    @property
    def reward_points(self) -> Optional[float]:
        if self.target is None:
            return None
        return abs(self.target - self.spot_level)

    @property
    def planned_r(self) -> Optional[float]:
        rp = self.reward_points
        if rp is None or self.risk_points <= 0:
            return None
        return rp / self.risk_points


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    exchange: Exchange
    option_type: OptionType
    strike: float
    expiry: date
    lot_size: int
    tick_size: float

    @property
    def expiry_str(self) -> str:
        return self.expiry.isoformat()


@dataclass
class Quote:
    symbol: str
    ltp: float
    ts: Optional[datetime] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[float] = None
    oi: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Fill:
    symbol: str
    side: str  # BUY / SELL
    qty: int
    price: float
    ts: datetime
    order_id: str
    slippage: float = 0.0
    charges: float = 0.0


@dataclass
class Position:
    """An open long-option position managed by the engine."""

    position_id: str
    contract: OptionContract
    signal: Signal
    lots: int
    qty: int
    entry_ts: datetime
    entry_price: float
    entry_spot: float
    spot_sl: float
    spot_target: Optional[float]
    premium_sl: Optional[float]
    premium_target: Optional[float]
    premium_model: str
    entry_charges: float = 0.0
    entry_slippage: float = 0.0
    initial_risk_points: float = 0.0
    best_spot: float = 0.0
    worst_spot: float = 0.0
    trailing_active: bool = False
    breakeven_set: bool = False
    bars_held: int = 0
    max_favourable_premium: float = 0.0
    max_adverse_premium: float = 0.0
    risk_budget: float = 0.0
    notes: str = ""

    @property
    def option_type(self) -> OptionType:
        return self.contract.option_type

    @property
    def is_call(self) -> bool:
        return self.contract.option_type == OptionType.CE

    def spot_move(self, spot: float) -> float:
        """Favourable underlying move in points (positive = in our favour)."""
        return (spot - self.entry_spot) if self.is_call else (self.entry_spot - spot)

    def r_at_spot(self, spot: float) -> float:
        if self.initial_risk_points <= 0:
            return 0.0
        return self.spot_move(spot) / self.initial_risk_points


@dataclass
class Trade:
    """A closed trade: exactly one row of the trade log CSV."""

    trade_id: str
    mode: str
    instrument: str
    underlying: str
    strategy: str
    signal_ts: datetime
    direction: str
    option_symbol: str
    strike: float
    expiry: str
    entry_ts: datetime
    entry_price: float
    qty: int
    lots: int
    sl: float
    target: Optional[float]
    exit_ts: datetime
    exit_price: float
    exit_reason: str
    gross_pnl: float
    charges: float
    net_pnl: float
    r_multiple: Optional[float]
    confidence: float
    premium_model: str
    entry_spot: float
    exit_spot: float
    spot_sl: float
    spot_target: Optional[float]
    planned_r: Optional[float]
    slippage: float
    max_favourable_premium: float
    max_adverse_premium: float
    hold_minutes: float
    notes: str = ""

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0


@dataclass
class DailySummary:
    trade_date: date
    instrument: str
    mode: str
    trades: int
    wins: int
    losses: int
    gross_pnl: float
    charges: float
    net_pnl: float
    peak_profit: float
    max_intraday_drawdown: float
    consecutive_losses: int
    lockout_reason: str
    signals_generated: int
    signals_rejected: int
    equity_end: float
