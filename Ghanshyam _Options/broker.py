"""
Broker Execution Adapters (Paper Trading & Fyers Live API)
"""

import csv
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional

from models import OptionType, Position, StrategyId

logger = logging.getLogger("GhanshyamTradingSystem")


class BaseBrokerAdapter(ABC):
    """Abstract Base Class for paper trading and live broker drivers."""

    @abstractmethod
    def place_option_order(
        self,
        symbol: str,
        underlying_code: str,
        option_type: OptionType,
        strike: float,
        quantity: int,
        option_ltp: float,
        timestamp: datetime,
        spot_entry: float,
        spot_sl: float,
        spot_target: float,
        strategy: StrategyId,
        option_sl: float,
        option_target: float,
        entry_reason: str = ""
    ) -> Optional[Position]:
        pass

    @abstractmethod
    def close_position(
        self,
        identifier: str,  # position_id or symbol
        current_ltp: float,
        timestamp: datetime,
        exit_reason: str
    ) -> Optional[Position]:
        pass

    @property
    @abstractmethod
    def positions(self) -> Dict[str, Position]:
        pass

    @property
    @abstractmethod
    def closed_positions(self) -> List[Position]:
        pass

    @abstractmethod
    def get_portfolio_summary(self) -> Dict:
        pass

    @abstractmethod
    def export_trades_to_csv(self, filepath: str = "trades_export.csv") -> str:
        pass


