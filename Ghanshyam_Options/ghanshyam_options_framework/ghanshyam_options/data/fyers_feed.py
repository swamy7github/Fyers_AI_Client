"""Adapter over the user's EXISTING ``fyersclient.fyers_client`` module.

What we rely on (mapped from ``fyersclient/fyers_client.py``):

``get_fyers_client() -> FyersDataService``
    Zero-argument entry point. Loads/validates the cached token, auto-logs in
    with TOTP when needed, and returns a ``FyersDataService`` whose methods are
    rate-limited (``FyersRateLimiter``) and retried.

``FyersDataService.history(data: dict) -> dict``
    Raw Fyers v3 history call. ``data`` = ``{"symbol", "resolution", "date_format",
    "range_from", "range_to", "cont_flag"}``. Returns ``{"s": "ok", "candles":
    [[epoch, o, h, l, c, v], ...]}`` or ``{}`` on failure.

``FyersDataService.get_candles_range(symbol, resolution, range_from_date, range_to_date) -> list``
    Convenience wrapper returning just the candle list for ``YYYY-MM-DD`` dates.

``FyersDataService.get_candles_bulk(symbols, resolution, range_from_date, range_to_date, max_days_per_chunk=None)``
    Chunks long ranges (10 days intraday / 100 days daily) - we use this for
    multi-week backfills.

``FyersDataService.get_quotes(symbols: list[str]) -> dict`` / ``quotes(symbols)``
    Returns ``{"s": "ok", "d": [{"n": "<symbol>", "v": {"lp": <ltp>, ...}}, ...]}``.

``FyersDataService.get_quotes_batched(symbols) -> {symbol: v-dict}``
    Batches quotes 50 at a time (``Config.QUOTES_BATCH_SIZE``).

``FyersDataService.place_order(symbol, qty, side, product_type="INTRADAY", order_tag=...)``
    The ONLY order path. Not used by this module; see ``execution/fyers_broker.py``.

Nothing in this file imports ``fyersclient`` at module import time, so the
package (and its tests) work without ``fyers_apiv3`` / credentials installed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from ..models import Quote
from .cache import CandleCache
from .feed import DataFeed, OHLCV_COLUMNS, candles_from_fyers

logger = logging.getLogger("ghanshyam.fyers_feed")

INTRADAY_CHUNK_DAYS = 10   # mirrors fyers_client.Config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK
DAILY_CHUNK_DAYS = 100


def _load_existing_client() -> Any:
    """Import the user's client lazily. Raises ImportError with a clear message."""
    try:
        from fyersclient.fyers_client import get_fyers_client  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "fyersclient.fyers_client could not be imported. Run from the repo root with the "
            "fyers_apiv3/pyotp/python-dotenv packages installed and fyersclient/.envfyers present."
        ) from exc
    return get_fyers_client()


