"""Price-action helpers used by the Ghanshyam setups.

The method is deliberately indicator-light (guide p1). What lives here:

* candle geometry (body / wick ratios, inside-bar, hammer, shooting star),
* consecutive-colour runs and uncorrected runs in points,
* ATR (for tolerances and trailing), VWAP, session EMA (bias context only),
* swing pivots and 3-touch trendlines,
* congestion-base detection.

Every vectorised function is causal: the value at index ``t`` depends only on
rows ``<= t``. Pivot detection by construction needs ``strength`` bars to the
right, so a pivot is only *confirmed* ``strength`` bars after it forms — the
helpers expose the confirmation index and strategies must only use pivots
whose confirmation index is <= the current bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .models import Candle

OHLC_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# DataFrame-level, vectorised, causal
# ---------------------------------------------------------------------------
def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.fillna(df["high"] - df["low"])


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR (RMA). First value is the simple mean of the first ``period`` TRs."""
    tr = true_range(df)
    out = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()
    return out


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=1).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP reset at each calendar date (the ``ts`` column must be datetime)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].fillna(0.0)
    day = pd.to_datetime(df["ts"]).dt.date
    pv = (typical * vol).groupby(day).cumsum()
    vv = vol.groupby(day).cumsum()
    vwap = pv / vv.replace(0.0, np.nan)
    return vwap.fillna(typical)


