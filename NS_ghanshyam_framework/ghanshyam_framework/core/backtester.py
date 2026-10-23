"""Date-wise backtesting engine plus the simulation primitives shared with paper trading.

Signals come from the underlying's 5-minute candles (fetched through the Fyers
data hooks). Option premiums are MODELLED with Black-Scholes, because option
candles for expired contracts are not reliably available. Premium-level results
therefore depend on the implied-volatility assumption and should be read as an
approximation; directional logic, stops and timing are exact on the candle data.

Execution assumptions (all deliberately conservative):
* Orders fill at the CLOSE of the signal candle (no look-ahead).
* Stops/targets are checked against each later candle's high/low; if one bar
  touches both, the stop is assumed to be hit first.
* Buys fill above and sells below the modelled premium by ``slippage_pct``.
"""
from __future__ import annotations

import calendar
import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config.settings import (CANDLE_MINUTES, ExecutionConfig, InstrumentConfig, RiskConfig,
                             StrategyConfig)
from core.data_handler import DataError, HistoricalDataSource, find_missing_bars, split_by_day
from core.logger_handler import (PerformanceMetrics, Trade, TradeLogger, compute_metrics)
from core.strategy import (DayRiskState, ExpiryResolver, GhanshyamStrategy, OptionContract,
                           Position, RiskManager, Signal, SignalResult, StrikeSelector,
                           resolve_underlying_symbol)

LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Option pricing
# --------------------------------------------------------------------------- #
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class OptionPricer(ABC):
    """Returns a modelled option premium for a contract at a time and spot."""

    @abstractmethod
    def price(self, contract: OptionContract, ts: datetime, spot: float) -> float:
        """Premium per unit (not per lot)."""


class BlackScholesPricer(OptionPricer):
    """European Black-Scholes premium with a flat volatility assumption."""

    def __init__(self, instrument: InstrumentConfig, iv: Optional[float] = None,
                 rate: float = 0.065) -> None:
        self.instrument = instrument
        self.iv = iv if iv is not None else instrument.default_iv
        self.rate = rate
        if self.iv <= 0:
            raise ValueError("iv must be positive")

    def price(self, contract: OptionContract, ts: datetime, spot: float) -> float:
        expiry_dt = datetime.combine(contract.expiry, self.instrument.expiry_time)
        seconds = max((expiry_dt - ts).total_seconds(), 60.0)   # floor at one minute
        t = seconds / (365.0 * 24 * 3600)
        sigma, strike, r = self.iv, contract.strike, self.rate
        vol_sqrt_t = sigma * math.sqrt(t)
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / vol_sqrt_t
        d2 = d1 - vol_sqrt_t
        discounted = strike * math.exp(-r * t)
        if contract.option_type == "CE":
            value = spot * _norm_cdf(d1) - discounted * _norm_cdf(d2)
        else:
            value = discounted * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        tick = self.instrument.tick_size
        return max(tick, round(value / tick) * tick)


# --------------------------------------------------------------------------- #
# Execution model
# --------------------------------------------------------------------------- #
class ExecutionModel:
    """Virtual slippage and per-order charges."""

    def __init__(self, instrument: InstrumentConfig,
                 config: Optional[ExecutionConfig] = None) -> None:
        self.instrument = instrument
        self.config = config or ExecutionConfig()

    def fill_price(self, price: float, side: str) -> float:
        """Premium after adverse slippage; ``side`` is "BUY" or "SELL"."""
        factor = 1 + self.config.slippage_pct if side == "BUY" else 1 - self.config.slippage_pct
        tick = self.instrument.tick_size
        return max(tick, round(price * factor / tick) * tick)

    def charges(self, price: float, quantity: int, side: str) -> float:
        """Brokerage + exchange + SEBI + tax + stamp duty + GST for one order."""
        cfg, ins = self.config, self.instrument
        turnover = price * quantity
        brokerage = min(cfg.brokerage_flat_inr, cfg.brokerage_max_pct * turnover)
        exchange = turnover * ins.exchange_txn_pct
        sebi = turnover * cfg.sebi_pct
        gst = cfg.gst_pct * (brokerage + exchange + sebi)
        tax = turnover * ins.stt_sell_pct if side == "SELL" else 0.0
        stamp = turnover * cfg.stamp_buy_pct if side == "BUY" else 0.0
        return brokerage + exchange + sebi + gst + tax + stamp


# --------------------------------------------------------------------------- #
# Simulated broker (shared by the backtester and the paper trader)
# --------------------------------------------------------------------------- #
@dataclass
class OpenTrade:
    """An entered trade waiting for its exit."""

    trade_id: str
    contract: OptionContract
    quantity: int
    entry_time: datetime
    entry_price: float
    entry_spot: float
    entry_charges: float
    position: Position


