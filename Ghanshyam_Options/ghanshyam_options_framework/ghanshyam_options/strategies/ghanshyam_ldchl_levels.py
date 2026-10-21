"""LDCHL level trades (guide p2-p3 table).

* **PDH**: CE on confirmed multi-minute *acceptance* above PDH (``acceptance_bars``
  consecutive closes above); PE on a clear *upper-wick rejection* at PDH.
* **PDL**: PE on sustained breakdown below PDL; CE on *lower-wick rejection*
  (liquidity absorption) at PDL.

Acceptance trades follow the break direction and must agree with the PDC
bias when ``require_bias_alignment`` is set. Rejection trades are counter to
the break; they are allowed only when ``counter_trend_allowed`` and are sized
at the counter-trend factor by the risk module. One trade per level-event.
"""

from __future__ import annotations

from typing import Optional

from ..indicators import wick_rejection_down, wick_rejection_up
from ..models import OptionType, Signal
from .base import BarContext, Strategy


class LdchlLevelsStrategy(Strategy):
    name = "ldchl_levels"

    def evaluate(self, ctx: BarContext) -> Optional[Signal]:
        if self.fired_count(ctx) >= 2:
            return None
        n = int(self.params.get("acceptance_bars", 2))
        if ctx.index < n:
            return None
        bar = ctx.bar
        pdh, pdl = ctx.levels.pdh, ctx.levels.pdl
        tol = ctx.instrument_value("bias.level_tolerance_points", 0.0) or 0.0
        ratio = float(self.params.get("wick_rejection_ratio", 2.0))
        target_r = float(self.params.get("target_r", 2.0))
        recent = ctx.candles[ctx.index - n + 1: ctx.index + 1]

        # -- acceptance above PDH -> CE ------------------------------------------
        if all(c.close > pdh for c in recent) and ctx.candles[ctx.index - n].close <= pdh:
            return self._make(ctx, OptionType.CE, level=bar.close, sl=min(c.low for c in recent) - tol / 2,
                              target_r=target_r, reason=f"{n}-bar acceptance above PDH {pdh:.1f}", pattern=0.75,
                              counter=False)
        # -- breakdown below PDL -> PE ---------------------------------------------
        if all(c.close < pdl for c in recent) and ctx.candles[ctx.index - n].close >= pdl:
            return self._make(ctx, OptionType.PE, level=bar.close, sl=max(c.high for c in recent) + tol / 2,
                              target_r=target_r, reason=f"{n}-bar breakdown below PDL {pdl:.1f}", pattern=0.75,
                              counter=False)
        if not bool(self.params.get("counter_trend_allowed", True)):
            return None
        # -- upper-wick rejection at PDH -> PE ----------------------------------------
        if bar.high >= pdh - tol and bar.close < pdh and wick_rejection_up(bar, ratio):
            return self._make(ctx, OptionType.PE, level=bar.low, sl=bar.high + max(tol / 4, 1.0), target_r=target_r,
                              reason=f"upper-wick rejection at PDH {pdh:.1f}", pattern=0.7, counter=True)
        # -- lower-wick rejection at PDL -> CE ----------------------------------------
        if bar.low <= pdl + tol and bar.close > pdl and wick_rejection_down(bar, ratio):
            return self._make(ctx, OptionType.CE, level=bar.high, sl=bar.low - max(tol / 4, 1.0), target_r=target_r,
                              reason=f"lower-wick rejection at PDL {pdl:.1f}", pattern=0.7, counter=True)
        return None

    def _make(self, ctx: BarContext, option_type: OptionType, *, level: float, sl: float, target_r: float,
              reason: str, pattern: float, counter: bool) -> Optional[Signal]:
        if not counter and not self.bias_ok(ctx, option_type):
            return None
        risk = abs(level - sl)
        if risk <= 0:
            return None
        target = level + target_r * risk * (1 if option_type == OptionType.CE else -1)
        conf, comps = self.score(ctx, option_type, pattern=pattern, structural=1.0, volume=self.volume_quality(ctx))
        if counter:
            conf *= 0.85  # rejection trades fight the break; demand more confluence elsewhere
        return Signal(
            instrument=ctx.spec.name, strategy=self.name, option_type=option_type, ts=ctx.bar.ts,
            spot_level=level, stop_loss=sl, target=target, reason=reason, confidence=conf, components=comps,
            counter_trend=counter or not self.aligned(ctx.bias, option_type),
        )
