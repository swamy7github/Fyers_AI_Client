import pandas as pd
import numpy as np
from typing import Dict, Any, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal

class MinerviniVCPStrategy(BaseStrategy):
    """
    Mark Minervini's SEPA (Specific Entry Point Analysis) & VCP (Volatility Contraction Pattern) Strategy.
    Supports Stage 2 Long Breakouts and optional Stage 4 Short Intraday Breakdowns.
    """
    def __init__(self, config: Dict[str, Any]):
        super().__init__("Mark Minervini VCP", config)
        tt_cfg = config.get("trend_template", {})
        self.sma_fast = tt_cfg.get("sma_fast", 50)
        self.sma_mid = tt_cfg.get("sma_mid", 150)
        self.sma_slow = tt_cfg.get("sma_slow", 200)
        self.min_dist_52w_low = tt_cfg.get("min_dist_52w_low_pct", 30.0)
        self.max_dist_52w_high = tt_cfg.get("max_dist_52w_high_pct", 25.0)
        self.min_rs_rating = tt_cfg.get("min_rs_rating", 70.0)

        vcp_cfg = config.get("vcp_criteria", {})
        self.min_contractions = vcp_cfg.get("min_contractions", 2)
        self.vol_dryup_ma = vcp_cfg.get("volume_dryup_ma_period", 20)
        self.breakout_vol_mult = vcp_cfg.get("breakout_volume_mult", 1.4)
        self.max_stop_pct = vcp_cfg.get("max_stop_loss_pct", 8.0)
        self.target_rr = vcp_cfg.get("target_rr_ratio", 3.0)

    def check_trend_template(self, df: pd.DataFrame) -> bool:
        """Validates Minervini's 8-Point Stage 2 Trend Template (Bullish)."""
        if len(df) < self.sma_slow + 20:
            return False

        latest = df.iloc[-1]
        c = latest['close']
        s50 = latest['sma_50']
        s150 = latest['sma_150']
        s200 = latest['sma_200']

        c1 = c > s150 and c > s200
        c2 = s150 > s200
        c3 = s200 > df['sma_200'].iloc[-22]
        c4 = s50 > s150 and s50 > s200
        c5 = c > s50
        c6 = latest['dist_from_52w_low_pct'] >= self.min_dist_52w_low
        c7 = latest['dist_from_52w_high_pct'] <= self.max_dist_52w_high
        c8 = latest.get('rs_rating', 75.0) >= self.min_rs_rating

        return c1 and c2 and c3 and c4 and c5 and c6 and c7 and c8

    def check_bearish_trend_template(self, df: pd.DataFrame) -> bool:
        """Validates Bearish Stage 4 Downtrend Template (Short)."""
        if len(df) < self.sma_slow + 20:
            return False

        latest = df.iloc[-1]
        c = latest['close']
        s50 = latest['sma_50']
        s150 = latest['sma_150']
        s200 = latest['sma_200']

        c1 = c < s150 and c < s200
        c2 = s150 < s200
        c3 = s200 < df['sma_200'].iloc[-22]  # 200 SMA sloping down
        c4 = s50 < s150 and s50 < s200
        c5 = c < s50
        c6 = latest.get('rs_rating', 50.0) <= 35.0 # Weak relative strength

        return c1 and c2 and c3 and c4 and c5 and c6

    def detect_vcp_contraction(self, df: pd.DataFrame, window: int = 40) -> Optional[Dict[str, float]]:
        """Detects volatility compression across recent price swings."""
        recent = df.iloc[-window:].copy()
        highs = recent['high'].values
        lows = recent['low'].values
        
        peaks = []
        troughs = []
        for i in range(2, len(recent) - 2):
            if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                peaks.append((i, highs[i]))
            if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
                troughs.append((i, lows[i]))

        if len(peaks) < 2 or len(troughs) < 1:
            pivot_price = recent['high'].iloc[:-1].max()
            contraction_low = recent['low'].iloc[-10:].min()
            return {"pivot_price": pivot_price, "contraction_low": contraction_low, "contractions": 2}

        pivot_price = peaks[-1][1]
        contraction_low = troughs[-1][1]
        
        return {
            "pivot_price": pivot_price,
            "contraction_low": contraction_low,
            "contractions": len(troughs)
        }

    def generate_signals(self, df: pd.DataFrame, symbol: str, index_df: Optional[pd.DataFrame] = None) -> Optional[TradeSignal]:
        if not self.enabled or df is None or len(df) < 200:
            return None

        vcp_info = self.detect_vcp_contraction(df)
        if not vcp_info:
            return None

        pivot_price = vcp_info["pivot_price"]
        contraction_low = vcp_info["contraction_low"]

        latest = df.iloc[-1]
        c = latest['close']
        v = latest['volume']
        v_ma = latest['vol_sma_20']

        # 1. LONG BUY SIGNAL (Stage 2 Breakout)
        if self.check_market_regime(index_df) and self.check_trend_template(df):
            if (c >= pivot_price) and (v >= v_ma * self.breakout_vol_mult):
                entry_price = c
                stop_loss = max(contraction_low, entry_price * (1.0 - self.max_stop_pct / 100.0))
                risk_per_share = entry_price - stop_loss
                
                if risk_per_share > 0:
                    target_price = entry_price + (risk_per_share * self.target_rr)
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
                        rationale=f"VCP Stage 2 Breakout above Pivot {pivot_price:.2f} with Volume Expansion ({v:.0f} vs MA {v_ma:.0f}).",
                        confidence_score=90.0 if vcp_info["contractions"] >= 3 else 80.0,
                        metadata=vcp_info
                    )

        # 2. SHORT SELL SIGNAL (Stage 4 Breakdown Intraday MIS)
        if self.enable_short and self.check_bearish_trend_template(df):
            if (c <= contraction_low) and (v >= v_ma * self.breakout_vol_mult):
                entry_price = c
                stop_loss = min(pivot_price, entry_price * (1.0 + self.max_stop_pct / 100.0))
                risk_per_share = stop_loss - entry_price
                
                if risk_per_share > 0:
                    target_price = entry_price - (risk_per_share * self.target_rr)
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
                        rationale=f"VCP Stage 4 Breakdown below Floor {contraction_low:.2f} with Volume Expansion ({v:.0f} vs MA {v_ma:.0f}).",
                        confidence_score=85.0,
                        metadata=vcp_info
                    )

        return None
