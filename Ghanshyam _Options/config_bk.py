"""
Benchmark Index & Strategy Configuration
"""

from dataclasses import dataclass
from datetime import time

@dataclass
class IndexConfig:
    """
    Instrument-specific parameters defining volatility limits,
    strike steps, and exchange contract lot sizes for index options.
    """
    symbol: str               # e.g., 'NSE:NIFTYBANK-INDEX' or 'NSE:NIFTY50-INDEX'
    code: str                 # e.g., 'BANKNIFTY' or 'NIFTY'
    strike_step: float        # 100.0 for Bank Nifty, 50.0 for Nifty
    lot_size: int             # 15 for Bank Nifty, 25 for Nifty
    max_920_range: float      # Max allowed 09:15 candle span (250 for Bank Nifty, 120 for Nifty)
    min_reversal_run: float   # Minimum required preceding run (100 for Bank Nifty, 50 for Nifty)
    default_expiry: str = "26OCT"

    @classmethod
    def bank_nifty_default(cls, expiry: str = "26OCT", lot_size: int = 30) -> "IndexConfig":
        """Factory method for BANK NIFTY configuration."""
        return cls(
            symbol="NSE:NIFTYBANK-INDEX",
            code="BANKNIFTY",
            strike_step=100.0,
            lot_size=lot_size,
            max_920_range=250.0,
            min_reversal_run=100.0,
            default_expiry=expiry
        )

    @classmethod
    def nifty_50_default(cls, expiry: str = "26OCT", lot_size: int = 65) -> "IndexConfig":
        """Factory method for NIFTY 50 configuration."""
        return cls(
            symbol="NSE:NIFTY50-INDEX",
            code="NIFTY",
            strike_step=50.0,
            lot_size=lot_size,
            max_920_range=120.0,
            min_reversal_run=50.0,
            default_expiry=expiry
        )

# Market Session Time Gates (Indian Standard Time)
MARKET_OPEN_TIME = time(9, 15)
STRATEGY_920_EVAL_TIME = time(9, 20)
STRATEGY_220_EVAL_TIME = time(14, 20)
MANDATORY_SQUARE_OFF_TIME = time(15, 15)
MARKET_CLOSE_TIME = time(15, 30)