class SimulatedBroker:
    """Holds at most one open option position and produces completed trades."""

    def __init__(self, instrument: InstrumentConfig, execution: ExecutionModel,
                 pricer: OptionPricer, mode: str) -> None:
        self.instrument = instrument
        self.execution = execution
        self.pricer = pricer
        self.mode = mode
        self.open_trade: Optional[OpenTrade] = None
        self._counter = 0

    def enter(self, strategy: GhanshyamStrategy, result: SignalResult,
              contract: OptionContract, ts: datetime, lots: int,
              premium: Optional[float] = None) -> OpenTrade:
        """Open a position. ``premium`` overrides the modelled price (live quotes)."""
        if self.open_trade is not None:
            raise RuntimeError("A position is already open")
        if lots <= 0:
            raise ValueError("lots must be positive")
        assert result.ref_spot is not None
        market = premium if premium is not None else self.pricer.price(contract, ts, result.ref_spot)
        fill = self.execution.fill_price(market, "BUY")
        quantity = lots * self.instrument.lot_size
        self._counter += 1
        self.open_trade = OpenTrade(
            trade_id=f"{self.mode}-{ts:%Y%m%d}-{self._counter:03d}",
            contract=contract, quantity=quantity, entry_time=ts, entry_price=fill,
            entry_spot=result.ref_spot,
            entry_charges=self.execution.charges(fill, quantity, "BUY"),
            position=strategy.open_position(result, ts),
        )
        return self.open_trade

    def exit(self, spot: float, ts: datetime, reason: str,
             premium: Optional[float] = None) -> Trade:
        """Close the open position and return the completed :class:`Trade`."""
        if self.open_trade is None:
            raise RuntimeError("No open position")
        ot = self.open_trade
        market = premium if premium is not None else self.pricer.price(ot.contract, ts, spot)
        fill = self.execution.fill_price(market, "SELL")
        charges = ot.entry_charges + self.execution.charges(fill, ot.quantity, "SELL")
        gross = (fill - ot.entry_price) * ot.quantity
        side = ot.position.side.value
        self.open_trade = None
        return Trade(
            trade_id=ot.trade_id, mode=self.mode, instrument=self.instrument.name,
            option_symbol=ot.contract.symbol, side=side, strike=ot.contract.strike,
            expiry=ot.contract.expiry.isoformat(), quantity=ot.quantity,
            entry_time=ot.entry_time, entry_price=ot.entry_price, entry_spot=ot.entry_spot,
            exit_time=ts, exit_price=fill, exit_spot=spot, exit_reason=reason,
            charges=charges, gross_pnl=gross, net_pnl=gross - charges,
        )


# --------------------------------------------------------------------------- #
# Backtester
# --------------------------------------------------------------------------- #
@dataclass
class BacktestResult:
    """Output of a backtest run."""

    trades: List[Trade] = field(default_factory=list)
    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    daily_pnl: Dict[date, float] = field(default_factory=dict)
    skipped_days: Dict[date, str] = field(default_factory=dict)


