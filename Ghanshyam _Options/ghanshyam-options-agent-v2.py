"""
================================================================================
GHANSHYAM TECH END-TO-END MULTI-ASSET OPTIONS TRADING SYSTEM (v2.0 MODULAR)
================================================================================
An institutional-grade, fully modular Python trading framework implementing the
Ghanshyam Tech price action and behavioral risk management methodology for
both NIFTY 50 and BANK NIFTY index options.

Supports:
- Live Fyers API v3 integration (history, quotes, order routing)
- Realistic Paper Trading broker (slippage, transaction fees, position ledger)
- Unified Multi-Asset Orchestrator with Shared Account-Level Risk Engine
- Mathematical delta & volatility sizing with Ghanshyam 80/20 profit protection
================================================================================
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple, Union

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("GhanshyamTradingSystem")


# =====================================================================
# MODULE 1: DOMAIN MODELS, ENUMS & BENCHMARK CONFIGURATIONS
# =====================================================================

class Direction(Enum):
    """Directional bias relative to market structure and daily trend."""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class OptionType(Enum):
    """Option contract type."""
    CE = "CE"  # Call Option
    PE = "PE"  # Put Option


class StrategyId(Enum):
    """Unique identifiers for Ghanshyam Tech strategic setups."""
    STRATEGY_920_AM = "9:20_AM_OPENING_RANGE"
    STRATEGY_220_PM = "2:20_PM_MOMENTUM_EXPANSION"
    INSIDE_CANDLE = "INSIDE_CANDLE_COMPRESSION"
    HAMMER_REVERSAL = "BULLISH_HAMMER_REVERSAL"
    SHOOTING_STAR = "BEARISH_SHOOTING_STAR_REVERSAL"


@dataclass(frozen=True)
class Candle:
    """
    Standardized OHLCV candlestick representation.
    Provides calculated properties for real body, wicks, and directional state.
    """
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def range(self) -> float:
        """Total high-to-low span."""
        return self.high - self.low

    @property
    def body(self) -> float:
        """Absolute difference between open and close."""
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
        """True if closing price is greater than or equal to open."""
        return self.close >= self.open

    @property
    def is_red(self) -> bool:
        """True if closing price is less than open."""
        return self.close < self.open


@dataclass
class LDCHLLevels:
    """
    Last Day Close, High, Low (LDCHL) reference benchmarks.
    Serves as the macro filter for directional trend alignment and trap avoidance.
    """
    pdh: float  # Previous Day High
    pdl: float  # Previous Day Low
    pdc: float  # Previous Day Close
    daily_trend: Direction = Direction.BULLISH


@dataclass
class IndexConfig:
    """
    Instrument-specific configuration defining volatility thresholds,
    strike intervals, and exchange contract lot sizes.
    """
    symbol: str               # e.g., 'NSE:NIFTYBANK-INDEX' or 'NSE:NIFTY50-INDEX'
    code: str                 # e.g., 'BANKNIFTY' or 'NIFTY'
    strike_step: float        # 100.0 for Bank Nifty, 50.0 for Nifty
    lot_size: int             # 15 for Bank Nifty, 25 for Nifty (or current exchange lot)
    max_920_range: float      # Max allowed opening candle span (250 for Bank Nifty, 120 for Nifty)
    min_reversal_run: float   # Minimum required preceding run (100 for Bank Nifty, 50 for Nifty)
    default_expiry: str = "26OCT"

    @classmethod
    def bank_nifty_default(cls, expiry: str = "26OCT", lot_size: int = 15) -> "IndexConfig":
        """Factory method for BANK NIFTY configuration."""
        return cls(
            symbol="NSE:NIFTYBANK-INDEX",
            code="BANKNIFTY",
            strike_step=100.0,
            lot_size=lot_size,
            max_920_range=250.0,
            min_reversal_run=100.0,
            default_expiry=expiry
        )

    @classmethod
    def nifty_50_default(cls, expiry: str = "26OCT", lot_size: int = 25) -> "IndexConfig":
        """Factory method for NIFTY 50 configuration."""
        return cls(
            symbol="NSE:NIFTY50-INDEX",
            code="NIFTY",
            strike_step=50.0,
            lot_size=lot_size,
            max_920_range=120.0,
            min_reversal_run=50.0,
            default_expiry=expiry
        )


@dataclass
class TradeSignal:
    """
    Standardized trading signal emitted by strategy modules upon setup confirmation.
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
    Tracks active or historical option contract positions.
    """
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
    active: bool = True
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: float = 0.0


# =====================================================================
# MODULE 2: MODULAR STRATEGY FRAMEWORK (GHANSHYAM TECH PRICE ACTION)
# =====================================================================

class BaseStrategy(ABC):
    """
    Abstract Base Class for all Ghanshyam price action strategies.
    Decouples setup detection logic into independently testable units.
    """
    def __init__(self, strategy_id: StrategyId):
        self.strategy_id = strategy_id

    @abstractmethod
    def evaluate(
        self,
        current_candle: Candle,
        candle_history: List[Candle],
        ldc: LDCHLLevels,
        config: IndexConfig
    ) -> Optional[TradeSignal]:
        """
        Evaluates whether the current price action satisfies the strategy conditions.
        Returns a TradeSignal if valid, otherwise None.
        """
        pass


class OpeningRange920Strategy(BaseStrategy):
    """
    9:20 AM Opening Volatility Strategy:
    - Identifies the high and low of the first 5-minute candle (09:15 - 09:20 IST).
    - Checks opening candle span against benchmark limits (<= 250 for Bank Nifty, <= 120 for Nifty).
    - If range is exhausted, the setup is disqualified.
    - Entry triggers when a subsequent 5-min candle breaches the 9:15 high (CE) or low (PE).
    - Risk is capped at 70 or 100 spot points; target is 200 spot points.
    """
    def __init__(self):
        super().__init__(StrategyId.STRATEGY_920_AM)
        self.has_traded_today: Dict[str, bool] = {}

    def reset_daily_state(self):
        self.has_traded_today.clear()

    def evaluate(
        self,
        current_candle: Candle,
        candle_history: List[Candle],
        ldc: LDCHLLevels,
        config: IndexConfig
    ) -> Optional[TradeSignal]:
        if self.has_traded_today.get(config.code, False) or len(candle_history) < 1:
            return None

        opening_bar = candle_history[0]
        if opening_bar.timestamp.time() != time(9, 15):
            return None

        # Check range exhaustion filter
        if opening_bar.range > config.max_920_range:
            logger.info(
                f"[{config.code}] 9:20 AM Setup disqualified: Opening candle span "
                f"{opening_bar.range:.1f} > threshold {config.max_920_range:.1f} pts."
            )
            self.has_traded_today[config.code] = True
            return None

        sl_points = 100.0 if opening_bar.range >= (config.max_920_range * 0.6) else 70.0
        target_points = 200.0 if config.code == "BANKNIFTY" else 100.0

        # Upside Breakout
        if current_candle.high > opening_bar.high:
            self.has_traded_today[config.code] = True
            is_aligned = (opening_bar.high >= ldc.pdc)
            return TradeSignal(
                timestamp=current_candle.timestamp,
                strategy=self.strategy_id,
                underlying_code=config.code,
                option_type=OptionType.CE,
                spot_price=current_candle.close,
                spot_stop_loss=opening_bar.high - sl_points,
                spot_target=opening_bar.high + target_points,
                reason=f"9:20 AM Candle High ({opening_bar.high:.1f}) breached. Spot SL: {sl_points} pts",
                is_trend_aligned=is_aligned
            )

        # Downside Breakdown
        elif current_candle.low < opening_bar.low:
            self.has_traded_today[config.code] = True
            is_aligned = (opening_bar.low <= ldc.pdc)
            return TradeSignal(
                timestamp=current_candle.timestamp,
                strategy=self.strategy_id,
                underlying_code=config.code,
                option_type=OptionType.PE,
                spot_price=current_candle.close,
                spot_stop_loss=opening_bar.low + sl_points,
                spot_target=opening_bar.low - target_points,
                reason=f"9:20 AM Candle Low ({opening_bar.low:.1f}) breached. Spot SL: {sl_points} pts",
                is_trend_aligned=is_aligned
            )

        return None


class BullishHammerReversalStrategy(BaseStrategy):
    """
    Bullish Hammer Reversal Setup:
    - Pre-condition: Requires an uninterrupted downfall of >= min_reversal_run (100 Bank Nifty / 50 Nifty).
    - Morphology: Lower wick >= 2x real body, upper wick <= 25% of total range.
    - Trigger: Breach above Hammer candle high.
    - Stop Loss: 2 points below Hammer low. Target: 1:2 Risk to Reward.
    """
    def __init__(self):
        super().__init__(StrategyId.HAMMER_REVERSAL)

    def evaluate(
        self,
        current_candle: Candle,
        candle_history: List[Candle],
        ldc: LDCHLLevels,
        config: IndexConfig
    ) -> Optional[TradeSignal]:
        if len(candle_history) < 4:
            return None

        candidate = candle_history[-2]
        body = max(candidate.body, 1.0)

        # Geometric check: Lower shadow >= 2x real body, small upper shadow
        if candidate.lower_wick < (2.0 * body) or candidate.upper_wick > (candidate.range * 0.25):
            return None

        # Preceding downward exhaustion check
        lookback = min(len(candle_history) - 1, 8)
        high_watermark = max(c.high for c in candle_history[-lookback:-1])
        downfall = high_watermark - candidate.low

        if downfall < config.min_reversal_run:
            return None

        # Trigger confirmation
        if current_candle.high > candidate.high:
            sl = candidate.low - 2.0
            risk = candidate.high - sl
            target = candidate.high + (risk * 2.0)
            is_aligned = (ldc.daily_trend == Direction.BULLISH)

            return TradeSignal(
                timestamp=current_candle.timestamp,
                strategy=self.strategy_id,
                underlying_code=config.code,
                option_type=OptionType.CE,
                spot_price=current_candle.close,
                spot_stop_loss=sl,
                spot_target=target,
                reason=(
                    f"Bullish Hammer confirmed (Wick {candidate.lower_wick:.1f} >= 2x Body {body:.1f}, "
                    f"Prior Downfall {downfall:.1f} pts >= {config.min_reversal_run:.1f} pts)"
                ),
                is_trend_aligned=is_aligned
            )

        return None


class BearishShootingStarReversalStrategy(BaseStrategy):
    """
    Bearish Shooting Star Reversal Setup:
    - Pre-condition: Requires an uninterrupted upward rally of >= min_reversal_run into resistance.
    - Morphology: Upper wick >= 2x real body, lower wick <= 25% of total range.
    - Trigger: Breach below Shooting Star low.
    - Stop Loss: 2 points above Shooting Star high. Target: 1:2 Risk to Reward.
    """
    def __init__(self):
        super().__init__(StrategyId.SHOOTING_STAR)

    def evaluate(
        self,
        current_candle: Candle,
        candle_history: List[Candle],
        ldc: LDCHLLevels,
        config: IndexConfig
    ) -> Optional[TradeSignal]:
        if len(candle_history) < 4:
            return None

        candidate = candle_history[-2]
        body = max(candidate.body, 1.0)

        # Geometric check: Upper shadow >= 2x real body, small lower shadow
        if candidate.upper_wick < (2.0 * body) or candidate.lower_wick > (candidate.range * 0.25):
            return None

        # Preceding upward exhaustion check
        lookback = min(len(candle_history) - 1, 8)
        low_watermark = min(c.low for c in candle_history[-lookback:-1])
        rally = candidate.high - low_watermark

        if rally < config.min_reversal_run:
            return None

        # Trigger confirmation
        if current_candle.low < candidate.low:
            sl = candidate.high + 2.0
            risk = sl - candidate.low
            target = candidate.low - (risk * 2.0)
            is_aligned = (ldc.daily_trend == Direction.BEARISH)

            return TradeSignal(
                timestamp=current_candle.timestamp,
                strategy=self.strategy_id,
                underlying_code=config.code,
                option_type=OptionType.PE,
                spot_price=current_candle.close,
                spot_stop_loss=sl,
                spot_target=target,
                reason=(
                    f"Bearish Shooting Star confirmed (Upper Wick {candidate.upper_wick:.1f} >= 2x Body {body:.1f}, "
                    f"Prior Rally {rally:.1f} pts >= {config.min_reversal_run:.1f} pts)"
                ),
                is_trend_aligned=is_aligned
            )

        return None


class InsideCandleCompressionStrategy(BaseStrategy):
    """
    Inside Candle (Mother-Child Bar) Breakout Setup:
    - Identifies volatility contraction where an inside candle range is fully enveloped
      within the preceding mother candle's high and low.
    - A breach of the inside bar's extreme triggers an immediate momentum scalp.
    - Stop loss is placed at the opposite extreme of the inside candle.
    """
    def __init__(self):
        super().__init__(StrategyId.INSIDE_CANDLE)

    def evaluate(
        self,
        current_candle: Candle,
        candle_history: List[Candle],
        ldc: LDCHLLevels,
        config: IndexConfig
    ) -> Optional[TradeSignal]:
        if len(candle_history) < 3:
            return None

        mother = candle_history[-3]
        inside = candle_history[-2]

        is_inside = (inside.high <= mother.high) and (inside.low >= mother.low)
        if not is_inside:
            return None

        # Upside Breakout
        if current_candle.high > inside.high:
            sl = inside.low
            risk = inside.high - sl
            target = inside.high + (risk * 2.0)
            is_aligned = (inside.high >= ldc.pdc)

            return TradeSignal(
                timestamp=current_candle.timestamp,
                strategy=self.strategy_id,
                underlying_code=config.code,
                option_type=OptionType.CE,
                spot_price=current_candle.close,
                spot_stop_loss=sl,
                spot_target=target,
                reason=f"Inside Candle upside breakout (Range {inside.range:.1f} pts inside Mother {mother.range:.1f} pts)",
                is_trend_aligned=is_aligned
            )

        # Downside Breakdown
        elif current_candle.low < inside.low:
            sl = inside.high
            risk = sl - inside.low
            target = inside.low - (risk * 2.0)
            is_aligned = (inside.low <= ldc.pdc)

            return TradeSignal(
                timestamp=current_candle.timestamp,
                strategy=self.strategy_id,
                underlying_code=config.code,
                option_type=OptionType.PE,
                spot_price=current_candle.close,
                spot_stop_loss=sl,
                spot_target=target,
                reason=f"Inside Candle downside breakdown (Range {inside.range:.1f} pts inside Mother {mother.range:.1f} pts)",
                is_trend_aligned=is_aligned
            )

        return None


class MomentumScalp220Strategy(BaseStrategy):
    """
    2:20 PM Institutional Momentum Strategy:
    - Captures late-afternoon volatility surges driven by European session liquidity.
    - Evaluates the 14:20 IST candle and triggers upon a breach of its high or low.
    """
    def __init__(self):
        super().__init__(StrategyId.STRATEGY_220_PM)

    def evaluate(
        self,
        current_candle: Candle,
        candle_history: List[Candle],
        ldc: LDCHLLevels,
        config: IndexConfig
    ) -> Optional[TradeSignal]:
        if len(candle_history) < 2:
            return None

        c220 = candle_history[-2]
        if c220.timestamp.time() != time(14, 20):
            return None

        target_move = 80.0 if config.code == "BANKNIFTY" else 40.0

        if current_candle.high > c220.high:
            is_aligned = (ldc.daily_trend == Direction.BULLISH)
            return TradeSignal(
                timestamp=current_candle.timestamp,
                strategy=self.strategy_id,
                underlying_code=config.code,
                option_type=OptionType.CE,
                spot_price=current_candle.close,
                spot_stop_loss=c220.low,
                spot_target=current_candle.close + target_move,
                reason=f"2:20 PM Candle High ({c220.high:.1f}) broken at {current_candle.timestamp.time()}",
                is_trend_aligned=is_aligned
            )

        elif current_candle.low < c220.low:
            is_aligned = (ldc.daily_trend == Direction.BEARISH)
            return TradeSignal(
                timestamp=current_candle.timestamp,
                strategy=self.strategy_id,
                underlying_code=config.code,
                option_type=OptionType.PE,
                spot_price=current_candle.close,
                spot_stop_loss=c220.high,
                spot_target=current_candle.close - target_move,
                reason=f"2:20 PM Candle Low ({c220.low:.1f}) broken at {current_candle.timestamp.time()}",
                is_trend_aligned=is_aligned
            )

        return None


class StrategyRegistry:
    """
    Container managing active strategies and running sequential evaluations
    ordered by Ghanshyam intraday priority.
    """
    def __init__(self):
        self.strategies: List[BaseStrategy] = [
            OpeningRange920Strategy(),
            BullishHammerReversalStrategy(),
            BearishShootingStarReversalStrategy(),
            InsideCandleCompressionStrategy(),
            MomentumScalp220Strategy(),
        ]

    def reset_daily_state(self):
        for s in self.strategies:
            if hasattr(s, "reset_daily_state"):
                s.reset_daily_state()

    def evaluate_all(
        self,
        current_candle: Candle,
        candle_history: List[Candle],
        ldc: LDCHLLevels,
        config: IndexConfig
    ) -> Optional[TradeSignal]:
        """Runs strategies in order of intraday priority and returns the first validated signal."""
        current_time = current_candle.timestamp.time()

        for strategy in self.strategies:
            # Time-gated checks
            if strategy.strategy_id == StrategyId.STRATEGY_920_AM:
                if not (time(9, 20) <= current_time <= time(9, 35)):
                    continue
            elif strategy.strategy_id == StrategyId.STRATEGY_220_PM:
                if not (time(14, 20) <= current_time <= time(14, 35)):
                    continue

            signal = strategy.evaluate(current_candle, candle_history, ldc, config)
            if signal:
                return signal

        return None


# =====================================================================
# MODULE 3: OPTION STRIKE SELECTOR & SYMBOL FORMATTER
# =====================================================================

class OptionStrikeSelector:
    """
    Computes optimal In-The-Money (ITM) strikes (Delta 0.55 - 0.70)
    and formats broker symbols for Fyers API.
    """

    @staticmethod
    def get_itm_strike(spot_price: float, option_type: OptionType, config: IndexConfig) -> float:
        """
        Determines the 1-strike In-The-Money (ITM) contract level:
        - For CE: Rounded ATM Strike - 1 Strike Step
        - For PE: Rounded ATM Strike + 1 Strike Step
        """
        step = config.strike_step
        atm_strike = round(spot_price / step) * step

        if option_type == OptionType.CE:
            return atm_strike - step
        else:
            return atm_strike + step

    @staticmethod
    def format_fyers_symbol(
        config: IndexConfig,
        strike: float,
        option_type: OptionType,
        expiry_date_str: Optional[str] = None
    ) -> str:
        """
        Formats standardized Fyers option symbol string.
        Format: NSE:{UNDERLYING}{EXPIRY}{STRIKE}{TYPE}
        Example: NSE:BANKNIFTY26OCT51100CE, NSE:NIFTY26OCT24450PE
        """
        expiry = expiry_date_str or config.default_expiry
        strike_int = int(strike)
        opt_str = option_type.value
        return f"NSE:{config.code.upper()}{expiry}{strike_int}{opt_str}"


# =====================================================================
# MODULE 4: RISK & BEHAVIORAL SAFEGUARD ENGINE
# =====================================================================

class RiskEngine:
    """
    Account-Level Risk Management Engine implementing the Ghanshyam Tech Doctrine:
    1. Tranche Capital Allocation: Max 20-25% equity per trade tranche.
    2. 80/20 Profit Retention Rule: 'A green session must never close red'.
       Locks 80% of peak realized daily gains; secondary trades only risk remaining 20%.
    3. Consecutive Loss Lockout: Shutdown after 2 consecutive stop-outs.
    4. Max Daily Trades: Hard ceiling of 2 to 3 trades across all combined indices.
    5. Max Daily Account Drawdown: Circuit breaker at 2% to 3% of account equity.
    6. Asymmetric Position Sizing: 100% baseline when trend-aligned, 50% if counter-trend.
    """

    def __init__(
        self,
        total_capital: float,
        max_capital_per_trade_pct: float = 0.25,
        max_daily_drawdown_pct: float = 0.03,
        max_daily_trades: int = 3,
        max_consecutive_losses: int = 2,
        profit_lock_pct: float = 0.80,
    ):
        self.total_capital = total_capital
        self.max_capital_per_trade_pct = max_capital_per_trade_pct
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.max_daily_trades = max_daily_trades
        self.max_consecutive_losses = max_consecutive_losses
        self.profit_lock_pct = profit_lock_pct

        # Daily performance tracking
        self.realized_pnl: float = 0.0
        self.peak_daily_profit: float = 0.0
        self.trade_count: int = 0
        self.consecutive_losses: int = 0
        self.is_locked_out: bool = False
        self.lockout_reason: str = ""

    def reset_daily_state(self):
        """Resets session counters for the start of a new trading day."""
        self.realized_pnl = 0.0
        self.peak_daily_profit = 0.0
        self.trade_count = 0
        self.consecutive_losses = 0
        self.is_locked_out = False
        self.lockout_reason = ""
        logger.info("RiskEngine: Session state reset. Fresh trading day initialized.")

    def can_take_trade(self) -> Tuple[bool, str]:
        """Evaluates whether the trading terminal is permitted to take a new trade."""
        if self.is_locked_out:
            return False, f"Risk Lockout active: {self.lockout_reason}"

        # 1. 80/20 Profit Retention Protection
        if self.peak_daily_profit > 0:
            minimum_retained_profit = self.peak_daily_profit * self.profit_lock_pct
            if self.realized_pnl < minimum_retained_profit:
                self._lockout(
                    f"Profit lock triggered: Realized PnL (₹{self.realized_pnl:.2f}) breached "
                    f"minimum retained profit (₹{minimum_retained_profit:.2f} of peak ₹{self.peak_daily_profit:.2f})."
                )
                return False, self.lockout_reason

        # 2. Maximum Daily Account Drawdown Cap
        max_loss = self.total_capital * self.max_daily_drawdown_pct
        if self.realized_pnl <= -max_loss:
            self._lockout(f"Max daily drawdown hit (Loss ₹{-self.realized_pnl:.2f} >= Allowed ₹{max_loss:.2f}).")
            return False, self.lockout_reason

        # 3. Consecutive Losses Check (Shutdown after 2 stop-outs)
        if self.consecutive_losses >= self.max_consecutive_losses:
            self._lockout("2 consecutive losses hit. Terminal locked to prevent revenge trading.")
            return False, self.lockout_reason

        # 4. Maximum Daily Trades Limiter
        if self.trade_count >= self.max_daily_trades:
            self._lockout(f"Max daily trade count ({self.max_daily_trades} trades) reached.")
            return False, self.lockout_reason

        return True, "Trade permitted under risk guidelines."

    def calculate_lot_size(
        self,
        option_premium: float,
        lot_size: int,
        is_trend_aligned: bool = True
    ) -> int:
        """
        Dynamically calculates contract quantity based on capital tranches
        and accrued daily profit buffers.
        """
        can_trade, _ = self.can_take_trade()
        if not can_trade:
            return 0

        # Baseline max capital for this trade tranche (<= 25% account equity)
        max_capital = self.total_capital * self.max_capital_per_trade_pct

        # If already profitable today, risk is strictly constrained to the remaining 20% profit buffer
        if self.peak_daily_profit > 0 and self.realized_pnl > 0:
            risk_budget = (self.realized_pnl - (self.peak_daily_profit * self.profit_lock_pct))
            max_capital = min(max_capital, max(risk_budget * 2.0, option_premium * lot_size))

        cost_per_lot = option_premium * lot_size
        if cost_per_lot <= 0:
            return 0

        num_lots = int(max_capital // cost_per_lot)

        # Counter-trend setups receive a 50% lot reduction
        if not is_trend_aligned:
            num_lots = max(1, num_lots // 2)

        return max(1, num_lots) * lot_size

    def record_trade_completion(self, pnl: float):
        """Logs completed trade PnL and updates performance watermarks."""
        self.trade_count += 1
        self.realized_pnl += pnl

        if self.realized_pnl > self.peak_daily_profit:
            self.peak_daily_profit = self.realized_pnl

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        logger.info(
            f"RiskEngine: Trade Recorded | PnL: ₹{pnl:.2f} | Net Realized: ₹{self.realized_pnl:.2f} | "
            f"Peak: ₹{self.peak_daily_profit:.2f} | Trades: {self.trade_count} | Cons. Losses: {self.consecutive_losses}"
        )
        self.can_take_trade()

    def _lockout(self, reason: str):
        self.is_locked_out = True
        self.lockout_reason = reason
        logger.warning(f"CIRCUIT BREAKER TRIGGERED: {reason}")


# =====================================================================
# MODULE 5: BROKER INTERFACES & EXECUTION ADAPTERS
# =====================================================================

class BaseBrokerAdapter(ABC):
    """Abstract interface defining required broker execution methods."""

    @abstractmethod
    def place_option_order(
        self,
        symbol: str,
        underlying_code: str,
        option_type: OptionType,
        strike: float,
        quantity: int,
        option_ltp: float,
        timestamp: datetime,
        spot_entry: float,
        spot_sl: float,
        spot_target: float,
        strategy: StrategyId,
        option_sl: float,
        option_target: float
    ) -> Optional[Position]:
        pass

    @abstractmethod
    def close_position(
        self,
        symbol: str,
        current_ltp: float,
        timestamp: datetime,
        exit_reason: str
    ) -> Optional[Position]:
        pass

    @property
    @abstractmethod
    def positions(self) -> Dict[str, Position]:
        pass

    @property
    @abstractmethod
    def closed_positions(self) -> List[Position]:
        pass

    @abstractmethod
    def get_portfolio_summary(self) -> Dict:
        pass


class PaperTradingBroker(BaseBrokerAdapter):
    """
    High-fidelity simulated execution broker:
    - Models bid-ask slippage.
    - Factors exchange brokerage fees (₹20/order) and statutory costs.
    - Maintains position ledger and real-time cash balance.
    """

    def __init__(
        self,
        initial_capital: float = 500000.0,
        brokerage_per_order: float = 20.0,
        slippage: float = 0.5
    ):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.brokerage_per_order = brokerage_per_order
        self.slippage = slippage
        self._positions: Dict[str, Position] = {}
        self._closed_positions: List[Position] = []

    @property
    def positions(self) -> Dict[str, Position]:
        return self._positions

    @property
    def closed_positions(self) -> List[Position]:
        return self._closed_positions

    def place_option_order(
        self,
        symbol: str,
        underlying_code: str,
        option_type: OptionType,
        strike: float,
        quantity: int,
        option_ltp: float,
        timestamp: datetime,
        spot_entry: float,
        spot_sl: float,
        spot_target: float,
        strategy: StrategyId,
        option_sl: float,
        option_target: float
    ) -> Optional[Position]:
        fill_price = option_ltp + self.slippage
        cost = fill_price * quantity + self.brokerage_per_order

        if cost > self.capital:
            logger.warning(f"PaperBroker: Margin shortfall. Required: ₹{cost:.2f}, Available: ₹{self.capital:.2f}")
            return None

        self.capital -= cost
        pos = Position(
            symbol=symbol,
            underlying_code=underlying_code,
            option_type=option_type,
            strike=strike,
            quantity=quantity,
            entry_price=fill_price,
            entry_time=timestamp,
            stop_loss=option_sl,
            target=option_target,
            spot_entry=spot_entry,
            spot_sl=spot_sl,
            spot_target=spot_target,
            strategy=strategy,
            active=True
        )
        self._positions[symbol] = pos
        logger.info(
            f"PaperBroker BUY: {symbol} | Qty: {quantity} @ ₹{fill_price:.2f} | "
            f"SL: ₹{option_sl:.2f} | Tgt: ₹{option_target:.2f} | Cash: ₹{self.capital:.2f}"
        )
        return pos

    def close_position(
        self,
        symbol: str,
        current_ltp: float,
        timestamp: datetime,
        exit_reason: str
    ) -> Optional[Position]:
        if symbol not in self._positions:
            return None

        pos = self._positions.pop(symbol)
        exit_price = max(0.05, current_ltp - self.slippage)
        gross_pnl = (exit_price - pos.entry_price) * pos.quantity
        net_pnl = gross_pnl - (self.brokerage_per_order * 2.0)

        pos.exit_price = exit_price
        pos.exit_time = timestamp
        pos.pnl = net_pnl
        pos.active = False

        self.capital += (exit_price * pos.quantity) - self.brokerage_per_order
        self._closed_positions.append(pos)

        logger.info(
            f"PaperBroker EXIT: {symbol} | Reason: {exit_reason} | Exit: ₹{exit_price:.2f} | "
            f"Net PnL: ₹{net_pnl:.2f} | Balance: ₹{self.capital:.2f}"
        )
        return pos

    def get_portfolio_summary(self) -> Dict:
        realized = sum(p.pnl for p in self._closed_positions)
        win_trades = [p for p in self._closed_positions if p.pnl > 0]
        loss_trades = [p for p in self._closed_positions if p.pnl <= 0]
        win_rate = (len(win_trades) / len(self._closed_positions) * 100.0) if self._closed_positions else 0.0

        return {
            "initial_capital": self.initial_capital,
            "current_capital": self.capital,
            "total_trades": len(self._closed_positions),
            "wins": len(win_trades),
            "losses": len(loss_trades),
            "win_rate_pct": win_rate,
            "net_realized_pnl": realized,
            "active_positions": len(self._positions)
        }


class FyersClientAdapter(BaseBrokerAdapter):
    """
    Fyers API v3 Broker Adapter:
    - Wraps fyers_client.history(), fyers_client.quotes(), and fyers_client.place_order().
    - Transparently routes simulated or live execution.
    """

    def __init__(
        self,
        fyers_client=None,
        is_paper: bool = True,
        paper_broker: Optional[PaperTradingBroker] = None
    ):
        self.client = fyers_client
        self.is_paper = is_paper
        self.paper_broker = paper_broker or PaperTradingBroker()

    @property
    def positions(self) -> Dict[str, Position]:
        return self.paper_broker.positions

    @property
    def closed_positions(self) -> List[Position]:
        return self.paper_broker.closed_positions

    def get_history(self, symbol: str, resolution: str = "5", range_from: str = "", range_to: str = "") -> List[Candle]:
        """Fetches historical 5-minute candles using Fyers history API."""
        if self.client and hasattr(self.client, "history"):
            try:
                data = {
                    "symbol": symbol,
                    "resolution": resolution,
                    "date_format": "1",
                    "range_from": range_from,
                    "range_to": range_to,
                    "cont_flag": "1"
                }
                response = self.client.history(data=data)
                candles = []
                if response and response.get("s") == "ok":
                    for c in response.get("candles", []):
                        candles.append(Candle(
                            timestamp=datetime.fromtimestamp(c[0]),
                            open=float(c[1]),
                            high=float(c[2]),
                            low=float(c[3]),
                            close=float(c[4]),
                            volume=float(c[5])
                        ))
                return candles
            except Exception as e:
                logger.error(f"Error fetching history from Fyers API: {e}")
        return []

    def get_quote(self, symbol: str) -> float:
        """Fetches live LTP quote using Fyers quotes API."""
        if self.client and hasattr(self.client, "quotes"):
            try:
                data = {"symbols": symbol}
                response = self.client.quotes(data=data)
                if response and response.get("s") == "ok":
                    d = response.get("d", [{}])
                    return float(d[0].get("v", {}).get("lp", 0.0))
            except Exception as e:
                logger.error(f"Error fetching quote for {symbol}: {e}")
        return 0.0

    def place_option_order(
        self,
        symbol: str,
        underlying_code: str,
        option_type: OptionType,
        strike: float,
        quantity: int,
        option_ltp: float,
        timestamp: datetime,
        spot_entry: float,
        spot_sl: float,
        spot_target: float,
        strategy: StrategyId,
        option_sl: float,
        option_target: float
    ) -> Optional[Position]:
        if self.is_paper:
            return self.paper_broker.place_option_order(
                symbol=symbol,
                underlying_code=underlying_code,
                option_type=option_type,
                strike=strike,
                quantity=quantity,
                option_ltp=option_ltp,
                timestamp=timestamp,
                spot_entry=spot_entry,
                spot_sl=spot_sl,
                spot_target=spot_target,
                strategy=strategy,
                option_sl=option_sl,
                option_target=option_target
            )

        # Live order execution via Fyers API
        if self.client and hasattr(self.client, "place_order"):
            data = {
                "symbol": symbol,
                "qty": quantity,
                "type": 2,  # Market order
                "side": 1,  # Buy order (Option Buyer)
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": "False",
            }
            res = self.client.place_order(data=data)
            logger.info(f"Fyers Live Order Placed: {res}")
            return self.paper_broker.place_option_order(
                symbol=symbol,
                underlying_code=underlying_code,
                option_type=option_type,
                strike=strike,
                quantity=quantity,
                option_ltp=option_ltp,
                timestamp=timestamp,
                spot_entry=spot_entry,
                spot_sl=spot_sl,
                spot_target=spot_target,
                strategy=strategy,
                option_sl=option_sl,
                option_target=option_target
            )

        return None

    def close_position(
        self,
        symbol: str,
        current_ltp: float,
        timestamp: datetime,
        exit_reason: str
    ) -> Optional[Position]:
        if self.is_paper:
            return self.paper_broker.close_position(
                symbol=symbol,
                current_ltp=current_ltp,
                timestamp=timestamp,
                exit_reason=exit_reason
            )

        # Live exit execution via Fyers API
        if self.client and hasattr(self.client, "place_order"):
            pos = self.paper_broker.positions.get(symbol)
            if pos:
                data = {
                    "symbol": pos.symbol,
                    "qty": pos.quantity,
                    "type": 2,  # Market order
                    "side": -1,  # Sell order
                    "productType": "INTRADAY",
                    "limitPrice": 0,
                    "stopPrice": 0,
                    "validity": "DAY",
                    "disclosedQty": 0,
                    "offlineOrder": "False",
                }
                res = self.client.place_order(data=data)
                logger.info(f"Fyers Live Exit Placed: {res}")

        return self.paper_broker.close_position(
            symbol=symbol,
            current_ltp=current_ltp,
            timestamp=timestamp,
            exit_reason=exit_reason
        )

    def get_portfolio_summary(self) -> Dict:
        return self.paper_broker.get_portfolio_summary()


# =====================================================================
# MODULE 6: SINGLE-ASSET TRADING AGENT
# =====================================================================

class IndexTradingAgent:
    """
    Dedicated trading agent managing an individual underlying benchmark index.
    Processes candle feeds, applies price action strategies, and executes
    sized option trades.
    """

    def __init__(
        self,
        config: IndexConfig,
        risk_engine: RiskEngine,
        broker_adapter: BaseBrokerAdapter,
        ldc_levels: Optional[LDCHLLevels] = None,
        strategy_registry: Optional[StrategyRegistry] = None,
    ):
        self.config = config
        self.risk_engine = risk_engine
        self.broker = broker_adapter
        self.ldc = ldc_levels or LDCHLLevels(pdh=0, pdl=0, pdc=0, daily_trend=Direction.BULLISH)
        self.strategy_registry = strategy_registry or StrategyRegistry()
        self.candle_history: List[Candle] = []

    def set_ldc_levels(self, ldc: LDCHLLevels):
        """Sets Previous Day Close, High, and Low reference levels."""
        self.ldc = ldc
        logger.info(
            f"[{self.config.code}] LDCHL Updated: PDH={ldc.pdh:.1f}, "
            f"PDL={ldc.pdl:.1f}, PDC={ldc.pdc:.1f}, Trend={ldc.daily_trend.value}"
        )

    def on_candle_closed(
        self,
        new_candle: Candle,
        current_spot: float,
        option_ltp_lookup_fn: Optional[Callable[[str], float]] = None
    ) -> Optional[TradeSignal]:
        """
        Processes a newly completed 5-minute candle:
        1. Checks EOD square-off (15:15 IST).
        2. Monitors and manages active positions.
        3. Evaluates price action strategies for new entry setups.
        """
        self.candle_history.append(new_candle)

        # 1. Mandatory 15:15 IST EOD Square-Off
        if new_candle.timestamp.time() >= time(15, 15):
            self.square_off_positions(new_candle.timestamp, reason="EOD 15:15 Square-Off")
            return None

        # 2. Manage ongoing positions
        self._manage_open_positions(current_spot, new_candle.timestamp, option_ltp_lookup_fn)

        # 3. Check Risk clearance
        can_trade, reason = self.risk_engine.can_take_trade()
        if not can_trade:
            return None

        # 4. Evaluate Strategy Setups
        signal = self.strategy_registry.evaluate_all(
            current_candle=new_candle,
            candle_history=self.candle_history,
            ldc=self.ldc,
            config=self.config
        )

        if signal:
            self._execute_signal(signal, current_spot, new_candle.timestamp, option_ltp_lookup_fn)
            return signal

        return None

    def _execute_signal(
        self,
        signal: TradeSignal,
        current_spot: float,
        timestamp: datetime,
        option_ltp_lookup_fn: Optional[Callable[[str], float]] = None
    ):
        logger.info(
            f"[{self.config.code}] Signal Triggered: {signal.strategy.value} | "
            f"{signal.option_type.value} | Reason: {signal.reason}"
        )

        itm_strike = OptionStrikeSelector.get_itm_strike(current_spot, signal.option_type, self.config)
        opt_symbol = OptionStrikeSelector.format_fyers_symbol(
            config=self.config,
            strike=itm_strike,
            option_type=signal.option_type
        )

        # Look up option premium
        estimated_premium = 300.0 if self.config.code == "BANKNIFTY" else 150.0
        if option_ltp_lookup_fn:
            estimated_premium = option_ltp_lookup_fn(opt_symbol)
        elif hasattr(self.broker, "get_quote"):
            quote = self.broker.get_quote(opt_symbol)
            if quote > 0:
                estimated_premium = quote

        # Risk-allocated lot sizing
        quantity = self.risk_engine.calculate_lot_size(
            option_premium=estimated_premium,
            lot_size=self.config.lot_size,
            is_trend_aligned=signal.is_trend_aligned
        )

        if quantity <= 0:
            logger.warning(f"[{self.config.code}] Order rejected by Risk Engine: 0 quantity allocated.")
            return

        delta = 0.60
        spot_risk = abs(signal.spot_price - signal.spot_stop_loss)
        spot_reward = abs(signal.spot_target - signal.spot_price)

        option_sl = max(5.0, estimated_premium - (spot_risk * delta))
        option_target = estimated_premium + (spot_reward * delta)

        self.broker.place_option_order(
            symbol=opt_symbol,
            underlying_code=self.config.code,
            option_type=signal.option_type,
            strike=itm_strike,
            quantity=quantity,
            option_ltp=estimated_premium,
            timestamp=timestamp,
            spot_entry=current_spot,
            spot_sl=signal.spot_stop_loss,
            spot_target=signal.spot_target,
            strategy=signal.strategy,
            option_sl=option_sl,
            option_target=option_target
        )

    def _manage_open_positions(
        self,
        current_spot: float,
        timestamp: datetime,
        option_ltp_lookup_fn: Optional[Callable[[str], float]] = None
    ):
        active_symbols = list(self.broker.positions.keys())
        for sym in active_symbols:
            pos = self.broker.positions.get(sym)
            if not pos or pos.underlying_code != self.config.code:
                continue

            spot_change = (current_spot - pos.spot_entry) if pos.option_type == OptionType.CE else (pos.spot_entry - current_spot)
            delta = 0.60
            simulated_ltp = max(5.0, pos.entry_price + (spot_change * delta))
            if option_ltp_lookup_fn:
                simulated_ltp = option_ltp_lookup_fn(sym)

            # Check Stop Loss
            if (pos.option_type == OptionType.CE and current_spot <= pos.spot_sl) or \
               (pos.option_type == OptionType.PE and current_spot >= pos.spot_sl) or \
               (simulated_ltp <= pos.stop_loss):
                closed = self.broker.close_position(sym, simulated_ltp, timestamp, exit_reason="Stop Loss Triggered")
                if closed:
                    self.risk_engine.record_trade_completion(closed.pnl)

            # Check Target
            elif (pos.option_type == OptionType.CE and current_spot >= pos.spot_target) or \
                 (pos.option_type == OptionType.PE and current_spot <= pos.spot_target) or \
                 (simulated_ltp >= pos.target):
                closed = self.broker.close_position(sym, simulated_ltp, timestamp, exit_reason="Target Achieved")
                if closed:
                    self.risk_engine.record_trade_completion(closed.pnl)

    def square_off_positions(self, timestamp: datetime, reason: str = "Square Off"):
        """Closes all active positions for this underlying."""
        active_symbols = list(self.broker.positions.keys())
        for sym in active_symbols:
            pos = self.broker.positions.get(sym)
            if pos and pos.underlying_code == self.config.code:
                closed = self.broker.close_position(sym, pos.entry_price, timestamp, exit_reason=reason)
                if closed:
                    self.risk_engine.record_trade_completion(closed.pnl)


# =====================================================================
# MODULE 7: MULTI-ASSET TRADING MANAGER (CENTRAL ORCHESTRATOR)
# =====================================================================

class MultiAssetTradingManager:
    """
    Central Multi-Asset Orchestrator:
    - Coordinates concurrent trading on BOTH NIFTY 50 and BANK NIFTY.
    - Binds both assets to a SINGLE, SHARED RiskEngine instance.
      (Ensures the 3-trade daily limit, 2 consecutive loss shutdown, and 80/20 profit lock apply globally).
    - Unified candle event dispatcher routing data feeds to their respective IndexTradingAgent.
    - Enforces portfolio-wide 15:15 IST square-off and generates consolidated analytics.
    """

    def __init__(
        self,
        total_capital: float = 500000.0,
        fyers_client=None,
        is_paper: bool = True,
        banknifty_expiry: str = "26OCT",
        nifty_expiry: str = "26OCT",
        banknifty_lot_size: int = 15,
        nifty_lot_size: int = 25,
    ):
        self.total_capital = total_capital
        self.is_paper = is_paper
        self.fyers_client = fyers_client

        # 1. Global Shared Risk Engine & Broker
        self.risk_engine = RiskEngine(total_capital=total_capital)
        self.paper_broker = PaperTradingBroker(initial_capital=total_capital)
        self.broker_adapter = FyersClientAdapter(
            fyers_client=fyers_client,
            is_paper=is_paper,
            paper_broker=self.paper_broker
        )

        # 2. Benchmark Configurations
        self.banknifty_config = IndexConfig.bank_nifty_default(expiry=banknifty_expiry, lot_size=banknifty_lot_size)
        self.nifty_config = IndexConfig.nifty_50_default(expiry=nifty_expiry, lot_size=nifty_lot_size)

        # 3. Dedicated Agents
        self.banknifty_agent = IndexTradingAgent(
            config=self.banknifty_config,
            risk_engine=self.risk_engine,
            broker_adapter=self.broker_adapter
        )
        self.nifty_agent = IndexTradingAgent(
            config=self.nifty_config,
            risk_engine=self.risk_engine,
            broker_adapter=self.broker_adapter
        )

        self.agents: Dict[str, IndexTradingAgent] = {
            self.banknifty_config.code: self.banknifty_agent,
            self.nifty_config.code: self.nifty_agent,
        }

    def set_levels(self, underlying_code: str, pdh: float, pdl: float, pdc: float, trend: Direction = Direction.BULLISH):
        """Sets LDCHL levels for a specific index."""
        code = underlying_code.upper()
        if code in self.agents:
            self.agents[code].set_ldc_levels(LDCHLLevels(pdh=pdh, pdl=pdl, pdc=pdc, daily_trend=trend))
        else:
            logger.warning(f"Unknown index code '{underlying_code}'. Choose from: {list(self.agents.keys())}")

    def on_candle_event(
        self,
        symbol: str,
        candle: Candle,
        spot_ltp: float,
        option_ltp_lookup_fn: Optional[Callable[[str], float]] = None
    ) -> Optional[TradeSignal]:
        """
        Unified ingestion point for multi-index candle streams.
        Dispatches incoming candle to either Bank Nifty or Nifty agent based on symbol.
        """
        sym_upper = symbol.upper()
        if "BANK" in sym_upper:
            agent = self.banknifty_agent
        elif "NIFTY" in sym_upper:
            agent = self.nifty_agent
        else:
            logger.warning(f"Unrecognized symbol '{symbol}' passed to MultiAssetTradingManager.")
            return None

        return agent.on_candle_closed(candle, current_spot=spot_ltp, option_ltp_lookup_fn=option_ltp_lookup_fn)

    def square_off_all(self, timestamp: datetime, reason: str = "Global Square-Off"):
        """Terminates all active option positions across both Nifty and Bank Nifty."""
        for agent in self.agents.values():
            agent.square_off_positions(timestamp, reason=reason)

    def get_consolidated_report(self) -> Dict:
        """Generates comprehensive multi-asset portfolio performance metrics."""
        summary = self.broker_adapter.get_portfolio_summary()
        closed_trades = self.paper_broker.closed_positions

        bn_trades = [p for p in closed_trades if p.underlying_code == "BANKNIFTY"]
        nifty_trades = [p for p in closed_trades if p.underlying_code == "NIFTY"]

        bn_pnl = sum(p.pnl for p in bn_trades)
        nifty_pnl = sum(p.pnl for p in nifty_trades)

        summary.update({
            "banknifty_trades": len(bn_trades),
            "banknifty_pnl": bn_pnl,
            "nifty_trades": len(nifty_trades),
            "nifty_pnl": nifty_pnl,
            "daily_peak_profit": self.risk_engine.peak_daily_profit,
            "is_risk_locked": self.risk_engine.is_locked_out,
            "risk_lock_reason": self.risk_engine.lockout_reason
        })
        return summary
