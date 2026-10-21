"""Bullish Hammer reversal (guide p4, p6).

Pre-conditions (either suffices):
* >= ``min_consecutive_candles`` (7-8) consecutive bearish 5m candles before the hammer, or
* an uncorrected slide of >= ``min_run_points`` (BN 150-200 / Nifty 70-100) in ``run_lookback_bars``.

The hammer must form at a major structural level (PDL, support zone, lower
channel boundary) when ``require_structural_level`` is true. Morphology:
lower shadow >= 2x body, small upper shadow; colour secondary.

Entry: breach of the hammer high. SL: ``sl_buffer_points`` below the hammer
low. Target: ``target_r`` x risk (minimum 1:2).
"""

from __future__ import annotations

from typing import Optional

from ..indicators import consecutive_colour_count, is_hammer, uncorrected_decline
from ..models import OptionType, Signal
from .base import BarContext, Strategy


class HammerStrategy(Strategy):
    name = "hammer"

    def evaluate(self, ctx: BarContext) -> Optional[Signal]:
        bar = ctx.bar
        if ctx.index < 3:
            return None
        if not is_hammer(bar, float(self.params.get("wick_body_ratio", 2.0)),
                         float(self.params.get("max_opposite_wick_ratio", 0.30))):
            return None

        prior = ctx.bars_before(int(self.params.get("run_lookback_bars", 12)))
        reds = consecutive_colour_count(prior, red=True)
        slide = uncorrected_decline(list(prior) + [bar], int(self.params.get("run_lookback_bars", 12)) + 1)
        min_consec = int(self.params.get("min_consecutive_candles", 7))
        min_run = ctx.instrument_value("strategies.hammer.min_run_points", 1e18) or 1e18
        if reds < min_consec and slide < min_run:
            return None

        tol = ctx.instrument_value("bias.level_tolerance_points", 0.0) or 0.0
        level_hit = ctx.structural_level_near(bar.low, "support", tol)
        if self.params.get("require_structural_level", True) and level_hit is None:
            return None

        option_type = OptionType.CE
        if not self.bias_ok(ctx, option_type):
            return None
        buffer = float(self.params.get("sl_buffer_points", 2.0))
        entry, sl = bar.high, bar.low - buffer
        risk = entry - sl
        if risk <= 0:
            return None
        target = entry + float(self.params.get("target_r", 2.0)) * risk

        exhaustion = min(1.0, max(reds / float(min_consec), slide / float(min_run)))
        wick_quality = min(1.0, bar.lower_wick / max(bar.body, bar.range * 0.05) / 4.0)
        pattern = 0.5 * exhaustion + 0.5 * wick_quality
        conf, comps = self.score(ctx, option_type, pattern=pattern, structural=1.0 if level_hit else 0.3,
                                 volume=self.volume_quality(ctx))
        return Signal(
            instrument=ctx.spec.name, strategy=self.name, option_type=option_type, ts=bar.ts,
            spot_level=entry, stop_loss=sl, target=target,
            reason=(f"hammer after {reds} red bars / {slide:.0f}pt slide at level "
                    f"{level_hit if level_hit is not None else 'n/a'}; entry {entry:.1f} SL {sl:.1f}"),
            confidence=conf, components=comps, counter_trend=not self.aligned(ctx.bias, option_type),
        )
