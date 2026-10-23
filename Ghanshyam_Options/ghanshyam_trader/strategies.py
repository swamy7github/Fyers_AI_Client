"""
Ghanshyam Tech Optimized Price Action Strategy Implementations
"""

import logging
from abc import ABC, abstractmethod
from datetime import time
from typing import Dict, List, Optional

from config import IndexConfig, CHOP_ZONE_START, CHOP_ZONE_END
from models import Candle, Direction, LDCHLLevels, OptionType, StrategyId, TradeSignal

logger = logging.getLogger("GhanshyamTradingSystem")


class BaseStrategy(ABC):
    """Abstract Base Class for all price action strategy modules."""
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
        """Evaluates whether current price action satisfies setup conditions."""
        pass


class OpeningRange920Strategy(BaseStrategy):
    """
    9:20 AM Opening Volatility Strategy:
    - Analyzes the opening 5-minute bar (09:15 - 09:20 IST).
    - Rejects setup if opening candle span exceeds max limit (200 pts Bank Nifty / 100 pts Nifty).
    - Triggers on breakout of 09:15 high (CE) or breakdown of low (PE).
    - Requires breakout within morning window (09:20 - 10:30 IST).
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

        current_time = current_candle.timestamp.time()
        if not (time(9, 20) <= current_time <= time(10, 30)):
            return None

        opening_bar = candle_history[0]
        if opening_bar.timestamp.time() != time(9, 15):
            return None

        if opening_bar.range > config.max_920_range:
            logger.info(
                f"[{config.code}] 9:20 AM Setup disqualified: Opening candle span "
                f"{opening_bar.range:.1f} > limit {config.max_920_range:.1f} pts."
            )
            self.has_traded_today[config.code] = True
            return None

        # Dynamic SL based on opening candle range
        sl_points = min(opening_bar.range * 0.5, 70.0 if config.code == "BANKNIFTY" else 35.0)
        sl_points = max(sl_points, 30.0 if config.code == "BANKNIFTY" else 15.0)
        target_points = sl_points * 2.0  # Asymmetric 1:2 Risk-Reward Ratio

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
                reason=f"9:20 AM High ({opening_bar.high:.1f}) breached. Spot Risk: {sl_points:.1f} pts | Target: {target_points:.1f} pts",
                is_trend_aligned=is_aligned
            )

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
                reason=f"9:20 AM Low ({opening_bar.low:.1f}) breached. Spot Risk: {sl_points:.1f} pts | Target: {target_points:.1f} pts",
                is_trend_aligned=is_aligned
            )

        return None


class BullishHammerReversalStrategy(BaseStrategy):
    """
    Bullish Hammer Reversal Setup:
    - Pre-condition: Uninterrupted downfall >= min_reversal_run (100 Bank Nifty / 50 Nifty).
    - Location Filter: Near Previous Day Low (PDL) or multi-day support.
    - Wick morphology: Lower wick >= 2x body, upper wick <= 25% total range.
    - Trigger: Breach above Hammer high.
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

        # Avoid midday dead zone
        c_time = current_candle.timestamp.time()
        if CHOP_ZONE_START <= c_time <= CHOP_ZONE_END:
            return None

        candidate = candle_history[-2]
        body = max(candidate.body, 1.0)

        if candidate.lower_wick < (2.0 * body) or candidate.upper_wick > (candidate.range * 0.25):
            return None

        lookback = min(len(candle_history) - 1, 8)
        high_watermark = max(c.high for c in candle_history[-lookback:-1])
        downfall = high_watermark - candidate.low

        if downfall < config.min_reversal_run:
            return None

        # Proximity to key level check (PDL or PDC)
        near_pdl = abs(candidate.low - ldc.pdl) <= (config.strike_step * 0.5) if ldc.pdl > 0 else True
        near_pdc = abs(candidate.low - ldc.pdc) <= (config.strike_step * 0.5) if ldc.pdc > 0 else True

        if not (near_pdl or near_pdc or downfall >= config.min_reversal_run * 1.2):
            return None

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
                    f"Bullish Hammer confirmed (Lower Wick {candidate.lower_wick:.1f} >= 2x Body {body:.1f}, "
                    f"Prior Downfall {downfall:.1f} pts)"
                ),
                is_trend_aligned=is_aligned
            )

        return None


class BearishShootingStarReversalStrategy(BaseStrategy):
    """
    Bearish Shooting Star Reversal Setup:
    - Pre-condition: Uninterrupted upward rally >= min_reversal_run.
    - Location Filter: Near Previous Day High (PDH) or resistance.
    - Wick morphology: Upper wick >= 2x body, lower wick <= 25% total range.
    - Trigger: Breach below Shooting Star low.
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

        # Avoid midday dead zone
        c_time = current_candle.timestamp.time()
        if CHOP_ZONE_START <= c_time <= CHOP_ZONE_END:
            return None

        candidate = candle_history[-2]
        body = max(candidate.body, 1.0)

        if candidate.upper_wick < (2.0 * body) or candidate.lower_wick > (candidate.range * 0.25):
            return None

        lookback = min(len(candle_history) - 1, 8)
        low_watermark = min(c.low for c in candle_history[-lookback:-1])
        rally = candidate.high - low_watermark

        if rally < config.min_reversal_run:
            return None

        near_pdh = abs(candidate.high - ldc.pdh) <= (config.strike_step * 0.5) if ldc.pdh > 0 else True
        near_pdc = abs(candidate.high - ldc.pdc) <= (config.strike_step * 0.5) if ldc.pdc > 0 else True

        if not (near_pdh or near_pdc or rally >= config.min_reversal_run * 1.2):
            return None

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
                    f"Prior Rally {rally:.1f} pts)"
                ),
                is_trend_aligned=is_aligned
            )

        return None


class InsideCandleCompressionStrategy(BaseStrategy):
    """
    Inside Candle (Mother-Child Bar) Compression Breakout Setup:
    - Mother candle must have solid range (>20 pts BN / >10 pts Nifty).
    - Inside candle range <= 65% of Mother candle range.
    - Trigger: Clean breach of Inside candle High or Low within 15-20 mins.
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

        # Avoid midday dead zone
        c_time = current_candle.timestamp.time()
        if CHOP_ZONE_START <= c_time <= CHOP_ZONE_END:
            return None

        mother = candle_history[-3]
        inside = candle_history[-2]

        min_mother_range = 20.0 if config.code == "BANKNIFTY" else 10.0
        if mother.range < min_mother_range:
            return None

        is_inside = (inside.high <= mother.high) and (inside.low >= mother.low) and (inside.range <= mother.range * 0.65)
        if not is_inside:
            return None

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
    2:20 PM Institutional Momentum Expansion Strategy:
    - Evaluates 2:20 PM candle range breakout.
    - Captures afternoon short-covering / long-unwinding surge.
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
    """Sequential evaluator ordering strategies by Ghanshyam intraday priority."""
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
        current_time = current_candle.timestamp.time()

        for strategy in self.strategies:
            if strategy.strategy_id == StrategyId.STRATEGY_920_AM:
                if not (time(9, 20) <= current_time <= time(10, 30)):
                    continue
            elif strategy.strategy_id == StrategyId.STRATEGY_220_PM:
                if not (time(14, 20) <= current_time <= time(14, 35)):
                    continue

            signal = strategy.evaluate(current_candle, candle_history, ldc, config)
            if signal:
                return signal

        return None
