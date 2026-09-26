"""
backtest_strategy_picks.py
-----------------------------
Backtests an arbitrary DAILY STOCK-SELECTION strategy: given a CSV of
{date, comma-separated Fyers stock picks}, simulates buying each of that
day's picks at the open and selling at the close, equal-weighted across
capital, deducts configurable slippage/brokerage, and reports the results
- both appended back into the same CSV and as a console tear-sheet.

This is a DIFFERENT tool from backtest.py in this same project:
backtest.py specifically replays this project's own live opening-range /
VWAP signal strategy (signal_engine.SignalEngine) against historical data.
This script instead backtests ANY daily stock-picking strategy you already
have picks for - it doesn't know or care how strategy_picks.csv's picks
were chosen, only what happened to each one intraday on the date given.

Design
-------
- DailyPriceCache does the one job the brief called out explicitly:
  fetch each unique symbol's daily OHLC ONCE for the whole needed date
  range (chunked via HistoryProvider.get_candles_range, reusing
  config.DAILY_HISTORY_MAX_DAYS_PER_CHUNK), cache it to a local CSV per
  symbol, and serve every day's lookup from that cache. A second run over
  the same symbols/date range makes ZERO API calls.
- BacktestEngine does the execution math: equal-weight allocation across
  that day's tradable ("active") picks, buy-at-open/sell-at-close, cost
  model applied per side, capital compounding day over day.
- compute_tear_sheet()/print_tear_sheet() turn the day-by-day results into
  the summary block, independent of how the trades were generated - kept
  separate so the statistics themselves are easy to unit-test in isolation
  (see test_backtest_strategy_picks.py).

What this does NOT do
-----------------------
- Model whole-share lot sizes: allocation is treated as continuous capital
  (allocated_capital * return_pct), not floor(capital / entry_price) whole
  shares. This is a standard simplifying assumption for a return-based
  backtest at this level, but it means the reported P&L won't exactly match
  what you'd get rounding to real share lots - flag this if you need
  lot-accurate results.
- Account for NSE trading holidays specifically - a date in
  strategy_picks.csv with no candle for ANY of its picks is treated as "no
  tradable data" and contributes zero P&L for that day (logged), not a
  crash, but it doesn't distinguish "holiday" from "bad symbol" beyond the
  logged reason per stock.
- Avoid survivorship/data-quality issues in whatever price history your
  Fyers plan returns - garbage in, garbage out, same as any backtester.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional, Protocol

import pandas as pd
import pytz

import config

logger = logging.getLogger("orb_agent.backtest_picks")

# ----------------------------------------------------------------------------
# Config - kept local to this script rather than in config.py (a generic
# picks-backtester isn't really "ORB agent configuration"), but it DOES
# reuse a couple of already-vetted constants from config.py:
# DAILY_HISTORY_MAX_DAYS_PER_CHUNK for chunking long date ranges, and
# TIMEZONE for correct epoch -> calendar-date conversion.
# ----------------------------------------------------------------------------
DEFAULT_INITIAL_CAPITAL = 200_000.0
DEFAULT_COST_PCT_PER_SIDE = 0.05     # % of notional, deducted on BOTH the buy and the sell
DEFAULT_CACHE_DIR = os.path.join(config.BASE_DIR, "cache", "strategy_picks_daily_ohlc")
DEFAULT_ANNUALIZATION_FACTOR = 252    # trading days/year, for Sharpe
DEFAULT_RISK_FREE_RATE_ANNUAL = 0.0   # simplifying assumption - pass a different value if you want one


class HistoryProvider(Protocol):
    """
    Structural type: anything exposing this ONE method works, typed or
    duck-typed. This project's fyers_service.FyersDataService already
    implements it exactly (rate-limited, retrying) - see the __main__ stub
    at the bottom for how to construct one from your active Fyers session,
    or how to adapt a raw fyersModel.FyersModel() instance if you have one
    from elsewhere.
    """
    def get_candles_range(self, symbol: str, resolution: str,
                           range_from_date: str, range_to_date: str) -> list: ...


# ----------------------------------------------------------------------------
# Data types
# ----------------------------------------------------------------------------
@dataclass
class Candle:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class TradeResult:
    date: str
    symbol: str
    entry: float        # raw open (not cost-adjusted) - the actual market price used
    exit: float          # raw close (not cost-adjusted)
    return_pct: float    # AFTER slippage/brokerage
    net_pnl: float        # AFTER slippage/brokerage, in rupees


@dataclass
class DayResult:
    date: str
    picks: list[str]
    active_symbols: list[str]
    skipped_symbols: list[str]
    skip_reasons: dict[str, str]
    starting_capital: float
    daily_net_profit: float
    daily_return_pct: float
    trades: list[TradeResult] = field(default_factory=list)


@dataclass
class TearSheet:
    initial_capital: float
    final_capital: float
    net_gain: float
    total_return_pct: float
    total_trades: int
    win_rate_pct: float
    gross_profit: float
    gross_loss: float          # <= 0 by construction (sum of losing trades)
    profit_factor: float        # gross_profit / abs(gross_loss); inf if no losses at all
    max_drawdown_pct: float     # <= 0
    sharpe_ratio: float


@dataclass
class CostModel:
    """Slippage + brokerage as a flat % of notional, applied to the EXECUTION price on each side."""
    pct_per_side: float = DEFAULT_COST_PCT_PER_SIDE

    def effective_entry(self, open_price: float) -> float:
        return open_price * (1 + self.pct_per_side / 100)

    def effective_exit(self, close_price: float) -> float:
        return close_price * (1 - self.pct_per_side / 100)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _date_chunks(start: date, end: date, max_days_per_chunk: int) -> list[tuple[date, date]]:
    chunks = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=max_days_per_chunk - 1), end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return chunks


def _is_invalid_or_illiquid(candle: Candle) -> Optional[str]:
    """Returns a human-readable reason to EXCLUDE this candle from that day's basket, or None if it's fine."""
    values = (candle.open, candle.high, candle.low, candle.close)
    if any(v is None for v in values):
        return "missing OHLC value"
    if any(isinstance(v, float) and math.isnan(v) for v in values):
        return "NaN in OHLC"
    if candle.open <= 0 or candle.close <= 0:
        return "non-positive open/close"
    if candle.volume is not None and candle.volume == 0:
        return "zero volume (illiquid)"
    return None


# ----------------------------------------------------------------------------
# Price cache
# ----------------------------------------------------------------------------
class DailyPriceCache:
    """
    Local on-disk cache of daily OHLCV per symbol, backed by any
    HistoryProvider. warm() makes at most one CHUNKED set of API calls per
    symbol per run, and none at all on a re-run whose needed date range is
    already fully covered on disk - this is the "prevent rate limits"
    mechanism the brief asked for.
    """

    def __init__(self, provider: HistoryProvider, cache_dir: str = DEFAULT_CACHE_DIR,
                 chunk_days: int = config.DAILY_HISTORY_MAX_DAYS_PER_CHUNK):
        self._provider = provider
        self._cache_dir = cache_dir
        self._chunk_days = chunk_days
        self._tz = pytz.timezone(config.TIMEZONE)
        self._by_symbol: dict[str, dict[str, Candle]] = {}
        os.makedirs(self._cache_dir, exist_ok=True)

    def _cache_path(self, symbol: str) -> str:
        safe = symbol.replace(":", "_").replace("&", "AND")
        return os.path.join(self._cache_dir, f"{safe}.csv")

    def _load_cache_file(self, symbol: str) -> pd.DataFrame:
        path = self._cache_path(symbol)
        if os.path.exists(path):
            try:
                return pd.read_csv(path, dtype={"date": str})
            except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
                logger.warning("Could not read cache file for %s (%s) - refetching.", symbol, exc)
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    def warm(self, symbols: list[str], start_date: str, end_date: str) -> None:
        for symbol in symbols:
            cached = self._load_cache_file(symbol)
            covers_range = (not cached.empty and cached["date"].min() <= start_date
                             and cached["date"].max() >= end_date)
            if not covers_range:
                fetched = self._fetch_range(symbol, start_date, end_date)
                if fetched:
                    fetched_df = pd.DataFrame([c.__dict__ for c in fetched])
                    cached = (pd.concat([cached, fetched_df], ignore_index=True)
                              .drop_duplicates(subset="date", keep="last")
                              .sort_values("date"))
                    cached.to_csv(self._cache_path(symbol), index=False)
                elif cached.empty:
                    logger.warning("No daily candles at all for %s in [%s, %s] - every date for this "
                                    "symbol will be skipped as missing.", symbol, start_date, end_date)

            self._by_symbol[symbol] = {
                str(row["date"]): Candle(str(row["date"]), float(row["open"]), float(row["high"]),
                                          float(row["low"]), float(row["close"]), float(row["volume"]))
                for _, row in cached.iterrows()
            }

    def _fetch_range(self, symbol: str, start_date: str, end_date: str) -> list[Candle]:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
        candles: list[Candle] = []
        for chunk_start, chunk_end in _date_chunks(start, end, self._chunk_days):
            try:
                raw = self._provider.get_candles_range(symbol, "D", str(chunk_start), str(chunk_end))
            except Exception as exc:
                logger.error("get_candles_range failed for %s [%s, %s]: %s", symbol, chunk_start, chunk_end, exc)
                continue
            for row in raw:
                try:
                    epoch, o, h, l, c, v = row
                    date_str = datetime.fromtimestamp(epoch, self._tz).strftime("%Y-%m-%d")
                    candles.append(Candle(date_str, float(o), float(h), float(l), float(c), float(v)))
                except (ValueError, TypeError) as exc:
                    logger.warning("Skipping malformed candle for %s: %s (%s)", symbol, row, exc)
        return candles

    def get(self, symbol: str, date_str: str) -> Optional[Candle]:
        return self._by_symbol.get(symbol, {}).get(date_str)


# ----------------------------------------------------------------------------
# Execution engine
# ----------------------------------------------------------------------------
class BacktestEngine:
    """
    Simulates buy-at-open/sell-at-close, equal-weighted across that day's
    ACTIVE picks (picks with valid, tradable data - see
    _is_invalid_or_illiquid), with capital compounding day over day. Because
    this is intraday-only (no overnight exposure), the equity curve is
    exactly initial_capital plus a running sum of daily_net_profit - no
    separate mark-to-market step is needed.
    """

    def __init__(self, price_cache: DailyPriceCache, initial_capital: float = DEFAULT_INITIAL_CAPITAL,
                 cost_model: Optional[CostModel] = None):
        self.price_cache = price_cache
        self.initial_capital = initial_capital
        self.cost_model = cost_model or CostModel()

    def run(self, picks_df: pd.DataFrame) -> tuple[list[DayResult], pd.DataFrame]:
        """
        Returns (chronological_day_results, updated_df).

        chronological_day_results is ALWAYS in date order (required for
        compute_tear_sheet's equity-curve/drawdown/Sharpe math to compound
        correctly), regardless of strategy_picks.csv's original row order.

        updated_df carries the 3 new columns back in the CSV's ORIGINAL row
        order/position - not by date string - so two rows that happen to
        share the same date can never have their results swapped by a
        dict-by-date collision.
        """
        capital = self.initial_capital
        chronological_results: list[DayResult] = []
        results_by_row_pos: dict = {}

        order = picks_df["date"].astype(str).str.strip().sort_values().index  # chronological; safe for ISO strings

        for row_pos in order:
            row = picks_df.loc[row_pos]
            date_str = str(row["date"]).strip()
            picks = [s.strip() for s in str(row["stocks"]).split(",") if s.strip()]

            active_symbols: list[str] = []
            skipped: list[str] = []
            skip_reasons: dict[str, str] = {}
            for symbol in picks:
                candle = self.price_cache.get(symbol, date_str)
                if candle is None:
                    skipped.append(symbol)
                    skip_reasons[symbol] = "no candle (holiday, data gap, or delisting)"
                    continue
                reason = _is_invalid_or_illiquid(candle)
                if reason:
                    skipped.append(symbol)
                    skip_reasons[symbol] = reason
                    continue
                active_symbols.append(symbol)

            starting_capital = capital
            trades: list[TradeResult] = []
            if active_symbols:
                allocation = starting_capital / len(active_symbols)
                for symbol in active_symbols:
                    candle = self.price_cache.get(symbol, date_str)
                    entry_eff = self.cost_model.effective_entry(candle.open)
                    exit_eff = self.cost_model.effective_exit(candle.close)
                    return_pct = (exit_eff - entry_eff) / entry_eff * 100
                    net_pnl = allocation * return_pct / 100
                    trades.append(TradeResult(
                        date=date_str, symbol=symbol,
                        entry=round(candle.open, 4), exit=round(candle.close, 4),
                        return_pct=round(return_pct, 4), net_pnl=round(net_pnl, 2),
                    ))
            else:
                logger.warning("%s: no tradable picks (all missing/illiquid) - day contributes zero P&L.", date_str)

            daily_net_profit = round(sum(t.net_pnl for t in trades), 2)
            daily_return_pct = round((daily_net_profit / starting_capital * 100) if starting_capital else 0.0, 4)
            capital = starting_capital + daily_net_profit

            result = DayResult(
                date=date_str, picks=picks, active_symbols=active_symbols,
                skipped_symbols=skipped, skip_reasons=skip_reasons,
                starting_capital=starting_capital, daily_net_profit=daily_net_profit,
                daily_return_pct=daily_return_pct, trades=trades,
            )
            chronological_results.append(result)
            results_by_row_pos[row_pos] = result

        updated_df = self._attach_columns(picks_df, results_by_row_pos)
        return chronological_results, updated_df

    @staticmethod
    def _attach_columns(picks_df: pd.DataFrame, results_by_row_pos: dict) -> pd.DataFrame:
        out = picks_df.copy()
        net_profits, return_pcts, details_json = [], [], []
        for row_pos in picks_df.index:
            d: Optional[DayResult] = results_by_row_pos.get(row_pos)
            if d is None:
                net_profits.append(None); return_pcts.append(None); details_json.append("[]")
                continue
            net_profits.append(d.daily_net_profit)
            return_pcts.append(d.daily_return_pct)
            details = [{"symbol": t.symbol, "entry": t.entry, "exit": t.exit,
                        "return_pct": t.return_pct, "net_pnl": t.net_pnl} for t in d.trades]
            details_json.append(json.dumps(details))
        out["daily_net_profit"] = net_profits
        out["daily_return_pct"] = return_pcts
        out["stock_performance_details"] = details_json
        return out


# ----------------------------------------------------------------------------
# Performance statistics
# ----------------------------------------------------------------------------
def compute_tear_sheet(chronological_day_results: list[DayResult], initial_capital: float,
                        annualization_factor: int = DEFAULT_ANNUALIZATION_FACTOR,
                        risk_free_rate_annual: float = DEFAULT_RISK_FREE_RATE_ANNUAL) -> TearSheet:
    """chronological_day_results MUST be in date order - see BacktestEngine.run()'s docstring."""
    all_trades = [t for d in chronological_day_results for t in d.trades]
    total_trades = len(all_trades)

    gross_profit = sum(t.net_pnl for t in all_trades if t.net_pnl > 0)
    gross_loss = sum(t.net_pnl for t in all_trades if t.net_pnl < 0)  # <= 0
    winners = sum(1 for t in all_trades if t.net_pnl > 0)
    win_rate_pct = (winners / total_trades * 100) if total_trades else 0.0

    if gross_loss < 0:
        profit_factor = gross_profit / abs(gross_loss)
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    equity = [initial_capital]
    for d in chronological_day_results:
        equity.append(equity[-1] + d.daily_net_profit)
    final_capital = equity[-1]
    net_gain = final_capital - initial_capital
    total_return_pct = (net_gain / initial_capital * 100) if initial_capital else 0.0

    running_peak = equity[0]
    max_dd_pct = 0.0
    for value in equity:
        running_peak = max(running_peak, value)
        if running_peak > 0:
            max_dd_pct = min(max_dd_pct, (value - running_peak) / running_peak * 100)

    daily_returns = [d.daily_return_pct / 100 for d in chronological_day_results]
    sharpe_ratio = _compute_sharpe(daily_returns, annualization_factor, risk_free_rate_annual)

    return TearSheet(
        initial_capital=initial_capital, final_capital=round(final_capital, 2),
        net_gain=round(net_gain, 2), total_return_pct=round(total_return_pct, 4),
        total_trades=total_trades, win_rate_pct=round(win_rate_pct, 2),
        gross_profit=round(gross_profit, 2), gross_loss=round(gross_loss, 2),
        profit_factor=round(profit_factor, 4) if math.isfinite(profit_factor) else profit_factor,
        max_drawdown_pct=round(max_dd_pct, 4), sharpe_ratio=round(sharpe_ratio, 4),
    )


