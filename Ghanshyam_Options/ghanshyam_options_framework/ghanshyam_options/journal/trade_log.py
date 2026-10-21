"""Append-only CSV trade journal (guide p12: post-market audit & journaling).

Files (inside ``paths.log_dir``):

* ``trades_YYYYMMDD.csv`` — one row per closed trade, ``TRADE_COLUMNS`` schema,
  the date in the file name is the *trade date* (so a multi-day backtest
  writes one file per session, like the paper runner does).
* ``daily_summary.csv`` — one row per (date, instrument, mode).
* ``signals_YYYYMMDD.csv`` — every accepted/rejected signal (optional).

Rows are appended with a header written once; nothing is ever rewritten.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

from ..models import DailySummary, Signal, Trade

logger = logging.getLogger("ghanshyam.journal")

TRADE_COLUMNS: List[str] = [
    "trade_id", "mode", "instrument", "underlying", "strategy", "signal_ts", "direction", "option_symbol",
    "strike", "expiry", "entry_ts", "entry_price", "qty", "lots", "sl", "target", "exit_ts", "exit_price",
    "exit_reason", "gross_pnl", "charges", "net_pnl", "r_multiple", "confidence", "premium_model",
    "entry_spot", "exit_spot", "spot_sl", "spot_target", "planned_r", "slippage",
    "max_favourable_premium", "max_adverse_premium", "hold_minutes", "notes",
]

DAILY_COLUMNS: List[str] = [
    "trade_date", "instrument", "mode", "trades", "wins", "losses", "gross_pnl", "charges", "net_pnl",
    "peak_profit", "max_intraday_drawdown", "consecutive_losses", "lockout_reason", "signals_generated",
    "signals_rejected", "equity_end",
]

SIGNAL_COLUMNS: List[str] = [
    "ts", "instrument", "strategy", "direction", "spot_level", "stop_loss", "target", "confidence", "accepted",
    "reject_reason", "reason",
]


def _fmt(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".") if v == v else ""
    if v is None:
        return ""
    return v


class TradeLog:
    def __init__(self, log_dir: Path | str, mode: str = "backtest", echo: bool = True):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.echo = echo
        self.written: List[Path] = []

    # -- paths ----------------------------------------------------------------------
    def trades_path(self, trade_date: date) -> Path:
        return self.dir / f"trades_{trade_date.strftime('%Y%m%d')}.csv"

    @property
    def daily_path(self) -> Path:
        return self.dir / "daily_summary.csv"

    def signals_path(self, trade_date: date) -> Path:
        return self.dir / f"signals_{trade_date.strftime('%Y%m%d')}.csv"

    # -- writes ---------------------------------------------------------------------
    def _append(self, path: Path, columns: List[str], rows: Iterable[dict]) -> None:
        new_file = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            if new_file:
                w.writeheader()
            for row in rows:
                w.writerow({k: _fmt(row.get(k)) for k in columns})
        if path not in self.written:
            self.written.append(path)

    def log_trade(self, trade: Trade) -> Path:
        row = asdict(trade)
        row["mode"] = row.get("mode") or self.mode
        path = self.trades_path(trade.entry_ts.date())
        self._append(path, TRADE_COLUMNS, [row])
        if self.echo:
            logger.info("TRADE %s %s %s %s qty=%d entry=%.2f exit=%.2f (%s) net=%.2f R=%s",
                        trade.trade_id, trade.instrument, trade.strategy, trade.direction, trade.qty,
                        trade.entry_price, trade.exit_price, trade.exit_reason, trade.net_pnl,
                        f"{trade.r_multiple:.2f}" if trade.r_multiple is not None else "n/a")
        return path

    def log_daily(self, summary: DailySummary) -> Path:
        self._append(self.daily_path, DAILY_COLUMNS, [asdict(summary)])
        return self.daily_path

    def log_signals(self, trade_date: date, accepted: Iterable[Signal], rejected: Iterable[tuple[Signal, str]]) -> Optional[Path]:
        rows = []
        for s in accepted:
            rows.append({"ts": s.ts, "instrument": s.instrument, "strategy": s.strategy, "direction": s.option_type.value,
                         "spot_level": s.spot_level, "stop_loss": s.stop_loss, "target": s.target,
                         "confidence": s.confidence, "accepted": 1, "reject_reason": "", "reason": s.reason})
        for s, why in rejected:
            rows.append({"ts": s.ts, "instrument": s.instrument, "strategy": s.strategy, "direction": s.option_type.value,
                         "spot_level": s.spot_level, "stop_loss": s.stop_loss, "target": s.target,
                         "confidence": s.confidence, "accepted": 0, "reject_reason": why, "reason": s.reason})
        if not rows:
            return None
        path = self.signals_path(trade_date)
        self._append(path, SIGNAL_COLUMNS, rows)
        return path

    # -- reads ----------------------------------------------------------------------
    def read_trades(self, start: Optional[date] = None, end: Optional[date] = None) -> pd.DataFrame:
        frames = []
        for p in sorted(self.dir.glob("trades_*.csv")):
            try:
                d = datetime.strptime(p.stem.split("_")[1], "%Y%m%d").date()
            except (IndexError, ValueError):
                continue
            if (start and d < start) or (end and d > end):
                continue
            frames.append(pd.read_csv(p))
        if not frames:
            return pd.DataFrame(columns=TRADE_COLUMNS)
        df = pd.concat(frames, ignore_index=True)
        for col in ("signal_ts", "entry_ts", "exit_ts"):
            df[col] = pd.to_datetime(df[col], errors="coerce")
        return df

    def read_daily(self) -> pd.DataFrame:
        if not self.daily_path.exists():
            return pd.DataFrame(columns=DAILY_COLUMNS)
        df = pd.read_csv(self.daily_path)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        return df
