"""Signal engine: builds the per-bar context, runs the enabled strategies,
de-duplicates, applies session / no-entry windows, the equilibrium-trap rule
and the confidence threshold.

The engine is stateful per *session* (``start_day``) so it can be driven by
the backtester (bar by bar from history) and by the paper runner (bar by bar
from the live feed) identically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence

import pandas as pd

from .config.config import SessionTimes, Settings
from .indicators import resample_candles
from .instruments import InstrumentSpec, is_expiry_day
from .models import Bias, Candle, DayLevels, Signal
from .strategies.base import BarContext, StrategyRegistry

logger = logging.getLogger("ghanshyam.signals")


@dataclass
class RejectedSignal:
    signal: Signal
    reason: str


@dataclass
class DayState:
    trade_date: date
    spec: InstrumentSpec
    levels: DayLevels
    candles: List[Candle] = field(default_factory=list)
    bias: Bias = Bias.NEUTRAL
    bias_history: List[Bias] = field(default_factory=list)
    accepted: List[Signal] = field(default_factory=list)
    rejected: List[RejectedSignal] = field(default_factory=list)
    last_signal_key: Optional[str] = None
    _atr: List[float] = field(default_factory=list)
    _avg_vol: List[float] = field(default_factory=list)


def compute_levels(prev_session: Sequence[Candle], trade_date: date, spec: InstrumentSpec,
                   earlier_sessions: Optional[Sequence[Sequence[Candle]]] = None) -> DayLevels:
    """LDCHL from the previous session's bars (guide p2); optional multi-day S/R from earlier sessions."""
    if not prev_session:
        raise ValueError("previous session candles are required to compute PDH/PDL/PDC")
    pdh = max(c.high for c in prev_session)
    pdl = min(c.low for c in prev_session)
    pdc = prev_session[-1].close
    supports: List[float] = []
    resistances: List[float] = []
    for sess in earlier_sessions or []:
        if sess:
            supports.append(min(c.low for c in sess))
            resistances.append(max(c.high for c in sess))
    daily_atr = None
    if earlier_sessions:
        ranges = [max(c.high for c in s) - min(c.low for c in s) for s in earlier_sessions if s]
        ranges.append(pdh - pdl)
        daily_atr = float(sum(ranges) / len(ranges)) if ranges else None
    return DayLevels(trade_date=trade_date, pdh=pdh, pdl=pdl, pdc=pdc, prev_open=prev_session[0].open,
                     support_levels=sorted(set(supports)), resistance_levels=sorted(set(resistances)),
                     daily_atr=daily_atr, is_expiry_day=is_expiry_day(trade_date, spec))


def compute_bias(candles: Sequence[Candle], levels: DayLevels, acceptance_bars: int, trap_band: float) -> Bias:
    """PDC trend filter (guide p3) with the equilibrium-trap rule (S8).

    * ``acceptance_bars`` consecutive closes above PDC -> BULLISH; below -> BEARISH.
    * If price is trapped between PDC and PDH (or PDL) and that band is
      narrower than ``trap_band`` -> NEUTRAL (no trade).
    """
    if len(candles) < acceptance_bars:
        return Bias.NEUTRAL
    recent = candles[-acceptance_bars:]
    last = candles[-1].close
    above = all(c.close > levels.pdc for c in recent)
    below = all(c.close < levels.pdc for c in recent)
    if above:
        if levels.pdh - levels.pdc < trap_band and last <= levels.pdh:
            return Bias.NEUTRAL
        return Bias.BULLISH
    if below:
        if levels.pdc - levels.pdl < trap_band and last >= levels.pdl:
            return Bias.NEUTRAL
        return Bias.BEARISH
    return Bias.NEUTRAL


