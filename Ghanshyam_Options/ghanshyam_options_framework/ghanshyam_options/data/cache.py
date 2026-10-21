"""On-disk candle cache keyed by ``symbol / resolution / date``.

Layout: ``<cache_dir>/<SYMBOL_SAFE>/<resolution>/<YYYY-MM-DD>.csv`` (or
``.parquet`` when configured and pyarrow is importable). One file per trading
day keeps partial-day re-fetches cheap and lets backtests skip the API
entirely for dates already on disk.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from .feed import OHLCV_COLUMNS, normalise_frame

logger = logging.getLogger("ghanshyam.cache")


def _safe(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)


class CandleCache:
    def __init__(self, cache_dir: Path | str, fmt: str = "csv"):
        self.root = Path(cache_dir)
        self.fmt = fmt if fmt in ("csv", "parquet") else "csv"
        if self.fmt == "parquet":
            try:
                import importlib

                importlib.import_module("pyarrow")
            except ImportError:
                logger.warning("pyarrow not installed; falling back to CSV cache")
                self.fmt = "csv"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, resolution: int | str, day: date) -> Path:
        return self.root / _safe(symbol) / str(resolution) / f"{day.isoformat()}.{self.fmt}"

    def has(self, symbol: str, resolution: int | str, day: date) -> bool:
        return self._path(symbol, resolution, day).exists()

    def get(self, symbol: str, resolution: int | str, day: date) -> Optional[pd.DataFrame]:
        p = self._path(symbol, resolution, day)
        if not p.exists():
            return None
        try:
            df = pd.read_parquet(p) if self.fmt == "parquet" else pd.read_csv(p)
        except Exception as exc:  # corrupt file -> treat as miss
            logger.warning("cache read failed for %s: %s", p, exc)
            return None
        return normalise_frame(df)

    def put(self, symbol: str, resolution: int | str, df: pd.DataFrame, *, only_complete_days: bool = True,
            today: Optional[date] = None) -> List[date]:
        """Split ``df`` by date and persist each day. Returns the dates written.

        The current day is skipped when ``only_complete_days`` is True because a
        partial session must not be served as history later.
        """
        if df is None or df.empty:
            return []
        df = normalise_frame(df)
        today = today or date.today()
        written: List[date] = []
        for day, chunk in df.groupby(df["ts"].dt.date):
            if only_complete_days and day >= today:
                continue
            p = self._path(symbol, resolution, day)
            p.parent.mkdir(parents=True, exist_ok=True)
            cols = OHLCV_COLUMNS + (["oi"] if "oi" in chunk.columns else [])
            chunk = chunk[cols]
            if self.fmt == "parquet":
                chunk.to_parquet(p, index=False)
            else:
                chunk.to_csv(p, index=False)
            written.append(day)
        return written

    def get_range(self, symbol: str, resolution: int | str, days: List[date]) -> Dict[date, pd.DataFrame]:
        out: Dict[date, pd.DataFrame] = {}
        for d in days:
            df = self.get(symbol, resolution, d)
            if df is not None and not df.empty:
                out[d] = df
        return out

    def clear(self, symbol: Optional[str] = None) -> int:
        """Delete cached files (all, or for one symbol). Returns the number removed."""
        base = self.root / _safe(symbol) if symbol else self.root
        n = 0
        if not base.exists():
            return 0
        for p in base.rglob(f"*.{self.fmt}"):
            p.unlink()
            n += 1
        return n
