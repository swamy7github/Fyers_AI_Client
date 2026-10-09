import pandas as pd
import numpy as np
from typing import Dict, Any, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal

class KullamagiMomentumStrategy(BaseStrategy):
    """
    Christian Kullamagi's Stairstep Momentum Strategy.
    Quantifies Bullish Stairstep Flags and Bearish Stairstep / Parabolic Shorts.
    """
    def __init__(self, config: Dict[str, Any]):
        super().__init__("Christian Kullamagi Momentum", config)
        self.prior_move_min = config.get("prior_move_min_pct", 30.0)
        self.prior_lookback = config.get("prior_move_lookback_days", 60)
        self.max_stop_adr_mult = config.get("max_stop_adr_mult", 1.0)
        self.partial_exit_days = config.get("partial_exit_days", 4)
        self.partial_exit_r = config.get("partial_exit_r", 2.5)

    def check_momentum_alignment(self, df: pd.DataFrame) -> bool:
        """Verifies Bullish Moving Average Surfing: Price > 10 EMA > 20 EMA > 50 SMA."""
        if len(df) < 50:
            return False

        latest = df.iloc[-1]
        c = latest['close']
        e10 = latest['ema_10']
        e20 = latest['ema_20']
        s50 = latest['sma_50']

        ma_aligned = (c > e10) and (e10 > e20) and (e20 > s50)
        
        past_window = df.iloc[-self.prior_lookback:-10]
        if past_window.empty:
            return False
        
        prior_low = past_window['low'].min()
        recent_high = df['high'].iloc[-20:].max()
        prior_move = ((recent_high - prior_low) / prior_low) * 100.0

        return ma_aligned and (prior_move >= self.prior_move_min)

    def check_bearish_momentum_alignment(self, df: pd.DataFrame) -> bool:
        """Verifies Bearish Moving Average Alignment: Price < 10 EMA < 20 EMA < 50 SMA."""
        if len(df) < 50:
            return False

        latest = df.iloc[-1]
        c = latest['close']
        e10 = latest['ema_10']
        e20 = latest['ema_20']
        s50 = latest['sma_50']

        ma_aligned = (c < e10) and (e10 < e20) and (e20 < s50)
        
        past_window = df.iloc[-self.prior_lookback:-10]
        if past_window.empty:
            return False
        
        prior_high = past_window['high'].max()
        recent_low = df['low'].iloc[-20:].min()
        prior_drop = ((prior_high - recent_low) / prior_high) * 100.0

        return ma_aligned and (prior_drop >= self.prior_move_min)

    def generate_signals(self, df: pd.DataFrame, symbol: str, index_df: Optional[pd.DataFrame] = None) -> Optional[TradeSignal]:
        if not self.enabled or df is None or len(df) < 60:
            return None

        latest = df.iloc[-1]
        adr_20 = latest['adr_20']
        c = latest['close']
        v = latest['volume']
        v_ma = latest['vol_sma_20']

        # 1. LONG BUY SIGNAL (Stairstep Momentum Flag)
        if self.check_market_regime(index_df) and self.check_momentum_alignment(df):
            consolidation = df.iloc[-15:-1]
            resistance_price = consolidation['high'].max()
            consolidation_low = df.iloc[-5:]['low'].min()

            if (c > resistance_price) and (v >= v_ma * 1.25):
                entry_price = c
                stop_loss = consolidation_low
                risk_pct = ((entry_price - stop_loss) / entry_price) * 100.0

                if risk_pct > (adr_20 * self.max_stop_adr_mult) or risk_pct <= 0:
                    stop_loss = entry_price * (1.0 - (adr_20 / 100.0))
                    risk_pct = adr_20

                risk_per_share = entry_price - stop_loss
                target_price = entry_price + (risk_per_share * self.partial_exit_r)
                rr_ratio = self.partial_exit_r

                pos_info = self.calculate_position_size(1000000.0, entry_price, stop_loss, risk_pct=0.75, max_alloc_pct=20.0, signal_type="BUY")

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
                    rationale=f"Stairstep Momentum Breakout above {resistance_price:.2f} (10/20 EMA Aligned, ADR%: {adr_20:.2f}%).",
                    confidence_score=92.0,
                    metadata={"adr_20": adr_20, "risk_pct": risk_pct, "ma_surfing": True}
                )

        # 2. SHORT SELL SIGNAL (Bearish Stairstep Breakdown)
        if self.enable_short and self.check_bearish_momentum_alignment(df):
            consolidation = df.iloc[-15:-1]
            support_price = consolidation['low'].min()
            consolidation_high = df.iloc[-5:]['high'].max()

            if (c < support_price) and (v >= v_ma * 1.25):
                entry_price = c
                stop_loss = consolidation_high
                risk_pct = ((stop_loss - entry_price) / entry_price) * 100.0

                if risk_pct > (adr_20 * self.max_stop_adr_mult) or risk_pct <= 0:
                    stop_loss = entry_price * (1.0 + (adr_20 / 100.0))
                    risk_pct = adr_20

                risk_per_share = stop_loss - entry_price
                target_price = entry_price - (risk_per_share * self.partial_exit_r)
                rr_ratio = self.partial_exit_r

                pos_info = self.calculate_position_size(1000000.0, entry_price, stop_loss, risk_pct=0.75, max_alloc_pct=20.0, signal_type="SELL")

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
                    rationale=f"Bearish Stairstep Breakdown below {support_price:.2f} (10/20 EMA Bearish Aligned, ADR%: {adr_20:.2f}%).",
                    confidence_score=90.0,
                    metadata={"adr_20": adr_20, "risk_pct": risk_pct, "ma_surfing": True}
                )

        return None