def month_chunks(start: date, end: date) -> List[Tuple[date, date]]:
    """Split ``start``..``end`` into per-calendar-month (start, end) pairs."""
    chunks: List[Tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        last = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
        chunk_end = min(last, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


class Backtester:
    """Replays historical 5-minute candles through the strategy, day by day."""

    def __init__(self, source: HistoricalDataSource, instrument: InstrumentConfig,
                 strategy_config: Optional[StrategyConfig] = None,
                 risk_config: Optional[RiskConfig] = None,
                 execution_config: Optional[ExecutionConfig] = None,
                 pricer: Optional[OptionPricer] = None,
                 expiry_resolver: Optional[ExpiryResolver] = None) -> None:
        self.source = source
        self.instrument = instrument
        self.strategy = GhanshyamStrategy(instrument, strategy_config)
        self.risk = RiskManager(risk_config or RiskConfig())
        self.execution = ExecutionModel(instrument, execution_config)
        self.pricer = pricer or BlackScholesPricer(instrument)
        self.resolver = expiry_resolver or ExpiryResolver(instrument)
        self.selector = StrikeSelector(instrument, self.strategy.config.strike_mode,
                                       self.strategy.config.strike_steps)

    # ---- public API -----------------------------------------------------------
    def run(self, start: date, end: date,
            trade_logger: Optional[TradeLogger] = None) -> BacktestResult:
        """Backtest every trading day from ``start`` to ``end`` inclusive."""
        if end < start:
            raise ValueError("end date is before start date")
        result = BacktestResult()
        for chunk_start, chunk_end in month_chunks(start, end):
            symbol = resolve_underlying_symbol(self.instrument, chunk_start)
            try:
                frame = self.source.fetch_history(symbol, chunk_start - timedelta(days=7),
                                                  chunk_end)
            except DataError as exc:
                LOGGER.error("Data fetch failed for %s %s..%s: %s", symbol, chunk_start,
                             chunk_end, exc)
                for day in pd.bdate_range(chunk_start, chunk_end):
                    result.skipped_days[day.date()] = f"data error: {exc}"
                continue
            self._run_chunk(frame, chunk_start, chunk_end, result)

        if trade_logger is not None:
            trade_logger.log_trades(result.trades)
        result.metrics = compute_metrics(result.trades)
        for trade in result.trades:
            result.daily_pnl[trade.trade_date] = (
                result.daily_pnl.get(trade.trade_date, 0.0) + trade.net_pnl)
        LOGGER.info("Backtest %s %s..%s: %d trades, net PnL %.2f", self.instrument.name,
                    start, end, result.metrics.total_trades, result.metrics.total_pnl)
        return result

    def _run_chunk(self, frame: pd.DataFrame, start: date, end: date,
                   result: BacktestResult) -> None:
        seen = set()
        prev_close: Optional[float] = None
        for day, day_df in split_by_day(frame):
            if day < start:
                prev_close = float(day_df["close"].iloc[-1])
                continue
            if day > end:
                break
            seen.add(day)
            open_dt = datetime.combine(day, self.instrument.session_open)
            close_dt = datetime.combine(day, self.instrument.session_close)
            missing = find_missing_bars(day_df, open_dt, close_dt)
            if len(missing) > 3:
                LOGGER.warning("%s: %d of the expected 5-min bars are missing", day, len(missing))
            result.trades.extend(self.simulate_day(day, day_df, prev_close))
            prev_close = float(day_df["close"].iloc[-1])
        for day in pd.bdate_range(start, end):
            if day.date() not in seen:
                result.skipped_days.setdefault(day.date(), "no data (holiday or missing)")

    # ---- single-day simulation ------------------------------------------------
    def simulate_day(self, day: date, day_df: pd.DataFrame,
                     prev_close: Optional[float] = None) -> List[Trade]:
        """Simulate one trading day and return its completed trades."""
        broker = SimulatedBroker(self.instrument, self.execution, self.pricer, "backtest")
        state = DayRiskState()
        trades: List[Trade] = []
        step = timedelta(minutes=CANDLE_MINUTES)

        def close_trade(spot: float, ts: datetime, reason: str) -> None:
            trade = broker.exit(spot, ts, reason)
            self.risk.register_exit(state, trade.net_pnl)
            trades.append(trade)

        for i in range(len(day_df)):
            bar = day_df.iloc[i]
            bar_end = day_df.index[i].to_pydatetime() + step
            exited_here = False

            if broker.open_trade is not None:
                position = broker.open_trade.position
                event = self.strategy.check_intrabar(position, bar)
                if event is not None:
                    close_trade(event.spot, bar_end, event.reason)
                    exited_here = True
                else:
                    self.strategy.update_position(position, bar)

            position = broker.open_trade.position if broker.open_trade else None
            signal = self.strategy.evaluate(day_df.iloc[: i + 1], prev_close, position)

            if broker.open_trade is not None and signal.signal == Signal.EXIT:
                close_trade(float(bar["close"]), bar_end, signal.reason)
            elif (broker.open_trade is None and not exited_here
                  and signal.signal in (Signal.BUY_CE, Signal.BUY_PE)):
                allowed, why = self.risk.can_enter(state)
                if not allowed:
                    LOGGER.debug("%s: entry skipped - %s", day, why)
                    continue
                assert signal.ref_spot is not None and signal.stop_spot is not None
                lots = self.risk.lots_for(signal.ref_spot, signal.stop_spot,
                                          self.instrument.lot_size)
                if lots <= 0:
                    LOGGER.debug("%s: entry skipped - stop too wide for risk budget", day)
                    continue
                contract = self.selector.contract(signal.ref_spot, signal.signal, day,
                                                  self.resolver)
                broker.enter(self.strategy, signal, contract, bar_end, lots)
                self.risk.register_entry(state)

        if broker.open_trade is not None:   # data ended before square-off
            last_end = day_df.index[-1].to_pydatetime() + step
            close_trade(float(day_df["close"].iloc[-1]), last_end, "end_of_data")
        return trades
