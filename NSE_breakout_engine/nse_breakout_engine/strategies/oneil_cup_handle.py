import pandas as pd
import numpy as np
from typing import Dict, Any, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal

class ONeilCupHandleStrategy(BaseStrategy):
    """
    William O'Neil's CAN SLIM Cup with Handle Strategy.
    Quantifies prior uptrend (>=30%), U-shaped cup (7-65 weeks, 12-35% depth), upper-half handle (1-4 weeks, 5-15% retrace),
    volume dry-up in handle, and +40-50% breakout volume surge.
    """
    def __init__(self, config: Dict[str, Any]):
        super().__init__("William O'Neil Cup & Handle", config)
        self.prior_uptrend_min = config.get("prior_uptrend_min_pct", 30.0)
        self.cup_min_weeks = config.get("cup_min_length_weeks", 7)
        self.cup_max_weeks = config.get("cup_max_length_weeks", 65)
        self.cup_min_depth = config.get("cup_depth_min_pct", 12.0)
        self.cup_max_depth = config.get("cup_depth_max_pct", 35.0)
        self.handle_min_days = config.get("handle_min_days", 5)
        self.handle_max_days = config.get("handle_max_weeks", 4) * 5
        self.breakout_vol_mult = config.get("breakout_volume_mult", 1.4)
        self.max_stop_pct = config.get("max_stop_loss_pct", 7.5)
        self.profit_target_pct = config.get("profit_target_pct", 25.0)

    def detect_cup_handle(self, df: pd.DataFrame) -> Optional[Dict[str, float]]:
        """Scans for Cup and Handle geometric parameters."""
        if len(df) < 100:
            return None

        # Handle window: last 5 to 20 bars
        handle_window = df.iloc[-self.handle_max_days:].copy()
        handle_high = handle_window['high'].max()
        handle_low = handle_window['low'].min()
        handle_retrace = ((handle_high - handle_low) / handle_high) * 100.0

        if handle_retrace > 18.0 or handle_retrace < 2.0:
            return None # Handle too deep or non-existent

        # Cup window: prior 35 to 250 bars
        cup_window = df.iloc[-150:-self.handle_min_days].copy()
        if cup_window.empty:
            return None

        left_lip = cup_window['high'].max()
        cup_bottom = cup_window['low'].min()
        cup_depth = ((left_lip - cup_bottom) / left_lip) * 100.0

        if cup_depth < self.cup_min_depth or cup_depth > self.cup_max_depth:
            return None

        # Verify Handle is in Upper Half of Cup
        cup_midpoint = cup_bottom + (left_lip - cup_bottom) * 0.5
        if handle_low < cup_midpoint:
            return None # Handle formed in lower half (invalid)

        # Verify Prior Uptrend (30%+ move prior to cup left lip)
        prior_window = df.iloc[:-150]
        if len(prior_window) >= 50:
            prior_low = prior_window['low'].min()
            prior_uptrend = ((left_lip - prior_low) / prior_low) * 100.0
            if prior_uptrend < self.prior_uptrend_min:
                return None

        return {
            "handle_pivot": handle_high,
            "handle_low": handle_low,
            "left_lip": left_lip,
            "cup_bottom": cup_bottom,
            "cup_depth_pct": cup_depth,
            "handle_retrace_pct": handle_retrace
        }

    def generate_signals(self, df: pd.DataFrame, symbol: str, index_df: Optional[pd.DataFrame] = None) -> Optional[TradeSignal]:
        if not self.enabled or df is None or len(df) < 150:
            return None

        # Step 1: Market Regime Check
        if not self.check_market_regime(index_df):
            return None

        # Step 2: Cup with Handle Geometry Check
        ch_info = self.detect_cup_handle(df)
        if not ch_info:
            return None

        pivot_price = ch_info["handle_pivot"]
        handle_low = ch_info["handle_low"]

        latest = df.iloc[-1]
        c = latest['close']
        v = latest['volume']
        v_ma_50 = latest['vol_sma_50']

        # Step 3: Breakout above Handle Pivot on Volume Surge (+40% above 50 SMA)
        if c >= pivot_price and v >= v_ma_50 * self.breakout_vol_mult:
            entry_price = c
            stop_loss = max(handle_low, entry_price * (1.0 - self.max_stop_pct / 100.0))
            risk_per_share = entry_price - stop_loss

            if risk_per_share <= 0:
                return None

            target_price = entry_price * (1.0 + self.profit_target_pct / 100.0)
            rr_ratio = (target_price - entry_price) / risk_per_share

            pos_info = self.calculate_position_size(1000000.0, entry_price, stop_loss, risk_pct=1.0, max_alloc_pct=20.0)

            return TradeSignal(
                symbol=symbol,
                timestamp=latest['timestamp'],
                signal_type="BUY",
                strategy_name=self.name,
                entry_price=round(entry_price, 2),
                stop_loss_price=round(stop_loss, 2),
                target_price=round(target_price, 2),
                position_shares=pos_info["shares"],
                position_value=round(pos_info["value"], 2),
                risk_reward_ratio=round(rr_ratio, 2),
                risk_per_share=round(risk_per_share, 2),
                account_risk_amount=round(pos_info["risk_amount"], 2),
                rationale=f"CAN SLIM Cup with Handle Breakout above Pivot {pivot_price:.2f} (Cup Depth: {ch_info['cup_depth_pct']:.1f}%).",
                confidence_score=88.0,
                metadata=ch_info
            )

        return None
