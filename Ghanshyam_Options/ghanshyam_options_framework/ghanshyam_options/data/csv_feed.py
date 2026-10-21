"""Offline feed that replays candles from CSV files (backtests, tests, demos).

Accepted sources:

* a directory containing ``<SYMBOL_SAFE>_<resolution>.csv`` or ``<SYMBOL_SAFE>.csv``
  files, or the cache layout written by :class:`CandleCache`;
* an explicit ``{symbol: path}`` mapping;
* in-memory frames via :meth:`add_frame` (used by the tests).

Quotes are served from the last candle loaded, optionally clamped to a
"replay clock" so the paper runner can be driven deterministically from
history without look-ahead (``set_clock``).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Dict, Mapping, Optional

import pandas as pd

from ..models import Quote
from .feed import DataFeed, normalise_frame, OHLCV_COLUMNS


def _safe(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)


class CsvFeed(DataFeed):
    name = "csv"

    def __init__(self, directory: Optional[Path | str] = None, files: Optional[Mapping[str, Path | str]] = None):
        self.directory = Path(directory) if directory else None
        self.files: Dict[str, Path] = {k: Path(v) for k, v in (files or {}).items()}
        self._frames: Dict[str, pd.DataFrame] = {}
        self._clock: Optional[datetime] = None

    # -- loading --------------------------------------------------------------
    def add_frame(self, symbol: str, df: pd.DataFrame, resolution: int | str = 5) -> None:
        self._frames[self._key(symbol, resolution)] = normalise_frame(df)

    @staticmethod
    def _key(symbol: str, resolution: int | str) -> str:
        return f"{symbol}|{resolution}"

    def _load(self, symbol: str, resolution: int | str) -> pd.DataFrame:
        key = self._key(symbol, resolution)
        if key in self._frames:
            return self._frames[key]
        candidates = []
        if symbol in self.files:
            candidates.append(self.files[symbol])
        if self.directory:
            s = _safe(symbol)
            candidates += [
                self.directory / f"{s}_{resolution}.csv",
                self.directory / f"{s}.csv",
                self.directory / s / str(resolution),  # cache layout
            ]
        for c in candidates:
            if c.is_dir():
                parts = [pd.read_csv(p) for p in sorted(c.glob("*.csv"))]
                if parts:
                    df = normalise_frame(pd.concat(parts, ignore_index=True))
                    self._frames[key] = df
                    return df
            elif c.exists():
                df = normalise_frame(pd.read_csv(c))
                self._frames[key] = df
                return df
        self._frames[key] = pd.DataFrame(columns=OHLCV_COLUMNS)
        return self._frames[key]

    # -- DataFeed -------------------------------------------------------------
    def history(self, symbol: str, resolution: int | str, start: date | datetime, end: date | datetime) -> pd.DataFrame:
        df = self._load(symbol, resolution)
        if df.empty:
            return df
        s = start if isinstance(start, datetime) else datetime.combine(start, time.min)
        e = end if isinstance(end, datetime) else datetime.combine(end, time.max)
        if self._clock is not None:
            e = min(e, self._clock)
        mask = (df["ts"] >= s) & (df["ts"] <= e)
        return df.loc[mask].reset_index(drop=True)

    def quote(self, symbol: str) -> Optional[Quote]:
        for key, df in self._frames.items():
            if key.split("|")[0] != symbol or df.empty:
                continue
            frame = df if self._clock is None else df[df["ts"] <= self._clock]
            if frame.empty:
                return None
            last = frame.iloc[-1]
            return Quote(symbol=symbol, ltp=float(last["close"]), ts=last["ts"].to_pydatetime(),
                         volume=float(last["volume"]))
        # lazy load from disk
        df = self._load(symbol, 5)
        if df.empty:
            return None
        return self.quote(symbol)

    # -- replay clock ---------------------------------------------------------
    def set_clock(self, now: Optional[datetime]) -> None:
        """Clamp history/quotes to ``now`` (bars whose open time is <= now)."""
        self._clock = now

    def symbols(self) -> list[str]:
        return sorted({k.split("|")[0] for k in self._frames})