class SignalEngine:
    def __init__(self, settings: Settings, registry: StrategyRegistry):
        self.settings = settings
        self.registry = registry
        self.min_confidence = float(settings.get("risk.min_confidence", 0.6))
        self._days: Dict[str, DayState] = {}

    # -- session management ------------------------------------------------------
    def start_day(self, spec: InstrumentSpec, trade_date: date, levels: DayLevels) -> DayState:
        self.registry.reset_day()
        state = DayState(trade_date=trade_date, spec=spec, levels=levels)
        self._days[spec.name] = state
        return state

    def state(self, spec: InstrumentSpec) -> DayState:
        return self._days[spec.name]

    def session(self, spec: InstrumentSpec) -> SessionTimes:
        return self.settings.session(spec.exchange.value)

    # -- bar processing ----------------------------------------------------------
    def on_bar(self, spec: InstrumentSpec, bar: Candle) -> List[Signal]:
        """Feed one closed bar; returns the accepted signals for that bar (0..n)."""
        st = self._days[spec.name]
        st.candles.append(bar)
        idx = len(st.candles) - 1
        session = self.session(spec)

        # opening range (first 15 minutes) once three 5-min bars are in
        if not st.levels.opening_range_ready and len(st.candles) >= 3 and st.candles[0].ts.time() == session.open:
            first = st.candles[:3]
            st.levels.opening_range_high = max(c.high for c in first)
            st.levels.opening_range_low = min(c.low for c in first)

        # rolling indicators (causal, incremental): Wilder ATR(14) and 20-bar mean volume
        if idx == 0:
            st._atr = [bar.high - bar.low]
            st._avg_vol = [bar.volume]
        else:
            prev_close = st.candles[idx - 1].close
            tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
            st._atr.append(st._atr[-1] + (tr - st._atr[-1]) / 14.0)
            window = st.candles[max(0, idx - 19): idx + 1]
            st._avg_vol.append(sum(c.volume for c in window) / len(window))

        trap_band = self.settings.instrument_value("bias.trap_band_points", spec.name, 0.0) or 0.0
        st.bias = compute_bias(st.candles, st.levels, int(self.settings.get("bias.acceptance_bars", 2)), trap_band)
        st.bias_history.append(st.bias)

        ctx = BarContext(
            spec=spec, settings=self.settings, session=session, candles=st.candles, index=idx, levels=st.levels,
            bias=st.bias, atr=float(st._atr[idx]), avg_volume=float(st._avg_vol[idx]),
            higher_tf=tuple(resample_candles(st.candles, 15, session.open.hour * 60 + session.open.minute)),
        )

        accepted: List[Signal] = []
        for strat in self.registry.for_spec(spec):
            try:
                sig = strat.on_bar(ctx)
            except Exception as exc:  # a buggy strategy must not kill the session
                logger.exception("strategy %s failed on %s: %s", strat.name, bar.ts, exc)
                continue
            if sig is None:
                continue
            verdict = self._filter(sig, ctx, st)
            if verdict is None:
                accepted.append(sig)
                st.accepted.append(sig)
                st.last_signal_key = self._key(sig)
            else:
                st.rejected.append(RejectedSignal(sig, verdict))
                logger.debug("rejected %s %s: %s", sig.strategy, sig.option_type.value, verdict)
        if len(accepted) > 1:
            accepted.sort(key=lambda s: s.confidence, reverse=True)
        return accepted

    # -- filters ------------------------------------------------------------------
    @staticmethod
    def _key(sig: Signal) -> str:
        return f"{sig.instrument}|{sig.strategy}|{sig.option_type.value}|{round(sig.spot_level, 1)}"

    def _filter(self, sig: Signal, ctx: BarContext, st: DayState) -> Optional[str]:
        t = ctx.bar_close_time
        session = ctx.session
        if not session.can_enter(t):
            if session.in_no_entry_window(t):
                return "midday no-entry window (guide p11)"
            return "outside entry hours"
        if sig.confidence < self.min_confidence:
            return f"confidence {sig.confidence:.2f} < {self.min_confidence:.2f}"
        if sig.risk_points <= 0:
            return "zero risk"
        if self._key(sig) == st.last_signal_key:
            return "duplicate signal"
        if ctx.bias == Bias.NEUTRAL and st.levels.pdh - st.levels.pdl > 0:
            # equilibrium trap: allow only strategies that do not require bias (09:20, squeeze)
            strat = next((s for s in self.registry.strategies if s.name == sig.strategy), None)
            if strat is not None and strat.requires_bias():
                return "neutral bias / equilibrium trap (guide p3)"
        return None

    # -- helpers for callers --------------------------------------------------------
    def signals_for_day(self, spec: InstrumentSpec) -> List[Signal]:
        return list(self._days[spec.name].accepted)

    def rejected_for_day(self, spec: InstrumentSpec) -> List[RejectedSignal]:
        return list(self._days[spec.name].rejected)


def split_sessions(df: pd.DataFrame) -> Dict[date, List[Candle]]:
    """Group a canonical frame into {date: [Candle, ...]} preserving order."""
    from .indicators import frame_to_candles

    out: Dict[date, List[Candle]] = {}
    df.to_csv("./dataatsplit_sessions.csv")
    if df is None or df.empty:
        return out
    for day, chunk in df.groupby(df["ts"].dt.date, sort=True):
        out[day] = frame_to_candles(chunk)
    return out

