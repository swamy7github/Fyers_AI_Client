"""
Domain Models, Data Classes, and Enums
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class Direction(Enum):
    """Directional trend bias relative to market structure."""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class OptionType(Enum):
    """Option contract direction."""
    CE = "CE"  # Call Option
    PE = "PE"  # Put Option


class StrategyId(Enum):
    """Identifiers for Ghanshyam Tech price action setups."""
    STRATEGY_920_AM = "9:20_AM_OPENING_RANGE"
    STRATEGY_220_PM = "2:20_PM_MOMENTUM_EXPANSION"
    INSIDE_CANDLE = "INSIDE_CANDLE_COMPRESSION"
    HAMMER_REVERSAL = "BULLISH_HAMMER_REVERSAL"
    SHOOTING_STAR = "BEARISH_SHOOTING_STAR_REVERSAL"


@dataclass(frozen=True)
class Candle:
    """
    Standardized OHLCV 5-minute candlestick representation with calculated geometry.
    """
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def range(self) -> float:
        """Total high-to-low price span."""
        return self.high - self.low

    @property
    def body(self) -> float:
        """Absolute body size between open and close."""
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        """Upper shadow length."""
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        """Lower shadow length."""
        return min(self.open, self.close) - self.low

    @property
    def is_green(self) -> bool:
        """True if closing price is >= opening price."""
        return self.close >= self.open

    @property
    def is_red(self) -> bool:
        """True if closing price is < opening price."""
        return self.close < self.open


@dataclass
class LDCHLLevels:
    """
    Last Day Close, High, Low (LDCHL) reference benchmarks.
    Used for macro trend alignment and trap avoidance.
    """
    pdh: float  # Previous Day High
    pdl: float  # Previous Day Low
    pdc: float  # Previous Day Close
    daily_trend: Direction = Direction.BULLISH


@dataclass
class TradeSignal:
    """
    Signal emitted by strategy modules when a valid setup confirms.
    """
    timestamp: datetime
    strategy: StrategyId
    underlying_code: str
    option_type: OptionType
    spot_price: float
    spot_stop_loss: float
    spot_target: float
    reason: str
    is_trend_aligned: bool = True


@dataclass
class Position:
    """
    Tracks active or closed option positions with entry/exit timestamps, reasons, and pricing.
    """
    position_id: str
    symbol: str
    underlying_code: str
    option_type: OptionType
    strike: float
    quantity: int
    entry_price: float
    entry_time: datetime
    stop_loss: float
    target: float
    spot_entry: float
    spot_sl: float
    spot_target: float
    strategy: StrategyId
    entry_reason: str = ""
    exit_reason: str = ""
    active: bool = True
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: float = 0.0