def _compute_sharpe(daily_returns: list[float], annualization_factor: int, risk_free_rate_annual: float) -> float:
    n = len(daily_returns)
    if n < 2:
        logger.warning("Fewer than 2 days of returns - Sharpe ratio isn't meaningful, returning 0.0.")
        return 0.0
    daily_rf = risk_free_rate_annual / annualization_factor
    excess = [r - daily_rf for r in daily_returns]
    mean_excess = sum(excess) / n
    variance = sum((r - mean_excess) ** 2 for r in excess) / (n - 1)  # sample variance, ddof=1
    std = math.sqrt(variance)
    if std == 0:
        logger.warning("Zero return volatility - Sharpe ratio undefined, returning 0.0.")
        return 0.0
    return mean_excess / std * math.sqrt(annualization_factor)


def print_tear_sheet(ts: TearSheet) -> None:
    pf_display = "inf" if isinstance(ts.profit_factor, float) and math.isinf(ts.profit_factor) else f"{ts.profit_factor:.2f}"
    print("=" * 22 + " FINAL CAPITAL & PERFORMANCE " + "=" * 22)
    print(f"Initial Capital             : \u20b9{ts.initial_capital:,.2f}")
    print(f"Final Capital               : \u20b9{ts.final_capital:,.2f}")
    print(f"Net Capital Gain (P&L)      : \u20b9{ts.net_gain:,.2f}")
    print(f"Total Return (%)            : {ts.total_return_pct:.2f}%")
    print(f"Total Executed Trades       : {ts.total_trades}")
    print(f"Win Rate (%)                : {ts.win_rate_pct:.2f}%")
    print(f"Gross Profit / Loss         : \u20b9{ts.gross_profit:,.2f} / \u20b9{ts.gross_loss:,.2f}")
    print(f"Profit Factor               : {pf_display}")
    print(f"Max Portfolio Drawdown (%)  : {ts.max_drawdown_pct:.2f}%")
    print(f"Sharpe Ratio                : {ts.sharpe_ratio:.2f}")
    print("=" * 72)


