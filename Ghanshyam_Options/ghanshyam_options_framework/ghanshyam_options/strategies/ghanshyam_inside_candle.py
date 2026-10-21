"""Inside Candle Compression (guide p4, p6).

Mother candle + inside candle fully contained in the mother's range. In an
uptrend (PDC bias bullish) buy CE when a subsequent candle breaches the inside
candle's high (conservative: the mother's high); in a downtrend buy PE on a
breach of the inside low. SL = opposite extreme of the inside candle. Target =
``target_range_multiple`` x the pattern's range (1.5-2.5x).

Implementation: at the close of the inside bar we emit a *pending* stop
signal; the engine arms it and fires on the first later bar that breaches the
level. A pattern older than two bars without a breach is discarded (the next
inside bar supersedes it).
"""

from __future__ import annotations

from typing import Optional

from ..indicators import is_inside
from ..models import Bias, OptionType, Signal
from .base import BarContext, Strategy


class InsideCandleStrategy(Strategy):
    name = "inside_candle"

    def evaluate(self, ctx: BarContext) -> Optional[Signal]:
        if ctx.index < 2:
            return None
        inside, mother = ctx.bar, ctx.candles[ctx.index - 1]
        if not is_inside(inside, mother):
            return None
        min_range = ctx.instrument_value("strategies.inside_candle.min_range_points", 0.0) or 0.0
        if inside.range < min_range:
            return None

        # Direction: PDC bias decides (p3/p4). Neutral bias -> no trade for a
        # bias-required strategy; else use the mother candle colour.
        if ctx.bias == Bias.BULLISH:
            option_type = OptionType.CE
        elif ctx.bias == Bias.BEARISH:
            option_type = OptionType.PE
        else:
            if self.requires_bias():
                return None
            option_type = OptionType.CE if mother.is_green else OptionType.PE

        trigger_bar = mother if str(self.params.get("trigger", "inside")) == "mother" else inside
        mult = float(self.params.get("target_range_multiple", 2.0))
        pattern_range = inside.range
        if option_type == OptionType.CE:
            level, sl = trigger_bar.high, inside.low
            target = level + mult * pattern_range
            structural = ctx.structural_level_near(inside.low, "support")
        else:
            level, sl = trigger_bar.low, inside.high
            target = level - mult * pattern_range
            structural = ctx.structural_level_near(inside.high, "resistance")
        if abs(level - sl) <= 0:
            return None

        # pattern quality: tighter inside bar relative to the mother = stronger compression
        compression = 1.0 - min(1.0, inside.range / max(mother.range, 1e-9))
        pattern = 0.6 + 0.4 * compression
        structural_score = 1.0 if structural is not None else 0.4
        conf, comps = self.score(ctx, option_type, pattern=pattern, structural=structural_score,
                                 volume=self.volume_quality(ctx, mother))
        if structural is not None:
            conf = min(1.0, conf + float(self.params.get("near_level_bonus", 0.0)))
        return Signal(
            instrument=ctx.spec.name, strategy=self.name, option_type=option_type, ts=ctx.bar.ts,
            spot_level=level, stop_loss=sl, target=target,
            reason=(f"inside bar {inside.range:.1f}pts within mother {mother.range:.1f}pts; "
                    f"trigger {level:.1f}, SL {sl:.1f}, target {mult:.1f}x range"),
            confidence=conf, components=comps, counter_trend=not self.aligned(ctx.bias, option_type),
        )