class FyersFeed(DataFeed):
    """DataFeed backed by the existing ``FyersDataService`` with an on-disk cache."""

    name = "fyers"

    def __init__(self, client: Any = None, cache: Optional[CandleCache] = None, *, use_cache: bool = True):
        self._client = client
        self.cache = cache
        self.use_cache = use_cache and cache is not None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _load_existing_client()
        return self._client

    # -- history ---------------------------------------------------------------
    def _fetch_range(self, symbol: str, resolution: str, start: date, end: date) -> pd.DataFrame:
        """Call the existing client, chunked the same way it chunks internally."""
        chunk_days = DAILY_CHUNK_DAYS if resolution.upper() == "D" else INTRADAY_CHUNK_DAYS
        frames: List[pd.DataFrame] = []
        cur = start
        while cur <= end:
            chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
            payload = {
                "symbol": symbol,
                "resolution": resolution,
                "date_format": "1",
                "range_from": cur.isoformat(),
                "range_to": chunk_end.isoformat(),
                "cont_flag": "1",
            }
            try:
                if hasattr(self.client, "get_candles_range"):
                    raw = self.client.get_candles_range(symbol, resolution, cur.isoformat(), chunk_end.isoformat())
                else:
                    resp = self.client.history(payload)
                    raw = resp.get("candles", []) if isinstance(resp, dict) else []
            except Exception as exc:
                logger.error("history failed for %s %s..%s: %s", symbol, cur, chunk_end, exc)
                raw = []
            if raw:
                frames.append(candles_from_fyers(raw))
            cur = chunk_end + timedelta(days=1)
        if not frames:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        return pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

    def history(self, symbol: str, resolution: int | str, start: date | datetime, end: date | datetime) -> pd.DataFrame:
        res = str(resolution)
        s = start.date() if isinstance(start, datetime) else start
        e = end.date() if isinstance(end, datetime) else end
        if not self.use_cache or res.upper() == "D":
            return self._fetch_range(symbol, res, s, e)

        days = [s + timedelta(days=i) for i in range((e - s).days + 1)]
        cached = self.cache.get_range(symbol, res, days)  # type: ignore[union-attr]
        missing = [d for d in days if d not in cached and d.weekday() < 5]
        fetched = pd.DataFrame(columns=OHLCV_COLUMNS)
        if missing:
            # fetch the contiguous span covering the misses (cheaper than many tiny calls)
            fetched = self._fetch_range(symbol, res, min(missing), max(missing))
            if not fetched.empty:
                self.cache.put(symbol, res, fetched)  # type: ignore[union-attr]
        parts = list(cached.values()) + ([fetched] if not fetched.empty else [])
        if not parts:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df = pd.concat(parts, ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        lo = datetime.combine(s, datetime.min.time())
        hi = datetime.combine(e, datetime.max.time())
        return df[(df["ts"] >= lo) & (df["ts"] <= hi)].reset_index(drop=True)

    def daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        return self._fetch_range(symbol, "D", start, end)

    # -- quotes ----------------------------------------------------------------
    @staticmethod
    def _parse_quote(symbol: str, item: Dict[str, Any]) -> Optional[Quote]:
        v = item.get("v", item) if isinstance(item, dict) else {}
        if not v or v.get("lp") is None:
            return None
        ts = v.get("tt")
        return Quote(
            symbol=symbol,
            ltp=float(v["lp"]),
            ts=datetime.fromtimestamp(int(ts)) if ts else None,
            bid=float(v["bid"]) if v.get("bid") is not None else None,
            ask=float(v["ask"]) if v.get("ask") is not None else None,
            volume=float(v["volume"]) if v.get("volume") is not None else None,
            oi=float(v["oi"]) if v.get("oi") is not None else None,
            raw=v,
        )

    def quotes(self, symbols: Iterable[str]) -> Dict[str, Quote]:
        syms = list(symbols)
        if not syms:
            return {}
        out: Dict[str, Quote] = {}
        try:
            if hasattr(self.client, "get_quotes_batched"):
                merged = self.client.get_quotes_batched(syms)
                for sym, v in merged.items():
                    q = self._parse_quote(sym, {"v": v})
                    if q:
                        out[sym] = q
                return out
            resp = self.client.get_quotes(syms)
        except Exception as exc:
            logger.error("quotes failed for %s: %s", syms, exc)
            return {}
        for item in (resp or {}).get("d", []):
            q = self._parse_quote(item.get("n", ""), item)
            if q:
                out[q.symbol] = q
        return out

    def quote(self, symbol: str) -> Optional[Quote]:
        return self.quotes([symbol]).get(symbol)

    # -- helpers used by the paper runner ---------------------------------------
    def latest_candles(self, symbol: str, resolution: int | str, lookback_days: int = 2) -> pd.DataFrame:
        """Today's bars plus ``lookback_days`` of history, uncached for today."""
        today = date.today()
        hist = self.history(symbol, resolution, today - timedelta(days=lookback_days), today - timedelta(days=1))
        today_df = self._fetch_range(symbol, str(resolution), today, today)
        parts = [p for p in (hist, today_df) if not p.empty]
        if not parts:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        return pd.concat(parts, ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
