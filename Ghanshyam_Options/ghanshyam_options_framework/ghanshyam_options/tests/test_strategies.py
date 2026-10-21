"""Each Ghanshyam setup against hand-built candle scenarios."""

from datetime import datetime, time, timedelta

import pytest

from ghanshyam_options.models import Bias, OptionType
from ghanshyam_options.strategies.ghanshyam_afternoon_1420 import Afternoon1420Strategy
from ghanshyam_options.strategies.ghanshyam_congestion_base import CongestionBaseStrategy
from ghanshyam_options.strategies.ghanshyam_hammer import HammerStrategy
from ghanshyam_options.strategies.ghanshyam_inside_candle import InsideCandleStrategy
from ghanshyam_options.strategies.ghanshyam_ldchl_levels import LdchlLevelsStrategy
from ghanshyam_options.strategies.ghanshyam_morning_0920 import Morning0920Strategy
from ghanshyam_options.strategies.ghanshyam_shooting_star import ShootingStarStrategy
from ghanshyam_options.strategies.ghanshyam_squeeze_1500 import Squeeze1500Strategy
from ghanshyam_options.strategies.ghanshyam_trendline import TrendlineStrategy
from .conftest import NIFTY_DAY, NIFTY_EXPIRY_DAY, bars_from_closes, candle, levels, make_ctx

T0 = datetime.combine(NIFTY_DAY, time(9, 15))
LV = levels(NIFTY_DAY, pdh=25300, pdl=25100, pdc=25200)


def ts(hh: int, mm: int) -> datetime:
    return datetime.combine(NIFTY_DAY, time(hh, mm))


# --------------------------------------------------------------------------- inside candle
def test_inside_candle_bullish_signal_levels(settings):
    pre = bars_from_closes([25210, 25220, 25230, 25240, 25250, 25260], ts(9, 30), spread=3)
    mother = candle(ts(10, 0), 25260, 25300, 25240, 25290)
    inside = candle(ts(10, 5), 25290, 25295, 25270, 25280)
    cs = pre + [mother, inside]
    ctx = make_ctx(settings, cs, len(cs) - 1, LV, Bias.BULLISH)
    sig = InsideCandleStrategy(settings).on_bar(ctx)
    assert sig is not None and sig.option_type == OptionType.CE
    assert sig.spot_level == inside.high and sig.stop_loss == inside.low
    assert sig.target == pytest.approx(inside.high + 2.0 * inside.range)  # target_range_multiple 2.0
    assert sig.entry_type == "stop" and 0 < sig.confidence <= 1
    assert sig.planned_r == pytest.approx(2.0)


def test_inside_candle_respects_bias_and_rejects_non_inside(settings):
    pre = bars_from_closes([25210] * 6, ts(9, 30), spread=3)
    mother = candle(ts(10, 0), 25260, 25300, 25240, 25290)
    inside = candle(ts(10, 5), 25290, 25295, 25270, 25280)
    ctx = make_ctx(settings, pre + [mother, inside], 7, LV, Bias.BEARISH)
    sig = InsideCandleStrategy(settings).on_bar(ctx)
    assert sig is not None and sig.option_type == OptionType.PE and sig.spot_level == inside.low
    # neutral bias + require_bias_alignment => no trade (equilibrium trap, p3)
    assert InsideCandleStrategy(settings).on_bar(make_ctx(settings, pre + [mother, inside], 7, LV, Bias.NEUTRAL)) is None
    outside = candle(ts(10, 5), 25290, 25310, 25270, 25280)
    assert InsideCandleStrategy(settings).on_bar(make_ctx(settings, pre + [mother, outside], 7, LV, Bias.BULLISH)) is None


