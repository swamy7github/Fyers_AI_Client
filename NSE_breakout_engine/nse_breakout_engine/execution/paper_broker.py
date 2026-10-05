import time
import uuid
from typing import Dict, List, Any, Optional
from execution.base_broker import AbstractBroker

class PaperBroker(AbstractBroker):
    """
    Realistic Paper Trading Broker Simulation Engine.
    Tracks cash balance, open positions, active stop-loss/targets, slippage, and P&L.
    """

    def __init__(self, initial_cash: float = 1000000.0, slippage_pct: float = 0.05, commission_pct: float = 0.05):
        self.cash = float(initial_cash)
        self.initial_cash = float(initial_cash)
        self.slippage_pct = float(slippage_pct) / 100.0
        self.commission_pct = float(commission_pct) / 100.0
        
        self.positions: Dict[str, Dict[str, Any]] = {}  # symbol -> position dict
        self.orders: List[Dict[str, Any]] = []          # list of order execution logs
        self.realized_pnl = 0.0

    def get_account_balance(self) -> Dict[str, float]:
        unrealized_pnl = sum(pos.get("unrealized_pnl", 0.0) for pos in self.positions.values())
        positions_value = sum(pos["quantity"] * pos["current_price"] for pos in self.positions.values())
        total_portfolio_value = self.cash + positions_value

        return {
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "positions_value": positions_value,
            "portfolio_value": total_portfolio_value,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "total_return_pct": ((total_portfolio_value - self.initial_cash) / self.initial_cash) * 100.0
        }

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "MARKET",
        price: float = 0.0,
        stop_price: float = 0.0,
        stop_loss: float = 0.0,
        target: float = 0.0,
        product_type: str = "CNC",
        tag: str = ""
    ) -> Dict[str, Any]:
        
        if quantity <= 0:
            return {"status": "REJECTED", "reason": "Quantity must be > 0"}

        side = side.upper()
        execution_price = price if price > 0 else 100.0 # Default if price unprovided

        # Apply realistic slippage model
        if side == "BUY":
            fill_price = execution_price * (1.0 + self.slippage_pct)
        else:
            fill_price = execution_price * (1.0 - self.slippage_pct)

        gross_cost = fill_price * quantity
        commission = gross_cost * self.commission_pct

        order_id = f"PAPER_ORD_{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}"

        if side == "BUY":
            total_cost = gross_cost + commission
            if self.cash < total_cost:
                return {
                    "order_id": order_id,
                    "status": "REJECTED",
                    "reason": f"Insufficient cash (Required: ₹{total_cost:.2f}, Available: ₹{self.cash:.2f})"
                }
            
            self.cash -= total_cost

            # Update or create position
            if symbol in self.positions:
                existing = self.positions[symbol]
                total_qty = existing["quantity"] + quantity
                avg_price = ((existing["quantity"] * existing["entry_price"]) + (quantity * fill_price)) / total_qty
                existing["quantity"] = total_qty
                existing["entry_price"] = avg_price
                existing["current_price"] = fill_price
                existing["stop_loss"] = stop_loss if stop_loss > 0 else existing["stop_loss"]
                existing["target"] = target if target > 0 else existing["target"]
            else:
                self.positions[symbol] = {
                    "symbol": symbol,
                    "quantity": quantity,
                    "entry_price": fill_price,
                    "current_price": fill_price,
                    "stop_loss": stop_loss,
                    "target": target,
                    "unrealized_pnl": 0.0,
                    "strategy_tag": tag,
                    "product_type": product_type
                }

        elif side == "SELL":
            if symbol not in self.positions or self.positions[symbol]["quantity"] < quantity:
                return {
                    "order_id": order_id,
                    "status": "REJECTED",
                    "reason": f"Insufficient position size to sell {quantity} shares of {symbol}"
                }

            pos = self.positions[symbol]
            pnl = (fill_price - pos["entry_price"]) * quantity - commission
            self.realized_pnl += pnl
            self.cash += (fill_price * quantity) - commission

            pos["quantity"] -= quantity
            if pos["quantity"] == 0:
                del self.positions[symbol]

        order_record = {
            "order_id": order_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "fill_price": round(fill_price, 2),
            "commission": round(commission, 2),
            "status": "EXECUTED",
            "stop_loss": stop_loss,
            "target": target,
            "tag": tag
        }
        self.orders.append(order_record)
        return order_record

    def process_market_tick(self, symbol: str, current_price: float, high_price: float = 0.0, low_price: float = 0.0):
        """
        Updates position current market value and auto-triggers Stop-Loss or Target exits.
        """
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]
        pos["current_price"] = current_price
        pos["unrealized_pnl"] = (current_price - pos["entry_price"]) * pos["quantity"]

        low_check = low_price if low_price > 0 else current_price
        high_check = high_price if high_price > 0 else current_price

        # Check Stop Loss trigger
        if pos["stop_loss"] > 0 and low_check <= pos["stop_loss"]:
            self.place_order(
                symbol=symbol,
                side="SELL",
                quantity=pos["quantity"],
                order_type="STOP_MARKET",
                price=pos["stop_loss"],
                tag=f"AUTO_STOP_LOSS ({pos['strategy_tag']})"
            )

        # Check Target trigger
        elif pos["target"] > 0 and high_check >= pos["target"]:
            self.place_order(
                symbol=symbol,
                side="SELL",
                quantity=pos["quantity"],
                order_type="LIMIT",
                price=pos["target"],
                tag=f"AUTO_TARGET_PROFIT ({pos['strategy_tag']})"
            )

    def cancel_order(self, order_id: str) -> bool:
        for ord_rec in self.orders:
            if ord_rec["order_id"] == order_id and ord_rec["status"] == "PENDING":
                ord_rec["status"] = "CANCELLED"
                return True
        return False

    def get_open_positions(self) -> List[Dict[str, Any]]:
        return list(self.positions.values())

    def get_order_history(self) -> List[Dict[str, Any]]:
        return self.orders
