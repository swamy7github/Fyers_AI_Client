from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np

@dataclass
class TradeSignal:
    symbol: str
    timestamp: pd.Timestamp
    signal_type: str  # "BUY", "SELL", "HOLD"
    strategy_name: str
    entry_price: float
    stop_loss_price: float
    target_price: float
    position_shares: int
    position_value: float
    risk_reward_ratio: float
    risk_per_share: float
    account_risk_amount: float
    rationale: str
    confidence_score: float  # 0.0 to 100.0
    metadata: Dict[str, Any]

class BaseStrategy(ABC):
    """
    Abstract Base Class for systematic breakout trading strategies.
    Forces uniform risk architecture, signal generation, and parameters across strategies.
    """
    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.config = config
        self.enabled = config.get("enabled", True)
        self.weight = config.get("weight", 0.25)

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame, symbol: str, index_df: Optional[pd.DataFrame] = None) -> Optional[TradeSignal]:
        """Analyzes historical bar data and returns a TradeSignal if setup criteria align on the latest bar."""
        pass

    def calculate_position_size(self, account_equity: float, entry_price: float, stop_loss_price: float,
                                risk_pct: float = 1.0, max_alloc_pct: float = 20.0) -> Dict[str, Any]:
        """
        Calculates position size using the strict Fixed Fractional / Risk-First Formula:
        Shares = (Account Equity * Risk %) / (Entry Price - Stop Loss Price)
        Bounded by Max Equity Allocation per Position (e.g. 20%).
        """
        if entry_price <= 0 or stop_loss_price >= entry_price or stop_loss_price <= 0:
            return {"shares": 0, "value": 0.0, "risk_amount": 0.0, "risk_per_share": 0.0}

        risk_amount = account_equity * (risk_pct / 100.0)
        risk_per_share = entry_price - stop_loss_price
        
        raw_shares = risk_amount / risk_per_share
        
        # Max capital allocation ceiling
        max_capital_allowed = account_equity * (max_alloc_pct / 100.0)
        max_shares_allowed = max_capital_allowed / entry_price
        
        final_shares = int(min(raw_shares, max_shares_allowed))
        position_value = final_shares * entry_price
        actual_risk = final_shares * risk_per_share

        return {
            "shares": final_shares,
            "value": position_value,
            "risk_amount": actual_risk,
            "risk_per_share": risk_per_share
        }

    def check_market_regime(self, index_df: pd.DataFrame, min_sma_period: int = 50) -> bool:
        """Checks if the broad market index (e.g., Nifty 50) is in a bullish regime."""
        if index_df is None or index_df.empty or len(index_df) < min_sma_period:
            return True # Default to permissive if benchmark unavailable
        
        latest = index_df.iloc[-1]
        index_close = latest['close']
        index_sma = index_df['close'].rolling(window=min_sma_period).mean().iloc[-1]
        
        return index_close > index_sma
