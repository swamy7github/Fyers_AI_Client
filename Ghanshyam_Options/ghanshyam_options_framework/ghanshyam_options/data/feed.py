"""Abstract market-data feed and the canonical OHLCV frame.

Canonical frame columns: ``ts`` (naive IST datetime, bar *open* time),
``open, high, low, close, volume`` and optional ``oi``. Rows are sorted and
de-duplicated on ``ts``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd

from ..models import Quote

OHLCV_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]

_COLUMN_ALIASES = {
    "timestamp": "ts", "datetime": "ts", "date": "ts", "time": "ts", "epoch": "ts",
    "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vol": "volume",
    "open_interest": "oi", "openinterest": "oi",
}


def normalise_frame(df: pd.DataFrame, tz: str = "Asia/Kolkata") -> pd.DataFrame:
    """Coerce an arbitrary candle frame into the canonical layout.

    * column names are lower-cased and aliased,
    * ``ts`` may be epoch seconds, ISO strings or datetimes (tz-aware values are
      converted to IST and made naive),
    * bad OHLC rows (NaN, high < low) are dropped, duplicates on ``ts`` are
      collapsed keeping the last, result is sorted.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    out = df.copy()
    out.columns = [_COLUMN_ALIASES.get(str(c).strip().lower(), str(c).strip().lower()) for c in out.columns]
    missing = [c for c in OHLCV_COLUMNS if c not in out.columns and c != "volume"]
    if missing:
        raise ValueError(f"candle frame missing columns {missing}; have {list(out.columns)}")
    if "volume" not in out.columns:
        out["volume"] = 0.0

    ts = out["ts"]
    if pd.api.types.is_numeric_dtype(ts):
        parsed = pd.to_datetime(ts.astype("int64"), unit="s", utc=True).dt.tz_convert(tz).dt.tz_localize(None)
    else:
        parsed = pd.to_datetime(ts, errors="coerce")
        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_convert(tz).dt.tz_localize(None)
    out["ts"] = parsed

    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "oi" in out.columns:
        out["oi"] = pd.to_numeric(out["oi"], errors="coerce")

    out = out.dropna(subset=["ts", "open", "high", "low", "close"])
    out = out[(out["high"] >= out["low"]) & (out["high"] >= out[["open", "close"]].max(axis=1) - 1e-9)
              & (out["low"] <= out[["open", "close"]].min(axis=1) + 1e-9)]
    out["volume"] = out["volume"].fillna(0.0)
    out = out.sort_values("ts").drop_duplicates(subset="ts", keep="last").reset_index(drop=True)
    cols = OHLCV_COLUMNS + (["oi"] if "oi" in out.columns else [])
    return out[cols]


def candles_from_fyers(raw: Sequence[Sequence[float]]) -> pd.DataFrame:
    """Fyers ``history`` returns ``[[epoch, o, h, l, c, v], ...]`` (plus ``oi`` when requested)."""
    if not raw:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    width = len(raw[0])
    cols = ["ts", "open", "high", "low", "close", "volume"] + (["oi"] if width >= 7 else [])
    df = pd.DataFrame([list(r)[:len(cols)] for r in raw], columns=cols)
    return normalise_frame(df)


class DataFeed(ABC):
    """Minimal feed interface used by the signal engine, backtester and paper runner."""

    name: str = "abstract"

    @abstractmethod
    def history(self, symbol: str, resolution: int | str, start: date | datetime, end: date | datetime) -> pd.DataFrame:
        """Return canonical candles for ``symbol`` between ``start`` and ``end`` (inclusive dates)."""

    @abstractmethod
    def quote(self, symbol: str) -> Optional[Quote]:
        """Latest quote or ``None`` if unavailable."""

    def quotes(self, symbols: Iterable[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for s in symbols:
            q = self.quote(s)
            if q is not None:
                out[s] = q
        return out

    def ltp(self, symbol: str) -> Optional[float]:
        q = self.quote(symbol)
        return q.ltp if q else None

    def daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Daily bars; default implementation aggregates the intraday history."""
        intra = self.history(symbol, "5", start, end)
        if intra.empty:
            return intra
        day = intra["ts"].dt.normalize()
        g = intra.groupby(day)
        out = pd.DataFrame({
            "ts": g["ts"].first().dt.normalize(),
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }).reset_index(drop=True)
        return out

    def available_dates(self, symbol: str, resolution: int | str, start: date, end: date) -> List[date]:
        df = self.history(symbol, resolution, start, end)
        if df.empty:
            return []
        return sorted(set(df["ts"].dt.date))

    def close(self) -> None:  # pragma: no cover - trivial
        return None
