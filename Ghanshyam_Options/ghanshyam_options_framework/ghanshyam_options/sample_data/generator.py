"""Deterministic synthetic intraday candle generator for demos and tests.

The generator produces 5-minute bars for an NSE (09:15-15:30) or MCX
(09:00-23:30) session with a mild regime structure (trend days, range days,
opening gaps, an afternoon push) so the Ghanshyam setups actually fire. It is
seeded, so the demo backtest in the README is reproducible.

Usage::

    python -m ghanshyam_options.sample_data.generator --out ghanshyam_options/sample_data

writes ``NSE_NIFTY50-INDEX_5.csv`` and ``MCX_CRUDEOIL26OCTFUT_5.csv``.
"""

from __future__ import annotations

import argparse
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from ..instruments import InstrumentSpec, get_instrument, is_trading_day

PROFILES = {
    "NIFTY50": {"start": 25400.0, "bar_vol": 0.00085, "day_drift": 0.0, "gap_vol": 0.004, "volume": 0},
    "BANKNIFTY": {"start": 56500.0, "bar_vol": 0.0011, "day_drift": 0.0, "gap_vol": 0.005, "volume": 0},
    "CRUDEOIL": {"start": 5500.0, "bar_vol": 0.0016, "day_drift": 0.0, "gap_vol": 0.006, "volume": 2500},
    "NATURALGAS": {"start": 250.0, "bar_vol": 0.0025, "day_drift": 0.0, "gap_vol": 0.008, "volume": 4000},
    "GOLD": {"start": 118000.0, "bar_vol": 0.0006, "day_drift": 0.0, "gap_vol": 0.003, "volume": 800},
    "SILVER": {"start": 145000.0, "bar_vol": 0.0011, "day_drift": 0.0, "gap_vol": 0.005, "volume": 900},
    "COPPER": {"start": 880.0, "bar_vol": 0.0010, "day_drift": 0.0, "gap_vol": 0.004, "volume": 1500},
}


def session_bar_times(spec: InstrumentSpec, day: date, minutes: int, open_t: time, close_t: time) -> List[datetime]:
    cur = datetime.combine(day, open_t)
    end = datetime.combine(day, close_t)
    out = []
    while cur < end:
        out.append(cur)
        cur += timedelta(minutes=minutes)
    return out


def generate(instrument: str, start: date, end: date, resolution: int = 5, seed: int = 7,
             open_t: Optional[time] = None, close_t: Optional[time] = None) -> pd.DataFrame:
    spec = get_instrument(instrument)
    prof = PROFILES[spec.name]
    rng = np.random.default_rng(seed)
    if open_t is None or close_t is None:
        open_t, close_t = (time(9, 15), time(15, 30)) if spec.exchange.value == "NSE" else (time(9, 0), time(23, 30))
    price = prof["start"]
    rows = []
    day = start
    day_idx = 0
    while day <= end:
        if not is_trading_day(day, spec):
            day += timedelta(days=1)
            continue
        times = session_bar_times(spec, day, resolution, open_t, close_t)
        n = len(times)
        regime = rng.choice(["trend_up", "trend_down", "range", "range", "reversal"])
        drift = {"trend_up": 1.0, "trend_down": -1.0, "range": 0.0, "reversal": 0.0}[regime]
        gap = rng.normal(0, prof["gap_vol"]) * price
        price += gap
        # intraday shape: morning impulse, midday chop, afternoon push
        for i, ts in enumerate(times):
            frac = i / max(n - 1, 1)
            if regime == "reversal":
                shape = -1.0 if frac < 0.45 else 1.2
            else:
                shape = drift * (1.4 if frac < 0.25 else (0.2 if frac < 0.7 else 1.0))
            vol_mult = 1.6 if frac < 0.08 else (0.6 if 0.35 < frac < 0.65 else 1.0)
            sigma = prof["bar_vol"] * price * vol_mult
            move = shape * 0.35 * sigma + rng.normal(0, sigma)
            o = price
            c = price + move
            wick_up = abs(rng.normal(0, sigma * 0.6))
            wick_dn = abs(rng.normal(0, sigma * 0.6))
            # occasional exhaustion wicks to create hammers / shooting stars
            if rng.random() < 0.05:
                if shape < 0:
                    wick_dn += sigma * 2.5
                else:
                    wick_up += sigma * 2.5
            h = max(o, c) + wick_up
            l = min(o, c) - wick_dn
            vol = 0.0 if prof["volume"] == 0 else float(max(1, rng.normal(prof["volume"] * vol_mult, prof["volume"] * 0.3)))
            rows.append((ts, round(o, 2), round(h, 2), round(l, 2), round(c, 2), round(vol, 0)))
            price = c
        day += timedelta(days=1)
        day_idx += 1
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    return df


def _safe(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)


def write_sample(out_dir: Path, instrument: str, start: date, end: date, seed: int) -> Path:
    spec = get_instrument(instrument)
    df = generate(instrument, start, end, seed=seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_safe(spec.underlying_symbol)}_5.csv"
    df.to_csv(path, index=False)
    return path


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="generate synthetic sample candles")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--from", dest="start", default="2026-09-01")
    ap.add_argument("--to", dest="end", default="2026-10-17")
    ap.add_argument("--instruments", nargs="*", default=["NIFTY50", "CRUDEOIL"])
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    for i, inst in enumerate(args.instruments):
        p = write_sample(Path(args.out), inst, date.fromisoformat(args.start), date.fromisoformat(args.end), args.seed + i)
        print(f"wrote {p}")


if __name__ == "__main__":
    main()