def test_inside_candle_mother_trigger_option(settings):
    settings.set("strategies.inside_candle.trigger", "mother")
    pre = bars_from_closes([25210] * 6, ts(9, 30), spread=3)
    mother = candle(ts(10, 0), 25260, 25300, 25240, 25290)
    inside = candle(ts(10, 5), 25290, 25295, 25270, 25280)
    sig = InsideCandleStrategy(settings).on_bar(make_ctx(settings, pre + [mother, inside], 7, LV, Bias.BULLISH))
    assert sig.spot_level == mother.high


# --------------------------------------------------------------------------- hammer / star
def _decline(n: int, start_price: float, step: float, t: datetime):
    return bars_from_closes([start_price - step * i for i in range(n)], t, spread=2)


def test_hammer_after_seven_red_candles_at_pdl(settings):
    decline = _decline(8, 25180, 10, ts(9, 30))          # 7 red bars ending ~25110
    hammer = candle(ts(10, 10), 25110, 25112, 25092, 25111)  # low at PDL 25100 +-tolerance, lower wick 18 vs body 1
    cs = decline + [hammer]
    ctx = make_ctx(settings, cs, len(cs) - 1, LV, Bias.BULLISH)
    sig = HammerStrategy(settings).on_bar(ctx)
    assert sig is not None and sig.option_type == OptionType.CE
    assert sig.spot_level == hammer.high
    assert sig.stop_loss == pytest.approx(hammer.low - 2.0)
    assert sig.target == pytest.approx(hammer.high + 2.0 * (hammer.high - sig.stop_loss))


def test_hammer_rejected_without_exhaustion_or_level(settings):
    short = _decline(3, 25130, 5, ts(9, 50))             # only 2 red bars, 15 pt slide
    hammer = candle(ts(10, 5), 25120, 25122, 25100, 25121)
    assert HammerStrategy(settings).on_bar(make_ctx(settings, short + [hammer], 3, LV, Bias.BULLISH)) is None
    # exhaustion ok but far from any level
    decline = _decline(8, 25280, 5, ts(9, 30))           # ends ~25245, nowhere near PDL/PDC/PDH tolerance 25
    hammer2 = candle(ts(10, 10), 25245, 25247, 25230, 25246)
    assert HammerStrategy(settings).on_bar(make_ctx(settings, decline + [hammer2], 8, LV, Bias.BULLISH)) is None
    settings.set("strategies.hammer.require_structural_level", False)
    assert HammerStrategy(settings).on_bar(make_ctx(settings, decline + [hammer2], 8, LV, Bias.BULLISH)) is not None


def test_shooting_star_after_rally_into_pdh(settings):
    rally = bars_from_closes([25220 + 10 * i for i in range(8)], ts(9, 30), spread=2)  # 7 green bars to 25290
    star = candle(ts(10, 10), 25290, 25312, 25289, 25291)  # upper wick 21 vs body 1, high near PDH 25300
    cs = rally + [star]
    sig = ShootingStarStrategy(settings).on_bar(make_ctx(settings, cs, len(cs) - 1, LV, Bias.BEARISH))
    assert sig is not None and sig.option_type == OptionType.PE
    assert sig.spot_level == star.low and sig.stop_loss == pytest.approx(star.high + 2.0)
    # bullish bias blocks the PE (require_bias_alignment true for shooting_star? it's false by default -> allowed)
    settings.set("strategies.shooting_star.require_bias_alignment", True)
    assert ShootingStarStrategy(settings).on_bar(make_ctx(settings, cs, len(cs) - 1, LV, Bias.BULLISH)) is None


# --------------------------------------------------------------------------- 09:20
def test_morning_0920_breakout_and_sl_rules(settings):
    opening = candle(ts(9, 15), 25200, 25240, 25180, 25230)   # 60 pt range (< wide 60? equal -> narrow)
    breakout = candle(ts(9, 20), 25230, 25260, 25225, 25255)
    ctx = make_ctx(settings, [opening, breakout], 1, LV, Bias.BULLISH)
    sig = Morning0920Strategy(settings).on_bar(ctx)
    assert sig is not None and sig.option_type == OptionType.CE
    assert sig.spot_level == opening.high and sig.stop_loss == opening.low  # narrow: opposite extreme
    assert sig.target == pytest.approx(opening.high + 35)                   # NIFTY target_points
    assert sig.entry_type == "stop" and sig.fill_same_bar


