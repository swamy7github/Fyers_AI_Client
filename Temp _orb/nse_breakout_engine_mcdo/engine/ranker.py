import pandas as pd
from typing import List, Dict, Any
from strategies.base_strategy import TradeSignal

class StockRanker:
    """
    Ranks NSE stocks based on multi-strategy signal alignment, Relative Strength,
    breakout volume, and risk-reward asymmetry.
    """
    def __init__(self, strategy_weights: Dict[str, float] = None):
        self.weights = strategy_weights or {
            "Mark Minervini VCP": 0.30,
            "Nicolas Darvas Box": 0.25,
            "William O'Neil Cup & Handle": 0.25,
            "Christian Kullamagi Momentum": 0.20
        }

    def rank_signals(self, signals: List[TradeSignal]) -> pd.DataFrame:
        """Ranks list of TradeSignal objects into a sorted DataFrame."""
        if not signals:
            return pd.DataFrame()

        rows = []
        for sig in signals:
            strat_w = self.weights.get(sig.strategy_name, 0.25)
            
            # Composite Scoring Formula (0 to 100)
            # 40% Confidence Score + 30% Strategy Weight + 20% Risk:Reward Ratio + 10% Position Size Quality
            score = (
                (sig.confidence_score * 0.40) +
                (strat_w * 100.0 * 0.30) +
                (min(sig.risk_reward_ratio * 10.0, 20.0)) +
                (10.0 if sig.position_shares > 0 else 0.0)
            )

            rows.append({
                "symbol": sig.symbol,
                "composite_score": round(score, 1),
                "strategy": sig.strategy_name,
                "signal": sig.signal_type,
                "entry_price": sig.entry_price,
                "stop_loss": sig.stop_loss_price,
                "target_price": sig.target_price,
                "risk_reward": sig.risk_reward_ratio,
                "position_shares": sig.position_shares,
                "position_value": sig.position_value,
                "rationale": sig.rationale,
                "timestamp": sig.timestamp
            })

        df = pd.DataFrame(rows)
        # Group by symbol to detect multi-strategy consensus confluence
        consensus = df.groupby('symbol')['strategy'].count().rename('strategy_confluence')
        df = df.merge(consensus, on='symbol', how='left')
        
        # Boost composite score by +15 points for stocks with multi-strategy confluence!
        df['composite_score'] = df['composite_score'] + (df['strategy_confluence'] - 1) * 15.0
        
        df = df.sort_values(by=['composite_score', 'risk_reward'], ascending=[False, False]).reset_index(drop=True)
        return df
