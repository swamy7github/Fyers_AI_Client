"""Risk engine implementing the guide's capital architecture (p7-p9) and the
intraday position-management rules (SL / target / trailing / time stop /
square-off).

Daily circuit breakers (checked before every entry):
* R3 max trades per day
* R4 consecutive-loss lockout (2 stop-outs)
* R5 max daily loss (% of total equity)
* R6 80/20 profit retention: once profit is banked, a later trade may risk at
  most ``secondary_risk_pct_of_banked`` of the banked amount, and the session
  must not give back more than ``1 - profit_retention_pct`` of the peak.
* one open position per instrument, active tranche cap, forced square-off time.

Position sizing: lots = floor(risk_budget / (per-lot premium risk)), capped by
the active tranche (premium outlay) and ``capital.max_lots``; counter-trend
signals are scaled by ``counter_trend_size_factor``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .config.config import SessionTimes, Settings
from .instruments import InstrumentSpec
from .models import ExitReason, Position, Signal

logger = logging.getLogger("ghanshyam.risk")


@dataclass
class DayRisk:
    trade_date: date
    realised_pnl: float = 0.0
    peak_pnl: float = 0.0
    trades: int = 0
    consecutive_losses: int = 0
    locked_out: bool = False
    lockout_reason: str = ""
    banked_profit: float = 0.0        # profit at the time the retention rule engaged
    open_risk: float = 0.0            # INR at risk across open positions
    deployed_premium: float = 0.0     # premium outlay across open positions
    max_drawdown: float = 0.0
    signals_rejected: int = 0


@dataclass
class SizingResult:
    lots: int
    qty: int
    risk_budget: float
    per_lot_risk: float
    premium_outlay: float
    reason: str = ""


class RiskEngine:
    def __init__(self, settings: Settings):
        self.s = settings
        self.total_capital = float(settings.get("capital.total"))
        self.active_tranche = self.total_capital * float(settings.get("capital.active_tranche_pct", 0.25))
        self.risk_per_trade = self.total_capital * float(settings.get("capital.risk_per_trade_pct", 0.01))
        self.max_lots = int(settings.get("capital.max_lots", 10))
        self.max_trades = int(settings.get("risk.max_trades_per_day", 3))
        self.max_consecutive_losses = int(settings.get("risk.max_consecutive_losses", 2))
        self.max_daily_loss = self.total_capital * float(settings.get("risk.max_daily_loss_pct", 0.02))
        self.retention_pct = float(settings.get("risk.profit_retention_pct", 0.8))
        self.secondary_risk_pct = float(settings.get("risk.secondary_risk_pct_of_banked", 0.25))
        self.one_per_instrument = bool(settings.get("risk.one_position_per_instrument", True))
        self.counter_trend_factor = float(settings.get("risk.counter_trend_size_factor", 0.5))
        self.trail_after_r = float(settings.get("risk.trail_to_breakeven_after_r", 1.0))
        self.trail_atr_fraction = float(settings.get("risk.trail_atr_fraction", 0.75))
        self.day: DayRisk = DayRisk(trade_date=date.today())
        self._open: Dict[str, Position] = {}
        self.history: List[DayRisk] = []

    # -- day lifecycle ------------------------------------------------------------
    def start_day(self, trade_date: date) -> DayRisk:
        if self.day.trades or self.day.realised_pnl:
            self.history.append(self.day)
        self.day = DayRisk(trade_date=trade_date)
        self._open.clear()
        return self.day

    def open_positions(self) -> List[Position]:
        return list(self._open.values())

    def has_open(self, instrument: str) -> bool:
        return any(p.contract.underlying == instrument for p in self._open.values())

    # -- entry gating --------------------------------------------------------------
    def can_trade(self, signal: Signal, now: datetime, session: SessionTimes) -> Tuple[bool, str]:
        d = self.day
        if d.locked_out:
            return False, d.lockout_reason
        if d.trades >= self.max_trades:
            self._lock("max trades per day reached (R3)")
            return False, self.day.lockout_reason
        if d.consecutive_losses >= self.max_consecutive_losses:
            self._lock(f"{d.consecutive_losses} consecutive stop-outs (R4)")
            return False, self.day.lockout_reason
        if d.realised_pnl <= -self.max_daily_loss:
            self._lock(f"daily loss {d.realised_pnl:.0f} <= -{self.max_daily_loss:.0f} (R5)")
            return False, self.day.lockout_reason
        if d.peak_pnl > 0 and d.realised_pnl < d.peak_pnl * self.retention_pct:
            self._lock(f"profit retention breached: {d.realised_pnl:.0f} < {self.retention_pct:.0%} of peak {d.peak_pnl:.0f} (R6)")
            return False, self.day.lockout_reason
        if not session.can_enter(now.time()):
            return False, "outside entry window"
        if self.one_per_instrument and self.has_open(signal.instrument):
            return False, "position already open for instrument"
        return True, "ok"

    def _lock(self, reason: str) -> None:
        if not self.day.locked_out:
            self.day.locked_out = True
            self.day.lockout_reason = reason
            logger.warning("RISK LOCKOUT (%s): %s", self.day.trade_date, reason)

    # -- sizing ---------------------------------------------------------------------
    def risk_budget(self) -> float:
        """INR the next trade may lose (R2 base, reduced by R6 when profit is banked)."""
        budget = self.risk_per_trade
        if self.day.peak_pnl > 0:
            banked = self.day.realised_pnl
            give_back_room = banked - self.day.peak_pnl * self.retention_pct
            secondary = banked * self.secondary_risk_pct
            budget = max(0.0, min(budget, secondary, give_back_room))
        budget -= self.day.open_risk
        return max(0.0, budget)

    def size(self, signal: Signal, spec: InstrumentSpec, entry_premium: float, premium_sl: Optional[float],
             delta: float) -> SizingResult:
        """Lots for ``signal``.

        Per-lot risk = premium risk per unit x lot size, where premium risk is
        the distance to the premium stop when known, else spot risk x delta.
        """
        if entry_premium <= 0:
            return SizingResult(0, 0, 0.0, 0.0, 0.0, "no premium")
        if premium_sl is not None and premium_sl < entry_premium:
            prem_risk = entry_premium - premium_sl
        else:
            prem_risk = max(signal.risk_points * max(delta, 0.3), spec.tick_size)
        per_lot_risk = prem_risk * spec.lot_size * spec.point_value
        budget = self.risk_budget()
        if signal.counter_trend:
            budget *= self.counter_trend_factor
        if per_lot_risk <= 0 or budget <= 0:
            return SizingResult(0, 0, budget, per_lot_risk, 0.0, "no risk budget left")
        lots = int(budget // per_lot_risk)
        # active tranche cap on premium outlay
        per_lot_outlay = entry_premium * spec.lot_size * spec.point_value
        tranche_room = self.active_tranche - self.day.deployed_premium
        lots = min(lots, int(tranche_room // per_lot_outlay) if per_lot_outlay > 0 else 0, self.max_lots)
        if lots < 1:
            return SizingResult(0, 0, budget, per_lot_risk, 0.0,
                                f"risk budget {budget:.0f} < per-lot risk {per_lot_risk:.0f} or tranche exhausted")
        return SizingResult(lots, lots * spec.lot_size, budget, per_lot_risk, lots * per_lot_outlay)

    # -- position bookkeeping --------------------------------------------------------
    def register_open(self, pos: Position, spec: InstrumentSpec) -> None:
        self._open[pos.position_id] = pos
        self.day.open_risk += pos.risk_budget
        self.day.deployed_premium += pos.entry_price * pos.qty * spec.point_value
        self.day.trades += 1

    def register_close(self, pos: Position, net_pnl: float, reason: ExitReason, spec: InstrumentSpec) -> None:
        self._open.pop(pos.position_id, None)
        self.day.open_risk = max(0.0, self.day.open_risk - pos.risk_budget)
        self.day.deployed_premium = max(0.0, self.day.deployed_premium - pos.entry_price * pos.qty * spec.point_value)
        self.day.realised_pnl += net_pnl
        self.day.peak_pnl = max(self.day.peak_pnl, self.day.realised_pnl)
        dd = self.day.peak_pnl - self.day.realised_pnl
        self.day.max_drawdown = max(self.day.max_drawdown, dd)
        if net_pnl < 0:
            self.day.consecutive_losses += 1
        else:
            self.day.consecutive_losses = 0
        # R6: after banking profit, a stopped-out secondary trade ends the day
        if self.day.peak_pnl > 0 and net_pnl < 0 and reason in (ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP):
            if self.day.realised_pnl < self.day.peak_pnl:
                self._lock("secondary trade stopped out after banked profit (R6)")
        if self.day.consecutive_losses >= self.max_consecutive_losses:
            self._lock(f"{self.day.consecutive_losses} consecutive stop-outs (R4)")
        if self.day.realised_pnl <= -self.max_daily_loss:
            self._lock(f"daily loss {self.day.realised_pnl:.0f} hit cap {self.max_daily_loss:.0f} (R5)")

    # -- in-trade management ---------------------------------------------------------
    def manage(self, pos: Position, spot: float, premium: float, now: datetime, session: SessionTimes,
               atr: float, bar_high: float, bar_low: float) -> Optional[ExitReason]:
        """Decide whether ``pos`` must exit on this bar. Mutates trailing fields.

        Order of checks mirrors the guide: hard SL first (spot or premium),
        then target, then time stop, then square-off. Intrabar touches use
        the bar's high/low so a stop that was hit inside the bar fires even if
        the close recovered.
        """
        pos.bars_held += 1
        pos.best_spot = max(pos.best_spot, bar_high) if pos.is_call else (min(pos.best_spot, bar_low) if pos.best_spot else bar_low)
        pos.max_favourable_premium = max(pos.max_favourable_premium, premium)
        pos.max_adverse_premium = min(pos.max_adverse_premium, premium) if pos.max_adverse_premium else premium

        # 1. hard stop on the underlying (intrabar)
        if pos.is_call and bar_low <= pos.spot_sl:
            return ExitReason.TRAILING_STOP if pos.trailing_active else ExitReason.STOP_LOSS
        if not pos.is_call and bar_high >= pos.spot_sl:
            return ExitReason.TRAILING_STOP if pos.trailing_active else ExitReason.STOP_LOSS
        # 2. premium stop (if defined; protects against IV crush even when spot holds)
        if pos.premium_sl is not None and premium <= pos.premium_sl:
            return ExitReason.STOP_LOSS
        # 3. targets
        if pos.spot_target is not None:
            if pos.is_call and bar_high >= pos.spot_target:
                return ExitReason.TARGET
            if not pos.is_call and bar_low <= pos.spot_target:
                return ExitReason.TARGET
        if pos.premium_target is not None and premium >= pos.premium_target:
            return ExitReason.TARGET
        # 4. time stop
        if pos.signal.max_hold_minutes:
            if now - pos.entry_ts >= timedelta(minutes=pos.signal.max_hold_minutes):
                return ExitReason.TIME_STOP
        # 5. square-off
        if now.time() >= session.square_off:
            return ExitReason.SQUARE_OFF
        # 6. trailing (R7): breakeven after +trail_after_r, then trail by ATR fraction
        r_now = pos.r_at_spot(pos.best_spot if pos.best_spot else spot)
        if self.trail_after_r > 0 and r_now >= self.trail_after_r:
            if not pos.breakeven_set:
                pos.spot_sl = pos.entry_spot
                pos.breakeven_set = True
                pos.trailing_active = True
            trail = self.trail_atr_fraction * max(atr, 0.0)
            if trail > 0:
                if pos.is_call:
                    pos.spot_sl = max(pos.spot_sl, pos.best_spot - trail)
                else:
                    pos.spot_sl = min(pos.spot_sl, pos.best_spot + trail)
        return None

    def must_square_off(self, now: datetime, session: SessionTimes) -> bool:
        return now.time() >= session.square_off

    def summary(self) -> Dict[str, float | int | str | bool]:
        d = self.day
        return {
            "date": d.trade_date.isoformat(), "realised_pnl": round(d.realised_pnl, 2), "peak_pnl": round(d.peak_pnl, 2),
            "trades": d.trades, "consecutive_losses": d.consecutive_losses, "locked_out": d.locked_out,
            "lockout_reason": d.lockout_reason, "max_drawdown": round(d.max_drawdown, 2),
        }
