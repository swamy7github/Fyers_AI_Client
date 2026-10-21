"""Date-wise backtester.

``run(instrument, from_date, to_date, resolution)`` loads underlying candles
through a :class:`DataFeed`, splits them into sessions, computes LDCHL from
the previous session, and replays each session bar by bar through
``TradingEngine`` (signals -> risk -> paper broker), squaring off at the
session's square-off time. Outputs trade rows, daily summaries, an equity
curve and :class:`Metrics`.

Option premium: if the feed can serve candles for the chosen option symbol on
that date they are used (``premium_model=market``); otherwise the synthetic
Black-Scholes model prices the contract from the underlying path
(``premium_model=synthetic``). The result reports how many trades used each.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence

import pandas as pd

from ..config.config import Settings
from ..data.feed import DataFeed
from ..engine import MarketPremium, SyntheticPremium, TradingEngine
from ..execution.paper_broker import PaperBroker
from ..instruments import get_instrument, parse_date
from ..journal.trade_log import TradeLog
from ..models import Candle, DailySummary, Trade
from ..risk import RiskEngine
from ..signals import SignalEngine, compute_levels, split_sessions
from ..strategies.base import build_strategies
from .metrics import Metrics, compute_metrics, equity_curve

logger = logging.getLogger("ghanshyam.backtest")


@dataclass
class BacktestResult:
    instrument: str
    start: date
    end: date
    resolution: int
    trades: List[Trade]
    daily: List[DailySummary]
    metrics: Metrics
    equity: pd.DataFrame
    trade_files: List[str] = field(default_factory=list)
    daily_file: Optional[str] = None
    premium_models: Dict[str, int] = field(default_factory=dict)
    sessions_run: int = 0
    sessions_skipped: List[str] = field(default_factory=list)

    def trades_frame(self) -> pd.DataFrame:
        from dataclasses import asdict

        return pd.DataFrame([asdict(t) for t in self.trades])

    def daily_frame(self) -> pd.DataFrame:
        from dataclasses import asdict

        return pd.DataFrame([asdict(d) for d in self.daily])


class Backtester:
    def __init__(self, settings: Settings, feed: DataFeed, journal: Optional[TradeLog] = None,
                 option_lookup: Optional[Callable[[str, datetime], Optional[float]]] = None,
                 strategies: Optional[Sequence[str]] = None):
        self.s = settings
        self.feed = feed
        self.journal = journal or TradeLog(settings.path("log_dir"), mode="backtest")
        self.option_lookup = option_lookup
        self.only = list(strategies) if strategies else None

    def run(self, instrument: str, from_date: str | date, to_date: str | date, resolution: int = 5,
            warmup_sessions: int = 3) -> BacktestResult:
        spec = get_instrument(instrument)
        start, end = parse_date(from_date), parse_date(to_date)
        if end < start:
            raise ValueError("to_date must be >= from_date")
        # pull a little history before `start` for PDH/PDL/PDC and multi-day S/R
        lookback = start - timedelta(days=max(7, warmup_sessions * 3))
        df = self.feed.history(spec.underlying_symbol, resolution, lookback, end)
        sessions = split_sessions(df)
        if not sessions:
            raise ValueError(f"no candles for {spec.underlying_symbol} between {lookback} and {end} in feed '{self.feed.name}'")
        days = sorted(sessions)

        registry = build_strategies(self.s, self.only)
        risk = RiskEngine(self.s)
        broker = PaperBroker.from_settings(self.s)
        sig_engine = SignalEngine(self.s, registry)
        premium_fn = SyntheticPremium(self.s, spec)
        if self.option_lookup is not None:
            premium_fn = MarketPremium(self.option_lookup, premium_fn)  # type: ignore[assignment]
        engine = TradingEngine(self.s, spec, sig_engine, risk, broker, self.journal, mode="backtest", premium_fn=premium_fn)

        daily: List[DailySummary] = []
        skipped: List[str] = []
        run_count = 0
        for i, d in enumerate(days):
            if d < start or d > end:
                continue
            if i == 0:
                skipped.append(f"{d}: no previous session for LDCHL")
                continue
            prev = sessions[days[i - 1]]
            earlier = [sessions[days[j]] for j in range(max(0, i - 1 - warmup_sessions), i - 1)]
            levels = compute_levels(prev, d, spec, earlier)
            bars = sessions[d]
            if len(bars) < 5:
                skipped.append(f"{d}: only {len(bars)} bars")
                continue
            engine.start_day(d, levels)
            self._replay(engine, bars)
            daily.append(engine.end_day(bars[-1]))
            run_count += 1
            logger.info("%s %s: trades=%d net=%.2f %s", spec.name, d, daily[-1].trades, daily[-1].net_pnl,
                        f"LOCKOUT: {daily[-1].lockout_reason}" if daily[-1].lockout_reason else "")

        trades = list(engine.trades)
        metrics = compute_metrics(trades, daily, self.s.total_capital)
        models: Dict[str, int] = {}
        for t in trades:
            models[t.premium_model] = models.get(t.premium_model, 0) + 1
        result = BacktestResult(
            instrument=spec.name, start=start, end=end, resolution=resolution, trades=trades, daily=daily,
            metrics=metrics, equity=equity_curve(trades, self.s.total_capital),
            trade_files=[str(p) for p in self.journal.written if p.name.startswith("trades_")],
            daily_file=str(self.journal.daily_path) if self.journal.daily_path.exists() else None,
            premium_models=models, sessions_run=run_count, sessions_skipped=skipped,
        )
        return result

    @staticmethod
    def _replay(engine: TradingEngine, bars: Sequence[Candle]) -> None:
        for idx, bar in enumerate(bars):
            nxt = bars[idx + 1].open if idx + 1 < len(bars) else None
            engine.on_bar(bar, next_bar_open=nxt)


def run(instrument: str, from_date: str, to_date: str, resolution: int = 5, settings: Optional[Settings] = None,
        feed: Optional[DataFeed] = None, journal: Optional[TradeLog] = None, strategies: Optional[Sequence[str]] = None,
        option_lookup=None) -> BacktestResult:
    """Convenience entry point matching the CLI signature."""
    from ..config.config import load_settings

    settings = settings or load_settings()
    if feed is None:
        from ..data.csv_feed import CsvFeed

        feed = CsvFeed(settings.path("sample_data_dir"))
    bt = Backtester(settings, feed, journal, option_lookup=option_lookup, strategies=strategies)
    return bt.run(instrument, from_date, to_date, resolution)


def option_lookup_from_feed(feed: DataFeed, resolution: int = 5) -> Callable[[str, datetime], Optional[float]]:
    """Build a ``(symbol, ts) -> close`` lookup that tries the feed for option candles and caches per symbol/day.

    Returns ``None`` (=> synthetic fallback) when the feed has nothing for that
    contract on that date, which is the normal case for expired contracts.
    """
    cache: Dict[tuple, pd.DataFrame] = {}

    def lookup(symbol: str, ts: datetime) -> Optional[float]:
        key = (symbol, ts.date())
        if key not in cache:
            try:
                cache[key] = feed.history(symbol, resolution, ts.date(), ts.date())
            except Exception:
                cache[key] = pd.DataFrame()
        df = cache[key]
        if df is None or df.empty:
            return None
        sub = df[df["ts"] <= ts]
        if sub.empty:
            return None
        return float(sub.iloc[-1]["close"])

    return lookup

