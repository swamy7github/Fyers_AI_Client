import pandas as pd
import numpy as np
from typing import Dict, Any, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal

class DarvasBoxStrategy(BaseStrategy):
    """
    Nicolas Darvas Box Theory Strategy.
    Implements 250-day (52-week) high momentum filter, strict 3-Day Rule for Ceiling/Floor construction,
    volume expansion entry, and box-floor trailing stop loss.
    """
    def __init__(self, config: Dict[str, Any]):
        super().__init__("Nicolas Darvas Box", config)
        self.lookback_high = config.get("lookback_high_period", 250)
        self.confirm_days = config.get("confirmation_days", 3)
        self.breakout_vol_mult = config.get("breakout_volume_mult", 1.5)

    def detect_darvas_box(self, df: pd.DataFrame) -> Optional[Dict[str, float]]:
        """
        Executes Darvas 3-Day Rule algorithm on historical OHLC data:
        1. Identify peak high within lookback window.
        2. Confirm Ceiling (C): 3 subsequent bars with High < C.
        3. Confirm Floor (F): Low point after C where 3 subsequent bars have Low > F.
        """
        if len(df) < 30:
            return None

        # Search backward for the most recent valid Darvas Box
        highs = df['high'].values
        lows = df['low'].values
        n = len(df)

        for i in range(n - 4, 10, -1):
            potential_ceiling = highs[i]
            
            # Check 3-Day Rule for Ceiling
            if i + 3 < n:
                if highs[i+1] < potential_ceiling and highs[i+2] < potential_ceiling and highs[i+3] < potential_ceiling:
                    # Ceiling Confirmed! Now search for Floor
                    ceiling_idx = i
                    ceiling_price = potential_ceiling

                    # Search forward for Floor
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

        # Step 1: Market Regime Check
        if not self.check_market_regime(index_df):
            return None

        # Step 2: 250-Day (52-Week) High Filter within last 20 days
        recent_max_high = df['high'].iloc[-250:].max()
        latest = df.iloc[-1]
        
        if latest['high'] < recent_max_high * 0.85: # Must be near new highs
            return None

        # Step 3: Construct Darvas Box via 3-Day Rule
        box = self.detect_darvas_box(df)
        if not box:
            return None

        ceiling = box["ceiling"]
        floor = box["floor"]

        c = latest['close']
        v = latest['volume']
        v_ma = latest['vol_sma_20']

        # Step 4: Breakout Trigger above Ceiling on Surge Volume
        if c > ceiling and v >= v_ma * self.breakout_vol_mult:
            entry_price = c
            stop_loss = floor
            risk_per_share = entry_price - stop_loss

            if risk_per_share <= 0 or (risk_per_share / entry_price) > 0.15:
                return None  # Reject if stop is too wide (>15%)

            target_price = entry_price + (risk_per_share * 3.0)
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
                rationale=f"Darvas Box Breakout above Ceiling {ceiling:.2f} (Floor: {floor:.2f}) on Volume Surge ({v:.0f}).",
                confidence_score=85.0,
                metadata=box
            )

        return None
