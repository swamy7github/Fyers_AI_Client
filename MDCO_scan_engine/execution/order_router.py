import math
import logging
import pandas as pd
from typing import Dict, List, Any, Optional
from execution.base_broker import AbstractBroker
from execution.paper_broker import PaperBroker
from execution.fyers_order_manager import FyersOrderManager
from data.fyers_client import FyersDataClient

logger = logging.getLogger("OrderRouter")

class OrderRouter:
    """
    Unified Strategy Order Router & Multi-Timeframe Risk Management Guard.
    Handles both Long (BUY) and Short Intraday MIS (SELL) orders.
    
    Workflow:
    1. Daily Screening: Receives daily breakout/breakdown signals (VCP, Cup & Handle, Darvas, Momentum).
    2. 5m Intraday ORB Trigger: Evaluates 5-minute intraday candles for Opening Range Breakout/Breakdown.
    3. Risk Sizing & Execution: Calculates risk-managed share quantities and dispatches orders
       to Paper Broker, FYERS API V3, or OpenAlgo webhooks.
    """

    def __init__(self, broker: AbstractBroker, config: Dict[str, Any]):
        self.broker = broker
        self.config = config
        
        self.risk_cfg = config.get("portfolio_risk", {})
        self.timeframe_cfg = config.get("timeframe", {})
        
        self.risk_per_trade_pct = float(self.risk_cfg.get("risk_per_trade_pct", 1.0)) / 100.0
        self.max_alloc_per_stock_pct = float(self.risk_cfg.get("max_portfolio_allocation_per_stock_pct", 20.0)) / 100.0
        self.max_open_positions = int(self.risk_cfg.get("max_open_positions", 8))
        self.default_product_type = self.timeframe_cfg.get("default_product_type", "MIS")

    def evaluate_intraday_orb_trigger(self, symbol: str, daily_signal_row: pd.Series, intraday_5m_df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Evaluates a 5-minute chart for a Daily Breakout or Breakdown candidate using Opening Range (ORB).
        """
        if intraday_5m_df.empty or len(intraday_5m_df) < 5:
            return None

        # First 3 bars (09:15, 09:20, 09:25) = 15 minute Opening Range
        orb_bars = intraday_5m_df.iloc[:3]
        orb_high = orb_bars["high"].max()
        orb_low = orb_bars["low"].min()

        intraday_5m_df["vol_ma20"] = intraday_5m_df["volume"].rolling(20, min_periods=1).mean()
        post_orb_bars = intraday_5m_df.iloc[3:]
        
        vol_surge_mult_threshold = float(self.timeframe_cfg.get("intraday_volume_surge_mult", 1.5))
        signal_dir = str(daily_signal_row.get("signal", daily_signal_row.get("signal_type", "BUY"))).upper()

        for idx, bar in post_orb_bars.iterrows():
            vol_ma = bar["vol_ma20"] if bar["vol_ma20"] > 0 else 1.0
            vol_ratio = bar["volume"] / vol_ma
            
            # 1. Long Breakout
            if signal_dir == "BUY" and bar["close"] > orb_high and vol_ratio >= vol_surge_mult_threshold:
                entry_price = round(float(bar["close"]), 2)
                stop_loss = round(float(orb_low), 2)
                risk_per_share = entry_price - stop_loss
                
                if risk_per_share <= 0:
                    stop_loss = round(entry_price * 0.98, 2)
                    risk_per_share = entry_price - stop_loss

                target_price = round(entry_price + (2.5 * risk_per_share), 2)
                rr_ratio = round((target_price - entry_price) / risk_per_share, 2)

                return {
                    "symbol": symbol,
                    "signal_type": "BUY",
                    "trigger_time": str(bar["timestamp"]),
                    "orb_high": round(float(orb_high), 2),
                    "orb_low": round(float(orb_low), 2),
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "target_price": target_price,
                    "risk_reward": rr_ratio,
                    "volume_surge_mult": round(vol_ratio, 2),
                    "strategy": f"{daily_signal_row['strategy']} (5m ORB)",
                    "daily_score": daily_signal_row.get("composite_score", 100.0)
                }

            # 2. Short Breakdown
            elif signal_dir == "SELL" and bar["close"] < orb_low and vol_ratio >= vol_surge_mult_threshold:
                entry_price = round(float(bar["close"]), 2)
                stop_loss = round(float(orb_high), 2)
                risk_per_share = stop_loss - entry_price
                
                if risk_per_share <= 0:
                    stop_loss = round(entry_price * 1.02, 2)
                    risk_per_share = stop_loss - entry_price

                target_price = round(entry_price - (2.5 * risk_per_share), 2)
                rr_ratio = round((entry_price - target_price) / risk_per_share, 2)

                return {
                    "symbol": symbol,
                    "signal_type": "SELL",
                    "trigger_time": str(bar["timestamp"]),
                    "orb_high": round(float(orb_high), 2),
                    "orb_low": round(float(orb_low), 2),
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "target_price": target_price,
                    "risk_reward": rr_ratio,
                    "volume_surge_mult": round(vol_ratio, 2),
                    "strategy": f"{daily_signal_row['strategy']} (5m ORB Short)",
                    "daily_score": daily_signal_row.get("composite_score", 100.0)
                }

        return None

    def process_screening_signals(self, signals_df: pd.DataFrame, data_client: Optional[FyersDataClient] = None) -> List[Dict[str, Any]]:
        """
        Takes ranked breakout/breakdown signals from Screener, evaluates 5m intraday ORB triggers
        if enabled, calculates risk-managed quantities, and executes BUY/SELL orders.
        """
        if signals_df.empty:
            logger.info("No breakout signals received for order routing.")
            return []

        intraday_enabled = self.timeframe_cfg.get("intraday_execution_enabled", True)
        if intraday_enabled and data_client is None:
            data_client = FyersDataClient(use_mock=False)

        balance_info = self.broker.get_account_balance()
        account_value = balance_info["portfolio_value"]
        available_cash = balance_info["cash"]

        open_positions = self.broker.get_open_positions()
        if len(open_positions) >= self.max_open_positions:
            logger.warning(f"Maximum open position cap ({self.max_open_positions}) reached. Order routing paused.")
            return []

        executed_orders = []

        for idx, row in signals_df.iterrows():
            symbol = row["symbol"]
            strategy_name = row["strategy"]
            signal_side = str(row.get("signal", row.get("signal_type", "BUY"))).upper()
            entry_price = float(row["entry_price"])
            stop_loss = float(row["stop_loss"])
            target = float(row.get("target_price", row.get("target", 0.0)))
            tag = strategy_name

            # Evaluate 5m Intraday ORB trigger if enabled
            if intraday_enabled and data_client is not None:
                intraday_df = data_client.get_intraday_data(symbol, resolution="5")
                orb_trigger = self.evaluate_intraday_orb_trigger(symbol, row, intraday_df)
                if orb_trigger:
                    entry_price = orb_trigger["entry_price"]
                    stop_loss = orb_trigger["stop_loss"]
                    target = orb_trigger["target_price"]
                    tag = orb_trigger["strategy"]
                    signal_side = orb_trigger["signal_type"]
                    logger.info(f"? 5m Intraday ORB Triggered for {symbol} ({signal_side}) at ?{entry_price}")
                else:
                    logger.info(f"? {symbol}: Daily setup valid, but 5m Intraday ORB trigger condition pending.")
                    continue

            # Check if stock already held
            if any(pos["symbol"] == symbol for pos in open_positions):
                logger.info(f"Skipping {symbol}: Already held in open portfolio.")
                continue

            risk_per_share = abs(entry_price - stop_loss)
            if risk_per_share <= 0:
                logger.warning(f"Invalid stop loss for {symbol}: Entry={entry_price}, Stop={stop_loss}")
                continue

            # Risk-based position sizing
            risk_budget = account_value * self.risk_per_trade_pct
            qty_by_risk = math.floor(risk_budget / risk_per_share)

            max_alloc_budget = account_value * self.max_alloc_per_stock_pct
            qty_by_alloc = math.floor(max_alloc_budget / entry_price)

            qty_by_cash = math.floor(available_cash / entry_price)

            final_quantity = min(qty_by_risk, qty_by_alloc, qty_by_cash)

            if final_quantity <= 0:
                logger.info(f"Skipping {symbol}: Insufficient funds or tight risk limit (Qty = 0).")
                continue

            # Product type for shorting cash stock is always MIS (intraday)
            prod_type = "MIS" if signal_side == "SELL" else self.default_product_type

            # Execute order via broker
            order_res = self.broker.place_order(
                symbol=symbol,
                side=signal_side,
                quantity=final_quantity,
                order_type="LIMIT",
                price=entry_price,
                stop_loss=stop_loss,
                target=target,
                product_type=prod_type,
                tag=tag
            )

            if order_res.get("status") in ["EXECUTED", "SUBMITTED", "OPENALGO_MOCK_SUCCESS", "SUCCESS"]:
                executed_orders.append(order_res)
                available_cash -= (entry_price * final_quantity)

            if len(open_positions) + len(executed_orders) >= self.max_open_positions:
                logger.info(f"Reached portfolio max position limit ({self.max_open_positions}).")
                break

        return executed_orders
