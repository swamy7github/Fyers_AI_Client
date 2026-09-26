"""
OrderManager
============
Abstract base implementing the full trade-management lifecycle (risk-based
sizing, pyramiding, partial profit booking, breakeven, trailing exit, and
the mandatory 15:15 square-off) on top of two concrete fill mechanics:

  * PaperTrader    -- fills against live Fyers quotes with simulated slippage.
  * BacktestTrader -- fills against historical bars with simulated slippage.
"""
from __future__ import annotations

import itertools
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from database_manager import DatabaseManager
from signal_engine import Signal
from utils import apply_slippage, calculate_charges, setup_logger


@dataclass
class OpenPosition:
    position_id: int
    symbol: str
    side: str  # "LONG" | "SHORT"
    total_qty: int
    initial_qty: int
    avg_entry_price: float
    atr_at_entry: float
    stop_loss: float
    target1: float
    opened_at: datetime
    target1_hit: bool = False
    breakeven_moved: bool = False
    pyramided: bool = False
    high_water: float = field(default=0.0)   # best favorable price seen, for trailing
    low_water: float = field(default=float("inf"))


class OrderManager(ABC):
    def __init__(self, db: DatabaseManager, config, mode: str):
        self.db = db
        self.cfg = config
        self.mode = mode
        self.log = setup_logger(f"order_manager.{mode.lower()}", config.LOG_DIR, config.LOG_LEVEL)
        self.open_positions: dict[str, OpenPosition] = {}
        self.equity = config.PORTFOLIO_EQUITY
        self._order_seq = itertools.count(1)

    # --------------------------------------------------------- fill mechanics
    @abstractmethod
    def _reference_price(self, symbol: str, bar: Optional[pd.Series]) -> float:
        """Current tradeable price for `symbol` -- live LTP (paper) or the
        current backtest bar's close (backtest)."""

    def _next_order_id(self) -> str:
        return f"{self.mode[:2]}-{next(self._order_seq):08d}-{uuid.uuid4().hex[:6]}"

    def _fill(self, symbol: str, side: str, qty: int, ref_price: float,
              timestamp: datetime, position_id: Optional[int] = None) -> dict:
        exec_price = apply_slippage(ref_price, side, self.cfg.SLIPPAGE_PCT)
        order = {
            "order_id": self._next_order_id(),
            "timestamp": timestamp.isoformat(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "requested_price": ref_price,
            "executed_price": exec_price,
            "slippage_pct": self.cfg.SLIPPAGE_PCT,
            "status": "FILLED",
            "mode": self.mode,
            "position_id": position_id,
        }
        self.db.insert_order(order)
        return order

    # ------------------------------------------------------------ sizing
    def _position_size(self, atr: float) -> int:
        risk_amount = self.equity * self.cfg.RISK_PER_TRADE_PCT / 100
        stop_distance = max(atr * self.cfg.STOP_LOSS_ATR_MULT, 1e-6)
        return max(int(risk_amount // stop_distance), 0)

    # ------------------------------------------------------------ entries
    def open_position(self, signal: Signal, bar: Optional[pd.Series] = None) -> Optional[OpenPosition]:
        if signal.symbol in self.open_positions:
            return None  # one open position per symbol at a time
        qty = self._position_size(signal.atr)
        if qty <= 0:
            return None

        side_for_fill = "BUY" if signal.direction == "LONG" else "SELL"
        ref_price = self._reference_price(signal.symbol, bar) or signal.trigger_price
        order = self._fill(signal.symbol, side_for_fill, qty, ref_price, signal.timestamp)
        entry_price = order["executed_price"]

        stop_loss = (entry_price - signal.atr * self.cfg.STOP_LOSS_ATR_MULT if signal.direction == "LONG"
                     else entry_price + signal.atr * self.cfg.STOP_LOSS_ATR_MULT)
        target1 = (entry_price + signal.atr * self.cfg.TARGET1_ATR_MULT if signal.direction == "LONG"
                   else entry_price - signal.atr * self.cfg.TARGET1_ATR_MULT)

        position_id = self.db.insert_position({
            "symbol": signal.symbol, "side": signal.direction, "initial_qty": qty,
            "total_qty": qty, "avg_entry_price": entry_price, "atr_at_entry": signal.atr,
            "stop_loss": stop_loss, "target1": target1, "mode": self.mode,
            "opened_at": signal.timestamp.isoformat(),
        })
        pos = OpenPosition(position_id, signal.symbol, signal.direction, qty, qty,
                            entry_price, signal.atr, stop_loss, target1, signal.timestamp,
                            high_water=entry_price, low_water=entry_price)
        self.open_positions[signal.symbol] = pos
        self.log.info("OPEN %s %s qty=%d @ %.2f stop=%.2f t1=%.2f",
                       signal.direction, signal.symbol, qty, entry_price, stop_loss, target1)
        return pos

    # --------------------------------------------------------- pyramiding
    def maybe_pyramid(self, pos: OpenPosition, current_price: float, vwap: float,
                       timestamp: datetime, bar: Optional[pd.Series] = None) -> None:
        if pos.pyramided:
            return
        favorable_move = ((current_price - pos.avg_entry_price) if pos.side == "LONG"
                           else (pos.avg_entry_price - current_price))
        if favorable_move < pos.atr_at_entry * self.cfg.PYRAMID_TRIGGER_ATR_MULT:
            return
        # "Successful retest of VWAP without violating it": price came back
        # close to VWAP this bar but stayed on the correct side of it.
        retested = abs(current_price - vwap) / vwap * 100 <= self.cfg.OPEN_EXTREME_TOLERANCE_PCT * 5
        held_side = (current_price >= vwap) if pos.side == "LONG" else (current_price <= vwap)
        if not (retested and held_side):
            return

        add_qty = max(int(pos.initial_qty * self.cfg.PYRAMID_QTY_PCT), 1)
        side_for_fill = "BUY" if pos.side == "LONG" else "SELL"
        ref_price = self._reference_price(pos.symbol, bar) or current_price
        order = self._fill(pos.symbol, side_for_fill, add_qty, ref_price, timestamp, pos.position_id)
        fill_price = order["executed_price"]

        new_total = pos.total_qty + add_qty
        pos.avg_entry_price = (pos.avg_entry_price * pos.total_qty + fill_price * add_qty) / new_total
        pos.total_qty = new_total
        pos.pyramided = True
        self.db.update_position(pos.position_id, total_qty=pos.total_qty,
                                 avg_entry_price=pos.avg_entry_price, pyramided=1)
        self.log.info("PYRAMID %s add=%d @ %.2f new_avg=%.2f",
                       pos.symbol, add_qty, fill_price, pos.avg_entry_price)

    # ---------------------------------------------------- partial profit
    def maybe_take_partial(self, pos: OpenPosition, current_price: float,
                            timestamp: datetime, bar: Optional[pd.Series] = None) -> None:
        if pos.target1_hit:
            return
        hit = (current_price >= pos.target1) if pos.side == "LONG" else (current_price <= pos.target1)
        if not hit:
            return

        exit_qty = min(max(int(pos.total_qty * self.cfg.TARGET1_BOOK_PCT), 1), pos.total_qty)
        self._close_qty(pos, exit_qty, current_price, timestamp, "TARGET1_PARTIAL", bar)

        pos.target1_hit = True
        pos.stop_loss = pos.avg_entry_price  # trail remainder to breakeven
        pos.breakeven_moved = True
        if pos.total_qty > 0:
            self.db.update_position(pos.position_id, stop_loss=pos.stop_loss,
                                     target1_hit=1, breakeven_moved=1)

    # -------------------------------------------------------- exits / stops
    def update_trailing_stop(self, pos: OpenPosition, current_price: float, vwap: float) -> None:
        if not pos.target1_hit or pos.total_qty <= 0:
            return  # trailing only applies to the runner, after T1 is booked
        if pos.side == "LONG":
            pos.high_water = max(pos.high_water, current_price)
            trail = pos.high_water - pos.atr_at_entry * self.cfg.TRAIL_ATR_MULT
            pos.stop_loss = max(pos.stop_loss, trail)
        else:
            pos.low_water = min(pos.low_water, current_price)
            trail = pos.low_water + pos.atr_at_entry * self.cfg.TRAIL_ATR_MULT
            pos.stop_loss = min(pos.stop_loss, trail)
        self.db.update_position(pos.position_id, stop_loss=pos.stop_loss)

    def check_stop_and_vwap_breach(self, pos: OpenPosition, current_price: float,
                                    vwap: float, timestamp: datetime,
                                    bar: Optional[pd.Series] = None) -> bool:
        stop_hit = (current_price <= pos.stop_loss) if pos.side == "LONG" else (current_price >= pos.stop_loss)
        vwap_breach = False
        if pos.target1_hit:  # the runner also exits on a VWAP breach
            vwap_breach = (current_price < vwap) if pos.side == "LONG" else (current_price > vwap)
        if stop_hit or vwap_breach:
            if vwap_breach and not stop_hit:
                reason = "TRAIL_VWAP_BREACH"
            elif pos.breakeven_moved:
                at_breakeven = abs(pos.stop_loss - pos.avg_entry_price) < 1e-6
                reason = "BREAKEVEN_STOP" if at_breakeven else "TRAILING_STOP"
            else:
                reason = "HARD_STOP"
            self._close_qty(pos, pos.total_qty, current_price, timestamp, reason, bar)
            return True
        return False

    def square_off(self, pos: OpenPosition, current_price: float, timestamp: datetime,
                    bar: Optional[pd.Series] = None) -> None:
        if pos.total_qty > 0:
            self._close_qty(pos, pos.total_qty, current_price, timestamp, "EOD_SQUARE_OFF", bar)

    def square_off_all(self, timestamp: datetime, price_lookup) -> None:
        for symbol in list(self.open_positions.keys()):
            pos = self.open_positions[symbol]
            price = price_lookup(symbol) or pos.avg_entry_price
            self.square_off(pos, price, timestamp)

    # ------------------------------------------------------------- helpers
    def _close_qty(self, pos: OpenPosition, qty: int, current_price: float,
                    timestamp: datetime, reason: str, bar: Optional[pd.Series] = None) -> None:
        side_for_fill = "SELL" if pos.side == "LONG" else "BUY"
        ref_price = self._reference_price(pos.symbol, bar) or current_price
        order = self._fill(pos.symbol, side_for_fill, qty, ref_price, timestamp, pos.position_id)
        exit_price = order["executed_price"]

        buy_value = (pos.avg_entry_price if pos.side == "LONG" else exit_price) * qty
        sell_value = (exit_price if pos.side == "LONG" else pos.avg_entry_price) * qty
        charges = calculate_charges(buy_value, sell_value, self.cfg)
        gross_pnl = ((exit_price - pos.avg_entry_price) * qty if pos.side == "LONG"
                     else (pos.avg_entry_price - exit_price) * qty)
        net_pnl = gross_pnl - charges.total
        r_multiple = (gross_pnl / (pos.atr_at_entry * self.cfg.STOP_LOSS_ATR_MULT * qty)
                      if pos.atr_at_entry else None)

        self.db.insert_trade({
            "position_id": pos.position_id, "symbol": pos.symbol, "side": pos.side, "qty": qty,
            "entry_price": pos.avg_entry_price, "exit_price": exit_price,
            "entry_time": pos.opened_at.isoformat(), "exit_time": timestamp.isoformat(),
            "gross_pnl": gross_pnl, "charges": charges.total, "net_pnl": net_pnl,
            "r_multiple": r_multiple, "exit_reason": reason,
            "is_pyramid_leg": pos.pyramided and qty != pos.initial_qty, "mode": self.mode,
        })
        self.equity += net_pnl
        pos.total_qty -= qty
        self.log.info("EXIT %s qty=%d @ %.2f reason=%s net_pnl=%.2f",
                       pos.symbol, qty, exit_price, reason, net_pnl)

        if pos.total_qty <= 0:
            self.db.close_position(pos.position_id, timestamp.isoformat())
            self.open_positions.pop(pos.symbol, None)
        else:
            self.db.update_position(pos.position_id, total_qty=pos.total_qty)

    # -------------------------------------------------- per-bar orchestration
    def manage_bar(self, symbol: str, bar: pd.Series) -> None:
        """Runs the full stop -> partial-profit -> pyramid -> trailing
        pipeline for one existing open position, for the current bar."""
        pos = self.open_positions.get(symbol)
        if not pos:
            return
        price = float(bar["close"])
        vwap = float(bar["vwap"]) if "vwap" in bar and pd.notna(bar["vwap"]) else price
        if self.check_stop_and_vwap_breach(pos, price, vwap, bar["timestamp"], bar):
            return
        self.maybe_take_partial(pos, price, bar["timestamp"], bar)
        self.maybe_pyramid(pos, price, vwap, bar["timestamp"], bar)
        self.update_trailing_stop(pos, price, vwap)


class PaperTrader(OrderManager):
    """Fills against the latest live Fyers quote; used when MODE == PAPER_TRADING."""

    def __init__(self, db: DatabaseManager, config, fyers_client):
        super().__init__(db, config, mode="PAPER_TRADING")
        self.fy = fyers_client

    def _reference_price(self, symbol: str, bar: Optional[pd.Series] = None) -> float:
        fy_symbol = f"NSE:{symbol}-EQ"
        quote = self.fy.get_quotes([fy_symbol]).get(fy_symbol, {})
        ltp = float(quote.get("lp", 0) or 0)
        if ltp:
            return ltp
        return float(bar["close"]) if bar is not None else 0.0


class BacktestTrader(OrderManager):
    """Fills against the historical bar being replayed; used when MODE == BACKTEST."""

    def __init__(self, db: DatabaseManager, config):
        super().__init__(db, config, mode="BACKTEST")

    def _reference_price(self, symbol: str, bar: Optional[pd.Series] = None) -> float:
        return float(bar["close"]) if bar is not None else 0.0
