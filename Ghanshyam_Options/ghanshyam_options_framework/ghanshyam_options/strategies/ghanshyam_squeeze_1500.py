"""15:00 Liquidity Sweep & Squeeze (guide p5, p6, p11).

Primarily expiry sessions (``expiry_days_only``). Mark the high/low of the
``base_start``-``base_end`` window (14:30-14:58). At the 14:59/15:00 open a
directional scalp is taken on a breach of that boundary. SL = midpoint of
the base. Target = ``target_premium_pct`` of the entry premium (15-30 %),
holding at most ``max_hold_minutes`` (3-10 min) and always squared off well
before the close.

With 5-minute bars the last base bar is the 14:50 bar (closes 14:55) and the
14:55 bar (closing 15:00) is the breakout bar; the fill happens at the 15:00
open, matching "boundary violation at 14:59/15:00 open".
"""

from __future__ import annotations

from typing import Optional

from ..config.config import parse_hhmm
from ..models import OptionType, Signal
from .base import BarContext, Strategy


class Squeeze1500Strategy(Strategy):
    name = "squeeze_1500"
    markets = ("NSE",)

    def evaluate(self, ctx: BarContext) -> Optional[Signal]:
        if self.fired_count(ctx) > 0:
            return None
        if bool(self.params.get("expiry_days_only", True)) and not ctx.levels.is_expiry_day:
            return None
        trigger = parse_hhmm(str(self.params.get("trigger_bar", "14:55")))
        bar = ctx.bar
        if (bar.ts.hour, bar.ts.minute) != (trigger.hour, trigger.minute):
            return None
        base_start = parse_hhmm(str(self.params.get("base_start", "14:30")))
        base_end = parse_hhmm(str(self.params.get("base_end", "14:55")))
        base = [c for c in ctx.candles[:ctx.index] if base_start <= c.ts.time() < base_end]
        if len(base) < 3:
            return None
        hi = max(c.high for c in base)
        lo = min(c.low for c in base)
        mid = (hi + lo) / 2.0

        if bar.close > hi:
            option_type, level = OptionType.CE, bar.close
        elif bar.close < lo:
            option_type, level = OptionType.PE, bar.close
        else:
            return None
        if not self.bias_ok(ctx, option_type):
            return None
        sl = mid
        if abs(level - sl) <= 0:
            return None
        push = abs(bar.close - (hi if option_type == OptionType.CE else lo)) / max(hi - lo, 1e-9)
        pattern = 0.6 + 0.4 * min(1.0, push)
        conf, comps = self.score(ctx, option_type, pattern=pattern, structural=0.6,
                                 volume=self.volume_quality(ctx), time_quality=1.0)
        return Signal(
            instrument=ctx.spec.name, strategy=self.name, option_type=option_type, ts=bar.ts,
            spot_level=level, stop_loss=sl, target=None,
            reason=f"15:00 squeeze: 14:30-14:55 base {lo:.1f}-{hi:.1f} broken by {bar.close:.1f}; SL midpoint {mid:.1f}",
            confidence=conf, components=comps,
            target_premium_pct=float(self.params.get("target_premium_pct", 0.20)),
            max_hold_minutes=int(self.params.get("max_hold_minutes", 10)),
            counter_trend=not self.aligned(ctx.bias, option_type), entry_type="market",
        )
