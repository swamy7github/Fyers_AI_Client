"""Real-time paper trading loop.

Every completed 5-minute candle is evaluated by the same strategy and risk code
the backtester uses. Between candles the loop polls the underlying's LTP to catch
stop-loss / target hits. Orders are simulated: premiums come from live option
quotes when available (falling back to the Black-Scholes model) and fills include
virtual slippage and a full charge model. No orders are ever sent to a broker.

Restart behaviour: only the latest completed candle is acted on, so restarting
mid-session never replays stale breakouts, but any position open before a restart
is forgotten.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from config.settings import (HOLIDAYS, TIMEZONE, ExecutionConfig, InstrumentConfig, PaperConfig,
                             RiskConfig, StrategyConfig)
from core.backtester import BlackScholesPricer, ExecutionModel, OptionPricer, SimulatedBroker
from core.data_handler import DataError, LiveCandleFeed
from core.logger_handler import Trade, TradeLogger, generate_eod_summary
from core.strategy import (DayRiskState, ExpiryResolver, GhanshyamStrategy, OptionContract,
                           RiskManager, Signal, StrikeSelector, resolve_underlying_symbol)

LOGGER = logging.getLogger(__name__)
QuoteFn = Callable[[str], float]


def ist_now() -> datetime:
    """Current Indian time as a naive datetime."""
    return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)


class MarketClock:
    """Session times for one instrument."""

    def __init__(self, instrument: InstrumentConfig,
                 holidays: Optional[List[date]] = None) -> None:
        self.instrument = instrument
        self.holidays = list(HOLIDAYS if holidays is None else holidays)

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def is_open(self, now: datetime) -> bool:
        ins = self.instrument
        return self.is_trading_day(now.date()) and ins.session_open <= now.time() < ins.session_close

    def last_boundary(self, now: datetime, minutes: int = 5) -> datetime:
        """Most recent 5-minute boundary at or before ``now``."""
        return now.replace(minute=now.minute - now.minute % minutes, second=0, microsecond=0)


class PaperTrader:
    """Simulated live trading on completed 5-minute candles."""

    def __init__(self, instrument: InstrumentConfig, feed: LiveCandleFeed,
                 quote_fn: Optional[QuoteFn] = None,
                 strategy_config: Optional[StrategyConfig] = None,
                 risk_config: Optional[RiskConfig] = None,
                 execution_config: Optional[ExecutionConfig] = None,
                 paper_config: Optional[PaperConfig] = None,
                 pricer: Optional[OptionPricer] = None,
                 trade_logger: Optional[TradeLogger] = None,
                 now_fn: Callable[[], datetime] = ist_now,
                 sleep_fn: Callable[[float], None] = _time.sleep) -> None:
        self.instrument = instrument
        self.feed = feed
        self.quote_fn = quote_fn
        self.strategy = GhanshyamStrategy(instrument, strategy_config)
        self.risk = RiskManager(risk_config or RiskConfig())
        self.paper = paper_config or PaperConfig()
        self.pricer = pricer or BlackScholesPricer(instrument)
        self.broker = SimulatedBroker(instrument, ExecutionModel(instrument, execution_config),
                                      self.pricer, "paper")
        self.selector = StrikeSelector(instrument, self.strategy.config.strike_mode,
                                       self.strategy.config.strike_steps)
        self.resolver = ExpiryResolver(instrument)
        self.clock = MarketClock(instrument)
        self.logger = trade_logger or TradeLogger()
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn
        self.trades: List[Trade] = []
        self.state = DayRiskState()
        self._day: Optional[date] = None
        self._prev_close: Optional[float] = None
        self._last_candle = None
        self._symbol = instrument.underlying_symbol

    # ---- day lifecycle --------------------------------------------------------
    def _start_day(self, now: datetime) -> None:
        self._day = now.date()
        self.state = DayRiskState()
        self._last_candle = None
        self._symbol = resolve_underlying_symbol(self.instrument, now.date())
        self.feed.symbol = self._symbol
        try:
            self._prev_close = self.feed.previous_close(now.date())
        except DataError as exc:
            LOGGER.warning("Could not load previous close (%s); gap filter disabled", exc)
            self._prev_close = None
        LOGGER.info("Paper session %s for %s (prev close %s)", now.date(), self._symbol,
                    self._prev_close)

    # ---- quotes ---------------------------------------------------------------
    def _underlying_ltp(self) -> Optional[float]:
        if self.quote_fn is None:
            return None
        try:
            return float(self.quote_fn(self._symbol))
        except Exception as exc:  # broker SDKs raise assorted exceptions
            LOGGER.warning("LTP unavailable for %s: %s", self._symbol, exc)
            return None

    def _premium(self, contract: OptionContract, spot: float, now: datetime) -> float:
        """Live option LTP if available, else the Black-Scholes model."""
        if self.quote_fn is not None:
            try:
                value = float(self.quote_fn(contract.symbol))
                if value > 0:
                    return value
            except Exception as exc:
                LOGGER.warning("Option quote failed for %s (%s); using model price",
                               contract.symbol, exc)
        return self.pricer.price(contract, now, spot)

    # ---- trade handling -------------------------------------------------------
    def _close(self, spot: float, now: datetime, reason: str) -> None:
        ot = self.broker.open_trade
        assert ot is not None
        trade = self.broker.exit(spot, now, reason, premium=self._premium(ot.contract, spot, now))
        self.risk.register_exit(self.state, trade.net_pnl)
        self.trades.append(trade)
        self.logger.log_exit(trade)
        LOGGER.info("EXIT  %s %s @ %.2f (%s) net PnL %.2f", trade.side, trade.option_symbol,
                    trade.exit_price, reason, trade.net_pnl)

    def _check_tick(self, now: datetime) -> bool:
        """Intrabar stop/target check using the underlying's LTP. True if it exited."""
        ot = self.broker.open_trade
        ltp = self._underlying_ltp()
        if ot is None or ltp is None:
            return False
        event = self.strategy.check_intrabar(
            ot.position, {"open": ltp, "high": ltp, "low": ltp, "close": ltp})
        if event is None:
            return False
        self._close(ltp, now, event.reason)
        return True

    def _on_candle(self, frame, now: datetime) -> None:
        """Process the newest completed candle (last row of ``frame``)."""
        bar = frame.iloc[-1]
        bar_end = frame.index[-1].to_pydatetime() + timedelta(minutes=5)
        exited_here = False
        ot = self.broker.open_trade
        if ot is not None and frame.index[-1] >= ot.entry_time.replace(second=0, microsecond=0):
            event = self.strategy.check_intrabar(ot.position, bar)
            if event is not None:
                self._close(event.spot, now, event.reason)
                exited_here = True
            else:
                self.strategy.update_position(ot.position, bar)

        position = self.broker.open_trade.position if self.broker.open_trade else None
        signal = self.strategy.evaluate(frame, self._prev_close, position)
        LOGGER.info("%s candle close %.2f -> %s (%s)", bar_end.strftime("%H:%M"),
                    float(bar["close"]), signal.signal.value, signal.reason)

        if self.broker.open_trade is not None and signal.signal == Signal.EXIT:
            self._close(float(bar["close"]), now, signal.reason)
        elif (self.broker.open_trade is None and not exited_here
              and signal.signal in (Signal.BUY_CE, Signal.BUY_PE)):
            allowed, why = self.risk.can_enter(self.state)
            if not allowed:
                LOGGER.info("Entry skipped: %s", why)
                return
            assert signal.ref_spot is not None and signal.stop_spot is not None
            lots = self.risk.lots_for(signal.ref_spot, signal.stop_spot, self.instrument.lot_size)
            if lots <= 0:
                LOGGER.info("Entry skipped: stop too wide for risk budget")
                return
            contract = self.selector.contract(signal.ref_spot, signal.signal, now.date(),
                                              self.resolver)
            premium = self._premium(contract, signal.ref_spot, now)
            opened = self.broker.enter(self.strategy, signal, contract, now, lots, premium=premium)
            self.risk.register_entry(self.state)
            self.logger.log_entry(
                opened.trade_id, "paper", self.instrument.name, contract.symbol,
                signal.signal.value, contract.strike, contract.expiry.isoformat(),
                opened.quantity, opened.entry_price, opened.entry_spot, now,
                opened.entry_charges, reason=signal.reason)
            LOGGER.info("ENTRY %s %s qty %d @ %.2f | stop %.2f target %.2f", signal.signal.value,
                        contract.symbol, opened.quantity, opened.entry_price,
                        signal.stop_spot, signal.target_spot)

    # ---- main step / loop -----------------------------------------------------
    def step(self, now: datetime) -> str:
        """Advance the simulation to ``now``. Returns a short action code.

        Codes: ``closed`` (market shut), ``squared_off``, ``tick_exit``,
        ``candle`` (a new candle was processed) or ``idle``.
        """
        if self._day != now.date():
            self._start_day(now)
        if not self.clock.is_open(now):
            return "closed"

        if self.broker.open_trade is not None and now.time() >= self.instrument.square_off:
            ltp = self._underlying_ltp()
            spot = ltp if ltp is not None else self.broker.open_trade.entry_spot
            self._close(spot, now, "square_off")
            return "squared_off"

        action = "idle"
        if self.broker.open_trade is not None and self._check_tick(now):
            action = "tick_exit"

        boundary = self.clock.last_boundary(now - timedelta(seconds=self.paper.candle_delay_seconds))
        try:
            frame = self.feed.completed_candles(boundary)
        except DataError as exc:
            LOGGER.warning("Candle fetch failed: %s", exc)
            return action
        if frame.empty or (self._last_candle is not None and frame.index[-1] <= self._last_candle):
            return action
        if self._last_candle is not None:
            skipped = int((frame.index > self._last_candle).sum()) - 1
            if skipped > 0:
                LOGGER.warning("Missed %d candle(s); acting on the latest only", skipped)
        self._last_candle = frame.index[-1]
        self._on_candle(frame, now)
        return "candle"

    def run(self, max_steps: Optional[int] = None) -> Dict:
        """Run until the session closes, then square off and write the EOD summary."""
        steps = 0
        start = self.now_fn()
        if not self.clock.is_trading_day(start.date()):
            LOGGER.warning("%s is not a trading day for %s", start.date(), self.instrument.name)
            return {}
        LOGGER.info("Paper trading %s (Ctrl+C to stop)", self.instrument.name)
        try:
            while max_steps is None or steps < max_steps:
                now = self.now_fn()
                if now.time() >= self.instrument.session_close:
                    break
                if now.time() < self.instrument.session_open:
                    self.sleep_fn(min(30.0, max(
                        1.0, (datetime.combine(now.date(), self.instrument.session_open)
                              - now).total_seconds())))
                    continue
                self.step(now)
                steps += 1
                self.sleep_fn(self.paper.poll_seconds)
        except KeyboardInterrupt:
            LOGGER.warning("Interrupted by user")
        return self.finish()

    def finish(self) -> Dict:
        """Force-close any open position and produce the EOD summary."""
        now = self.now_fn()
        if self.broker.open_trade is not None:
            ltp = self._underlying_ltp()
            spot = ltp if ltp is not None else self.broker.open_trade.entry_spot
            self._close(spot, now, "session_end")
        return generate_eod_summary(now.date(), mode="paper", log_path=self.logger.path,
                                    out_dir=self.logger.path.parent)
