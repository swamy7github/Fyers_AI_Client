import pandas as pd
import numpy as np
from typing import Dict, Any, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal

class DarvasBoxStrategy(BaseStrategy):
    """
    Nicolas Darvas Box Theory Strategy.
    Implements 250-day high/low momentum filter, strict 3-Day Rule for Ceiling/Floor construction,
    volume expansion entry, and box trailing stop loss for Long and Short setups.
    """
    def __init__(self, config: Dict[str, Any]):
        super().__init__("Nicolas Darvas Box", config)
        self.lookback_high = config.get("lookback_high_period", 250)
        self.confirm_days = config.get("confirmation_days", 3)
        self.breakout_vol_mult = config.get("breakout_volume_mult", 1.5)

    def detect_darvas_box(self, df: pd.DataFrame) -> Optional[Dict[str, float]]:
        """Executes Darvas 3-Day Rule algorithm on historical OHLC data."""
        if len(df) < 30:
            return None

        highs = df['high'].values
        lows = df['low'].values
        n = len(df)

        for i in range(n - 4, 10, -1):
            potential_ceiling = highs[i]
            
            if i + 3 < n:
                if highs[i+1] < potential_ceiling and highs[i+2] < potential_ceiling and highs[i+3] < potential_ceiling:
                    ceiling_idx = i
                    ceiling_price = potential_ceiling

                    for j in range(ceiling_idx + 1, min(ceiling_idx + 25, n - 3)):
                        potential_floor = lows[j]
                        if lows[j+1] > potential_floor and lows[j+2] > potential_floor and lows[j+3] > potential_floor:
                            floor_price = potential_floor
                            return {
                                "ceiling": ceiling_price,
                                "floor": floor_price,
                                "ceiling_idx": ceiling_idx,
                                "floor_idx": j,
                                "box_height_pct": ((ceiling_price - floor_price) / ceiling_price) * 100.0
                            }
        return None

    def generate_signals(self, df: pd.DataFrame, symbol: str, index_df: Optional[pd.DataFrame] = None) -> Optional[TradeSignal]:
        if not self.enabled or df is None or len(df) < self.lookback_high:
            return None

        recent_max_high = df['high'].iloc[-250:].max()
        recent_min_low = df['low'].iloc[-250:].min()
        latest = df.iloc[-1]
        
        box = self.detect_darvas_box(df)
        if not box:
            return None

        ceiling = box["ceiling"]
        floor = box["floor"]

        c = latest['close']
        v = latest['volume']
        v_ma = latest['vol_sma_20']

        # 1. LONG BUY SIGNAL (Bullish Darvas Box Breakout)
        if self.check_market_regime(index_df) and latest['high'] >= recent_max_high * 0.85:
            if c > ceiling and v >= v_ma * self.breakout_vol_mult:
                entry_price = c
                stop_loss = floor
                risk_per_share = entry_price - stop_loss

                if 0 < risk_per_share <= (entry_price * 0.15):
                    target_price = entry_price + (risk_per_share * 3.0)
                    rr_ratio = (target_price - entry_price) / risk_per_share
                    pos_info = self.calculate_position_size(1000000.0, entry_price, stop_loss, risk_pct=1.0, max_alloc_pct=20.0, signal_type="BUY")

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
                        rationale=f"Darvas Box Breakout above Ceiling {ceiling:.2f} (Floor: {floor:.2f}) on Volume Surge ({v:.0f}).",
                        confidence_score=85.0,
                        metadata=box
                    )

        # 2. SHORT SELL SIGNAL (Bearish Darvas Box Breakdown)
        if self.enable_short and latest['low'] <= recent_min_low * 1.15:
            if c < floor and v >= v_ma * self.breakout_vol_mult:
                entry_price = c
                stop_loss = ceiling
                risk_per_share = stop_loss - entry_price

                if 0 < risk_per_share <= (entry_price * 0.15):
                    target_price = entry_price - (risk_per_share * 3.0)
                    rr_ratio = (entry_price - target_price) / risk_per_share
                    pos_info = self.calculate_position_size(1000000.0, entry_price, stop_loss, risk_pct=1.0, max_alloc_pct=20.0, signal_type="SELL")

                    return TradeSignal(
                        symbol=symbol,
                        timestamp=latest['timestamp'],
                        signal_type="SELL",
                        strategy_name=f"{self.name} (SHORT)",
                        entry_price=round(entry_price, 2),
                        stop_loss_price=round(stop_loss, 2),
                        target_price=round(target_price, 2),
                        position_shares=pos_info["shares"],
                        position_value=round(pos_info["value"], 2),
                        risk_reward_ratio=round(rr_ratio, 2),
                        risk_per_share=round(risk_per_share, 2),
                        account_risk_amount=round(pos_info["risk_amount"], 2),
                        rationale=f"Bearish Darvas Box Breakdown below Floor {floor:.2f} (Ceiling: {ceiling:.2f}) on Volume Surge ({v:.0f}).",
                        confidence_score=82.0,
                        metadata=box
                    )

        return None