class PaperTradingBroker(BaseBrokerAdapter):
    """
    Simulated broker matching fills with bid-ask slippage and transaction costs.
    Stores active positions in a dictionary keyed by unique position_id.
    """

    def __init__(
        self,
        initial_capital: float = 500000.0,
        brokerage_per_order: float = 20.0,
        slippage: float = 0.5
    ):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.brokerage_per_order = brokerage_per_order
        self.slippage = slippage
        self._positions: Dict[str, Position] = {}  # pos_id -> Position
        self._closed_positions: List[Position] = []

    @property
    def positions(self) -> Dict[str, Position]:
        return self._positions

    @property
    def closed_positions(self) -> List[Position]:
        return self._closed_positions

    def place_option_order(
        self,
        symbol: str,
        underlying_code: str,
        option_type: OptionType,
        strike: float,
        quantity: int,
        option_ltp: float,
        timestamp: datetime,
        spot_entry: float,
        spot_sl: float,
        spot_target: float,
        strategy: StrategyId,
        option_sl: float,
        option_target: float,
        entry_reason: str = ""
    ) -> Optional[Position]:
        fill_price = option_ltp + self.slippage
        cost = fill_price * quantity + self.brokerage_per_order

        if cost > self.capital:
            logger.warning(f"PaperBroker: Margin shortfall. Required: ₹{cost:.2f}, Available: ₹{self.capital:.2f}")
            return None

        self.capital -= cost
        pos_id = f"POS_{underlying_code}_{timestamp.strftime('%Y%m%d%H%M%S')}_{len(self._closed_positions) + len(self._positions) + 1}"
        pos = Position(
            position_id=pos_id,
            symbol=symbol,
            underlying_code=underlying_code,
            option_type=option_type,
            strike=strike,
            quantity=quantity,
            entry_price=fill_price,
            entry_time=timestamp,
            stop_loss=option_sl,
            target=option_target,
            spot_entry=spot_entry,
            spot_sl=spot_sl,
            spot_target=spot_target,
            strategy=strategy,
            entry_reason=entry_reason,
            active=True
        )
        self._positions[pos_id] = pos
        logger.info(
            f"PaperBroker BUY: {symbol} (ID: {pos_id}) | Qty: {quantity} @ ₹{fill_price:.2f} | "
            f"SL: ₹{option_sl:.2f} | Tgt: ₹{option_target:.2f} | Reason: {entry_reason} | Cash: ₹{self.capital:.2f}"
        )
        return pos

    def close_position(
        self,
        identifier: str,  # position_id or symbol
        current_ltp: float,
        timestamp: datetime,
        exit_reason: str
    ) -> Optional[Position]:
        pos_key = None
        if identifier in self._positions:
            pos_key = identifier
        else:
            for k, p in self._positions.items():
                if p.symbol == identifier or k == identifier:
                    pos_key = k
                    break

        if not pos_key:
            return None

        pos = self._positions.pop(pos_key)
        exit_price = max(0.05, current_ltp - self.slippage)
        gross_pnl = (exit_price - pos.entry_price) * pos.quantity
        net_pnl = gross_pnl - (self.brokerage_per_order * 2.0)

        pos.exit_price = exit_price
        pos.exit_time = timestamp
        pos.pnl = net_pnl
        pos.exit_reason = exit_reason
        pos.active = False

        self.capital += (exit_price * pos.quantity) - self.brokerage_per_order
        self._closed_positions.append(pos)

        logger.info(
            f"PaperBroker EXIT: {pos.symbol} (ID: {pos.position_id}) | Reason: {exit_reason} | Exit: ₹{exit_price:.2f} | "
            f"Net PnL: ₹{net_pnl:.2f} | Balance: ₹{self.capital:.2f}"
        )
        return pos

    def get_portfolio_summary(self) -> Dict:
        realized = sum(p.pnl for p in self._closed_positions)
        win_trades = [p for p in self._closed_positions if p.pnl > 0]
        loss_trades = [p for p in self._closed_positions if p.pnl <= 0]
        win_rate = (len(win_trades) / len(self._closed_positions) * 100.0) if self._closed_positions else 0.0

        return {
            "initial_capital": self.initial_capital,
            "current_capital": self.capital,
            "total_trades": len(self._closed_positions),
            "wins": len(win_trades),
            "losses": len(loss_trades),
            "win_rate_pct": win_rate,
            "net_realized_pnl": realized,
            "active_positions": len(self._positions)
        }

    def export_trades_to_csv(self, filepath: str = "trades_export.csv") -> str:
        """Exports all executed and closed positions to CSV."""
        fieldnames = [
            "Trade_ID",
            "Underlying",
            "Symbol",
            "Option_Type",
            "Strike",
            "Quantity",
            "Entry_Time",
            "Exit_Time",
            "Strategy",
            "Entry_Reason",
            "Exit_Reason",
            "Spot_Entry",
            "Spot_SL",
            "Spot_Target",
            "Option_Entry_Price",
            "Option_Exit_Price",
            "Gross_PnL",
            "Brokerage_Charges",
            "Net_PnL"
        ]
        with open(filepath, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for pos in self._closed_positions:
                gross = ((pos.exit_price or pos.entry_price) - pos.entry_price) * pos.quantity if pos.exit_price else 0.0
                charges = 40.0 if pos.exit_price else 20.0
                writer.writerow({
                    "Trade_ID": pos.position_id,
                    "Underlying": pos.underlying_code,
                    "Symbol": pos.symbol,
                    "Option_Type": pos.option_type.value if hasattr(pos.option_type, 'value') else str(pos.option_type),
                    "Strike": pos.strike,
                    "Quantity": pos.quantity,
                    "Entry_Time": pos.entry_time.strftime("%Y-%m-%d %H:%M:%S") if pos.entry_time else "",
                    "Exit_Time": pos.exit_time.strftime("%Y-%m-%d %H:%M:%S") if pos.exit_time else "",
                    "Strategy": pos.strategy.value if hasattr(pos.strategy, 'value') else str(pos.strategy),
                    "Entry_Reason": pos.entry_reason,
                    "Exit_Reason": pos.exit_reason,
                    "Spot_Entry": pos.spot_entry,
                    "Spot_SL": pos.spot_sl,
                    "Spot_Target": pos.spot_target,
                    "Option_Entry_Price": pos.entry_price,
                    "Option_Exit_Price": pos.exit_price if pos.exit_price is not None else "",
                    "Gross_PnL": round(gross, 2),
                    "Brokerage_Charges": round(charges, 2),
                    "Net_PnL": round(pos.pnl, 2)
                })
        logger.info(f"Successfully exported {len(self._closed_positions)} trades to CSV: {filepath}")
        return filepath


class FyersClientAdapter(BaseBrokerAdapter):
    """Fyers API v3 Driver supporting quotes, history, and live/paper routing."""

    def __init__(
        self,
        fyers_client=None,
        is_paper: bool = True,
        paper_broker: Optional[PaperTradingBroker] = None
    ):
        self.client = fyers_client
        self.is_paper = is_paper
        self.paper_broker = paper_broker or PaperTradingBroker()

    @property
    def positions(self) -> Dict[str, Position]:
        return self.paper_broker.positions

    @property
    def closed_positions(self) -> List[Position]:
        return self.paper_broker.closed_positions

    def get_history(self, symbol: str, resolution: str = "5", range_from: str = "", range_to: str = "") -> List:
        if self.client and hasattr(self.client, "history"):
            try:
                data = {
                    "symbol": symbol,
                    "resolution": resolution,
                    "date_format": "1",
                    "range_from": range_from,
                    "range_to": range_to,
                    "cont_flag": "1"
                }
                response = self.client.history(data=data)
                return response.get("candles", []) if response and response.get("s") == "ok" else []
            except Exception as e:
                logger.error(f"Error fetching history from Fyers API: {e}")
        return []

    def get_quote(self, symbol: str) -> float:
        if self.client and hasattr(self.client, "quotes"):
            try:
                data = {"symbols": symbol}
                response = self.client.quotes(data=data)
                if response and response.get("s") == "ok":
                    d = response.get("d", [{}])
                    return float(d[0].get("v", {}).get("lp", 0.0))
            except Exception as e:
                logger.error(f"Error fetching quote for {symbol}: {e}")
        return 0.0

    def place_option_order(
        self,
        symbol: str,
        underlying_code: str,
        option_type: OptionType,
        strike: float,
        quantity: int,
        option_ltp: float,
        timestamp: datetime,
        spot_entry: float,
        spot_sl: float,
        spot_target: float,
        strategy: StrategyId,
        option_sl: float,
        option_target: float,
        entry_reason: str = ""
    ) -> Optional[Position]:
        if self.is_paper:
            return self.paper_broker.place_option_order(
                symbol=symbol,
                underlying_code=underlying_code,
                option_type=option_type,
                strike=strike,
                quantity=quantity,
                option_ltp=option_ltp,
                timestamp=timestamp,
                spot_entry=spot_entry,
                spot_sl=spot_sl,
                spot_target=spot_target,
                strategy=strategy,
                option_sl=option_sl,
                option_target=option_target,
                entry_reason=entry_reason
            )

        if self.client and hasattr(self.client, "place_order"):
            data = {
                "symbol": symbol,
                "qty": quantity,
                "type": 2,
                "side": 1,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": "False",
            }
            res = self.client.place_order(data=data)
            logger.info(f"Fyers Live Order Placed: {res}")
            return self.paper_broker.place_option_order(
                symbol=symbol,
                underlying_code=underlying_code,
                option_type=option_type,
                strike=strike,
                quantity=quantity,
                option_ltp=option_ltp,
                timestamp=timestamp,
                spot_entry=spot_entry,
                spot_sl=spot_sl,
                spot_target=spot_target,
                strategy=strategy,
                option_sl=option_sl,
                option_target=option_target,
                entry_reason=entry_reason
            )

        return None

    def close_position(
        self,
        identifier: str,  # position_id or symbol
        current_ltp: float,
        timestamp: datetime,
        exit_reason: str
    ) -> Optional[Position]:
        if self.is_paper:
            return self.paper_broker.close_position(
                identifier=identifier,
                current_ltp=current_ltp,
                timestamp=timestamp,
                exit_reason=exit_reason
            )

        if self.client and hasattr(self.client, "place_order"):
            pos_to_close = None
            if identifier in self.paper_broker.positions:
                pos_to_close = self.paper_broker.positions[identifier]
            else:
                for p in self.paper_broker.positions.values():
                    if p.symbol == identifier:
                        pos_to_close = p
                        break

            if pos_to_close:
                data = {
                    "symbol": pos_to_close.symbol,
                    "qty": pos_to_close.quantity,
                    "type": 2,
                    "side": -1,
                    "productType": "INTRADAY",
                    "limitPrice": 0,
                    "stopPrice": 0,
                    "validity": "DAY",
                    "disclosedQty": 0,
                    "offlineOrder": "False",
                }
                res = self.client.place_order(data=data)
                logger.info(f"Fyers Live Exit Placed: {res}")

        return self.paper_broker.close_position(
            identifier=identifier,
            current_ltp=current_ltp,
            timestamp=timestamp,
            exit_reason=exit_reason
        )

    def get_portfolio_summary(self) -> Dict:
        return self.paper_broker.get_portfolio_summary()

    def export_trades_to_csv(self, filepath: str = "trades_export.csv") -> str:
        return self.paper_broker.export_trades_to_csv(filepath=filepath)
