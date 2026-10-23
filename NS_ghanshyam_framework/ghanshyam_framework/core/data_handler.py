"""Market data access: history, 5-minute candle aggregation and a live feed.

The existing Fyers wrapper (``core/fyers_client.py``) is NOT imported here. Its
interface is unknown to this module, so the data layer talks to it through two
small callables you supply (see :class:`FyersDataSource`). ``from_fyers_model``
covers the raw Fyers SDK shape, and :class:`CsvDataSource` allows offline runs.

All timestamps are naive datetimes in Indian Standard Time and mark the START
of a candle (a 09:15 candle covers 09:15:00-09:19:59).
"""
from __future__ import annotations

import functools
import logging
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

import pandas as pd

from config.settings import CANDLE_MINUTES, TIMEZONE

LOGGER = logging.getLogger(__name__)
OHLCV: List[str] = ["open", "high", "low", "close", "volume"]


class DataError(Exception):
    """Raised when market data cannot be fetched or is malformed."""


class RateLimitError(DataError):
    """Raised when the broker API signals rate limiting (HTTP 429 or similar)."""


# --------------------------------------------------------------------------- #
# Retry helper
# --------------------------------------------------------------------------- #
def with_retries(max_attempts: int = 4, base_delay: float = 1.0,
                 sleep_fn: Callable[[float], None] = _time.sleep):
    """Retry a call on :class:`DataError` / network errors with exponential backoff.

    Rate-limit errors back off four times longer than other errors.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except (DataError, ConnectionError, TimeoutError, OSError) as exc:
                    if attempt == max_attempts:
                        raise
                    wait = delay * (4 if isinstance(exc, RateLimitError) else 1)
                    LOGGER.warning("%s failed (%s); retry %d/%d in %.1fs",
                                   func.__name__, exc, attempt, max_attempts - 1, wait)
                    sleep_fn(wait)
                    delay *= 2
        return wrapper
    return decorator


# --------------------------------------------------------------------------- #
# Candle normalisation and helpers
# --------------------------------------------------------------------------- #
def _empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=OHLCV, dtype=float)
    frame.index = pd.DatetimeIndex([], name="timestamp")
    return frame


def normalize_candles(raw: Any) -> pd.DataFrame:
    """Convert broker output into a clean OHLCV frame indexed by IST start time.

    Accepts a DataFrame, a Fyers response dict (``{"s": "ok", "candles": [...]}``)
    or a list of ``[epoch, o, h, l, c, v]`` rows. Duplicates are dropped, rows are
    sorted and malformed rows (NaN prices, high < low) are discarded.
    """
    if isinstance(raw, dict):
        status = str(raw.get("s", "ok")).lower()
        if status != "ok" and status != "no_data":
            code = raw.get("code")
            message = str(raw.get("message", raw))
            if code == 429 or "limit" in message.lower():
                raise RateLimitError(message)
            raise DataError(message)
        raw = raw.get("candles", [])

    if isinstance(raw, pd.DataFrame):
        frame = raw.copy()
        if "timestamp" in frame.columns:
            frame = frame.set_index("timestamp")
    else:
        rows = list(raw or [])
        if not rows:
            return _empty_frame()
        frame = pd.DataFrame(rows, columns=["timestamp"] + OHLCV[: len(rows[0]) - 1])
        frame = frame.set_index("timestamp")

    if frame.empty:
        return _empty_frame()
    frame.columns = [str(c).lower() for c in frame.columns]
    missing = [c for c in ("open", "high", "low", "close") if c not in frame.columns]
    if missing:
        raise DataError(f"Candle data missing columns: {missing}")
    if "volume" not in frame.columns:
        frame["volume"] = 0.0

    index = frame.index
    if pd.api.types.is_numeric_dtype(index):
        index = pd.to_datetime(index, unit="s", utc=True).tz_convert(TIMEZONE).tz_localize(None)
    else:
        index = pd.to_datetime(index)
        if index.tz is not None:
            index = index.tz_convert(TIMEZONE).tz_localize(None)
    frame.index = pd.DatetimeIndex(index, name="timestamp")

    frame = frame[OHLCV].astype(float)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    bad = frame[["open", "high", "low", "close"]].isna().any(axis=1) | (frame["high"] < frame["low"])
    if bad.any():
        LOGGER.warning("Dropping %d malformed candle(s)", int(bad.sum()))
        frame = frame[~bad]
    return frame


def resample_candles(frame: pd.DataFrame, minutes: int = CANDLE_MINUTES) -> pd.DataFrame:
    """Aggregate finer candles (e.g. 1-minute) into ``minutes``-minute candles."""
    if frame.empty:
        return frame
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = frame.resample(f"{minutes}min", label="left", closed="left").agg(agg)
    return out.dropna(subset=["open"])


def find_missing_bars(frame: pd.DataFrame, session_start: datetime, session_end: datetime,
                      minutes: int = CANDLE_MINUTES) -> List[datetime]:
    """Return expected candle start times between the given bounds that are absent."""
    expected = pd.date_range(session_start, session_end - timedelta(minutes=minutes),
                             freq=f"{minutes}min")
    return [ts.to_pydatetime() for ts in expected.difference(frame.index)]


class CandleAggregator:
    """Build 5-minute candles from a tick stream.

    ``add_tick`` returns the just-completed candle (as a dict) when a tick opens a
    new bucket, otherwise ``None``. Ticks with timestamps older than the current
    bucket are ignored.
    """

    def __init__(self, minutes: int = CANDLE_MINUTES) -> None:
        self.minutes = minutes
        self._bucket: Optional[datetime] = None
        self._candle: Optional[Dict[str, float]] = None

    def _bucket_start(self, ts: datetime) -> datetime:
        minute = ts.minute - ts.minute % self.minutes
        return ts.replace(minute=minute, second=0, microsecond=0)

    def add_tick(self, ts: datetime, price: float, qty: float = 0.0) -> Optional[Dict[str, Any]]:
        bucket = self._bucket_start(ts)
        if self._bucket is not None and bucket < self._bucket:
            return None
        finished: Optional[Dict[str, Any]] = None
        if self._bucket is None or bucket > self._bucket:
            if self._candle is not None:
                finished = {"timestamp": self._bucket, **self._candle}
            self._bucket = bucket
            self._candle = {"open": price, "high": price, "low": price, "close": price,
                            "volume": qty}
            return finished
        assert self._candle is not None
        self._candle["high"] = max(self._candle["high"], price)
        self._candle["low"] = min(self._candle["low"], price)
        self._candle["close"] = price
        self._candle["volume"] += qty
        return None

    def flush(self) -> Optional[Dict[str, Any]]:
        """Return the in-progress candle (used at session close)."""
        if self._bucket is None or self._candle is None:
            return None
        return {"timestamp": self._bucket, **self._candle}


# --------------------------------------------------------------------------- #
# Data sources
# --------------------------------------------------------------------------- #
class HistoricalDataSource(Protocol):
    """Anything that can return normalised candles for a symbol and date range."""

    def fetch_history(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...


HistoryFn = Callable[[str, str, date, date], Any]
QuoteFn = Callable[[str], float]


class FyersDataSource:
    """Adapter that sits on top of the already-implemented Fyers hooks.

    Args:
        history_fn: ``(symbol, resolution, start_date, end_date) -> raw`` where raw
            is anything :func:`normalize_candles` understands.
        quote_fn: ``(symbol) -> last traded price``.
        chunk_days: Fyers limits intraday history per request (about 100 days).
        resolution: candle resolution in minutes, as a string ("5").
    """

    def __init__(self, history_fn: HistoryFn, quote_fn: Optional[QuoteFn] = None,
                 chunk_days: int = 60, resolution: str = "5",
                 sleep_fn: Callable[[float], None] = _time.sleep) -> None:
        self._history_fn = history_fn
        self._quote_fn = quote_fn
        self.chunk_days = chunk_days
        self.resolution = resolution
        self._fetch_chunk = with_retries(sleep_fn=sleep_fn)(self._fetch_chunk_once)

    @classmethod
    def from_fyers_model(cls, fyers: Any, **kwargs: Any) -> "FyersDataSource":
        """Wrap a raw ``fyersModel.FyersModel`` object (v3 SDK)."""

        def history(symbol: str, resolution: str, start: date, end: date) -> Any:
            return fyers.history(data={
                "symbol": symbol, "resolution": resolution, "date_format": "1",
                "range_from": start.isoformat(), "range_to": end.isoformat(), "cont_flag": "1",
            })

        def quote(symbol: str) -> float:
            response = fyers.quotes(data={"symbols": symbol})
            if response.get("s") != "ok" or not response.get("d"):
                raise DataError(f"Quote failed for {symbol}: {response}")
            return float(response["d"][0]["v"]["lp"])

        return cls(history, quote, **kwargs)

    def _fetch_chunk_once(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        try:
            raw = self._history_fn(symbol, self.resolution, start, end)
        except (DataError, ConnectionError, TimeoutError, OSError):
            raise
        except Exception as exc:  # broker SDKs raise assorted exceptions
            raise DataError(f"History call failed for {symbol}: {exc}") from exc
        return normalize_candles(raw)

    def fetch_history(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Fetch candles for ``start``..``end`` inclusive, chunking long ranges."""
        if end < start:
            raise ValueError("end date is before start date")
        frames: List[pd.DataFrame] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=self.chunk_days - 1))
            frame = self._fetch_chunk(symbol, cursor, chunk_end)
            if frame.empty:
                LOGGER.debug("No candles for %s %s..%s", symbol, cursor, chunk_end)
            frames.append(frame)
            cursor = chunk_end + timedelta(days=1)
        frames = [f for f in frames if not f.empty]
        if not frames:
            return _empty_frame()
        merged = pd.concat(frames)
        return merged[~merged.index.duplicated(keep="last")].sort_index()

    def ltp(self, symbol: str) -> float:
        """Last traded price for ``symbol`` (with retries)."""
        if self._quote_fn is None:
            raise DataError("No quote function configured")
        quote_fn = self._quote_fn

        @with_retries()
        def _call() -> float:
            try:
                return float(quote_fn(symbol))
            except (DataError, ConnectionError, TimeoutError, OSError):
                raise
            except Exception as exc:
                raise DataError(f"Quote failed for {symbol}: {exc}") from exc

        return _call()


