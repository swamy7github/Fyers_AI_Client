"""Logging, trade records, performance metrics, CSV trade log and EOD summary."""
from __future__ import annotations

import csv
import json
import logging
import math
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config.settings import LOG_DIR, TRADES_LOG_PATH

_LOGGER = logging.getLogger(__name__)

CSV_COLUMNS: List[str] = [
    "trade_id", "mode", "date", "event", "timestamp", "instrument", "option_symbol",
    "side", "strike", "expiry", "quantity", "price", "spot", "reason",
    "charges", "gross_pnl", "net_pnl",
]


# --------------------------------------------------------------------------- #
# Terminal logging
# --------------------------------------------------------------------------- #
class ColorFormatter(logging.Formatter):
    """Level-coloured formatter. Colours are disabled when stdout is not a TTY."""

    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;41m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True) -> None:
        super().__init__("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        return f"{self.COLORS.get(record.levelno, '')}{text}{self.RESET}"


def setup_logging(level: int = logging.INFO, log_file: Optional[Path] = None) -> None:
    """Configure root logging with a colour console handler and optional file."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ColorFormatter(use_color=sys.stdout.isatty()))
    root.addHandler(console)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(ColorFormatter(use_color=False))
        root.addHandler(file_handler)


# --------------------------------------------------------------------------- #
# Trade record and metrics
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    """A completed round trip (entry + exit) on one option contract."""

    trade_id: str
    mode: str
    instrument: str
    option_symbol: str
    side: str                 # BUY_CE or BUY_PE
    strike: float
    expiry: str               # ISO date
    quantity: int
    entry_time: datetime
    entry_price: float
    entry_spot: float
    exit_time: datetime
    exit_price: float
    exit_spot: float
    exit_reason: str
    charges: float
    gross_pnl: float = 0.0
    net_pnl: float = 0.0

    @property
    def trade_date(self) -> date:
        return self.entry_time.date()


@dataclass
class PerformanceMetrics:
    """Aggregate statistics for a list of trades (all money values in INR)."""

    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    gross_pnl: float = 0.0
    total_charges: float = 0.0
    total_pnl: float = 0.0          # net of charges
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if math.isinf(data["profit_factor"]):
            data["profit_factor"] = "inf"
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in data.items()}


def compute_max_drawdown(pnls: Iterable[float]) -> float:
    """Largest peak-to-trough fall of the cumulative PnL curve (starts at 0)."""
    equity = peak = max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def compute_metrics(trades: Iterable[Trade]) -> PerformanceMetrics:
    """Compute win rate, PnL, profit factor and drawdown from net trade PnLs.

    Trades are ordered by exit time for the drawdown calculation. A trade with
    net PnL exactly zero counts as neither a win nor a loss. Profit factor is
    gross profit divided by gross loss; it is ``inf`` when there are wins but no
    losses and ``0`` when there are no wins.
    """
    ordered = sorted(trades, key=lambda t: t.exit_time)
    if not ordered:
        return PerformanceMetrics()
    pnls = [t.net_pnl for t in ordered]
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(win_pnls), -sum(loss_pnls)
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = math.inf if gross_win > 0 else 0.0
    return PerformanceMetrics(
        total_trades=len(ordered),
        wins=len(win_pnls),
        losses=len(loss_pnls),
        win_rate_pct=100.0 * len(win_pnls) / len(ordered),
        gross_pnl=sum(t.gross_pnl for t in ordered),
        total_charges=sum(t.charges for t in ordered),
        total_pnl=sum(pnls),
        profit_factor=profit_factor,
        max_drawdown=compute_max_drawdown(pnls),
        avg_win=gross_win / len(win_pnls) if win_pnls else 0.0,
        avg_loss=-gross_loss / len(loss_pnls) if loss_pnls else 0.0,
        best_trade=max(pnls),
        worst_trade=min(pnls),
    )


def format_metrics(metrics: PerformanceMetrics, title: str = "Performance") -> str:
    """Return a printable block for ``metrics``."""
    pf = "inf" if math.isinf(metrics.profit_factor) else f"{metrics.profit_factor:.2f}"
    rows = [
        ("Total trades", f"{metrics.total_trades}"),
        ("Wins / Losses", f"{metrics.wins} / {metrics.losses}"),
        ("Win rate", f"{metrics.win_rate_pct:.1f}%"),
        ("Gross PnL", f"{metrics.gross_pnl:,.2f}"),
        ("Charges", f"{metrics.total_charges:,.2f}"),
        ("Net PnL", f"{metrics.total_pnl:,.2f}"),
        ("Profit factor", pf),
        ("Max drawdown", f"{metrics.max_drawdown:,.2f}"),
        ("Avg win / loss", f"{metrics.avg_win:,.2f} / {metrics.avg_loss:,.2f}"),
        ("Best / worst", f"{metrics.best_trade:,.2f} / {metrics.worst_trade:,.2f}"),
    ]
    width = 46
    lines = ["=" * width, title.center(width), "=" * width]
    lines += [f"{label:<18}{value:>{width - 18}}" for label, value in rows]
    lines.append("=" * width)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CSV trade log
# --------------------------------------------------------------------------- #
class TradeLogger:
    """Append-only CSV log with one row per ENTRY and one per EXIT event."""

    def __init__(self, path: Path = TRADES_LOG_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _append(self, row: Dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
                if new_file:
                    writer.writeheader()
                writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})

    def log_entry(self, trade_id: str, mode: str, instrument: str, option_symbol: str,
                  side: str, strike: float, expiry: str, quantity: int, price: float,
                  spot: float, timestamp: datetime, charges: float, reason: str = "signal") -> None:
        """Record an entry fill."""
        self._append({
            "trade_id": trade_id, "mode": mode, "date": timestamp.date().isoformat(),
            "event": "ENTRY", "timestamp": timestamp.isoformat(timespec="seconds"),
            "instrument": instrument, "option_symbol": option_symbol, "side": side,
            "strike": strike, "expiry": expiry, "quantity": quantity,
            "price": round(price, 2), "spot": round(spot, 2), "reason": reason,
            "charges": round(charges, 2),
        })

    def log_exit(self, trade: Trade) -> None:
        """Record an exit fill together with the realised PnL of the round trip."""
        self._append({
            "trade_id": trade.trade_id, "mode": trade.mode,
            "date": trade.exit_time.date().isoformat(), "event": "EXIT",
            "timestamp": trade.exit_time.isoformat(timespec="seconds"),
            "instrument": trade.instrument, "option_symbol": trade.option_symbol,
            "side": trade.side, "strike": trade.strike, "expiry": trade.expiry,
            "quantity": trade.quantity, "price": round(trade.exit_price, 2),
            "spot": round(trade.exit_spot, 2), "reason": trade.exit_reason,
            "charges": round(trade.charges, 2), "gross_pnl": round(trade.gross_pnl, 2),
            "net_pnl": round(trade.net_pnl, 2),
        })

    def log_trades(self, trades: Iterable[Trade]) -> None:
        """Write complete round trips (used by the backtester)."""
        for t in trades:
            self.log_entry(t.trade_id, t.mode, t.instrument, t.option_symbol, t.side,
                           t.strike, t.expiry, t.quantity, t.entry_price, t.entry_spot,
                           t.entry_time, 0.0)
            self.log_exit(t)

    def read_trades(self, trade_date: Optional[date] = None,
                    mode: Optional[str] = None) -> List[Trade]:
        """Rebuild completed trades from the CSV, optionally filtered."""
        if not self.path.exists():
            return []
        entries: Dict[str, Dict[str, str]] = {}
        trades: List[Trade] = []
        with self.path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if mode and row["mode"] != mode:
                    continue
                if row["event"] == "ENTRY":
                    entries[row["trade_id"]] = row
                    continue
                entry = entries.get(row["trade_id"])
                if entry is None:
                    _LOGGER.warning("EXIT without ENTRY for trade %s", row["trade_id"])
                    continue
                trade = Trade(
                    trade_id=row["trade_id"], mode=row["mode"], instrument=row["instrument"],
                    option_symbol=row["option_symbol"], side=row["side"],
                    strike=float(row["strike"]), expiry=row["expiry"],
                    quantity=int(row["quantity"]),
                    entry_time=datetime.fromisoformat(entry["timestamp"]),
                    entry_price=float(entry["price"]), entry_spot=float(entry["spot"]),
                    exit_time=datetime.fromisoformat(row["timestamp"]),
                    exit_price=float(row["price"]), exit_spot=float(row["spot"]),
                    exit_reason=row["reason"], charges=float(row["charges"] or 0),
                    gross_pnl=float(row["gross_pnl"] or 0), net_pnl=float(row["net_pnl"] or 0),
                )
                if trade_date is None or trade.exit_time.date() == trade_date:
                    trades.append(trade)
        return trades


# --------------------------------------------------------------------------- #
# End-of-day summary
# --------------------------------------------------------------------------- #
def generate_eod_summary(trade_date: date, mode: Optional[str] = "paper",
                         log_path: Path = TRADES_LOG_PATH, out_dir: Path = LOG_DIR,
                         echo: bool = True) -> Dict[str, Any]:
    """Build, print and save the EOD summary for ``trade_date``.

    Writes ``eod_summary_<date>.json`` and ``.txt`` into ``out_dir`` and returns
    the summary dictionary.
    """
    trades = TradeLogger(log_path).read_trades(trade_date=trade_date, mode=mode)
    metrics = compute_metrics(trades)
    summary: Dict[str, Any] = {
        "date": trade_date.isoformat(),
        "mode": mode or "all",
        "metrics": metrics.to_dict(),
        "win_loss_ratio": (round(metrics.wins / metrics.losses, 2) if metrics.losses
                           else ("inf" if metrics.wins else 0)),
        "peak_drawdown": round(metrics.max_drawdown, 2),
        "trades": [
            {"trade_id": t.trade_id, "symbol": t.option_symbol, "side": t.side,
             "qty": t.quantity, "entry": round(t.entry_price, 2),
             "exit": round(t.exit_price, 2), "reason": t.exit_reason,
             "net_pnl": round(t.net_pnl, 2)}
            for t in sorted(trades, key=lambda x: x.exit_time)
        ],
    }
    text = format_metrics(metrics, f"EOD SUMMARY {trade_date.isoformat()} ({mode or 'all'})")
    text += f"\nWin/Loss ratio : {summary['win_loss_ratio']}\nPeak drawdown  : {metrics.max_drawdown:,.2f}\n"
    if not trades:
        text += "No completed trades for this date.\n"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"eod_summary_{trade_date.isoformat()}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / f"eod_summary_{trade_date.isoformat()}.txt").write_text(text, encoding="utf-8")
    if echo:
        print(text)
    return summary
