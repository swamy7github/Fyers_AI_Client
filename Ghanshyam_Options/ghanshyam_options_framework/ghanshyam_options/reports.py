"""Text / markdown reports and an optional matplotlib equity-curve PNG."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from .backtest.metrics import Metrics, compute_metrics, equity_curve
from .journal.trade_log import TradeLog
from .models import DailySummary, Trade

logger = logging.getLogger("ghanshyam.reports")


def _inr(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}₹{abs(v):,.0f}"


def metrics_markdown(m: Metrics, title: str = "Backtest summary", extra: Optional[dict] = None) -> str:
    pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
    lines = [
        f"# {title}", "",
        "| Metric | Value |", "|---|---|",
        f"| Trades | {m.trades} (wins {m.wins}, losses {m.losses}) |",
        f"| Win rate | {m.win_rate:.1%} |",
        f"| Net P&L | {_inr(m.net_pnl)} (charges {_inr(m.charges)}) |",
        f"| Return on capital | {m.return_pct:.2f}% |",
        f"| Profit factor | {pf} |",
        f"| Avg win / avg loss | {_inr(m.avg_win)} / {_inr(m.avg_loss)} |",
        f"| Avg R / expectancy | {m.avg_r:.2f}R / {_inr(m.expectancy)} per trade |",
        f"| Max drawdown | {_inr(m.max_drawdown)} ({m.max_drawdown_pct:.2f}%) |",
        f"| Best / worst day | {_inr(m.best_day)} ({m.best_day_date}) / {_inr(m.worst_day)} ({m.worst_day_date}) |",
        f"| Trading days | {m.trading_days} (profitable {m.profitable_days}) |",
        f"| Avg hold | {m.avg_hold_minutes:.0f} min |",
        f"| Synthetic-premium trades | {m.synthetic_premium_trades} of {m.trades} |",
    ]
    if extra:
        for k, v in extra.items():
            lines.append(f"| {k} | {v} |")
    if m.by_strategy:
        lines += ["", "## By strategy", "", "| Strategy | Trades | Win rate | Net P&L | Avg R |", "|---|---|---|---|---|"]
        for name, s in sorted(m.by_strategy.items(), key=lambda kv: -kv[1]["net_pnl"]):
            lines.append(f"| {name} | {s['trades']} | {s['win_rate']:.0%} | {_inr(s['net_pnl'])} | {s['avg_r']:.2f} |")
    if m.by_exit_reason:
        lines += ["", "## Exit reasons", ""]
        lines += [f"- {k}: {v}" for k, v in sorted(m.by_exit_reason.items(), key=lambda kv: -kv[1])]
    lines += ["", "_Past performance, simulated or real, does not predict future results. Synthetic premiums are a model, not market data._"]
    return "\n".join(lines)


def daily_table(daily: Sequence[DailySummary]) -> str:
    if not daily:
        return "(no sessions)"
    hdr = f"{'DATE':<11}|{'INST':<10}|{'TR':>3}|{'W':>3}|{'L':>3}|{'NET P&L':>12}|{'PEAK':>10}|{'DD':>10}| STATUS"
    rows = [hdr, "-" * len(hdr)]
    for d in daily:
        rows.append(f"{d.trade_date.isoformat():<11}|{d.instrument:<10}|{d.trades:>3}|{d.wins:>3}|{d.losses:>3}|"
                    f"{d.net_pnl:>12,.0f}|{d.peak_profit:>10,.0f}|{d.max_intraday_drawdown:>10,.0f}| "
                    f"{d.lockout_reason or 'ok'}")
    return "\n".join(rows)


def write_report(trades: Sequence[Trade], daily: Sequence[DailySummary], start_equity: float, out_dir: Path | str,
                 name: str = "report", title: str = "Backtest summary", extra: Optional[dict] = None,
                 png: bool = True) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    m = compute_metrics(trades, daily, start_equity)
    md = metrics_markdown(m, title, extra) + "\n\n## Daily\n\n```\n" + daily_table(daily) + "\n```\n"
    md_path = out / f"{name}.md"
    md_path.write_text(md, encoding="utf-8")
    paths = {"markdown": str(md_path)}
    if png and trades:
        try:
            paths["png"] = str(plot_equity(trades, start_equity, out / f"{name}_equity.png", title))
        except Exception as exc:  # matplotlib optional
            logger.warning("equity plot skipped: %s", exc)
    return paths


def plot_equity(trades: Sequence[Trade], start_equity: float, path: Path, title: str = "Equity") -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    curve = equity_curve(trades, start_equity)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    x = list(range(len(curve)))
    ax1.plot(x, curve["equity"], color="#1f5f8b", linewidth=1.5)
    ax1.fill_between(x, curve["peak"], curve["equity"], color="#c0392b", alpha=0.15)
    ax1.set_ylabel("Equity (INR)")
    ax1.set_title(title)
    ax1.grid(alpha=0.3)
    ax2.bar(x, curve["drawdown"], color="#c0392b", alpha=0.6)
    ax2.set_ylabel("Drawdown")
    ax2.set_xlabel("Trade #")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def report_from_logs(log_dir: Path | str, start: Optional[date] = None, end: Optional[date] = None,
                     start_equity: float = 500000.0) -> str:
    """Rebuild a summary from the CSV journal (``python -m ghanshyam_options report``)."""
    jl = TradeLog(log_dir)
    df = jl.read_trades(start, end)
    daily = jl.read_daily()
    if df.empty:
        return "No trades found in the journal."
    trades = [
        Trade(**{k: (row[k] if not (isinstance(row[k], float) and pd.isna(row[k])) else None) for k in df.columns if k in Trade.__dataclass_fields__})
        for _, row in df.iterrows()
    ]
    for t in trades:
        t.net_pnl = float(t.net_pnl or 0.0)
        t.gross_pnl = float(t.gross_pnl or 0.0)
        t.charges = float(t.charges or 0.0)
        t.hold_minutes = float(t.hold_minutes or 0.0)
        t.r_multiple = float(t.r_multiple) if t.r_multiple is not None else None
    summaries: list[DailySummary] = []
    if not daily.empty:
        for _, row in daily.iterrows():
            summaries.append(DailySummary(**{k: row[k] for k in daily.columns if k in DailySummary.__dataclass_fields__}))
    m = compute_metrics(trades, summaries, float(start_equity))
    return metrics_markdown(m, "Journal summary") + "\n\n```\n" + daily_table(summaries) + "\n```\n"
