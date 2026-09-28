"""
backtest.py
------------
Replays the exact same opening-range + VWAP/volume/weakness-event signal
logic used live (signal_engine.SignalEngine, completely unmodified - see
its feed_candle() method) against historical Fyers data, one past trading
day at a time, and aggregates the results. Supports both directions -
LONG (the original gap-up screen) and SHORT (the mirrored gap-down screen) -
via --direction on the command line; a single run replays one fixed
direction across the whole date range (this tool does not decide direction
from sentiment the way bootstrap.py/main.py do live - run it twice, once
per direction, to compare both over the same period).

Design: this module only handles DATA - fetching each day's opening-range
shortlist and the full session's 5-min candles for each shortlisted stock -
and feeds that data through SignalEngine.feed_candle() in the same
5-minute-tick order live polling would have delivered it. If you change the
strategy in signal_engine.py, the backtest picks up the change
automatically; there's only one place the rules live, so a backtest result
can't drift out of sync with what actually runs live.

Fetch strategy (why this is fast rather than painfully slow)
-----------------------------------------------------------------
The naive approach - fetch each of the universe's ~500 stocks separately
for EVERY backtested day - means roughly 500 API calls PER DAY just to
build each day's opening-range shortlist. Over even a month of trading days
that's tens of thousands of calls.

Instead, for the whole requested date range, this module fetches each
universe symbol's FULL 5-min candle history and FULL daily-close history
ONCE (chunked to respect Fyers' per-request limits - see
config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK / DAILY_HISTORY_MAX_DAYS_PER_CHUNK,
sourced from Fyers' own docs/community forum), then slices out each day's
opening-range candle and previous close from that pre-fetched data in
memory. Total API calls end up roughly proportional to (universe size x
date-range-chunks), NOT (universe size x trading days) - so backtesting 5
days costs about the same as backtesting 60, as long as both fit inside one
chunk per symbol.

What this does NOT do
-----------------------
- Model slippage, brokerage, STT, or bid/ask spread - real fills will be
  worse than what you see here.
- Account for NSE trading holidays when generating the date range - it only
  skips weekends. A holiday just comes back with no opening-range candle for
  any stock and is silently skipped for that day (logged), not a crash, but
  you'll want a proper holiday calendar before trusting day-count-sensitive
  metrics.
- Avoid survivorship bias: it filters using TODAY's ind_nifty500list.csv and
  TODAY's sector_index_map.csv, so stocks that have since been removed from
  the index, or whose Industry classification has changed, are backtested
  as if today's universe/classification always applied. This tends to
  inflate apparent historical performance somewhat, since it only "sees"
  stocks that survived to today's list.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd
import pytz

import config
from fyers_service import FyersDataService
from opening_range_scanner import load_universe
from signal_engine import SignalEngine
from trade_direction import TradeDirection

logger = logging.getLogger("orb_agent.backtest")


def trading_days_between(start: date, end: date) -> list[date]:
    """Weekdays only - does NOT account for NSE trading holidays, see module docstring."""
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:  # Monday=0 .. Friday=4
            days.append(current)
        current += timedelta(days=1)
    return days


def _date_chunks(start: date, end: date, max_days_per_chunk: int) -> list[tuple[date, date]]:
    chunks = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=max_days_per_chunk - 1), end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return chunks


def _fetch_full_range_candles(data_service: FyersDataService, symbol: str, resolution: str,
                               start: date, end: date, max_days_per_chunk: int) -> list:
    """Fetches `resolution`-candles for [start, end], chunked to respect Fyers' per-request limit."""
    all_candles = []
    for chunk_start, chunk_end in _date_chunks(start, end, max_days_per_chunk):
        candles = data_service.get_candles_range(symbol, resolution, str(chunk_start), str(chunk_end))
        all_candles.extend(candles)
    return all_candles


def _candles_on_date(candles: list, target_date: date, tz) -> list:
    """Filters a symbol's full-range candle list down to just the ones on target_date, in order."""
    return [c for c in candles if datetime.fromtimestamp(c[0], tz).date() == target_date]


def _previous_close_on_date(daily_candles: list, target_date: date, tz) -> float | None:
    prior = [c for c in daily_candles if datetime.fromtimestamp(c[0], tz).date() < target_date]
    return prior[-1][4] if prior else None


