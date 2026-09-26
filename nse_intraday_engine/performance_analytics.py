"""
PerformanceAnalyzer
====================
Turns the `trades` and `daily_metrics` tables into the performance teardown
requested by the spec: net PnL, win rate, profit factor, average realized
R:R, max drawdown, and an initial-vs-pyramided breakdown.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from database_manager import DatabaseManager


class PerformanceAnalyzer:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def summary(self, mode: str) -> dict:
        trades = pd.DataFrame(self.db.get_trades(mode=mode))
        if trades.empty:
            return {"mode": mode, "num_trades": 0, "message": "No trades recorded."}

        wins = trades[trades["net_pnl"] > 0]
        losses = trades[trades["net_pnl"] <= 0]
        gross_profit = wins["net_pnl"].sum()
        gross_loss = -losses["net_pnl"].sum()
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        equity = pd.DataFrame(self.db.get_equity_curve(mode))
        mdd_pct, mdd_abs = self._max_drawdown(equity)

        initial = trades[trades["is_pyramid_leg"] == 0]
        pyramided = trades[trades["is_pyramid_leg"] == 1]

        return {
            "mode": mode,
            "num_trades": len(trades),
            "total_net_pnl": round(trades["net_pnl"].sum(), 2),
            "total_gross_pnl": round(trades["gross_pnl"].sum(), 2),
            "total_charges": round(trades["charges"].sum(), 2),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
            "avg_r_multiple": (round(trades["r_multiple"].dropna().mean(), 2)
                                if trades["r_multiple"].notna().any() else None),
            "max_drawdown_pct": mdd_pct,
            "max_drawdown_abs": mdd_abs,
            "initial_leg_trades": len(initial),
            "initial_leg_net_pnl": round(initial["net_pnl"].sum(), 2),
            "pyramided_leg_trades": len(pyramided),
            "pyramided_leg_net_pnl": round(pyramided["net_pnl"].sum(), 2),
        }

    @staticmethod
    def _max_drawdown(equity_df: pd.DataFrame) -> tuple[Optional[float], Optional[float]]:
        if equity_df.empty:
            return None, None
        eq = equity_df["equity_close"]
        running_max = eq.cummax()
        drawdown_abs = eq - running_max
        drawdown_pct = drawdown_abs / running_max * 100
        return round(drawdown_pct.min(), 2), round(drawdown_abs.min(), 2)

    def print_report(self, mode: str) -> None:
        s = self.summary(mode)
        print(f"\n{'=' * 60}\nPERFORMANCE TEARDOWN -- {mode}\n{'=' * 60}")
        if s.get("num_trades", 0) == 0:
            print("No trades recorded.")
            return
        print(f"Trades:              {s['num_trades']}")
        print(f"Total Net PnL:       Rs.{s['total_net_pnl']:,.2f}")
        print(f"Total Gross PnL:     Rs.{s['total_gross_pnl']:,.2f}")
        print(f"Total Charges:       Rs.{s['total_charges']:,.2f}")
        print(f"Win Rate:            {s['win_rate_pct']}%")
        print(f"Profit Factor:       {s['profit_factor']}")
        print(f"Avg R-Multiple:      {s['avg_r_multiple']}")
        print(f"Max Drawdown:        {s['max_drawdown_pct']}%  (Rs.{s['max_drawdown_abs']:,.2f})")
        print(f"Initial legs:        {s['initial_leg_trades']}  -> Rs.{s['initial_leg_net_pnl']:,.2f}")
        print(f"Pyramided legs:      {s['pyramided_leg_trades']}  -> Rs.{s['pyramided_leg_net_pnl']:,.2f}")
        print(f"{'=' * 60}\n")
