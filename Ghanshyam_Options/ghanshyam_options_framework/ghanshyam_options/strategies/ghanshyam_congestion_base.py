"""Intraday congestion base / opening-range breakout (guide p3).

A horizontal multi-candle consolidation lasting ``min_base_minutes`` to
``max_base_minutes`` (20-45 min in the guide) whose height is <=
``max_base_range_atr`` x ATR. Entry strictly on a boundary violation **with
volume confirmation** (``volume_factor`` x base average; skipped when the feed
has no volume, e.g. NSE index symbols — then the confidence component is
neutral instead of blocking). Direction must agree with the PDC bias.

When ``use_opening_range`` is true the 09:15-09:30 opening range (guide p3
"Opening 15-Minute Range") is also accepted as a base once at least 3 bars are
in, so the first breach of the opening corridor is a continuation scalp.

SL = opposite boundary of the base; target = ``target_r`` x risk.
"""

from __future__ import annotations

from typing import Optional

from ..indicators import Base, detect_base
from ..models import OptionType, Signal
from .base import BarContext, Strategy


class CongestionBaseStrategy(Strategy):
    name = "congestion_base"

    def evaluate(self, ctx: BarContext) -> Optional[Signal]:
        if self.fired_count(ctx) >= 2:
            return None
        tf = ctx.timeframe_minutes
        min_bars = max(3, int(self.params.get("min_base_minutes", 30)) // tf)
        max_bars = max(min_bars, int(self.params.get("max_base_minutes", 60)) // tf)
        if ctx.index < min_bars:
            return None
        max_height = float(self.params.get("max_base_range_atr", 1.5)) * max(ctx.atr, 1e-9)
        base = detect_base(ctx.candles, ctx.index - 1, min_bars, max_bars, max_height)
        if base is None and bool(self.params.get("use_opening_range", True)) and ctx.levels.opening_range_ready:
            # opening-range corridor: bars 0..2 (09:15-09:30) once complete
            if ctx.index >= 3:
                first = ctx.candles[:3]
                base = Base(0, 2, ctx.levels.opening_range_high or max(c.high for c in first),
                            ctx.levels.opening_range_low or min(c.low for c in first),
                            float(sum(c.volume for c in first) / 3.0))
                if base.height > max_height * 1.5:
                    base = None
        if base is None:
            return None
        bar = ctx.bar
        if bar.close > base.high:
            option_type, level, sl = OptionType.CE, bar.close, base.low
        elif bar.close < base.low:
            option_type, level, sl = OptionType.PE, bar.close, base.high
        else:
            return None
        if not self.bias_ok(ctx, option_type):
            return None

        vol_ok = True
        vol_score = 0.5
        if base.avg_volume > 0 and bar.volume > 0:
            vol_ok = bar.volume >= float(self.params.get("volume_factor", 1.2)) * base.avg_volume
            vol_score = min(1.0, bar.volume / (2.0 * base.avg_volume))
            if not vol_ok:
                return None
        risk = abs(level - sl)
        if risk <= 0:
            return None
        target = level + float(self.params.get("target_r", 2.0)) * risk * (1 if option_type == OptionType.CE else -1)
        compression = 1.0 - min(1.0, base.height / max(max_height, 1e-9))
        pattern = 0.5 + 0.3 * compression + 0.2 * min(1.0, base.bars / max_bars)
        conf, comps = self.score(ctx, option_type, pattern=pattern, structural=0.7, volume=vol_score)
        return Signal(
            instrument=ctx.spec.name, strategy=self.name, option_type=option_type, ts=bar.ts,
            spot_level=level, stop_loss=sl, target=target,
            reason=(f"{base.bars * tf}-min base {base.low:.1f}-{base.high:.1f} broken by close {bar.close:.1f}"
                    f"{' with volume' if base.avg_volume > 0 and bar.volume > 0 else ''}"),
            confidence=conf, components=comps, counter_trend=not self.aligned(ctx.bias, option_type),
            entry_type="market",
        )
