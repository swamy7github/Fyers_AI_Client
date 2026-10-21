"""09:20 Morning Range Breakout (guide p5, p6, p10).

At 09:20:00 mark the high/low of the first 5-minute candle (09:15-09:20). No
orders during that candle. If the candle is wider than ``max_range_points``
(BN 200-250) the setup is invalid for the day (range exhaustion). Otherwise
enter in the direction of the first breach of either boundary — a stop order
sitting at the boundary, filled *inside* the breaching candle (``entry_type=
"stop"``, ``fill_same_bar=True``). SL = opposite extreme for a narrow range,
or the 50 % midpoint when the range exceeds ``wide_range_points``. Target =
``target_points`` (40-80 BN spot points).

For MCX the "first candle" is the first bar of the MCX session (09:00-09:05).
One trade per day; the breakout must occur within ``breakout_bars_window``
bars of the opening bar. A bar that breaches *both* boundaries is ambiguous
(order unknown) and voids the setup for the day.
"""

from __future__ import annotations

from typing import Optional

from ..models import OptionType, Signal
from .base import BarContext, Strategy


class Morning0920Strategy(Strategy):
    name = "morning_0920"

    def evaluate(self, ctx: BarContext) -> Optional[Signal]:
        if self.fired_count(ctx) > 0:
            return None
        if not ctx.candles or ctx.index < 1:
            return None
        opening = ctx.candles[0]
        session_open = ctx.session.open
        if (opening.ts.hour, opening.ts.minute) != (session_open.hour, session_open.minute):
            return None  # the day's bars do not start at the session open -> cannot trust the range
        window = int(self.params.get("breakout_bars_window", 3))
        if ctx.index > window:
            return None

        rng = opening.range
        max_range = ctx.instrument_value("strategies.morning_0920.max_range_points", 1e18) or 1e18
        if rng > max_range:
            self.mark_fired(ctx)  # invalidated for the day (p10: range exhaustion)
            return None
        wide = ctx.instrument_value("strategies.morning_0920.wide_range_points", 1e18) or 1e18
        target_pts = ctx.instrument_value("strategies.morning_0920.target_points", 0.0) or 0.0
        bar = ctx.bar
        up, down = bar.high > opening.high, bar.low < opening.low
        if up and down:
            self.mark_fired(ctx)  # whipsaw through both boundaries inside one bar: ambiguous, skip the day
            return None
        if up:
            option_type, level = OptionType.CE, opening.high
            sl = opening.midpoint if rng > wide else opening.low
            target = level + target_pts
        elif down:
            option_type, level = OptionType.PE, opening.low
            sl = opening.midpoint if rng > wide else opening.high
            target = level - target_pts
        else:
            return None
        if not self.bias_ok(ctx, option_type):
            return None
        risk = abs(level - sl)
        if risk <= 0:
            return None

        # pattern quality: how decisively the bar pushed through (close beyond the boundary is best)
        push = (bar.close - level) / max(rng, 1e-9) if option_type == OptionType.CE else (level - bar.close) / max(rng, 1e-9)
        pattern = 0.55 + 0.45 * max(0.0, min(1.0, 0.5 + push))
        time_quality = 1.0 if ctx.index == 1 else max(0.4, 1.0 - 0.2 * (ctx.index - 1))
        conf, comps = self.score(ctx, option_type, pattern=pattern, structural=0.6,
                                 volume=self.volume_quality(ctx), time_quality=time_quality)
        return Signal(
            instrument=ctx.spec.name, strategy=self.name, option_type=option_type, ts=bar.ts,
            spot_level=level, stop_loss=sl, target=target,
            reason=(f"opening range {opening.low:.1f}-{opening.high:.1f} ({rng:.0f}pts) breached at {level:.1f}; "
                    f"SL {'midpoint' if rng > wide else 'opposite extreme'} {sl:.1f}; target +{target_pts:.0f}pts"),
            confidence=conf, components=comps, counter_trend=not self.aligned(ctx.bias, option_type),
            entry_type="stop", fill_same_bar=True,
        )
