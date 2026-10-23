"""Synthetic data builders and fakes shared by the tests (no network, no credentials)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable, List, Sequence, Tuple

import pandas as pd

from core.data_handler import normalize_candles
from core.logger_handler import Trade

Bar = Tuple[float, float, float, float]  # open, high, low, close


def build_day(day: date, bars: Sequence[Bar], start: time = time(9, 15)) -> pd.DataFrame:
    """5-minute candles beginning at ``start`` from (o, h, l, c) tuples."""
    first = datetime.combine(day, start)
    index = [first + timedelta(minutes=5 * i) for i in range(len(bars))]
    frame = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=index)
    frame["volume"] = 0.0
    frame.index.name = "timestamp"
    return frame


def flat_bars(price: float, count: int, half_range: float = 5.0) -> List[Bar]:
    """Quiet candles oscillating tightly around ``price``."""
    return [(price, price + half_range, price - half_range, price)] * count


def breakout_day(day: date, direction: str = "up", follow: Iterable[Bar] = ()) -> pd.DataFrame:
    """Opening range 23980-24050, an inside candle, a breakout candle, then ``follow``.

    Up breakout closes at 24070; down breakout closes at 23960.
    """
    opening = (24000.0, 24050.0, 23980.0, 24020.0)
    inside = (24020.0, 24040.0, 24000.0, 24030.0)
    if direction == "up":
        trigger = (24030.0, 24075.0, 24025.0, 24070.0)
    else:
        trigger = (24020.0, 24025.0, 23955.0, 23960.0)
    return build_day(day, [opening, inside, trigger, *follow])


def make_trade(net: float, exit_minute: int = 0, charges: float = 0.0,
               day: date = date(2026, 6, 1), mode: str = "backtest") -> Trade:
    """A Trade with a chosen net PnL (gross = net + charges)."""
    entry = datetime.combine(day, time(9, 30))
    exit_time = entry + timedelta(minutes=exit_minute + 5)
    return Trade(
        trade_id=f"T{exit_minute}-{net}", mode=mode, instrument="NIFTY",
        option_symbol="NSE:NIFTY26JUN24000CE", side="BUY_CE", strike=24000.0,
        expiry="2026-06-30", quantity=65, entry_time=entry, entry_price=100.0,
        entry_spot=24070.0, exit_time=exit_time, exit_price=100.0 + net / 65,
        exit_spot=24100.0, exit_reason="target", charges=charges,
        gross_pnl=net + charges, net_pnl=net,
    )


class FakeSource:
    """In-memory HistoricalDataSource built from one or more day frames."""

    def __init__(self, *frames: pd.DataFrame) -> None:
        self.frame = normalize_candles(pd.concat(frames)) if frames else normalize_candles([])
        self.calls: List[Tuple[str, date, date]] = []

    def fetch_history(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self.calls.append((symbol, start, end))
        lo, hi = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
        return self.frame[(self.frame.index >= lo) & (self.frame.index < hi)].copy()


class ConstPricer:
    """Pricer returning a scripted sequence of premiums (last value repeats)."""

    def __init__(self, *values: float) -> None:
        self.values = list(values)

    def price(self, contract, ts, spot) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]
