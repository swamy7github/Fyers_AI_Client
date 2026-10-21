"""14:20 Trend Resumption / Afternoon Pulse (guide p5, p6, p11).

Map the consolidation base that forms between ``base_start`` (13:30) and
``base_end`` (14:15). Evaluate the close of the 14:15-14:20 candle: if it
breaches the midday band (or retests a key S/R level in the bias direction),
buy at the 14:20 bar open. SL = the base's opposite swing (base low for CE,
base high for PE). Target = ``target_r`` x risk.
"""

from __future__ import annotations

from datetime import time
from typing import Optional

from ..config.config import parse_hhmm
from ..models import Bias, OptionType, Signal
from .base import BarContext, Strategy


class Afternoon1420Strategy(Strategy):
    name = "afternoon_1420"
    markets = ("NSE",)

    def evaluate(self, ctx: BarContext) -> Optional[Signal]:
        if self.fired_count(ctx) > 0:
            return None
        trigger: time = parse_hhmm(str(self.params.get("trigger_bar", "14:15")))
        bar = ctx.bar
        if (bar.ts.hour, bar.ts.minute) != (trigger.hour, trigger.minute):
            return None
        base_start = parse_hhmm(str(self.params.get("base_start", "13:30")))
        base_end = parse_hhmm(str(self.params.get("base_end", "14:15")))
        base = [c for c in ctx.candles[:ctx.index] if base_start <= c.ts.time() < base_end]
        if len(base) < 3:
            return None
        base_high = max(c.high for c in base)
        base_low = min(c.low for c in base)

        if bar.close > base_high:
            option_type, level, sl = OptionType.CE, bar.close, base_low
        elif bar.close < base_low:
            option_type, level, sl = OptionType.PE, bar.close, base_high
        else:
            return None
        if not self.bias_ok(ctx, option_type):
            return None
        risk = abs(level - sl)
        if risk <= 0 or risk > 3.0 * max(ctx.atr, 1e-9):
            return None  # a base taller than 3 ATR is not a consolidation
        target = level + float(self.params.get("target_r", 2.0)) * risk * (1 if option_type == OptionType.CE else -1)

        base_height = base_high - base_low
        compression = 1.0 - min(1.0, base_height / max(3.0 * ctx.atr, 1e-9))
        push = abs(bar.close - (base_high if option_type == OptionType.CE else base_low)) / max(base_height, 1e-9)
        pattern = 0.5 + 0.3 * compression + 0.2 * min(1.0, push)
        structural = 1.0 if ctx.structural_level_near(level, "resistance" if option_type == OptionType.CE else "support") else 0.5
        conf, comps = self.score(ctx, option_type, pattern=pattern, structural=structural,
                                 volume=self.volume_quality(ctx), time_quality=1.0)
        return Signal(
            instrument=ctx.spec.name, strategy=self.name, option_type=option_type, ts=bar.ts,
            spot_level=level, stop_loss=sl, target=target,
            reason=(f"14:15 close {bar.close:.1f} broke midday base {base_low:.1f}-{base_high:.1f}; "
                    f"SL at base {'low' if option_type == OptionType.CE else 'high'}"),
            confidence=conf, components=comps, counter_trend=not self.aligned(ctx.bias, option_type)
            and ctx.bias != Bias.NEUTRAL, entry_type="market",
        )
