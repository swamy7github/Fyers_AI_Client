"""
Ghanshyam Trader Initialization
"""

from config import IndexConfig
from models import Direction, OptionType, StrategyId, Candle, LDCHLLevels, TradeSignal, Position
from strategies import StrategyRegistry, BaseStrategy
from strikes import OptionStrikeSelector
from risk import RiskEngine
from broker import PaperTradingBroker, FyersClientAdapter, BaseBrokerAdapter
from agent import IndexTradingAgent
from backtester import BacktestEngine
from realtime_runner import RealtimeSessionRunner, MultiAssetTradingManager

__version__ = "3.1.0"
__all__ = [
    "IndexConfig",
    "Direction",
    "OptionType",
    "StrategyId",
    "Candle",
    "LDCHLLevels",
    "TradeSignal",
    "Position",
    "StrategyRegistry",
    "BaseStrategy",
    "OptionStrikeSelector",
    "RiskEngine",
    "PaperTradingBroker",
    "FyersClientAdapter",
    "BaseBrokerAdapter",
    "IndexTradingAgent",
    "BacktestEngine",
    "RealtimeSessionRunner",
    "MultiAssetTradingManager",
]
