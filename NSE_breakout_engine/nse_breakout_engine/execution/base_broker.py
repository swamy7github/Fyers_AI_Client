from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional

class AbstractBroker(ABC):
    """
    Abstract interface for broker execution (Paper Trading, FYERS API V3, OpenAlgo).
    """

    @abstractmethod
    def get_account_balance(self) -> Dict[str, float]:
        """
        Returns dictionary with keys: 'cash', 'portfolio_value', 'unrealized_pnl', 'realized_pnl'
        """
        pass

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: str,  # 'BUY' or 'SELL'
        quantity: int,
        order_type: str = "LIMIT",  # 'MARKET', 'LIMIT', 'STOP_MARKET'
        price: float = 0.0,
        stop_price: float = 0.0,
        stop_loss: float = 0.0,
        target: float = 0.0,
        product_type: str = "CNC",  # 'CNC', 'INTRADAY', 'MARGIN'
        tag: str = ""
    ) -> Dict[str, Any]:
        """
        Places order and returns standard order status dictionary.
        """
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancels pending order by ID.
        """
        pass

    @abstractmethod
    def get_open_positions(self) -> List[Dict[str, Any]]:
        """
        Returns list of open position dictionaries.
        """
        pass

    @abstractmethod
    def get_order_history(self) -> List[Dict[str, Any]]:
        """
        Returns full order execution audit log.
        """
        pass
