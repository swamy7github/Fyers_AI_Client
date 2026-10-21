"""Paper-trading runner.

Polls the feed for the underlying's intraday candles during market hours,
feeds each *newly closed* bar to the :class:`TradingEngine`, and routes fills
to the :class:`PaperBroker`. It never places orders with the broker API, so it
is safe to run against the real Fyers feed. Option premiums come from live
option quotes when the feed can serve them (``premium_model=market``),
otherwise from the synthetic model (``synthetic``).

The runner is deliberately pull-based (no websocket) so it works with the
existing rate-limited ``FyersDataService`` and can also be driven from a
``CsvFeed`` with a replay clock for deterministic tests (``clock`` argument).
"""

from __future__ import annotations

import logging
import time as pytime
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence

from ..config.config import Settings
from ..data.feed import DataFeed
from ..engine import MarketPremium, SyntheticPremium, TradingEngine
from ..execution.broker import Broker
from ..execution.paper_broker import PaperBroker
from ..instruments import InstrumentSpec, get_instrument, previous_trading_day
from ..journal.trade_log import TradeLog
from ..models import Candle, DailySummary
from ..risk import RiskEngine
from ..signals import SignalEngine, compute_levels, split_sessions
from ..strategies.base import build_strategies

logger = logging.getLogger("ghanshyam.paper")


class PaperRunner:
    def __init__(self, settings: Settings, feed: DataFeed, instrument: str, resolution: int = 5,
                 broker: Optional[Broker] = None, journal: Optional[TradeLog] = None,
                 clock: Optional[Callable[[], datetime]] = None, poll_seconds: int = 15,
                 strategies: Optional[Sequence[str]] = None):
        self.s = settings
        self.feed = feed
        self.spec: InstrumentSpec = get_instrument(instrument)
        self.resolution = int(resolution)
        self.session = settings.session(self.spec.exchange.value)
        self.clock = clock or datetime.now
        self.poll_seconds = poll_seconds
        self.journal = journal or TradeLog(settings.path("log_dir"), mode="paper")
        self.broker = broker or PaperBroker.from_settings(settings)
        mode = "live" if getattr(self.broker, "is_live", False) else "paper"
        registry = build_strategies(settings, strategies)
        self.engine = TradingEngine(settings, self.spec, SignalEngine(settings, registry), RiskEngine(settings),
                                    self.broker, self.journal, mode=mode,
                                    premium_fn=MarketPremium(self._option_price, SyntheticPremium(settings, self.spec)))
        self._seen: set[datetime] = set()
        self._quote_cache: Dict[str, tuple[datetime, float]] = {}
        self.summary: Optional[DailySummary] = None

    # -- option premium from live quotes --------------------------------------------
    def _option_price(self, symbol: str, ts: datetime) -> Optional[float]:
        cached = self._quote_cache.get(symbol)
        if cached and abs((cached[0] - ts).total_seconds()) < 60:
            return cached[1]
        try:
            q = self.feed.quote(symbol)
        except Exception as exc:  # pragma: no cover - network
            logger.warning("quote failed for %s: %s", symbol, exc)
            return None
        if q is None or q.ltp <= 0:
            return None
        self._quote_cache[symbol] = (ts, q.ltp)
        return q.ltp

    # -- levels --------------------------------------------------------------------------
    def _prepare_levels(self, today: date):
        prev_day = previous_trading_day(today, self.spec)
        hist = self.feed.history(self.spec.underlying_symbol, self.resolution, prev_day - timedelta(days=10), prev_day)
        sessions = split_sessions(hist)
        if not sessions:
            raise RuntimeError(f"no history for {self.spec.underlying_symbol} before {today}; cannot compute LDCHL")
        days = sorted(sessions)
        prev = sessions[days[-1]]
        earlier = [sessions[d] for d in days[-4:-1]]
        return compute_levels(prev, today, self.spec, earlier)

    def _closed_bars(self, now: datetime, today: date) -> List[Candle]:
        """Bars of today that have *closed* by ``now`` (bar open + resolution <= now)."""
        df = self.feed.history(self.spec.underlying_symbol, self.resolution, today, today)
        bars = split_sessions(df).get(today, [])
        cutoff = now - timedelta(minutes=self.resolution)
        return [b for b in bars if b.ts <= cutoff]

    # -- main loop --------------------------------------------------------------------------
    def run_day(self, today: Optional[date] = None, max_iterations: Optional[int] = None) -> DailySummary:
        today = today or self.clock().date()
        levels = self._prepare_levels(today)
        self.engine.start_day(today, levels)
        logger.info("PAPER %s %s PDH=%.1f PDL=%.1f PDC=%.1f expiry_day=%s", self.spec.name, today, levels.pdh,
                    levels.pdl, levels.pdc, levels.is_expiry_day)
        iterations = 0
        last_bar: Optional[Candle] = None
        while True:
            now = self.clock()
            if now.date() != today:
                break
            if now.time() >= self.session.close:
                break
            if now.time() >= self.session.open:
                for bar in self._closed_bars(now, today):
                    if bar.ts in self._seen:
                        continue
                    self._seen.add(bar.ts)
                    self.engine.on_bar(bar)
                    last_bar = bar
                if now.time() >= self.session.square_off and not self.engine.open_positions() and last_bar is not None:
                    bar_end = last_bar.ts + timedelta(minutes=self.resolution)
                    if bar_end.time() >= self.session.square_off:
                        break
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            self._sleep()
        self.summary = self.engine.end_day(last_bar)
        logger.info("PAPER day done: %s", self.summary)
        return self.summary

    def _sleep(self) -> None:
        if self.clock is datetime.now:
            pytime.sleep(self.poll_seconds)

    def signals_today(self):
        return self.engine.signals.signals_for_day(self.spec)

