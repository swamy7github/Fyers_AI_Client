"""Performance metrics from a list of trades and the per-day equity path."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..models import DailySummary, Trade


@dataclass
class Metrics:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    charges: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_r: float = 0.0
    expectancy: float = 0.0          # INR per trade
    expectancy_r: float = 0.0        # R per trade
    max_drawdown: float = 0.0        # INR, on the per-trade equity path
    max_drawdown_pct: float = 0.0
    best_day: float = 0.0
    worst_day: float = 0.0
    best_day_date: Optional[str] = None
    worst_day_date: Optional[str] = None
    trading_days: int = 0
    profitable_days: int = 0
    avg_hold_minutes: float = 0.0
    synthetic_premium_trades: int = 0
    by_strategy: Dict[str, Dict[str, float]] = field(default_factory=dict)
    by_exit_reason: Dict[str, int] = field(default_factory=dict)
    start_equity: float = 0.0
    end_equity: float = 0.0
    return_pct: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def equity_curve(trades: Sequence[Trade], start_equity: float) -> pd.DataFrame:
    rows = [{"ts": None, "equity": start_equity, "trade_id": ""}]
    eq = start_equity
    for t in sorted(trades, key=lambda x: x.exit_ts):
        eq += t.net_pnl
        rows.append({"ts": t.exit_ts, "equity": eq, "trade_id": t.trade_id})
    df = pd.DataFrame(rows)
    df["peak"] = df["equity"].cummax()
    df["drawdown"] = df["equity"] - df["peak"]
    return df


def compute_metrics(trades: Sequence[Trade], daily: Sequence[DailySummary], start_equity: float) -> Metrics:
    m = Metrics(start_equity=start_equity)
    m.trades = len(trades)
    m.trading_days = len({d.trade_date for d in daily})
    if daily:
        pnl_by_day: Dict[date, float] = {}
        for d in daily:
            pnl_by_day[d.trade_date] = pnl_by_day.get(d.trade_date, 0.0) + d.net_pnl
        best = max(pnl_by_day.items(), key=lambda kv: kv[1])
        worst = min(pnl_by_day.items(), key=lambda kv: kv[1])
        m.best_day, m.best_day_date = round(best[1], 2), best[0].isoformat()
        m.worst_day, m.worst_day_date = round(worst[1], 2), worst[0].isoformat()
        m.profitable_days = sum(1 for v in pnl_by_day.values() if v > 0)
    if not trades:
        m.end_equity = start_equity
        return m
    net = np.array([t.net_pnl for t in trades], dtype=float)
    wins = net[net > 0]
    losses = net[net <= 0]
    m.wins, m.losses = int(len(wins)), int(len(losses))
    m.win_rate = round(m.wins / m.trades, 4)
    m.gross_profit = round(float(wins.sum()), 2)
    m.gross_loss = round(float(-losses.sum()), 2)
    m.net_pnl = round(float(net.sum()), 2)
    m.charges = round(float(sum(t.charges for t in trades)), 2)
    m.profit_factor = round(m.gross_profit / m.gross_loss, 3) if m.gross_loss > 0 else (float("inf") if m.gross_profit > 0 else 0.0)
    m.avg_win = round(float(wins.mean()), 2) if len(wins) else 0.0
    m.avg_loss = round(float(losses.mean()), 2) if len(losses) else 0.0
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    m.avg_r = round(float(np.mean(rs)), 3) if rs else 0.0
    m.expectancy = round(float(net.mean()), 2)
    m.expectancy_r = m.avg_r
    curve = equity_curve(trades, start_equity)
    m.max_drawdown = round(float(-curve["drawdown"].min()), 2)
    peak_at_dd = float(curve.loc[curve["drawdown"].idxmin(), "peak"]) if len(curve) else start_equity
    m.max_drawdown_pct = round(m.max_drawdown / peak_at_dd * 100.0, 3) if peak_at_dd else 0.0
    m.avg_hold_minutes = round(float(np.mean([t.hold_minutes for t in trades])), 1)
    m.synthetic_premium_trades = sum(1 for t in trades if t.premium_model == "synthetic")
    m.end_equity = round(start_equity + m.net_pnl, 2)
    m.return_pct = round(m.net_pnl / start_equity * 100.0, 3) if start_equity else 0.0
    by: Dict[str, List[Trade]] = {}
    for t in trades:
        by.setdefault(t.strategy, []).append(t)
    for name, ts in by.items():
        n = np.array([t.net_pnl for t in ts])
        m.by_strategy[name] = {
            "trades": len(ts), "win_rate": round(float((n > 0).mean()), 3), "net_pnl": round(float(n.sum()), 2),
            "avg_r": round(float(np.mean([t.r_multiple for t in ts if t.r_multiple is not None] or [0.0])), 3),
        }
    for t in trades:
        m.by_exit_reason[t.exit_reason] = m.by_exit_reason.get(t.exit_reason, 0) + 1
    return m
