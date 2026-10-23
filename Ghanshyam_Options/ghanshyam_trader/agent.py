"""
Index Trading Agent (Single Benchmark Instance with Trailing SL & Profit Lock)
"""

import logging
from datetime import datetime, time
from typing import Callable, List, Optional

from broker import BaseBrokerAdapter
from config import IndexConfig, MANDATORY_SQUARE_OFF_TIME
from models import Candle, Direction, LDCHLLevels, OptionType, TradeSignal
from risk import RiskEngine
from strategies import StrategyRegistry
from strikes import OptionStrikeSelector

logger = logging.getLogger("GhanshyamTradingSystem")


class IndexTradingAgent:
    """Agent managing strategy evaluation, trailing SL, and order execution for a single index benchmark."""

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
        """Sets LDCHL reference benchmarks."""
        self.ldc = ldc
        logger.info(
            f"[{self.config.code}] LDCHL Updated: PDH={ldc.pdh:.1f}, "
            f"PDL={ldc.pdl:.1f}, PDC={ldc.pdc:.1f}, Trend={ldc.daily_trend.value}"
        )

    def has_active_position(self) -> bool:
        """Checks if there is already an active position for this underlying index."""
        for pos in self.broker.positions.values():
            if pos.underlying_code == self.config.code and pos.active:
                return True
        return False

    def on_candle_closed(
        self,
        new_candle: Candle,
        current_spot: float,
        option_ltp_lookup_fn: Optional[Callable[[str], float]] = None
    ) -> Optional[TradeSignal]:
        """Processes 5-minute closed candle, manages open trades, and evaluates setups."""
        self.candle_history.append(new_candle)

        # 1. Mandatory 15:15 IST EOD Square-Off
        if new_candle.timestamp.time() >= MANDATORY_SQUARE_OFF_TIME:
            self.square_off_positions(new_candle.timestamp, reason="EOD 15:15 Square-Off")
            return None

        # 2. Manage active positions with Trailing SL & Profit Locks
        self._manage_open_positions(current_spot, new_candle.timestamp, option_ltp_lookup_fn)

        # 3. Prevent taking a new position if already in an active trade on this index
        if self.has_active_position():
            return None

        # 4. Check Risk clearance
        can_trade, reason = self.risk_engine.can_take_trade()
        if not can_trade:
            return None

        # 5. Evaluate price action strategies
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

        # Dynamic premium estimation (~0.6% of spot index level)
        estimated_premium = round(current_spot * 0.006, 1)
        if option_ltp_lookup_fn:
            quote = option_ltp_lookup_fn(opt_symbol)
            if quote > 0:
                estimated_premium = quote
        elif hasattr(self.broker, "get_quote"):
            quote = self.broker.get_quote(opt_symbol)
            if quote > 0:
                estimated_premium = quote

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
            option_target=option_target,
            entry_reason=signal.reason
        )

    def _manage_open_positions(
        self,
        current_spot: float,
        timestamp: datetime,
        option_ltp_lookup_fn: Optional[Callable[[str], float]] = None
    ):
        active_positions = [pos for pos in list(self.broker.positions.values()) if pos.underlying_code == self.config.code and pos.active]
        for pos in active_positions:
            spot_change = (current_spot - pos.spot_entry) if pos.option_type == OptionType.CE else (pos.spot_entry - current_spot)
            delta = 0.60
            simulated_ltp = max(5.0, pos.entry_price + (spot_change * delta))
            if option_ltp_lookup_fn:
                lookup_ltp = option_ltp_lookup_fn(pos.symbol)
                if lookup_ltp > 0:
                    simulated_ltp = lookup_ltp

            spot_risk = abs(pos.spot_entry - pos.spot_sl)
            spot_gain = spot_change

            # Trailing SL Stage 1: At 1:1 R:R (spot_gain >= spot_risk), move SL to Break-Even (entry price)
            if spot_gain >= spot_risk:
                new_spot_sl = pos.spot_entry
                if pos.option_type == OptionType.CE:
                    pos.spot_sl = max(pos.spot_sl, new_spot_sl)
                else:
                    pos.spot_sl = min(pos.spot_sl, new_spot_sl)
                pos.stop_loss = max(pos.stop_loss, pos.entry_price)

            # Trailing SL Stage 2: At 1.5:1 R:R, lock in 1:1 R:R profit
            if spot_gain >= (spot_risk * 1.5):
                if pos.option_type == OptionType.CE:
                    new_spot_sl = pos.spot_entry + spot_risk
                    pos.spot_sl = max(pos.spot_sl, new_spot_sl)
                else:
                    new_spot_sl = pos.spot_entry - spot_risk
                    pos.spot_sl = min(pos.spot_sl, new_spot_sl)
                pos.stop_loss = max(pos.stop_loss, pos.entry_price + (spot_risk * delta))

            # 1. Check Stop Loss
            is_sl_hit = False
            if pos.option_type == OptionType.CE and current_spot <= pos.spot_sl:
                is_sl_hit = True
            elif pos.option_type == OptionType.PE and current_spot >= pos.spot_sl:
                is_sl_hit = True
            elif simulated_ltp <= pos.stop_loss:
                is_sl_hit = True

            if is_sl_hit:
                reason = "Trailing SL Triggered" if pos.stop_loss >= pos.entry_price else "Stop Loss Triggered"
                closed = self.broker.close_position(pos.position_id, simulated_ltp, timestamp, exit_reason=reason)
                if closed:
                    self.risk_engine.record_trade_completion(closed.pnl)
                continue

            # 2. Check Target
            is_target_hit = False
            if pos.option_type == OptionType.CE and current_spot >= pos.spot_target:
                is_target_hit = True
            elif pos.option_type == OptionType.PE and current_spot <= pos.spot_target:
                is_target_hit = True
            elif simulated_ltp >= pos.target:
                is_target_hit = True

            if is_target_hit:
                closed = self.broker.close_position(pos.position_id, simulated_ltp, timestamp, exit_reason="Target Achieved")
                if closed:
                    self.risk_engine.record_trade_completion(closed.pnl)

    def square_off_positions(self, timestamp: datetime, reason: str = "Square Off"):
        active_positions = [pos for pos in list(self.broker.positions.values()) if pos.underlying_code == self.config.code and pos.active]
        for pos in active_positions:
            closed = self.broker.close_position(pos.position_id, pos.entry_price, timestamp, exit_reason=reason)
            if closed:
                self.risk_engine.record_trade_completion(closed.pnl)
