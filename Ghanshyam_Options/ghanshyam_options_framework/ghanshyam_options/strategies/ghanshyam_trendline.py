"""Trendline Break & Retest (guide p5, p6-p7).

* Trendlines through >= ``min_touches`` (3) swing highs (resistance) or swing
  lows (support), swing strength ``pivot_strength``; a touch is within
  ``touch_tolerance_atr`` x ATR of the line. Pivots are only used once
  confirmed (``pivot_strength`` bars later) — no look-ahead.
* Breakouts are not chased: after a close through the line we wait for a
  retest within ``retest_max_bars`` bars whose bar touches the line again on
  **lower volume** (``low_volume_factor`` x 20-bar average; indices without
  volume skip the volume check).
* Entry when the retest is confirmed by taking out the *breakout candle's*
  extreme; SL behind the retest swing (retest bar low/high); target
  ``target_r`` x risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from ..indicators import fit_trendlines, swing_pivots
from ..models import Candle, OptionType, Signal
from .base import BarContext, Strategy


@dataclass
class _Pending:
    kind: str                 # "resistance" / "support"
    break_index: int
    break_bar: Candle
    line_slope: float
    line_intercept: float
    touches: int


class TrendlineStrategy(Strategy):
    name = "trendline"

    def __init__(self, settings):
        super().__init__(settings)
        self._pending: Dict[str, _Pending] = {}

    def reset_day(self) -> None:
        super().reset_day()
        self._pending.clear()

    def evaluate(self, ctx: BarContext) -> Optional[Signal]:
        strength = int(self.params.get("pivot_strength", 2))
        if ctx.index < 4 * strength + 4:
            return None
        key = ctx.spec.name
        bar = ctx.bar
        tol = float(self.params.get("touch_tolerance_atr", 0.35)) * max(ctx.atr, 1e-9)

        pending = self._pending.get(key)
        if pending is not None:
            if ctx.index - pending.break_index > int(self.params.get("retest_max_bars", 6)):
                self._pending.pop(key, None)
                pending = None
        if pending is not None:
            return self._check_retest(ctx, pending, tol)

        highs = [c.high for c in ctx.candles[:ctx.index + 1]]
        lows = [c.low for c in ctx.candles[:ctx.index + 1]]
        pivots = swing_pivots(highs, lows, strength)
        lines = fit_trendlines(pivots, highs, lows, tol, int(self.params.get("min_touches", 3)), upto_index=ctx.index)
        for ln in lines:
            if ln.last_index >= ctx.index - strength:  # line must be established before the break bar
                continue
            lv = ln.value_at(ctx.index)
            prev_lv = ln.value_at(ctx.index - 1)
            prev = ctx.candles[ctx.index - 1]
            if ln.kind == "resistance" and bar.close > lv + tol and prev.close <= prev_lv + tol:
                self._pending[key] = _Pending("resistance", ctx.index, bar, ln.slope, ln.intercept, ln.touches)
                return None
            if ln.kind == "support" and bar.close < lv - tol and prev.close >= prev_lv - tol:
                self._pending[key] = _Pending("support", ctx.index, bar, ln.slope, ln.intercept, ln.touches)
                return None
        return None

    def _check_retest(self, ctx: BarContext, p: _Pending, tol: float) -> Optional[Signal]:
        bar = ctx.bar
        lv = p.line_intercept + p.line_slope * ctx.index
        low_vol_ok = True
        if ctx.avg_volume > 0 and bar.volume > 0:
            low_vol_ok = bar.volume <= float(self.params.get("low_volume_factor", 0.9)) * ctx.avg_volume
        if p.kind == "resistance":
            option_type = OptionType.CE
            # failure: close back below the line => breakout failed
            if bar.close < lv - tol:
                self._pending.pop(ctx.spec.name, None)
                return None
            touched = bar.low <= lv + tol
            if not touched or not low_vol_ok:
                return None
            level, sl = p.break_bar.high, bar.low - tol
        else:
            option_type = OptionType.PE
            if bar.close > lv + tol:
                self._pending.pop(ctx.spec.name, None)
                return None
            touched = bar.high >= lv - tol
            if not touched or not low_vol_ok:
                return None
            level, sl = p.break_bar.low, bar.high + tol
        if not self.bias_ok(ctx, option_type):
            self._pending.pop(ctx.spec.name, None)
            return None
        risk = abs(level - sl)
        if risk <= 0:
            return None
        target = level + float(self.params.get("target_r", 2.0)) * risk * (1 if option_type == OptionType.CE else -1)
        self._pending.pop(ctx.spec.name, None)
        pattern = min(1.0, 0.5 + 0.15 * (p.touches - 2))
        conf, comps = self.score(ctx, option_type, pattern=pattern, structural=0.8, volume=1.0 if low_vol_ok else 0.3)
        return Signal(
            instrument=ctx.spec.name, strategy=self.name, option_type=option_type, ts=bar.ts,
            spot_level=level, stop_loss=sl, target=target,
            reason=(f"{p.kind} trendline ({p.touches} touches) broken at bar {p.break_index}, retested at "
                    f"{lv:.1f}; entry on break-bar extreme {level:.1f}, SL behind retest {sl:.1f}"),
            confidence=conf, components=comps, counter_trend=not self.aligned(ctx.bias, option_type),
        )
