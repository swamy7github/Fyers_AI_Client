"""Bearish Shooting Star reversal (guide p4-p5, p6). Mirror of the hammer.

Prior rally of >= 7-8 consecutive bullish candles or 150-200+ pts (BN) into a
major resistance / PDH. Upper shadow >= 2x body with minimal lower shadow.
Entry: ATM PE on a breach of the star's low. Invalidation: spot above the wick
high (SL ``sl_buffer_points`` above). Target: ``target_r`` x risk.
"""

from __future__ import annotations

from typing import Optional

from ..indicators import consecutive_colour_count, is_shooting_star, uncorrected_rally
from ..models import OptionType, Signal
from .base import BarContext, Strategy


class ShootingStarStrategy(Strategy):
    name = "shooting_star"

    def evaluate(self, ctx: BarContext) -> Optional[Signal]:
        bar = ctx.bar
        if ctx.index < 3:
            return None
        if not is_shooting_star(bar, float(self.params.get("wick_body_ratio", 2.0)),
                                float(self.params.get("max_opposite_wick_ratio", 0.30))):
            return None

        lookback = int(self.params.get("run_lookback_bars", 12))
        prior = ctx.bars_before(lookback)
        greens = consecutive_colour_count(prior, red=False)
        rally = uncorrected_rally(list(prior) + [bar], lookback + 1)
        min_consec = int(self.params.get("min_consecutive_candles", 7))
        min_run = ctx.instrument_value("strategies.shooting_star.min_run_points", 1e18) or 1e18
        if greens < min_consec and rally < min_run:
            return None

        tol = ctx.instrument_value("bias.level_tolerance_points", 0.0) or 0.0
        level_hit = ctx.structural_level_near(bar.high, "resistance", tol)
        if self.params.get("require_structural_level", True) and level_hit is None:
            return None

        option_type = OptionType.PE
        if not self.bias_ok(ctx, option_type):
            return None
        buffer = float(self.params.get("sl_buffer_points", 2.0))
        entry, sl = bar.low, bar.high + buffer
        risk = sl - entry
        if risk <= 0:
            return None
        target = entry - float(self.params.get("target_r", 2.0)) * risk

        exhaustion = min(1.0, max(greens / float(min_consec), rally / float(min_run)))
        wick_quality = min(1.0, bar.upper_wick / max(bar.body, bar.range * 0.05) / 4.0)
        pattern = 0.5 * exhaustion + 0.5 * wick_quality
        conf, comps = self.score(ctx, option_type, pattern=pattern, structural=1.0 if level_hit else 0.3,
                                 volume=self.volume_quality(ctx))
        return Signal(
            instrument=ctx.spec.name, strategy=self.name, option_type=option_type, ts=bar.ts,
            spot_level=entry, stop_loss=sl, target=target,
            reason=(f"shooting star after {greens} green bars / {rally:.0f}pt rally at level "
                    f"{level_hit if level_hit is not None else 'n/a'}; entry {entry:.1f} SL {sl:.1f}"),
            confidence=conf, components=comps, counter_trend=not self.aligned(ctx.bias, option_type),
        )
