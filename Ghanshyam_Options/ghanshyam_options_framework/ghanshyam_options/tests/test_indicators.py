from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ghanshyam_options import indicators as ind
from ghanshyam_options.models import Candle
from .conftest import bars_from_closes, candle


def _df(n: int = 60, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 25000 + np.cumsum(rng.normal(0, 10, n))
    ts = [datetime(2026, 10, 15, 9, 15) + timedelta(minutes=5 * i) for i in range(n)]
    o = np.r_[closes[0], closes[:-1]]
    h = np.maximum(o, closes) + rng.uniform(0, 5, n)
    l = np.minimum(o, closes) - rng.uniform(0, 5, n)
    v = rng.uniform(100, 200, n)
    return pd.DataFrame({"ts": ts, "open": o, "high": h, "low": l, "close": closes, "volume": v})


def test_indicators_have_no_lookahead():
    """Every indicator value at t must be identical whether or not future bars exist."""
    full = _df(80)
    for fn in (lambda d: ind.atr(d, 14), lambda d: ind.ema(d["close"], 9), ind.session_vwap,
               lambda d: ind.rolling_volume_mean(d, 20), ind.inside_bar_mask, ind.consecutive_run):
        full_vals = fn(full).to_numpy()
        for cut in (15, 30, 55):
            part_vals = fn(full.iloc[:cut].reset_index(drop=True)).to_numpy()
            np.testing.assert_allclose(part_vals, full_vals[:cut], rtol=1e-10, atol=1e-10)


def test_atr_matches_wilder_reference():
    df = _df(40, seed=3)
    tr = ind.true_range(df).to_numpy()
    ref = np.zeros(len(tr))
    ref[0] = tr[0]
    for i in range(1, len(tr)):
        ref[i] = ref[i - 1] + (tr[i] - ref[i - 1]) / 14.0
    np.testing.assert_allclose(ind.atr(df, 14).to_numpy(), ref, rtol=1e-12)


def test_candle_morphology():
    ts = datetime(2026, 10, 15, 10, 0)
    hammer = candle(ts, 100, 101, 90, 100.5)        # body 0.5, lower wick 9.5 (>= 2x), upper wick 0.5
    star = candle(ts, 100, 112, 99.5, 99.8)         # upper wick 12, body 0.2, lower 0.3
    plain = candle(ts, 100, 103, 97, 102)
    assert ind.is_hammer(hammer) and not ind.is_hammer(star) and not ind.is_hammer(plain)
    assert ind.is_shooting_star(star) and not ind.is_shooting_star(hammer)
    # red hammer still counts (colour secondary, guide p4)
    red_hammer = candle(ts, 100.5, 101, 90, 100)
    assert ind.is_hammer(red_hammer)
    mother = candle(ts, 100, 110, 90, 105)
    inside = candle(ts, 104, 108, 95, 100)
    outside = candle(ts, 104, 111, 95, 100)
    assert ind.is_inside(inside, mother) and not ind.is_inside(outside, mother)


def test_runs_and_levels():
    start = datetime(2026, 10, 15, 9, 15)
    reds = bars_from_closes([110, 108, 106, 104, 102, 100, 98, 96], start)
    assert ind.consecutive_colour_count(reds, red=True) == 7
    assert ind.consecutive_colour_count(reds, red=False) == 0
    assert ind.uncorrected_decline(reds, 8) == 110 - 96
    greens = bars_from_closes([96, 98, 100, 102], start)
    assert ind.consecutive_colour_count(greens, red=False) == 3
    assert ind.uncorrected_rally(greens, 4) == 102 - 96
    assert ind.near_level(25001.0, [24900.0, 25010.0, 25100.0], tolerance=20) == 25010.0
    assert ind.near_level(25050.0, [24900.0, 25010.0], tolerance=20) is None


def test_pivots_confirm_only_after_strength_bars():
    highs = [1, 2, 3, 5, 3, 2, 1, 2, 4, 2, 1]
    lows = [0, 1, 2, 4, 2, 1, 0, 1, 3, 1, 0]
    piv = ind.swing_pivots(highs, lows, strength=2)
    hs = [p for p in piv if p.kind == "H"]
    assert [p.index for p in hs] == [3, 8]
    assert hs[0].confirmed_at == 5  # index + strength, so bar 3 is unknown until bar 5


def test_trendline_requires_three_touches_and_is_causal():
    # three descending swing highs on a straight line: 100 @2, 98 @6, 96 @10; everything else below the line
    highs = [95, 96, 100, 96, 95, 94, 98, 94, 93, 92, 96, 92, 91, 90, 91]
    lows = [h - 3 for h in highs]
    piv = ind.swing_pivots(highs, lows, strength=2)
    assert [p.index for p in piv if p.kind == "H"] == [2, 6, 10]
    lines = ind.fit_trendlines(piv, highs, lows, tolerance=0.3, min_touches=3, upto_index=14)
    res = [l for l in lines if l.kind == "resistance"]
    assert res and res[0].touches == 3 and abs(res[0].slope + 0.5) < 1e-9
    assert res[0].value_at(14) == pytest.approx(94.0)
    # before the third pivot is confirmed (index 10 + 2 = 12) no 3-touch line can exist
    assert not [l for l in ind.fit_trendlines(piv, highs, lows, 0.3, 3, upto_index=11) if l.kind == "resistance"]


def test_detect_base_and_resample():
    start = datetime(2026, 10, 15, 9, 15)
    closes = [100, 101, 100.5, 101.2, 100.8, 101.1, 100.9, 101.3]
    cs = bars_from_closes(closes, start, spread=0.2)
    base = ind.detect_base(cs, end_index=7, min_bars=4, max_bars=8, max_height=3.0)
    assert base is not None and base.bars == 8 and base.high >= 101.3 and base.low <= 100
    assert ind.detect_base(cs, 7, 4, 8, max_height=0.5) is None
    # 5m -> 15m: 8 bars -> 2 complete buckets (bars 0-2, 3-5); bars 6-7 incomplete
    c15 = ind.resample_candles(cs, 15, session_open_minute=9 * 60 + 15)
    assert len(c15) == 2
    assert c15[0].open == cs[0].open and c15[0].close == cs[2].close
    assert c15[0].high == max(c.high for c in cs[:3])


def test_frame_round_trip():
    start = datetime(2026, 10, 15, 9, 15)
    cs = bars_from_closes([1, 2, 3], start, volume=5)
    df = ind.candle_frame(cs)
    back = ind.frame_to_candles(df)
    assert back == cs
    assert isinstance(back[0], Candle)
