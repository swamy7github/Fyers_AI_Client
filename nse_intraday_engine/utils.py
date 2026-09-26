"""
Shared utilities: logging, retry/backoff, rate limiting, cost model and
IST time helpers used across the engine.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
T = TypeVar("T")


def setup_logger(name: str, log_dir: str, level: str = "INFO") -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    fh = logging.FileHandler(Path(log_dir) / f"{name}.log")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(s: str) -> dtime:
    h, m = (int(x) for x in s.split(":"))
    return dtime(hour=h, minute=m)


def is_between(ts: datetime, start: str, end: str) -> bool:
    t = ts.timetz().replace(tzinfo=None)
    return parse_hhmm(start) <= t <= parse_hhmm(end)


class RateLimiter:
    """Guarantees at least `min_interval_sec` seconds between consecutive
    calls made through `.wait()` — a simple pacing limiter for API calls."""

    def __init__(self, min_interval_sec: float):
        self.min_interval = min_interval_sec
        self._last_call = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()


def retry_with_backoff(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = (Exception,),
    logger: logging.Logger | None = None,
):
    """Decorator: exponential backoff with jitter around flaky network / API calls."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    attempt += 1
                    if attempt > max_retries:
                        if logger:
                            logger.error(
                                "Giving up on %s after %d attempts: %s",
                                fn.__name__, attempt, exc,
                            )
                        raise
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay += random.uniform(0, delay * 0.1)
                    if logger:
                        logger.warning(
                            "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                            fn.__name__, attempt, max_retries, exc, delay,
                        )
                    time.sleep(delay)
        return wrapper
    return decorator


@dataclass
class Charges:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    stamp_duty: float
    gst: float

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exchange_txn
                + self.sebi + self.stamp_duty + self.gst)


def calculate_charges(buy_value: float, sell_value: float, cfg) -> Charges:
    """Approximate standard NSE intraday-equity charges, mirroring a typical
    discount-broker cost structure. Treat as an estimate, not a tax quote --
    always reconcile against your broker's actual contract note."""
    turnover = buy_value + sell_value
    brokerage = (min(cfg.BROKERAGE_PER_ORDER, buy_value * 0.0003)
                 + min(cfg.BROKERAGE_PER_ORDER, sell_value * 0.0003))
    stt = sell_value * cfg.STT_SELL_PCT / 100
    exchange_txn = turnover * cfg.EXCHANGE_TXN_PCT / 100
    sebi = turnover * cfg.SEBI_CHARGES_PCT / 100
    stamp_duty = buy_value * cfg.STAMP_DUTY_BUY_PCT / 100
    gst = (brokerage + exchange_txn) * cfg.GST_PCT / 100
    return Charges(brokerage, stt, exchange_txn, sebi, stamp_duty, gst)


def apply_slippage(price: float, side: str, slippage_pct: float) -> float:
    """BUY fills worse (higher), SELL fills worse (lower)."""
    factor = slippage_pct / 100
    return price * (1 + factor) if side == "BUY" else price * (1 - factor)
