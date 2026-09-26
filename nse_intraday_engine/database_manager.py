"""
DatabaseManager
===============
Thin, dependency-free wrapper around SQLite that owns the schema and every
read/write path used by the rest of the engine (screener snapshots, signals,
orders, positions, trades and the end-of-day metrics rollup).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = _row_factory
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        if not SCHEMA_FILE.exists():
            raise FileNotFoundError(f"schema.sql not found at {SCHEMA_FILE}")
        self._conn.executescript(SCHEMA_FILE.read_text())
        self._conn.commit()

    @contextmanager
    def cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- universe
    def insert_universe_snapshot(self, trade_date: str, rows: Iterable[dict]) -> None:
        with self.cursor() as cur:
            for r in rows:
                cur.execute(
                    """INSERT OR REPLACE INTO universe_snapshot
                       (trade_date, symbol, sector_index, avg_turnover, spread_pct, rank)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (trade_date, r["symbol"], r["sector_index"], r.get("avg_turnover"),
                     r.get("spread_pct"), r.get("rank")),
                )

    # ---------------------------------------------------------------- signals
    def insert_signal(self, trade_date: str, signal, taken: bool = False) -> int:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO signals
                   (trade_date, "timestamp", symbol, direction, trigger_price, atr,
                    stock_rs, sector_rs, reason, taken)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trade_date, signal.timestamp.isoformat(), signal.symbol, signal.direction,
                 signal.trigger_price, signal.atr, signal.stock_rs, signal.sector_rs,
                 signal.reason, int(taken)),
            )
            return cur.lastrowid

    # ---------------------------------------------------------------- orders
    def insert_order(self, order: dict) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO orders
                   (order_id, "timestamp", symbol, side, qty, order_type,
                    requested_price, executed_price, slippage_pct, status, mode, position_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order["order_id"], order["timestamp"], order["symbol"], order["side"],
                 order["qty"], order.get("order_type", "MARKET"), order.get("requested_price"),
                 order.get("executed_price"), order.get("slippage_pct"),
                 order.get("status", "FILLED"), order["mode"], order.get("position_id")),
            )

    # ---------------------------------------------------------------- positions
    def insert_position(self, pos: dict) -> int:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO positions
                   (symbol, side, initial_qty, total_qty, avg_entry_price, atr_at_entry,
                    stop_loss, target1, mode, opened_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pos["symbol"], pos["side"], pos["initial_qty"], pos["total_qty"],
                 pos["avg_entry_price"], pos["atr_at_entry"], pos["stop_loss"], pos["target1"],
                 pos["mode"], pos["opened_at"], pos["opened_at"]),
            )
            return cur.lastrowid

    def update_position(self, position_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = datetime.now().isoformat()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.cursor() as cur:
            cur.execute(
                f"UPDATE positions SET {cols} WHERE position_id = ?",
                (*fields.values(), position_id),
            )

    def get_open_positions(self, mode: Optional[str] = None) -> list[dict]:
        q = "SELECT * FROM positions WHERE status = 'OPEN'"
        params: tuple = ()
        if mode:
            q += " AND mode = ?"
            params = (mode,)
        with self.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall()

    def get_open_position_for_symbol(self, symbol: str, mode: str) -> Optional[dict]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM positions WHERE symbol = ? AND mode = ? AND status = 'OPEN'",
                (symbol, mode),
            )
            return cur.fetchone()

    def close_position(self, position_id: int, closed_at: str) -> None:
        self.update_position(position_id, status="CLOSED", closed_at=closed_at)

    # ---------------------------------------------------------------- trades
    def insert_trade(self, trade: dict) -> int:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO trades
                   (position_id, symbol, side, qty, entry_price, exit_price, entry_time,
                    exit_time, gross_pnl, charges, net_pnl, r_multiple, exit_reason,
                    is_pyramid_leg, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trade["position_id"], trade["symbol"], trade["side"], trade["qty"],
                 trade["entry_price"], trade["exit_price"], trade["entry_time"],
                 trade["exit_time"], trade["gross_pnl"], trade["charges"], trade["net_pnl"],
                 trade.get("r_multiple"), trade["exit_reason"],
                 int(trade.get("is_pyramid_leg", False)), trade["mode"]),
            )
            return cur.lastrowid

    def get_trades(self, mode: Optional[str] = None,
                    date_from: Optional[str] = None,
                    date_to: Optional[str] = None) -> list[dict]:
        q = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []
        if mode:
            q += " AND mode = ?"
            params.append(mode)
        if date_from:
            q += " AND exit_time >= ?"
            params.append(date_from)
        if date_to:
            q += " AND exit_time <= ?"
            params.append(date_to)
        q += " ORDER BY exit_time"
        with self.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall()

    # ---------------------------------------------------------------- metrics
    def upsert_daily_metric(self, metric: dict) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO daily_metrics
                   (trade_date, mode, gross_pnl, total_charges, net_pnl, num_trades,
                    wins, losses, equity_close)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(trade_date, mode) DO UPDATE SET
                     gross_pnl=excluded.gross_pnl, total_charges=excluded.total_charges,
                     net_pnl=excluded.net_pnl, num_trades=excluded.num_trades,
                     wins=excluded.wins, losses=excluded.losses,
                     equity_close=excluded.equity_close""",
                (metric["trade_date"], metric["mode"], metric["gross_pnl"],
                 metric["total_charges"], metric["net_pnl"], metric["num_trades"],
                 metric["wins"], metric["losses"], metric["equity_close"]),
            )

    def get_equity_curve(self, mode: str) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT trade_date, equity_close FROM daily_metrics "
                "WHERE mode = ? ORDER BY trade_date",
                (mode,),
            )
            return cur.fetchall()
