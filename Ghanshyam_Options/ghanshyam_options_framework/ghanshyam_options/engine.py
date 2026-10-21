"""Trading engine shared by the backtester and the paper runner.

Responsibilities per closed underlying bar:

1. manage open positions (SL / target / trail / time stop / square-off) using
   the bar's high/low and the option premium for that bar;
2. ask the :class:`SignalEngine` for new signals;
3. for each signal: pick the strike, price the option (market candles if
   available, else synthetic Black-Scholes), size through the risk engine,
   fill through the broker and open the position.

Option premium sources
----------------------
``PremiumSource`` returns (premium, model) for a contract at a timestamp.
``model`` is ``"market"`` when a real option bar/quote was used and
``"synthetic"`` when Black-Scholes from the underlying was used. The value is
carried into every trade row (``premium_model``) so synthetic results are
never mistaken for market fills.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from .config.config import Settings
from .execution.broker import Broker, OrderRequest
from .instruments import InstrumentSpec
from .journal.trade_log import TradeLog
from .models import Candle, DailySummary, DayLevels, ExitReason, OptionContract, Position, Signal, Trade
from .pricing import option_delta, synthetic_premium
from .risk import RiskEngine
from .signals import SignalEngine
from .strikes import StrikeSelector

logger = logging.getLogger("ghanshyam.engine")

PremiumFn = Callable[[OptionContract, datetime, float], Tuple[float, str]]


@dataclass
class PendingEntry:
    """A signal accepted at bar close, to be filled at the next bar."""

    signal: Signal
    created_index: int


@dataclass
class EngineStats:
    signals: int = 0
    rejected: int = 0
    entries: int = 0
    exits: int = 0
    sizing_rejections: List[str] = field(default_factory=list)


class SyntheticPremium:
    """Default premium source: Black-Scholes from the underlying spot."""

    def __init__(self, settings: Settings, spec: InstrumentSpec):
        self.iv = float(settings.instrument_value("strike.iv_assumption", spec.name, 0.15) or 0.15)
        self.r = float(settings.get("strike.risk_free_rate", 0.065))
        self.min_premium = float(settings.get("premium.synthetic_min_premium", 1.0))

    def __call__(self, contract: OptionContract, ts: datetime, spot: float) -> Tuple[float, str]:
        return synthetic_premium(spot, contract.strike, ts, contract.expiry, self.iv, contract.option_type,
                                 self.r, self.min_premium), "synthetic"


class MarketPremium:
    """Premium from real option candles when available, synthetic otherwise.

    ``lookup(symbol, ts)`` must return the option bar's close at ``ts`` (bar
    open time) or ``None``. ``bar_open(symbol, ts)`` returns the open price for
    next-bar fills.
    """

    def __init__(self, lookup: Callable[[str, datetime], Optional[float]], fallback: SyntheticPremium):
        self.lookup = lookup
        self.fallback = fallback

    def __call__(self, contract: OptionContract, ts: datetime, spot: float) -> Tuple[float, str]:
        px = self.lookup(contract.symbol, ts)
        if px is not None and px > 0:
            return float(px), "market"
        return self.fallback(contract, ts, spot)


class TradingEngine:
    def __init__(self, settings: Settings, spec: InstrumentSpec, signal_engine: SignalEngine, risk: RiskEngine,
                 broker: Broker, journal: TradeLog, mode: str, premium_fn: Optional[PremiumFn] = None):
        self.s = settings
        self.spec = spec
        self.signals = signal_engine
        self.risk = risk
        self.broker = broker
        self.journal = journal
        self.mode = mode
        self.session = settings.session(spec.exchange.value)
        iv = float(settings.instrument_value("strike.iv_assumption", spec.name, 0.15) or 0.15)
        self.selector = StrikeSelector(
            mode=str(settings.get("strike.mode", "ATM_ITM")), min_delta=float(settings.get("strike.min_delta", 0.5)),
            iv=iv, risk_free_rate=float(settings.get("strike.risk_free_rate", 0.065)),
        )
        self.premium_fn: PremiumFn = premium_fn or SyntheticPremium(settings, spec)
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.pending: List[PendingEntry] = []
        self.stats = EngineStats()
        self._ids = itertools.count(1)
        self._bar_index = -1
        self._last_bar: Optional[Candle] = None
        self._trade_date: Optional[date] = None
        self._day_trades_start = 0
        self.equity = float(settings.total_capital)

    # -- day lifecycle --------------------------------------------------------------
    def start_day(self, trade_date: date, levels: DayLevels) -> None:
        self._trade_date = trade_date
        self._bar_index = -1
        self._last_bar = None
        self.pending.clear()
        self.positions.clear()
        self.stats = EngineStats()
        self._day_trades_start = len(self.trades)
        self.signals.start_day(self.spec, trade_date, levels)
        self.risk.start_day(trade_date)

    def end_day(self, last_bar: Optional[Candle] = None) -> DailySummary:
        bar = last_bar or self._last_bar
        if bar is not None and self.positions:
            exit_ts = datetime.combine(bar.ts.date(), self.session.square_off)
            exit_ts = max(exit_ts, bar.ts)
            for pos in list(self.positions.values()):
                self._exit(pos, ExitReason.SQUARE_OFF, exit_ts, bar.close, bar_open=None)
        self.pending.clear()
        day_trades = self.trades[self._day_trades_start:]
        d = self.risk.day
        wins = sum(1 for t in day_trades if t.net_pnl > 0)
        st = self.signals.state(self.spec)
        summary = DailySummary(
            trade_date=self._trade_date or (bar.ts.date() if bar else date.today()), instrument=self.spec.name,
            mode=self.mode, trades=len(day_trades), wins=wins, losses=len(day_trades) - wins,
            gross_pnl=round(sum(t.gross_pnl for t in day_trades), 2), charges=round(sum(t.charges for t in day_trades), 2),
            net_pnl=round(sum(t.net_pnl for t in day_trades), 2), peak_profit=round(d.peak_pnl, 2),
            max_intraday_drawdown=round(d.max_drawdown, 2), consecutive_losses=d.consecutive_losses,
            lockout_reason=d.lockout_reason, signals_generated=len(st.accepted) + len(st.rejected),
            signals_rejected=len(st.rejected) + self.stats.rejected, equity_end=round(self.equity, 2),
        )
        self.journal.log_daily(summary)
        self.journal.log_signals(summary.trade_date, st.accepted, [(r.signal, r.reason) for r in st.rejected])
        return summary

    # -- per-bar -----------------------------------------------------------------------
    def on_bar(self, bar: Candle, next_bar_open: Optional[float] = None) -> None:
        """Process one closed underlying bar.

        ``next_bar_open`` (backtest only) is the open of the following bar, used
        for next-bar fills of entries decided on this bar's close. The paper
        runner passes ``None`` and fills pending entries when the next bar
        arrives (see :meth:`fill_pending`).
        """
        self._bar_index += 1
        self._last_bar = bar
        # 1. fills for entries decided on the previous bar close happen at this bar's open
        self.fill_pending(bar)
        # 2. manage open positions on this bar
        self._manage_positions(bar)
        # 3. new signals at this bar's close
        if self.risk.must_square_off(bar.ts + timedelta(minutes=self._timeframe()), self.session):
            return
        for sig in self.signals.on_bar(self.spec, bar):
            self.stats.signals += 1
            ok, why = self.risk.can_trade(sig, bar.ts + timedelta(minutes=self._timeframe()), self.session)
            if not ok:
                self.stats.rejected += 1
                self.stats.sizing_rejections.append(f"{bar.ts:%H:%M} {sig.strategy}: {why}")
                continue
            if any(p.signal.instrument == sig.instrument for p in self.pending):
                continue
            if sig.fill_same_bar and sig.entry_type == "stop":
                # a resting stop order at the boundary was hit inside this bar (09:20 setup):
                # fill at the boundary, then judge the rest of the bar
                if not ((sig.option_type.value == "CE" and bar.open <= sig.stop_loss) or
                        (sig.option_type.value == "PE" and bar.open >= sig.stop_loss)):
                    self._enter(sig, bar, sig.spot_level, same_bar=True)
                break
            self.pending.append(PendingEntry(sig, self._bar_index))
            break  # one new entry per bar per instrument

    def _timeframe(self) -> int:
        return int(self.s.get("data.default_resolution", 5))

    def fill_pending(self, bar: Candle) -> None:
        """Fill pending entries at ``bar``'s open (stop-entry semantics).

        For pattern-extreme signals the trigger level must be *reachable* in
        this bar: a CE stop above the open fills when ``bar.high >= level`` at
        max(level, open); a PE fills when ``bar.low <= level`` at min(level,
        open). Signals whose level is the previous close (breakout-on-close
        setups) fill at the open. Unfilled pending entries expire after two bars.
        """
        for pe in list(self.pending):
            sig = pe.signal
            age = self._bar_index - pe.created_index
            if age > 2:
                self.pending.remove(pe)
                continue
            is_call = sig.option_type.value == "CE"
            level = sig.spot_level
            if sig.entry_type == "market":
                fill_spot: Optional[float] = bar.open
            elif is_call:
                reachable = bar.high >= level
                fill_spot = max(level, bar.open) if reachable else None
            else:
                reachable = bar.low <= level
                fill_spot = min(level, bar.open) if reachable else None
            if fill_spot is None:
                continue
            # the open already sits at/through the stop => skip (the setup is broken before it fills)
            if (is_call and bar.open <= sig.stop_loss) or (not is_call and bar.open >= sig.stop_loss):
                self.pending.remove(pe)
                self.stats.rejected += 1
                self.stats.sizing_rejections.append(f"{bar.ts:%H:%M} {sig.strategy}: opened through stop")
                continue
            self.pending.remove(pe)
            self._enter(sig, bar, fill_spot)

    # -- entries -----------------------------------------------------------------------
    def _enter(self, sig: Signal, bar: Candle, fill_spot: float, same_bar: bool = False) -> Optional[Position]:
        ok, why = self.risk.can_trade(sig, bar.ts, self.session)
        if not ok:
            self.stats.rejected += 1
            self.stats.sizing_rejections.append(f"{bar.ts:%H:%M} {sig.strategy}: {why}")
            return None
        choice = self.selector.select(self.spec, fill_spot, sig.option_type, bar.ts)
        contract = choice.contract
        premium, model = self.premium_fn(contract, bar.ts, fill_spot)
        delta = option_delta(fill_spot, contract.strike, bar.ts, contract.expiry, self.selector.iv, contract.option_type, self.selector.r)
        risk_pts = abs(fill_spot - sig.stop_loss)
        if risk_pts <= 0:
            return None
        # premium-space SL/target estimates (delta approximation) for logging & the IV-crush stop
        premium_sl = max(self.spec.tick_size, premium - risk_pts * delta)
        if sig.target_premium_pct is not None:
            premium_target: Optional[float] = premium * (1.0 + sig.target_premium_pct)
            spot_target: Optional[float] = None
        else:
            premium_target = None
            spot_target = sig.target
        sizing = self.risk.size(sig, self.spec, premium, premium_sl, delta)
        if sizing.lots < 1:
            self.stats.rejected += 1
            self.stats.sizing_rejections.append(f"{bar.ts:%H:%M} {sig.strategy}: {sizing.reason}")
            return None
        req = OrderRequest(symbol=contract.symbol, side="BUY", qty=sizing.qty, exchange=self.spec.exchange, ts=bar.ts,
                           reference_price=premium, tick_size=contract.tick_size, tag=f"{sig.strategy}",
                           bar_open=premium)
        res = self.broker.buy(req)
        if not res.ok or res.fill is None:
            self.stats.rejected += 1
            self.stats.sizing_rejections.append(f"{bar.ts:%H:%M} {sig.strategy}: broker {res.message}")
            return None
        fill = res.fill
        pos = Position(
            position_id=f"{self.mode[:2].upper()}-{self.spec.name}-{bar.ts:%Y%m%d}-{next(self._ids):03d}",
            contract=contract, signal=sig, lots=sizing.lots, qty=sizing.qty, entry_ts=bar.ts, entry_price=fill.price,
            entry_spot=fill_spot, spot_sl=sig.stop_loss, spot_target=spot_target, premium_sl=premium_sl,
            premium_target=premium_target, premium_model=model, entry_charges=fill.charges, entry_slippage=fill.slippage,
            initial_risk_points=risk_pts, best_spot=fill_spot, worst_spot=fill_spot, risk_budget=sizing.per_lot_risk * sizing.lots,
            max_favourable_premium=fill.price, max_adverse_premium=fill.price,
            notes=f"{choice.moneyness} delta={choice.model_delta:.2f} planned_r={sig.planned_r if sig.planned_r is not None else 'prem%'}",
        )
        self.positions[pos.position_id] = pos
        self.risk.register_open(pos, self.spec)
        self.stats.entries += 1
        logger.info("ENTER %s %s %s x%d @ %.2f (%s) spot=%.1f SL=%.1f tgt=%s conf=%.2f [%s]", pos.position_id,
                    contract.symbol, sig.strategy, pos.qty, pos.entry_price, model, fill_spot, sig.stop_loss,
                    f"{spot_target:.1f}" if spot_target else f"+{sig.target_premium_pct:.0%} prem", sig.confidence, sig.reason)
        if same_bar:
            # the fill happened inside this bar: a stop that was hit later in the bar exits now
            self._manage_single(pos, bar, same_bar=True)
        return pos

    # -- management --------------------------------------------------------------------
    def _manage_positions(self, bar: Candle) -> None:
        for pos in list(self.positions.values()):
            self._manage_single(pos, bar, same_bar=False)

    def _manage_single(self, pos: Position, bar: Candle, same_bar: bool) -> None:
        st = self.signals.state(self.spec)
        atr_now = float(st._atr[-1]) if st._atr else 0.0
        bar_end = bar.ts + timedelta(minutes=self._timeframe())
        premium, _ = self.premium_fn(pos.contract, bar_end, bar.close)
        if same_bar:
            # after an intrabar fill only the remainder of the bar is known to have traded; be conservative:
            # the stop can be hit (bar extreme beyond SL) but the target is not credited on this bar.
            hit = (pos.is_call and bar.low <= pos.spot_sl) or (not pos.is_call and bar.high >= pos.spot_sl)
            if hit:
                pos.bars_held += 1
                self._exit(pos, ExitReason.STOP_LOSS, bar_end, bar.close, bar_open=None, intrabar_level=pos.spot_sl)
            return
        reason = self.risk.manage(pos, bar.close, premium, bar_end, self.session, atr_now, bar.high, bar.low)
        if reason is None:
            return
        level = None
        if reason in (ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP):
            level = pos.spot_sl
        elif reason == ExitReason.TARGET and pos.spot_target is not None:
            level = pos.spot_target
        self._exit(pos, reason, bar_end, bar.close, bar_open=None, intrabar_level=level)

    def _exit(self, pos: Position, reason: ExitReason, ts: datetime, spot_close: float, bar_open: Optional[float],
              intrabar_level: Optional[float] = None) -> Trade:
        exit_spot = spot_close
        if intrabar_level is not None and reason in (ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP, ExitReason.TARGET):
            exit_spot = intrabar_level  # a touched level fills at the level (with slippage added by the broker)
        premium, _model = self.premium_fn(pos.contract, ts, exit_spot)
        if reason == ExitReason.TARGET and pos.premium_target is not None and pos.spot_target is None:
            premium = max(premium, pos.premium_target)
        req = OrderRequest(symbol=pos.contract.symbol, side="SELL", qty=pos.qty, exchange=self.spec.exchange, ts=ts,
                           reference_price=premium, tick_size=pos.contract.tick_size, tag=reason.value, bar_open=premium)
        res = self.broker.sell(req)
        fill = res.fill
        exit_price = fill.price if fill else premium
        charges = pos.entry_charges + (fill.charges if fill else 0.0)
        mult = self.spec.point_value
        gross = (exit_price - pos.entry_price) * pos.qty * mult
        net = gross - charges
        # R-multiple = net P&L / INR risked at entry (per-lot premium risk x lots, see RiskEngine.size)
        r_mult = (net / pos.risk_budget) if pos.risk_budget > 0 else None
        trade = Trade(
            trade_id=pos.position_id, mode=self.mode, instrument=self.spec.name, underlying=self.spec.underlying_symbol,
            strategy=pos.signal.strategy, signal_ts=pos.signal.ts, direction=pos.contract.option_type.value,
            option_symbol=pos.contract.symbol, strike=pos.contract.strike, expiry=pos.contract.expiry_str,
            entry_ts=pos.entry_ts, entry_price=pos.entry_price, qty=pos.qty, lots=pos.lots,
            sl=pos.premium_sl if pos.premium_sl is not None else 0.0, target=pos.premium_target, exit_ts=ts,
            exit_price=exit_price, exit_reason=reason.value, gross_pnl=round(gross, 2), charges=round(charges, 2),
            net_pnl=round(net, 2), r_multiple=round(r_mult, 3) if r_mult is not None else None,
            confidence=round(pos.signal.confidence, 3), premium_model=pos.premium_model, entry_spot=pos.entry_spot,
            exit_spot=exit_spot, spot_sl=pos.signal.stop_loss, spot_target=pos.spot_target, planned_r=pos.signal.planned_r,
            slippage=round(pos.entry_slippage + (fill.slippage if fill else 0.0), 4),
            max_favourable_premium=pos.max_favourable_premium, max_adverse_premium=pos.max_adverse_premium,
            hold_minutes=round((ts - pos.entry_ts).total_seconds() / 60.0, 1),
            notes=f"{pos.notes}; {pos.signal.reason}",
        )
        self.trades.append(trade)
        self.positions.pop(pos.position_id, None)
        self.risk.register_close(pos, net, reason, self.spec)
        self.equity += net
        self.stats.exits += 1
        self.journal.log_trade(trade)
        return trade

    # -- introspection ----------------------------------------------------------------
    def open_positions(self) -> List[Position]:
        return list(self.positions.values())

    def day_trades(self) -> List[Trade]:
        return self.trades[self._day_trades_start:]