def _latest_candle_time_str(per_symbol_day_candles: dict, tz) -> str | None:
    """The latest timestamp actually seen across all of a day's replayed candles, used as
    the EOD square-off time - see SignalEngine.square_off_all()'s docstring for why this
    matters (avoids stamping backtest exits with the real wall-clock time instead)."""
    latest_epoch = None
    for day_candles in per_symbol_day_candles.values():
        if day_candles:
            latest_epoch = max(latest_epoch or 0, day_candles[-1][0])
    if latest_epoch is None:
        return None
    return datetime.fromtimestamp(latest_epoch, tz).strftime("%H:%M:%S")


def _build_shortlist_for_date(symbols: list[str], intraday_by_symbol: dict, daily_by_symbol: dict,
                               target_date: date, tz, direction: TradeDirection = TradeDirection.LONG) -> pd.DataFrame:
    """Same filtering + gain-% logic as opening_range_scanner.scan_opening_range_accurate()
    (LONG gap-up band, or the mirrored SHORT gap-down band), but sliced from pre-fetched
    bulk data instead of making fresh API calls per day."""
    open_time = datetime.strptime(config.MARKET_OPEN_TIME, "%H:%M:%S").time()

    rows = []
    for symbol in symbols:
        day_candles = _candles_on_date(intraday_by_symbol.get(symbol, []), target_date, tz)
        if not day_candles:
            continue
        or_candle = day_candles[0]
        candle_time = datetime.fromtimestamp(or_candle[0], tz).time()
        if candle_time != open_time:
            logger.warning("%s on %s: first candle starts at %s, not market open (%s) - "
                            "data may have a gap; using it as the opening-range candle anyway.",
                            symbol, target_date, candle_time, open_time)

        _, o, h, l, c, v = or_candle
        prev_close = _previous_close_on_date(daily_by_symbol.get(symbol, []), target_date, tz)
        if not prev_close:
            continue

        pct_change = (c - prev_close) / prev_close * 100
        if direction == TradeDirection.LONG:
            passes = config.MIN_GAIN_PCT < pct_change <= config.MAX_GAIN_PCT
        else:
            passes = -config.MAX_LOSS_PCT <= pct_change < -config.MIN_LOSS_PCT
        if passes:
            rows.append({
                "symbol": symbol, "open": o, "high": h, "low": l, "close": c,
                "volume": v, "prev_close": prev_close, "pct_change": round(pct_change, 2),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # LONG wants the biggest gainers first; SHORT wants the biggest losers (most negative) first.
    df = df.sort_values("pct_change", ascending=(direction == TradeDirection.SHORT))
    return df.head(config.TOP_N_STOCKS).reset_index(drop=True)


def _run_backtest_for_date(data_service: FyersDataService, target_date: date, symbols: list[str],
                            intraday_by_symbol: dict, daily_by_symbol: dict, tz,
                            direction: TradeDirection = TradeDirection.LONG) -> dict:
    shortlist = _build_shortlist_for_date(symbols, intraday_by_symbol, daily_by_symbol, target_date, tz, direction)
    if shortlist.empty:
        logger.info("%s: no %s opening-range shortlist - skipping (holiday, or no stock matched the band).",
                    target_date, direction.value)
        return {"date": target_date, "shortlist": shortlist, "trades": pd.DataFrame(), "summary": {}}

    logger.info("%s: %s shortlist -> %s", target_date, direction.value, list(shortlist["symbol"]))
    engine = SignalEngine(data_service, shortlist, direction=direction)

    per_symbol_day_candles = {
        row["symbol"]: _candles_on_date(intraday_by_symbol.get(row["symbol"], []), target_date, tz)
        for _, row in shortlist.iterrows()
    }
    max_len = max((len(c) for c in per_symbol_day_candles.values()), default=0)

    # Start at index 1: index 0 is the opening-range candle itself, already consumed above to
    # build the shortlist - matches live, where signal-checking only begins on the FIRST
    # candle after the opening range.
    for i in range(1, max_len):
        for symbol, day_candles in per_symbol_day_candles.items():
            if i >= len(day_candles):
                continue
            candle = day_candles[i]
            candle_time_str = datetime.fromtimestamp(candle[0], tz).strftime("%H:%M:%S")
            engine.feed_candle(symbol, candle, candle_time_str)

    engine.square_off_all(at_time_str=_latest_candle_time_str(per_symbol_day_candles, tz))
    trades_df = engine.trade_book.as_dataframe()
    if not trades_df.empty:
        trades_df.insert(0, "date", str(target_date))
    return {"date": target_date, "shortlist": shortlist, "trades": trades_df, "summary": engine.trade_book.summary()}


def run_backtest(data_service: FyersDataService, start_date: date, end_date: date,
                  direction: TradeDirection = TradeDirection.LONG) -> dict:
    """
    Runs the strategy over every weekday in [start_date, end_date], in EITHER
    direction (LONG or SHORT - see trade_direction.py; this does NOT decide
    direction from sentiment the way bootstrap.py/main.py do live, since a
    backtest replays one fixed direction across the whole range by design -
    pass --direction on the command line, or call this twice, once per
    direction, if you want to compare both over the same period), and
    returns {"trades": combined trade-book DataFrame, "daily_summary":
    per-day DataFrame, "overall": aggregate dict}.
    """
    symbols = load_universe()
    trading_days = trading_days_between(start_date, end_date)
    if not trading_days:
        logger.warning("No weekdays in the requested range (%s to %s).", start_date, end_date)
        return {"trades": pd.DataFrame(), "daily_summary": pd.DataFrame(), "overall": {}}

    intraday_chunks = len(_date_chunks(start_date, end_date, config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK))
    daily_lookback_start = start_date - timedelta(days=20)  # buffer so day 1 has a "previous close"
    daily_chunks = len(_date_chunks(daily_lookback_start, end_date, config.DAILY_HISTORY_MAX_DAYS_PER_CHUNK))
    estimated_calls = len(symbols) * (intraday_chunks + daily_chunks)
    logger.info("Pre-fetching %d-symbol universe history for %d trading day(s) (%s to %s). "
                "Estimated ~%d API calls (~%.0f seconds at %d calls/sec) - this is the slow part.",
                len(symbols), len(trading_days), start_date, end_date,
                estimated_calls, estimated_calls / config.FYERS_MAX_CALLS_PER_SECOND,
                config.FYERS_MAX_CALLS_PER_SECOND)

    intraday_by_symbol: dict[str, list] = {}
    daily_by_symbol: dict[str, list] = {}
    for idx, symbol in enumerate(symbols, start=1):
        intraday_by_symbol[symbol] = _fetch_full_range_candles(
            data_service, symbol, "5", start_date, end_date, config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK)
        daily_by_symbol[symbol] = _fetch_full_range_candles(
            data_service, symbol, "D", daily_lookback_start, end_date, config.DAILY_HISTORY_MAX_DAYS_PER_CHUNK)
        if idx % 50 == 0 or idx == len(symbols):
            logger.info("Pre-fetch progress: %d/%d symbols.", idx, len(symbols))

    tz = pytz.timezone(config.TIMEZONE)
    all_trades = []
    daily_summaries = []
    for d in trading_days:
        result = _run_backtest_for_date(data_service, d, symbols, intraday_by_symbol, daily_by_symbol,
                                         tz, direction=direction)
        if not result["trades"].empty:
            all_trades.append(result["trades"])
        daily_summaries.append({"date": str(d), **result["summary"]})

    combined_trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    daily_summary_df = pd.DataFrame(daily_summaries)

    overall = {
        "days_tested": len(daily_summaries),
        "days_with_trades": (int((daily_summary_df["total_trades"] > 0).sum())
                              if "total_trades" in daily_summary_df else 0),
        "total_trades": int(combined_trades.shape[0]) if not combined_trades.empty else 0,
        "total_points": round(float(combined_trades["points"].sum()), 2) if not combined_trades.empty else 0.0,
        "win_rate_pct": (round(float((combined_trades["points"] > 0).mean() * 100), 2)
                          if not combined_trades.empty else 0.0),
    }
    return {"trades": combined_trades, "daily_summary": daily_summary_df, "overall": overall}


if __name__ == "__main__":
    import argparse

    from fyers_service import FyersAuthManager

    parser = argparse.ArgumentParser(description="Backtest the ORB strategy (LONG or SHORT) over a historical date range.")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--direction", default="LONG", choices=["LONG", "SHORT"],
                         help="Which side to backtest (default LONG). This tool doesn't decide "
                              "direction from sentiment the way the live agent does - it replays "
                              "one fixed direction across the whole range.")
    parser.add_argument("--out", default=None, help="Optional path to save the combined trade log as CSV")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    direction = TradeDirection[args.direction]

    auth = FyersAuthManager()
    access_token = auth.get_access_token()
    data_service = FyersDataService(access_token)

    results = run_backtest(data_service, start, end, direction=direction)

    print("\n=== Daily Summary ===")
    print(results["daily_summary"].to_string(index=False) if not results["daily_summary"].empty
          else "No trading days in range.")
    print("\n=== Overall ===")
    print(results["overall"])

    if args.out:
        results["trades"].to_csv(args.out, index=False)
        print(f"\nSaved combined trade log to {args.out}")