def test_morning_0920_wide_range_uses_midpoint_and_exhaustion_invalidates(settings):
    opening = candle(ts(9, 15), 25200, 25290, 25190, 25280)   # 100 pts > wide 60 -> midpoint SL
    breakdown = candle(ts(9, 20), 25280, 25285, 25170, 25175)
    strat = Morning0920Strategy(settings)
    sig = strat.on_bar(make_ctx(settings, [opening, breakdown], 1, LV, Bias.BEARISH))
    assert sig.option_type == OptionType.PE and sig.stop_loss == pytest.approx(opening.midpoint)
    huge = candle(ts(9, 15), 25200, 25330, 25180, 25300)      # 150 pts > max 110 -> invalid all day
    strat2 = Morning0920Strategy(settings)
    assert strat2.on_bar(make_ctx(settings, [huge, candle(ts(9, 20), 25300, 25350, 25290, 25340)], 1, LV, Bias.BULLISH)) is None
    assert strat2.fired_count(make_ctx(settings, [huge], 0, LV, Bias.BULLISH)) == 1  # marked as used


def test_morning_0920_only_within_window_and_once(settings):
    opening = candle(ts(9, 15), 25200, 25240, 25180, 25230)
    inside = [candle(ts(9, 20 + 5 * i), 25230, 25238, 25190, 25225) for i in range(4)]
    late = candle(ts(9, 40), 25225, 25250, 25220, 25245)
    cs = [opening] + inside + [late]
    strat = Morning0920Strategy(settings)
    for i in range(1, len(cs)):
        assert strat.on_bar(make_ctx(settings, cs, i, LV, Bias.BULLISH)) is None  # bars 1-4 no break; bar 5 beyond window


# --------------------------------------------------------------------------- 14:20
def test_afternoon_1420_breaks_midday_base(settings):
    base = [candle(ts(13, 30) + timedelta(minutes=5 * i), 25200, 25215, 25185, 25200 + (i % 2) * 5) for i in range(9)]
    trigger = candle(ts(14, 15), 25205, 25240, 25200, 25235)   # closes above base high 25215
    cs = base + [trigger]
    sig = Afternoon1420Strategy(settings).on_bar(make_ctx(settings, cs, len(cs) - 1, LV, Bias.BULLISH, atr=20))
    assert sig is not None and sig.option_type == OptionType.CE and sig.entry_type == "market"
    assert sig.stop_loss == 25185 and sig.spot_level == trigger.close
    # wrong bar time -> nothing
    off = candle(ts(14, 10), 25205, 25240, 25200, 25235)
    assert Afternoon1420Strategy(settings).on_bar(make_ctx(settings, base + [off], 9, LV, Bias.BULLISH)) is None


# --------------------------------------------------------------------------- 15:00
def test_squeeze_1500_expiry_only_and_premium_target(settings):
    base = [candle(ts(14, 30) + timedelta(minutes=5 * i), 25200, 25212, 25190, 25205) for i in range(5)]  # 14:30..14:50
    trig = candle(ts(14, 55), 25205, 25230, 25204, 25228)
    cs = base + [trig]
    lv_exp = levels(NIFTY_EXPIRY_DAY, 25300, 25100, 25200, expiry=True)
    sig = Squeeze1500Strategy(settings).on_bar(make_ctx(settings, cs, 5, lv_exp, Bias.BULLISH))
    assert sig is not None and sig.option_type == OptionType.CE
    assert sig.stop_loss == pytest.approx((25212 + 25190) / 2)
    assert sig.target is None and sig.target_premium_pct == pytest.approx(0.20) and sig.max_hold_minutes == 10
    assert Squeeze1500Strategy(settings).on_bar(make_ctx(settings, cs, 5, LV, Bias.BULLISH)) is None  # non-expiry day


