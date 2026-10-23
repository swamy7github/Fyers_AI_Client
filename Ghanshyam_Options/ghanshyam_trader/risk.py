"""
Account Risk Management and Behavioral Safeguard Engine
"""

import logging
from typing import Tuple

logger = logging.getLogger("GhanshyamTradingSystem")


class RiskEngine:
    """
    Implements Ghanshyam Tech Risk Architecture:
    1. Tranche Capital Allocation: Max 20-25% equity per trade tranche.
    2. 80/20 Profit Retention Rule: 'A green session must never close red'.
       Locks 80% of peak daily profit; secondary trades risk remaining 20%.
    3. Consecutive Loss Lockout: Shutdown after 2 consecutive stop-outs.
    4. Max Daily Trades Ceiling: Hard limit of 2 to 3 trades across all assets.
    5. Max Daily Drawdown Stop: Hard stop at 3% of account equity.
    6. Asymmetric Lot Sizing: 100% baseline when trend-aligned, 50% if counter-trend.
    """

    def __init__(
        self,
        total_capital: float = 50000.0,
        max_capital_per_trade_pct: float = 0.25,
        max_daily_drawdown_pct: float = 0.03,
        max_daily_trades: int = 3,
        max_consecutive_losses: int = 2,
        profit_lock_pct: float = 0.80,
    ):
        self.total_capital = total_capital
        self.max_capital_per_trade_pct = max_capital_per_trade_pct
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.max_daily_trades = max_daily_trades
        self.max_consecutive_losses = max_consecutive_losses
        self.profit_lock_pct = profit_lock_pct

        # Daily performance tracking
        self.realized_pnl: float = 0.0
        self.peak_daily_profit: float = 0.0
        self.trade_count: int = 0
        self.consecutive_losses: int = 0
        self.is_locked_out: bool = False
        self.lockout_reason: str = ""

    def reset_daily_state(self):
        """Resets session risk counters for the start of a new trading day."""
        self.realized_pnl = 0.0
        self.peak_daily_profit = 0.0
        self.trade_count = 0
        self.consecutive_losses = 0
        self.is_locked_out = False
        self.lockout_reason = ""
        logger.info("RiskEngine: Session risk state reset for new trading day.")

    def can_take_trade(self) -> Tuple[bool, str]:
        """Evaluates whether trading is permitted under current risk metrics."""
        if self.is_locked_out:
            return False, f"Risk Lockout active: {self.lockout_reason}"

        # 1. 80/20 Profit Retention Check
        if self.peak_daily_profit > 0:
            minimum_retained_profit = self.peak_daily_profit * self.profit_lock_pct
            if self.realized_pnl < minimum_retained_profit:
                self._lockout(
                    f"Profit lock triggered: Realized PnL (₹{self.realized_pnl:.2f}) breached "
                    f"minimum retained profit (₹{minimum_retained_profit:.2f} of peak ₹{self.peak_daily_profit:.2f})."
                )
                return False, self.lockout_reason

        # 2. Maximum Daily Account Drawdown Cap
        max_loss = self.total_capital * self.max_daily_drawdown_pct
        if self.realized_pnl <= -max_loss:
            self._lockout(f"Max daily drawdown hit (Loss ₹{-self.realized_pnl:.2f} >= Allowed ₹{max_loss:.2f}).")
            return False, self.lockout_reason

        # 3. Consecutive Losses Lockout (2 stop-outs max)
        if self.consecutive_losses >= self.max_consecutive_losses:
            self._lockout("2 consecutive losses hit. Terminal locked to prevent revenge trading.")
            return False, self.lockout_reason

        # 4. Maximum Daily Trades Limiter
        if self.trade_count >= self.max_daily_trades:
            self._lockout(f"Max daily trade count ({self.max_daily_trades} trades) reached.")
            return False, self.lockout_reason

        return True, "Trade permitted under risk guidelines."

    def calculate_lot_size(
        self,
        option_premium: float,
        lot_size: int,
        is_trend_aligned: bool = True
    ) -> int:
        """Calculates contract quantity based on capital tranches and trend alignment."""
        can_trade, _ = self.can_take_trade()
        if not can_trade:
            return 0

        max_capital = self.total_capital * self.max_capital_per_trade_pct

        if self.peak_daily_profit > 0 and self.realized_pnl > 0:
            risk_budget = (self.realized_pnl - (self.peak_daily_profit * self.profit_lock_pct))
            max_capital = min(max_capital, max(risk_budget * 2.0, option_premium * lot_size))

        cost_per_lot = option_premium * lot_size
        if cost_per_lot <= 0:
            return 0

        num_lots = int(max_capital // cost_per_lot)

        if not is_trend_aligned:
            num_lots = max(1, num_lots // 2)

        return max(1, num_lots) * lot_size

    def record_trade_completion(self, pnl: float):
        """Logs completed trade outcome and updates peak profit watermarks."""
        self.trade_count += 1
        self.realized_pnl += pnl

        if self.realized_pnl > self.peak_daily_profit:
            self.peak_daily_profit = self.realized_pnl

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        logger.info(
            f"RiskEngine: Trade Recorded | PnL: ₹{pnl:.2f} | Net Realized: ₹{self.realized_pnl:.2f} | "
            f"Peak: ₹{self.peak_daily_profit:.2f} | Trades: {self.trade_count} | Cons. Losses: {self.consecutive_losses}"
        )
        self.can_take_trade()

    def _lockout(self, reason: str):
        self.is_locked_out = True
        self.lockout_reason = reason
        logger.warning(f"CIRCUIT BREAKER TRIGGERED: {reason}")