class CsvDataSource:
    """Offline source reading 5-minute candles from a CSV (for tests and dry runs).

    Expected columns: ``timestamp, open, high, low, close[, volume]`` with
    timestamps in IST (naive) or ISO strings.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._frame = normalize_candles(pd.read_csv(self.path, parse_dates=["timestamp"]))

    def fetch_history(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        lo = pd.Timestamp(start)
        hi = pd.Timestamp(end) + pd.Timedelta(days=1)
        return self._frame[(self._frame.index >= lo) & (self._frame.index < hi)].copy()


class LiveCandleFeed:
    """Polls history for today's COMPLETED 5-minute candles.

    The in-progress candle is always dropped, so strategy code only ever sees
    closed bars.
    """

    def __init__(self, source: HistoricalDataSource, symbol: str,
                 minutes: int = CANDLE_MINUTES) -> None:
        self.source = source
        self.symbol = symbol
        self.minutes = minutes

    def completed_candles(self, now: datetime) -> pd.DataFrame:
        """Today's candles whose end time is at or before ``now``."""
        frame = self.source.fetch_history(self.symbol, now.date(), now.date())
        if frame.empty:
            return frame
        ends = frame.index + pd.Timedelta(minutes=self.minutes)
        return frame[ends <= pd.Timestamp(now)]

    def previous_close(self, today: date, lookback_days: int = 10) -> Optional[float]:
        """Close of the last candle before ``today`` (None if unavailable)."""
        frame = self.source.fetch_history(self.symbol, today - timedelta(days=lookback_days),
                                          today - timedelta(days=1))
        return float(frame["close"].iloc[-1]) if not frame.empty else None


def split_by_day(frame: pd.DataFrame) -> List[Tuple[date, pd.DataFrame]]:
    """Split a multi-day frame into ``(date, day_frame)`` pairs in date order."""
    return [(day.date() if hasattr(day, "date") else day, grp)
            for day, grp in frame.groupby(frame.index.normalize())]