# --------------------------------------------------------------------------- LDCHL levels
def test_ldchl_acceptance_above_pdh_and_wick_rejection(settings):
    pre = bars_from_closes([25280, 25290, 25295], ts(9, 30), spread=2)
    acc = bars_from_closes([25310, 25320], ts(9, 45), spread=2)   # two closes above PDH 25300
    cs = pre + acc
    strat = LdchlLevelsStrategy(settings)
    sig = strat.on_bar(make_ctx(settings, cs, len(cs) - 1, LV, Bias.BULLISH))
    assert sig is not None and sig.option_type == OptionType.CE and "acceptance above PDH" in sig.reason
    rej = candle(ts(10, 0), 25290, 25306, 25288, 25289)   # upper wick 16 vs body 1 at PDH
    strat2 = LdchlLevelsStrategy(settings)
    sig2 = strat2.on_bar(make_ctx(settings, pre + [rej], 3, LV, Bias.BULLISH))
    assert sig2 is not None and sig2.option_type == OptionType.PE and sig2.counter_trend


# --------------------------------------------------------------------------- congestion base
def test_congestion_base_breakout_needs_volume_when_available(settings):
    base = [candle(ts(9, 35) + timedelta(minutes=5 * i), 25200, 25210, 25190, 25200 + (i % 3), v=100) for i in range(7)]
    weak = candle(ts(10, 10), 25203, 25225, 25200, 25222, v=90)      # breakout without volume
    strong = candle(ts(10, 10), 25203, 25225, 25200, 25222, v=200)
    lv = levels(NIFTY_DAY, 25400, 25000, 25150)
    strat = CongestionBaseStrategy(settings)
    assert strat.on_bar(make_ctx(settings, base + [weak], 7, lv, Bias.BULLISH, atr=15, avg_volume=100)) is None
    sig = CongestionBaseStrategy(settings).on_bar(make_ctx(settings, base + [strong], 7, lv, Bias.BULLISH, atr=15, avg_volume=100))
    assert sig is not None and sig.option_type == OptionType.CE and sig.stop_loss == 25190 and "with volume" in sig.reason
    # index feed without volume: volume check is skipped
    base0 = [candle(c.ts, c.open, c.high, c.low, c.close, 0.0) for c in base]
    nov = candle(ts(10, 10), 25203, 25225, 25200, 25222, 0.0)
    assert CongestionBaseStrategy(settings).on_bar(make_ctx(settings, base0 + [nov], 7, lv, Bias.BULLISH, atr=15)) is not None


# --------------------------------------------------------------------------- trendline
def test_trendline_break_then_low_volume_retest(settings):
    # descending resistance through swing highs at bars 2, 6, 10 (100, 98, 96); break at bar 14, retest bar 15
    highs = [97, 98, 100, 98, 97, 96, 98, 96, 95, 94, 96, 94, 93, 93, 97.0, 94.8, 95]
    lows = [h - 2 for h in highs]
    cs = []
    for i, (h, l) in enumerate(zip(highs, lows)):
        close = h - 0.5 if i != 14 else h - 0.2
        if i == 15:
            close = 94.6
        cs.append(candle(ts(9, 30) + timedelta(minutes=5 * i), h - 1, h, l, close, v=100 if i != 15 else 50))
    lv = levels(NIFTY_DAY, 120, 80, 90)
    settings.set("strategies.trendline.touch_tolerance_atr", 0.3)
    strat = TrendlineStrategy(settings)
    sig = None
    for i in range(len(cs)):
        s = strat.on_bar(make_ctx(settings, cs, i, lv, Bias.BULLISH, atr=1.0, avg_volume=100))
        if s:
            sig = s
            break
    assert sig is not None and sig.option_type == OptionType.CE
    assert sig.spot_level == 97.0  # break-bar high
    assert sig.stop_loss < cs[15].low + 0.01
