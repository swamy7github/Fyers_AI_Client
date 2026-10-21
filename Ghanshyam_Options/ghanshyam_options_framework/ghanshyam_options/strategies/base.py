"""Strategy interface and the per-bar context every setup receives.

A strategy sees the *closed* bar ``context.bar`` (index ``context.index`` in
``context.candles``) and everything before it. It may not read bars after
``context.index`` — the backtester only materialises bars up to the current
one, and the live runner only has the past, so this is enforced structurally.

Signals are *stop entries* at a pattern extreme: ``Signal.spot_level`` is the
level whose breach triggers the trade; the signal engine converts that into a
fill at the next bar (paper) or at the breach (backtest, see ``backtest/``).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Dict, List, Optional, Sequence, Tuple

from ..config.config import SessionTimes, Settings
from ..indicators import near_level
from ..instruments import InstrumentSpec
from ..models import Bias, Candle, DayLevels, OptionType, Signal

logger = logging.getLogger("ghanshyam.strategy")


@dataclass
class BarContext:
    spec: InstrumentSpec
    settings: Settings
    session: SessionTimes
    candles: Sequence[Candle]          # all bars of the current session up to and including ``index``
    index: int                         # index of the closed bar being evaluated
    levels: DayLevels
    bias: Bias
    atr: float                         # ATR(14) of the underlying on the strategy timeframe at ``index``
    avg_volume: float                  # 20-bar average volume at ``index``
    prev_session: Sequence[Candle] = field(default_factory=tuple)
    higher_tf: Sequence[Candle] = field(default_factory=tuple)  # 15m bars completed so far (optional)
    extra: Dict[str, object] = field(default_factory=dict)

    @property
    def bar(self) -> Candle:
        return self.candles[self.index]

    @property
    def prev(self) -> Optional[Candle]:
        return self.candles[self.index - 1] if self.index >= 1 else None

    @property
    def now(self) -> datetime:
        return self.bar.ts

    @property
    def bar_close_time(self) -> time:
        """Wall-clock time at which this bar closed (bar open + timeframe)."""
        tf = self.timeframe_minutes
        total = self.bar.ts.hour * 60 + self.bar.ts.minute + tf
        return time(min(total // 60, 23), total % 60)

    @property
    def timeframe_minutes(self) -> int:
        if len(self.candles) >= 2:
            return int(round((self.candles[1].ts - self.candles[0].ts).total_seconds() / 60.0)) or 5
        return int(self.settings.get("data.default_resolution", 5))

    def bars_before(self, n: int) -> List[Candle]:
        """The ``n`` bars strictly before the current bar."""
        start = max(0, self.index - n)
        return list(self.candles[start:self.index])

    def instrument_value(self, dotted: str, default: Optional[float] = None) -> Optional[float]:
        return self.settings.instrument_value(dotted, self.spec.name, default)

    def structural_level_near(self, price: float, side: str, tolerance: Optional[float] = None) -> Optional[float]:
        tol = tolerance if tolerance is not None else (
            self.settings.instrument_value("bias.level_tolerance_points", self.spec.name, 0.0) or 0.0)
        levels = self.levels.all_supports() if side == "support" else self.levels.all_resistances()
        return near_level(price, levels, tol)


class Strategy(ABC):
    """One Ghanshyam setup. Subclasses implement :meth:`evaluate`."""

    name: str = "base"
    markets: Tuple[str, ...] = ("NSE", "MCX")

    def __init__(self, settings: Settings):
        self.settings = settings
        self.params: Dict[str, object] = settings.strategy(self.name)
        self._fired_today: Dict[str, int] = {}

    # -- lifecycle --------------------------------------------------------------
    def reset_day(self) -> None:
        self._fired_today.clear()

    @property
    def timeframe(self) -> int:
        return int(self.params.get("timeframe", 5))

    def enabled_for(self, spec: InstrumentSpec) -> bool:
        if not self.params.get("enabled", False):
            return False
        allowed = self.params.get("markets")
        if allowed and spec.exchange.value not in allowed:
            return False
        return spec.exchange.value in self.markets

    # -- time gating ------------------------------------------------------------
    def in_window(self, ctx: BarContext) -> bool:
        windows = self.settings.strategy_windows(self.name, ctx.spec.exchange.value)
        if not windows:
            return True
        t = ctx.bar_close_time
        return any(start <= t <= end for start, end in windows)

    # -- bias helpers -----------------------------------------------------------
    def requires_bias(self) -> bool:
        return bool(self.params.get("require_bias_alignment", False))

    @staticmethod
    def aligned(bias: Bias, option_type: OptionType) -> bool:
        return (bias == Bias.BULLISH and option_type == OptionType.CE) or (bias == Bias.BEARISH and option_type == OptionType.PE)

    def bias_ok(self, ctx: BarContext, option_type: OptionType) -> bool:
        if not self.requires_bias():
            return True
        return self.aligned(ctx.bias, option_type)

    # -- confidence scoring (guide: trade only high-confluence setups) ----------
    def score(self, ctx: BarContext, option_type: OptionType, *, pattern: float, structural: float,
              volume: float = 0.5, time_quality: float = 1.0) -> Tuple[float, Dict[str, Optional[float]]]:
        w = self.settings.get("confluence.weights", {}) or {}
        bias_score = 1.0 if self.aligned(ctx.bias, option_type) else (0.0 if ctx.bias != Bias.NEUTRAL else 0.4)
        comps = {
            "pattern": max(0.0, min(1.0, pattern)),
            "bias_alignment": bias_score,
            "structural_level": max(0.0, min(1.0, structural)),
            "time_window": max(0.0, min(1.0, time_quality)),
            "volume": max(0.0, min(1.0, volume)),
        }
        total = sum(float(w.get(k, 0.0)) * v for k, v in comps.items())
        total = max(0.0, min(1.0, total))
        return total, comps  # type: ignore[return-value]

    def volume_quality(self, ctx: BarContext, bar: Optional[Candle] = None) -> float:
        bar = bar or ctx.bar
        if ctx.avg_volume <= 0 or bar.volume <= 0:
            return 0.5  # indices have no volume on Fyers; neutral score
        return max(0.0, min(1.0, bar.volume / (1.5 * ctx.avg_volume)))

    # -- bookkeeping ------------------------------------------------------------
    def mark_fired(self, ctx: BarContext) -> None:
        self._fired_today[ctx.spec.name] = self._fired_today.get(ctx.spec.name, 0) + 1

    def fired_count(self, ctx: BarContext) -> int:
        return self._fired_today.get(ctx.spec.name, 0)

    def max_hold(self) -> Optional[int]:
        v = self.params.get("max_hold_minutes")
        return int(v) if v is not None else None

    # -- the one method subclasses implement -----------------------------------
    @abstractmethod
    def evaluate(self, ctx: BarContext) -> Optional[Signal]:
        """Return a Signal when the closed bar completes a valid setup."""

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        if not self.enabled_for(ctx.spec) or not self.in_window(ctx):
            return None
        sig = self.evaluate(ctx)
        if sig is not None:
            sig.timeframe = self.timeframe
            if sig.max_hold_minutes is None:
                sig.max_hold_minutes = self.max_hold()
            sig.bar_ts = ctx.bar.ts
            self.mark_fired(ctx)
        return sig


class StrategyRegistry:
    def __init__(self, strategies: Sequence[Strategy]):
        self.strategies: List[Strategy] = list(strategies)

    def reset_day(self) -> None:
        for s in self.strategies:
            s.reset_day()

    def names(self) -> List[str]:
        return [s.name for s in self.strategies]

    def for_spec(self, spec: InstrumentSpec) -> List[Strategy]:
        return [s for s in self.strategies if s.enabled_for(spec)]


def build_strategies(settings: Settings, only: Optional[Sequence[str]] = None) -> StrategyRegistry:
    """Instantiate every enabled strategy (lazy import to avoid cycles)."""
    from .ghanshyam_inside_candle import InsideCandleStrategy
    from .ghanshyam_hammer import HammerStrategy
    from .ghanshyam_shooting_star import ShootingStarStrategy
    from .ghanshyam_morning_0920 import Morning0920Strategy
    from .ghanshyam_afternoon_1420 import Afternoon1420Strategy
    from .ghanshyam_squeeze_1500 import Squeeze1500Strategy
    from .ghanshyam_trendline import TrendlineStrategy
    from .ghanshyam_ldchl_levels import LdchlLevelsStrategy
    from .ghanshyam_congestion_base import CongestionBaseStrategy

    classes = [
        Morning0920Strategy, InsideCandleStrategy, HammerStrategy, ShootingStarStrategy,
        LdchlLevelsStrategy, CongestionBaseStrategy, TrendlineStrategy, Afternoon1420Strategy, Squeeze1500Strategy,
    ]
    out: List[Strategy] = []
    for cls in classes:
        if only and cls.name not in only:
            continue
        if settings.get(f"strategies.{cls.name}") is None:
            continue
        strat = cls(settings)
        if strat.params.get("enabled", False):
            out.append(strat)
    return StrategyRegistry(out)
