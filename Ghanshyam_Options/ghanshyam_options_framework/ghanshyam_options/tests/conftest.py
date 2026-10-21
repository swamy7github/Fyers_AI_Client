"""Shared fixtures: settings, synthetic candles, hand-built scenarios. No network."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Sequence

import pandas as pd
import pytest

from ghanshyam_options.config.config import Settings, load_settings
from ghanshyam_options.instruments import get_instrument
from ghanshyam_options.models import Bias, Candle, DayLevels
from ghanshyam_options.signals import SignalEngine
from ghanshyam_options.strategies.base import BarContext, StrategyRegistry


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = load_settings(overrides={
        "paths.log_dir": str(tmp_path / "logs"),
        "paths.cache_dir": str(tmp_path / "cache"),
        "paths.report_dir": str(tmp_path / "reports"),
        "paths.sample_data_dir": str(tmp_path / "sample"),
    })
    return s


def bars_from_closes(closes: Sequence[float], start: datetime, minutes: int = 5, spread: float = 0.0,
                     volume: float = 0.0) -> List[Candle]:
    """Flat-ish candles whose open is the previous close; spread adds symmetric wicks."""
    out: List[Candle] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev if i else c
        hi = max(o, c) + spread
        lo = min(o, c) - spread
        out.append(Candle(ts=start + timedelta(minutes=minutes * i), open=o, high=hi, low=lo, close=c, volume=volume))
        prev = c
    return out


def candle(ts: datetime, o: float, h: float, l: float, c: float, v: float = 0.0) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v)


def levels(trade_date: date, pdh: float, pdl: float, pdc: float, expiry: bool = False) -> DayLevels:
    return DayLevels(trade_date=trade_date, pdh=pdh, pdl=pdl, pdc=pdc, is_expiry_day=expiry)


def make_ctx(settings: Settings, candles: Sequence[Candle], index: int, lv: DayLevels, bias: Bias,
             instrument: str = "NIFTY50", atr: float = 20.0, avg_volume: float = 0.0) -> BarContext:
    spec = get_instrument(instrument)
    return BarContext(spec=spec, settings=settings, session=settings.session(spec.exchange.value), candles=list(candles),
                      index=index, levels=lv, bias=bias, atr=atr, avg_volume=avg_volume)


def run_engine(settings: Settings, registry: StrategyRegistry, candles: Sequence[Candle], lv: DayLevels,
               instrument: str = "NIFTY50"):
    """Drive a SignalEngine through all candles; returns (engine, accepted signals)."""
    spec = get_instrument(instrument)
    eng = SignalEngine(settings, registry)
    eng.start_day(spec, lv.trade_date, lv)
    out = []
    for c in candles:
        out.extend(eng.on_bar(spec, c))
    return eng, out


def session_times(day: date, open_t: time = time(9, 15), close_t: time = time(15, 30), minutes: int = 5) -> List[datetime]:
    cur = datetime.combine(day, open_t)
    end = datetime.combine(day, close_t)
    out = []
    while cur < end:
        out.append(cur)
        cur += timedelta(minutes=minutes)
    return out


def flat_day(day: date, price: float, open_t: time = time(9, 15), close_t: time = time(15, 30),
             wiggle: float = 2.0, volume: float = 0.0) -> List[Candle]:
    """A full session of tiny alternating bars around ``price`` (no setups fire)."""
    out = []
    for i, ts in enumerate(session_times(day, open_t, close_t)):
        d = wiggle if i % 2 == 0 else -wiggle
        o = price
        c = price + d * 0.5
        out.append(Candle(ts=ts, open=o, high=max(o, c) + wiggle, low=min(o, c) - wiggle, close=c, volume=volume))
    return out


def frame(candles: Sequence[Candle]) -> pd.DataFrame:
    return pd.DataFrame([(c.ts, c.open, c.high, c.low, c.close, c.volume) for c in candles],
                        columns=["ts", "open", "high", "low", "close", "volume"])


NIFTY_DAY = date(2026, 10, 15)   # Thursday, not an expiry (NIFTY weekly = Tuesday)
NIFTY_EXPIRY_DAY = date(2026, 10, 13)  # Tuesday
