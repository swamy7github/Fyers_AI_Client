import pandas as pd
import numpy as np
from typing import Dict, Any, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal

class KullamagiMomentumStrategy(BaseStrategy):
    """
    Christian Kullamagi's Stairstep Momentum / High Tight Flag Strategy.
    Quantifies explosive prior advance (+30-100%+), moving average surfing (Price > 10 EMA > 20 EMA > 50 SMA),
    1x ADR% max stop rule, partial profit taking at 2.5R, and 10 EMA / 20 EMA trailing exits.
    """
    def __init__(self, config: Dict[str, Any]):
        super().__init__("Christian Kullamagi Momentum", config)
        self.prior_move_min = config.get("prior_move_min_pct", 30.0)
        self.prior_lookback = config.get("prior_move_lookback_days", 60)
        self.max_stop_adr_mult = config.get("max_stop_adr_mult", 1.0)
        self.partial_exit_days = config.get("partial_exit_days", 4)
        self.partial_exit_r = config.get("partial_exit_r", 2.5)

    def check_momentum_alignment(self, df: pd.DataFrame) -> bool:
        """Verifies Moving Average Surfing: Price > 10 EMA > 20 EMA > 50 SMA."""
        if len(df) < 50:
            return False

        latest = df.iloc[-1]
        c = latest['close']
        e10 = latest['ema_10']
        e20 = latest['ema_20']
        s50 = latest['sma_50']

        # Price surfing short EMAs above 50 SMA
        ma_aligned = (c > e10) and (e10 > e20) and (e20 > s50)
        
        # Prior advance check: Stock up >= 30% in last 1-3 months
        past_window = df.iloc[-self.prior_lookback:-10]
        if past_window.empty:
            return False
        
        prior_low = past_window['low'].min()
        recent_high = df['high'].iloc[-20:].max()
        prior_move = ((recent_high - prior_low) / prior_low) * 100.0

        return ma_aligned and (prior_move >= self.prior_move_min)

    def generate_signals(self, df: pd.DataFrame, symbol: str, index_df: Optional[pd.DataFrame] = None) -> Optional[TradeSignal]:
        if not self.enabled or df is None or len(df) < 60:
            return None

        # Step 1: Market Regime Check
        if not self.check_market_regime(index_df):
            return None

        # Step 2: Momentum & MA Alignment
        if not self.check_momentum_alignment(df):
            return None

        latest = df.iloc[-1]
        adr_20 = latest['adr_20']  # 20-day Average Daily Range %
        c = latest['close']
        v = latest['volume']
        v_ma = latest['vol_sma_20']

        # Consolidation resistance level (High of last 10 days excluding latest bar)
        consolidation = df.iloc[-15:-1]
        resistance_price = consolidation['high'].max()
        consolidation_low = df.iloc[-5:]['low'].min()  # Tight flag floor

        # Step 3: Micro-Pivot Breakout Execution
        is_breakout = (c > resistance_price) and (v >= v_ma * 1.25)

        if is_breakout:
            entry_price = c
            stop_loss = consolidation_low
            risk_pct = ((entry_price - stop_loss) / entry_price) * 100.0

            # CRITICAL QULLAMAGGIE RULE: Stop distance MUST NEVER exceed 1x ADR%
            if risk_pct > (adr_20 * self.max_stop_adr_mult) or risk_pct <= 0:
                # Adjust stop to 1x ADR if consolidation low is too wide
                stop_loss = entry_price * (1.0 - (adr_20 / 100.0))
                risk_pct = adr_20

            risk_per_share = entry_price - stop_loss
            target_price = entry_price + (risk_per_share * self.partial_exit_r)
            rr_ratio = self.partial_exit_r

            # Account Risk: 0.5% - 1.0% per trade
            pos_info = self.calculate_position_size(1000000.0, entry_price, stop_loss, risk_pct=0.75, max_alloc_pct=20.0)

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

        return None
