import math
import logging
import pandas as pd
from typing import Dict, List, Any, Optional
from execution.base_broker import AbstractBroker
from execution.paper_broker import PaperBroker
from execution.fyers_order_manager import FyersOrderManager

logger = logging.getLogger("OrderRouter")

class OrderRouter:
    """
    Unified Strategy Order Router & Risk Management Guard.
    Receives breakout signals, calculates strict position sizing, verifies account controls,
    and dispatches orders to Paper Trading Broker or FYERS / OpenAlgo execution.
    """

    def __init__(self, broker: AbstractBroker, config: Dict[str, Any]):
        self.broker = broker
        self.config = config
        
        self.risk_cfg = config.get("portfolio_risk", {})
        self.risk_per_trade_pct = float(self.risk_cfg.get("risk_per_trade_pct", 1.0)) / 100.0
        self.max_alloc_per_stock_pct = float(self.risk_cfg.get("max_portfolio_allocation_per_stock_pct", 20.0)) / 100.0
        self.max_open_positions = int(self.risk_cfg.get("max_open_positions", 8))

    def process_screening_signals(self, signals_df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Takes ranked breakout signals from Screener, calculates risk-managed quantities,
        and executes BUY orders through the active broker.
        """
        if signals_df.empty:
            logger.info("No breakout signals received for order routing.")
            return []

        balance_info = self.broker.get_account_balance()
        account_value = balance_info["portfolio_value"]
        available_cash = balance_info["cash"]

        open_positions = self.broker.get_open_positions()
        if len(open_positions) >= self.max_open_positions:
            logger.warning(f"Maximum open position cap ({self.max_open_positions}) reached. Order routing paused.")
            return []

        executed_orders = []

        # Iterate through signals in order of consensus score rank
        for idx, row in signals_df.iterrows():
            symbol = row["symbol"]
            strategy_name = row["strategy"]
            entry_price = float(row["entry_price"])
            stop_loss = float(row["stop_loss"])
            target = float(row.get("target_price", row.get("target", 0.0)))

            # Check if stock already held
            if any(pos["symbol"] == symbol for pos in open_positions):
                logger.info(f"Skipping {symbol}: Already held in open portfolio.")
                continue

            risk_per_share = entry_price - stop_loss
            if risk_per_share <= 0:
                logger.warning(f"Invalid stop loss for {symbol}: Entry={entry_price}, Stop={stop_loss}")
                continue

            # 1. Risk-Based Position Sizing: Risk Amount = Equity * Risk %
            risk_budget = account_value * self.risk_per_trade_pct
            qty_by_risk = math.floor(risk_budget / risk_per_share)

            # 2. Maximum Capital Allocation Sizing: Max Allocation = Equity * 20%
            max_alloc_budget = account_value * self.max_alloc_per_stock_pct
            qty_by_alloc = math.floor(max_alloc_budget / entry_price)

            # 3. Available Cash Constraint Sizing
            qty_by_cash = math.floor(available_cash / entry_price)

            # Final Quantity is constrained by all 3 risk rules
            final_quantity = min(qty_by_risk, qty_by_alloc, qty_by_cash)

            if final_quantity <= 0:
                logger.info(f"Skipping {symbol}: Insufficient funds or tight risk limit (Qty = 0).")
                continue

            # Execute order via broker
            order_res = self.broker.place_order(
                symbol=symbol,
                side="BUY",
                quantity=final_quantity,
                order_type="LIMIT",
                price=entry_price,
                stop_loss=stop_loss,
                target=target,
                product_type="CNC",
                tag=strategy_name
            )

            if order_res.get("status") in ["EXECUTED", "SUBMITTED", "OPENALGO_MOCK_SUCCESS", "SUCCESS"]:
                executed_orders.append(order_res)
                available_cash -= (entry_price * final_quantity)

            if len(open_positions) + len(executed_orders) >= self.max_open_positions:
                logger.info(f"Reached portfolio max position limit ({self.max_open_positions}).")
                break

        return executed_orders