def rolling_volume_mean(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return df["volume"].rolling(period, min_periods=1).mean()


def inside_bar_mask(df: pd.DataFrame) -> pd.Series:
    """True at ``t`` when bar ``t`` is fully inside bar ``t-1``."""
    return (df["high"] <= df["high"].shift(1)) & (df["low"] >= df["low"].shift(1))


def consecutive_run(df: pd.DataFrame) -> pd.Series:
    """Signed run length of same-colour candles ending at ``t`` (+n green, -n red)."""
    colour = np.sign(df["close"].values - df["open"].values)
    out = np.zeros(len(df), dtype=int)
    for i in range(len(df)):
        if colour[i] == 0:
            out[i] = 0
        elif i > 0 and colour[i] == colour[i - 1]:
            out[i] = out[i - 1] + int(colour[i])
        else:
            out[i] = int(colour[i])
    return pd.Series(out, index=df.index)


def candle_frame(candles: Sequence[Candle]) -> pd.DataFrame:
    rows = [(c.ts, c.open, c.high, c.low, c.close, c.volume) for c in candles]
    df = pd.DataFrame(rows, columns=OHLC_COLUMNS)
    return df


# ---------------------------------------------------------------------------
# Single-candle morphology (guide p4-p5)
# ---------------------------------------------------------------------------
def is_hammer(c: Candle, wick_body_ratio: float = 2.0, max_upper_wick_ratio: float = 0.30) -> bool:
    """Lower shadow >= ``wick_body_ratio`` x body, small upper shadow; colour secondary (p4)."""
    if c.range <= 0:
        return False
    body = max(c.body, c.range * 0.05)  # avoid division blow-ups on doji-like bars
    return c.lower_wick >= wick_body_ratio * body and c.upper_wick <= max_upper_wick_ratio * c.range


def is_shooting_star(c: Candle, wick_body_ratio: float = 2.0, max_lower_wick_ratio: float = 0.30) -> bool:
    """Upper shadow >= ``wick_body_ratio`` x body with minimal/no lower shadow (p4-p5)."""
    if c.range <= 0:
        return False
    body = max(c.body, c.range * 0.05)
    return c.upper_wick >= wick_body_ratio * body and c.lower_wick <= max_lower_wick_ratio * c.range


def is_inside(inside: Candle, mother: Candle) -> bool:
    return inside.high <= mother.high and inside.low >= mother.low and mother.range > 0


def wick_rejection_up(c: Candle, ratio: float = 2.0) -> bool:
    """Upper-wick rejection (used for PDH rejection PE trades, p2)."""
    return is_shooting_star(c, wick_body_ratio=ratio, max_lower_wick_ratio=0.5)


def wick_rejection_down(c: Candle, ratio: float = 2.0) -> bool:
    return is_hammer(c, wick_body_ratio=ratio, max_upper_wick_ratio=0.5)


# ---------------------------------------------------------------------------
# Runs (guide p4: 7-8 consecutive candles OR 150-200 pt uncorrected move)
# ---------------------------------------------------------------------------
def consecutive_colour_count(candles: Sequence[Candle], red: bool) -> int:
    """Number of consecutive red (or green) candles ending at the last element."""
    n = 0
    for c in reversed(candles):
        if (c.is_red if red else c.is_green):
            n += 1
        else:
            break
    return n


def uncorrected_decline(candles: Sequence[Candle], lookback: int) -> float:
    """Points from the highest high in the lookback to the last candle's low."""
    window = list(candles)[-lookback:]
    if not window:
        return 0.0
    hi = max(c.high for c in window)
    return hi - window[-1].low


def uncorrected_rally(candles: Sequence[Candle], lookback: int) -> float:
    window = list(candles)[-lookback:]
    if not window:
        return 0.0
    lo = min(c.low for c in window)
    return window[-1].high - lo


def near_level(price: float, levels: Sequence[float], tolerance: float) -> Optional[float]:
    """Return the nearest level within ``tolerance`` points of ``price`` or None."""
    best: Optional[float] = None
    best_dist = tolerance
    for lvl in levels:
        d = abs(price - lvl)
        if d <= best_dist:
            best, best_dist = lvl, d
    return best


# ---------------------------------------------------------------------------
# Swing pivots and trendlines (guide p5: >= 3 touch points, 5m/15m)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Pivot:
    index: int          # bar index of the pivot
    price: float
    kind: str           # "H" or "L"
    confirmed_at: int   # bar index at which the pivot becomes known (index + strength)


def swing_pivots(highs: Sequence[float], lows: Sequence[float], strength: int = 2) -> List[Pivot]:
    """Fractal pivots: a high is a pivot if it is the max of the +/- strength window."""
    n = len(highs)
    out: List[Pivot] = []
    for i in range(strength, n - strength):
        hi_win = highs[i - strength:i + strength + 1]
        lo_win = lows[i - strength:i + strength + 1]
        if highs[i] == max(hi_win) and list(hi_win).count(highs[i]) == 1:
            out.append(Pivot(i, float(highs[i]), "H", i + strength))
        if lows[i] == min(lo_win) and list(lo_win).count(lows[i]) == 1:
            out.append(Pivot(i, float(lows[i]), "L", i + strength))
    out.sort(key=lambda p: p.index)
    return out


@dataclass(frozen=True)
class Trendline:
    kind: str           # "resistance" (through highs) or "support" (through lows)
    slope: float        # price per bar
    intercept: float    # price at bar index 0
    touches: int
    first_index: int
    last_index: int
    confirmed_at: int

    def value_at(self, index: int) -> float:
        return self.intercept + self.slope * index


def fit_trendlines(
    pivots: Sequence[Pivot],
    highs: Sequence[float],
    lows: Sequence[float],
    tolerance: float,
    min_touches: int = 3,
    upto_index: Optional[int] = None,
) -> List[Trendline]:
    """Build lines through pairs of same-kind pivots; keep those with >= ``min_touches`` touches.

    Only pivots confirmed at or before ``upto_index`` are used, so the result is
    causal when called bar by bar. A line is valid only if no bar between the
    first and last pivot closes *through* it by more than ``tolerance``.
    """
    lines: List[Trendline] = []
    for kind in ("H", "L"):
        pts = [p for p in pivots if p.kind == kind and (upto_index is None or p.confirmed_at <= upto_index)]
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                a, b = pts[i], pts[j]
                if b.index == a.index:
                    continue
                slope = (b.price - a.price) / (b.index - a.index)
                intercept = a.price - slope * a.index
                touches = 0
                for p in pts:
                    if abs((intercept + slope * p.index) - p.price) <= tolerance:
                        touches += 1
                if touches < min_touches:
                    continue
                # validity: no violation between the first and last pivot
                violated = False
                series = highs if kind == "H" else lows
                for k in range(a.index, b.index + 1):
                    lv = intercept + slope * k
                    if kind == "H" and series[k] > lv + tolerance:
                        violated = True
                        break
                    if kind == "L" and series[k] < lv - tolerance:
                        violated = True
                        break
                if violated:
                    continue
                touched = [p for p in pts if abs((intercept + slope * p.index) - p.price) <= tolerance]
                lines.append(Trendline(
                    kind="resistance" if kind == "H" else "support",
                    slope=slope, intercept=intercept, touches=touches,
                    first_index=min(p.index for p in touched), last_index=max(p.index for p in touched),
                    confirmed_at=max(p.confirmed_at for p in touched),
                ))
    # de-duplicate near-identical lines, keep the most-touched
    lines.sort(key=lambda l: (-l.touches, l.first_index))
    unique: List[Trendline] = []
    for ln in lines:
        if any(ln.kind == u.kind and abs(ln.slope - u.slope) < 1e-9 and abs(ln.intercept - u.intercept) <= tolerance for u in unique):
            continue
        unique.append(ln)
    return unique


# ---------------------------------------------------------------------------
# Congestion base (guide p3: 20-45 min horizontal consolidation)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Base:
    start_index: int
    end_index: int
    high: float
    low: float
    avg_volume: float

    @property
    def height(self) -> float:
        return self.high - self.low

    @property
    def bars(self) -> int:
        return self.end_index - self.start_index + 1


def detect_base(
    candles: Sequence[Candle],
    end_index: int,
    min_bars: int,
    max_bars: int,
    max_height: float,
) -> Optional[Base]:
    """Longest window ending at ``end_index`` with ``min_bars <= len <= max_bars`` and height <= ``max_height``."""
    best: Optional[Base] = None
    for length in range(min_bars, max_bars + 1):
        start = end_index - length + 1
        if start < 0:
            break
        window = candles[start:end_index + 1]
        hi = max(c.high for c in window)
        lo = min(c.low for c in window)
        if hi - lo <= max_height:
            best = Base(start, end_index, hi, lo, float(np.mean([c.volume for c in window])))
        else:
            break  # longer windows can only get taller
    return best


def frame_to_candles(df: pd.DataFrame) -> List[Candle]:
    out: List[Candle] = []
    has_oi = "oi" in df.columns
    for row in df.itertuples(index=False):
        ts = row.ts.to_pydatetime() if hasattr(row.ts, "to_pydatetime") else row.ts
        out.append(Candle(
            ts=ts, open=float(row.open), high=float(row.high), low=float(row.low), close=float(row.close),
            volume=float(getattr(row, "volume", 0.0) or 0.0),
            oi=float(row.oi) if has_oi and row.oi == row.oi else None,
        ))
    return out


def resample_candles(candles: Sequence[Candle], minutes: int, session_open_minute: int = 0) -> List[Candle]:
    """Aggregate ``candles`` (assumed uniform, e.g. 5m) into ``minutes`` bars anchored at the session open.

    A bucket is emitted only when it is complete (its last constituent bar has
    arrived), so this is safe to call incrementally.
    """
    if not candles:
        return []
    base_minutes = minutes
    out: List[Candle] = []
    bucket: List[Candle] = []
    bucket_key: Optional[Tuple[object, int]] = None
    for c in candles:
        mins = c.ts.hour * 60 + c.ts.minute - session_open_minute
        key = (c.ts.date(), mins // base_minutes)
        if bucket_key is not None and key != bucket_key:
            out.append(_merge(bucket))
            bucket = []
        bucket.append(c)
        bucket_key = key
    # last bucket: emit only if complete (its span covers the full bucket)
    if bucket:
        span = (bucket[-1].ts - bucket[0].ts).total_seconds() / 60.0
        step = (candles[1].ts - candles[0].ts).total_seconds() / 60.0 if len(candles) > 1 else base_minutes
        if span + step >= base_minutes:
            out.append(_merge(bucket))
    return out


def _merge(bucket: Sequence[Candle]) -> Candle:
    return Candle(
        ts=bucket[0].ts,
        open=bucket[0].open,
        high=max(c.high for c in bucket),
        low=min(c.low for c in bucket),
        close=bucket[-1].close,
        volume=float(sum(c.volume for c in bucket)),
        oi=bucket[-1].oi,
    )