# ----------------------------------------------------------------------------
# CSV I/O
# ----------------------------------------------------------------------------
def load_picks_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)  # read everything as string - avoids reformatting dates/numbers on write-back
    missing = {"date", "stocks"} - set(df.columns)
    if missing:
        raise ValueError(f"{path} is missing required column(s): {missing}")
    return df


def save_picks_csv(df: pd.DataFrame, path: str) -> None:
    # quoting=csv.QUOTE_MINIMAL (pandas' own default) correctly wraps any field containing
    # commas or quotes - both the comma-separated `stocks` column and the JSON
    # `stock_performance_details` column - in double quotes and escapes embedded quotes.
    # That's what keeps the file valid, re-readable CSV rather than "corrupting formatting".
    df.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def run_backtest_from_csv(
    csv_path: str,
    history_provider: HistoryProvider,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    cost_pct_per_side: float = DEFAULT_COST_PCT_PER_SIDE,
    cache_dir: str = DEFAULT_CACHE_DIR,
) -> TearSheet:
    picks_df = load_picks_csv(csv_path)
    if picks_df.empty:
        raise ValueError(f"{csv_path} has no rows to backtest.")

    all_symbols = sorted({
        s.strip() for stocks in picks_df["stocks"].astype(str) for s in stocks.split(",") if s.strip()
    })
    start_date, end_date = picks_df["date"].astype(str).str.strip().min(), picks_df["date"].astype(str).str.strip().max()
    logger.info("Backtesting %d date(s), %d unique symbol(s), range [%s, %s].",
                len(picks_df), len(all_symbols), start_date, end_date)

    cache = DailyPriceCache(history_provider, cache_dir=cache_dir)
    cache.warm(all_symbols, start_date, end_date)

    engine = BacktestEngine(cache, initial_capital=initial_capital,
                             cost_model=CostModel(pct_per_side=cost_pct_per_side))
    chronological_results, updated_df = engine.run(picks_df)

    save_picks_csv(updated_df, csv_path)
    logger.info("Wrote daily_net_profit / daily_return_pct / stock_performance_details back to %s", csv_path)

    tear_sheet = compute_tear_sheet(chronological_results, initial_capital)
    print_tear_sheet(tear_sheet)
    return tear_sheet


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    # ------------------------------------------------------------------
    # STUB: plug in YOUR already-authenticated Fyers client here.
    # ------------------------------------------------------------------
    # Option A (recommended) - this project's own rate-limited, retrying wrapper:
    #
    #   from fyers_service import FyersAuthManager, FyersDataService
    #   access_token = FyersAuthManager().get_access_token()
    #   history_provider = FyersDataService(access_token)   # already implements HistoryProvider
    #
    # Option B - you already have a raw, active fyers_apiv3 client from elsewhere
    # (e.g. built outside this project) and don't want to go through Option A's
    # auth flow. Wrap it in ~5 lines so it satisfies HistoryProvider:
    #
    #   from fyers_apiv3 import fyersModel
    #   fyers = fyersModel.FyersModel(client_id=YOUR_APP_ID, is_async=False,
    #                                  token=YOUR_ACCESS_TOKEN, log_path="")
    #
    #   class RawFyersAdapter:
    #       def get_candles_range(self, symbol, resolution, range_from_date, range_to_date):
    #           data = {"symbol": symbol, "resolution": resolution, "date_format": "1",
    #                    "range_from": range_from_date, "range_to": range_to_date, "cont_flag": "1"}
    #           return fyers.history(data=data).get("candles", [])
    #
    #   history_provider = RawFyersAdapter()
    #
    # Option B gets NO rate-limiting or retries of its own here - DailyPriceCache
    # calls get_candles_range() directly with no throttling built in on this side,
    # relying on the provider to be well-behaved (Option A already is). Add your
    # own rate limiting to the adapter if you go with Option B on a large universe.

    history_provider: HistoryProvider = ...  # <-- replace with Option A or B above

    run_backtest_from_csv(
        csv_path="strategy_picks.csv",
        history_provider=history_provider,
        initial_capital=200_000.0,
        cost_pct_per_side=0.05,
    )
