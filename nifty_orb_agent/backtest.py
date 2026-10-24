"""
backtest.py
------------
Replays the exact same opening-range + VWAP/volume/weakness-event signal
logic used live (signal_engine.SignalEngine, completely unmodified - see
its feed_candle() method) against historical Fyers data, one past trading
day at a time, and aggregates the results.

Direction is decided PER DAY from historical global sentiment, exactly the
way bootstrap.py/main.py do live (via bootstrap.decide_direction_for_date()) -
not one fixed direction for the whole range. This matters for the backtest
to mean anything: the deployed strategy switches between LONG and SHORT
daily based on sentiment, so a backtest that only ever replays one side
is testing a DIFFERENT, simpler strategy than what actually runs live. Pass
--direction to override this and force one fixed side across the whole
range instead (useful for deliberately comparing "what if this had always
traded LONG" against the real, sentiment-gated history) - but that is now
an explicit opt-in, not the default.

Also computes and logs the sector leaderboard for each day, from the same
historical daily-candle data already being fetched for the previous-close
calculation - the historical counterpart to sector_scanner.get_sector_leaderboard(),
for parity with what the live system's sector scan reports.

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

The intraday fetch range is extended INTRADAY_SEED_LOOKBACK_DAYS calendar
days before start_date too, purely so day 1 of the backtest has real
prior-session candles available to seed its volume-MA windows from (see
signal_engine.seed_volume_windows_from_candles) - without this, every
backtested day would understate activity in its first ~100 minutes, the
same bug this fixes live (see bootstrap.fetch_and_seed_volume_windows).

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
- Cache the per-day historical sentiment fetch (fetch_global_snapshot_as_of)
  across runs - each yfinance call is independent of Fyers' own rate limit,
  but a long date range still means one small burst of yfinance calls per
  day being backtested; expect this to be the slower part of a long
  multi-month backtest, not the Fyers data pre-fetch.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import pytz, sys 

import config
from bootstrap import decide_commodity_direction_for_date, decide_direction_for_date
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fyersclient.fyers_client as fyres_client
from fyersclient.fyers_client import get_fyers_client
from opening_range_scanner import _passes_wick_filter, or_candle_wick_pct, load_universe
from sector_scanner import load_sector_map
from signal_engine import SignalEngine, seed_volume_windows_from_candles
from trade_direction import TradeDirection
from sector_scanner import get_index_snapshot, get_sector_leaderboard
from pathlib import Path

logger = logging.getLogger("orb_agent.backtest")

INTRADAY_SEED_LOOKBACK_DAYS = 7  # calendar days of extra intraday history fetched before
                                  # start_date, purely to seed day 1's volume-MA windows


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
        if candles:
            all_candles.extend(candles)
    return all_candles


def _candles_on_date(candles: list, target_date: date, tz) -> list:
    """Filters a symbol's full-range candle list down to just the ones on target_date, in order."""
    return [c for c in candles if datetime.fromtimestamp(c[0], tz).date() == target_date]


def _previous_close_on_date(daily_candles: list, target_date: date, tz) -> float | None:
    prior = [c for c in daily_candles if datetime.fromtimestamp(c[0], tz).date() < target_date]
    return prior[-1][4] if prior else None


def _seed_candles_before_date(candles: list, target_date: date, tz, needed: int) -> list:
    """
    Returns up to `needed` of the most recent candles STRICTLY BEFORE
    target_date, in chronological order - used to seed a fresh
    per-backtest-day SignalEngine's volume-MA windows from the prior
    session(s), sliced out of data already pre-fetched for the whole
    backtest range (zero extra API calls). See
    signal_engine.seed_volume_windows_from_candles for what this fixes.
    """
    prior = sorted((c for c in candles if datetime.fromtimestamp(c[0], tz).date() < target_date),
                   key=lambda c: c[0])
    return prior[-needed:] if prior else []


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
        # Same opening-range "closing strength" screen live trading applies (see
        # opening_range_scanner.scan_opening_range/scan_symbols_accurate and config.
        # MAX_OR_CANDLE_WICK_PCT's docs) - kept in lockstep here so a backtest's shortlist
        # never silently diverges from what live trading would actually have screened for.
        passes = passes and _passes_wick_filter(o, h, l, c, direction)
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


def _compute_historical_sector_leaderboard(sector_daily_by_symbol: dict, sector_map: pd.DataFrame,
                                            target_date: date, tz) -> pd.DataFrame:
    """
    Historical counterpart to sector_scanner.get_sector_leaderboard(): the
    same is_sector_index-filtered, pct-change-ranked view, but computed from
    pre-fetched DAILY candles for each sector index symbol (today's close
    vs. the prior trading day's close) instead of live quotes - there's no
    "live quote" for a historical date, so this is the only way to get a
    per-day sector leaderboard for a backtest.
    """
    if sector_map is None or sector_map.empty:
        return pd.DataFrame()

    rows = []
    for _, row in sector_map[sector_map["is_sector_index"]].iterrows():
        symbol = row["symbol"]
        daily_candles = sector_daily_by_symbol.get(symbol, [])
        today_candles = _candles_on_date(daily_candles, target_date, tz)
        if not today_candles:
            continue
        today_close = today_candles[0][4]
        prev_close = _previous_close_on_date(daily_candles, target_date, tz)
        if not prev_close:
            continue
        pct_change = (today_close - prev_close) / prev_close * 100
        rows.append({"symbol": symbol, "index_name": row["index_name"], "pct_change": round(pct_change, 2)})

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("pct_change", ascending=False).reset_index(drop=True)


def _run_backtest_for_date(data_service: FyersDataService, target_date: date, symbols: list[str],
                            intraday_by_symbol: dict, daily_by_symbol: dict, tz,
                            direction: TradeDirection = TradeDirection.LONG,
                            sector_map: Optional[pd.DataFrame] = None,
                            sector_daily_by_symbol: Optional[dict] = None) -> dict:
    sector_leaderboard = _compute_historical_sector_leaderboard(sector_daily_by_symbol or {}, sector_map, target_date, tz)
    if not sector_leaderboard.empty:
        top3 = sector_leaderboard.head(3)[["index_name", "pct_change"]].to_dict("records")
        logger.info("%s: sector leaderboard (top 3) -> %s", target_date, top3)

    shortlist = _build_shortlist_for_date(symbols, intraday_by_symbol, daily_by_symbol, target_date, tz, direction)
    if shortlist.empty:
        logger.info("%s: no %s opening-range shortlist - skipping (holiday, or no stock matched the band).",
                    target_date, direction.value)
        return {"date": target_date, "shortlist": shortlist, "trades": pd.DataFrame(), "summary": {},
                "sector_leaderboard": sector_leaderboard}

    logger.info("%s: %s shortlist -> %s", target_date, direction.value, list(shortlist["symbol"]))
    engine = SignalEngine(data_service, shortlist, direction=direction)

    # Seed volume-MA windows from the prior session(s), sliced out of the SAME pre-fetched
    # intraday data (zero extra API calls) - without this, entries in the first ~100 minutes
    # of every backtested day would be silently suppressed (volume MA stays None). See
    # signal_engine.seed_volume_windows_from_candles's docstring for the full story.
    seed_candles_by_symbol = {
        row["symbol"]: _seed_candles_before_date(intraday_by_symbol.get(row["symbol"], []), target_date, tz,
                                                  config.VOLUME_MA_PERIOD)
        for _, row in shortlist.iterrows()
    }
    seed_volume_windows_from_candles(engine.trackers, seed_candles_by_symbol)

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
    return {"date": target_date, "shortlist": shortlist, "trades": trades_df, "summary": engine.trade_book.summary(),
            "sector_leaderboard": sector_leaderboard}


def _prefetch_equity_history(data_service: FyersDataService, start_date: date, end_date: date) -> dict:
    """
    Pre-fetches EVERYTHING run_backtest() needs for [start_date, end_date] in one pass - the
    stock universe's full intraday + daily candle history, and the sector-index daily history -
    exactly as run_backtest() used to do inline. Factored out so the SAME pre-fetched data can be
    replayed more than once (see sweep_or_candle_wick_threshold() below, which reruns the local,
    API-call-free replay step once per candidate opening-range wick-filter threshold against this
    one pre-fetch) without ever re-hitting Fyers - re-fetching per threshold would multiply the
    already-described API-call cost by the number of thresholds tried, for data that didn't change.

    Returns a bundle dict consumed by _run_backtest_days() below; an empty/no-op bundle
    (empty "trading_days") if the range has no weekdays at all.
    """
    snapshot = get_index_snapshot(data_service)
    if snapshot.empty:
        logger.debug(" No data from get_index_snapshot()")
        exit()
    
    leaderboard = get_sector_leaderboard(snapshot)
    symbols = []
    
    logger.info("Backtesting strategy from %s to %s.", start_date, end_date)
    logger.info(f" leader board {leaderboard.head(5)}")
    symbols = load_universe(leaderboard.head(5))  
    trading_days = trading_days_between(start_date, end_date)
    tz = pytz.timezone(config.TIMEZONE)
    if not trading_days:
        logger.warning("No weekdays in the requested range (%s to %s).", start_date, end_date)
        return {"symbols": symbols, "trading_days": [], "intraday_by_symbol": {}, "daily_by_symbol": {},
                "sector_map": pd.DataFrame(), "sector_daily_by_symbol": {}, "tz": tz}

    intraday_fetch_start = start_date - timedelta(days=INTRADAY_SEED_LOOKBACK_DAYS)
    intraday_chunks = len(_date_chunks(intraday_fetch_start, end_date, config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK))
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
            data_service, symbol, "5", intraday_fetch_start, end_date, config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK)
        daily_by_symbol[symbol] = _fetch_full_range_candles(
            data_service, symbol, "D", daily_lookback_start, end_date, config.DAILY_HISTORY_MAX_DAYS_PER_CHUNK)
        if idx % 50 == 0 or idx == len(symbols):
            logger.info("Pre-fetch progress: %d/%d symbols.", idx, len(symbols))

    # Sector index daily candles, for the per-day historical sector leaderboard - same
    # date range/chunking as the stock universe's daily fetch, but a much smaller symbol
    # set (only the is_sector_index=True rows), so this is a small incremental cost.
    sector_map = load_sector_map()
    sector_index_symbols = (sector_map[sector_map["is_sector_index"]]["symbol"].tolist()
                             if not sector_map.empty else [])
    sector_daily_by_symbol: dict[str, list] = {}
    for symbol in sector_index_symbols:
        sector_daily_by_symbol[symbol] = _fetch_full_range_candles(
            data_service, symbol, "D", daily_lookback_start, end_date, config.DAILY_HISTORY_MAX_DAYS_PER_CHUNK)

    return {"symbols": symbols, "trading_days": trading_days, "intraday_by_symbol": intraday_by_symbol,
            "daily_by_symbol": daily_by_symbol, "sector_map": sector_map,
            "sector_daily_by_symbol": sector_daily_by_symbol, "tz": tz}


def _run_backtest_days(data_service: FyersDataService, prefetched: dict,
                        direction: Optional[TradeDirection]) -> tuple[list, list, dict]:
    """
    Replays every pre-fetched trading day (see _prefetch_equity_history() above) and returns the
    raw per-day results - PURE LOCAL COMPUTATION (shortlist filtering, signal replay, P&L) with NO
    further API calls, which is what lets sweep_or_candle_wick_threshold() call this once per
    candidate config.MAX_OR_CANDLE_WICK_PCT value against the SAME pre-fetched data at zero extra
    Fyers cost. `data_service` is only threaded through to SignalEngine's constructor (unused
    during a backtest replay - every candle is fed directly, never fetched live) for parity with
    the live construction path.

    Returns (all_trades: list[DataFrame], daily_summaries: list[dict],
    sector_leaderboards: {date_str: DataFrame}) - the same three accumulators run_backtest() used
    to build inline, now shared with the sweep.
    """
    symbols = prefetched["symbols"]
    trading_days = prefetched["trading_days"]
    intraday_by_symbol = prefetched["intraday_by_symbol"]
    daily_by_symbol = prefetched["daily_by_symbol"]
    sector_map = prefetched["sector_map"]
    sector_daily_by_symbol = prefetched["sector_daily_by_symbol"]
    tz = prefetched["tz"]

    all_trades = []
    daily_summaries = []
    sector_leaderboards: dict[str, pd.DataFrame] = {}
    forced_direction = direction is not None

    for d in trading_days:
        if forced_direction:
            day_direction, sentiment_label, sentiment_score = direction, "FORCED", 0.0
        else:
            day_direction, sentiment_label, sentiment_score = decide_direction_for_date(d)

        if day_direction is None:
            logger.info("%s: sentiment is %s (score %.2f) - no clear direction and no "
                        "ALLOW_LONG_ON_NEUTRAL/ALLOW_SHORT_ON_NEUTRAL fallback configured, "
                        "skipping (no trades today, matching what live trading would do).",
                        d, sentiment_label, sentiment_score)
            daily_summaries.append({"date": str(d), "direction": None, "sentiment": sentiment_label,
                                     "sentiment_score": sentiment_score,
                                     "total_trades": 0, "total_points": 0.0, "win_rate_pct": 0.0})
            continue

        result = _run_backtest_for_date(data_service, d, symbols, intraday_by_symbol, daily_by_symbol, tz,
                                         direction=day_direction, sector_map=sector_map,
                                         sector_daily_by_symbol=sector_daily_by_symbol)
        if not result["trades"].empty:
            all_trades.append(result["trades"])
        daily_summaries.append({"date": str(d), "direction": day_direction.value, "sentiment": sentiment_label,
                                 "sentiment_score": sentiment_score, **result["summary"]})
        sector_leaderboards[str(d)] = result["sector_leaderboard"]

    return all_trades, daily_summaries, sector_leaderboards


def _aggregate_backtest_results(all_trades: list, daily_summaries: list,
                                 sector_leaderboards: Optional[dict] = None) -> dict:
    """Turns _run_backtest_days()'s raw accumulators into the public {"trades", "daily_summary",
    "sector_leaderboards", "overall"} shape - the exact aggregation run_backtest() always did
    inline, now shared with sweep_or_candle_wick_threshold()."""
    combined_trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    daily_summary_df = pd.DataFrame(daily_summaries)

    overall = {
        "days_tested": len(daily_summaries),
        "days_skipped_no_direction": int(daily_summary_df["direction"].isna().sum()) if "direction" in daily_summary_df else 0,
        "days_with_trades": (int((daily_summary_df["total_trades"] > 0).sum())
                              if "total_trades" in daily_summary_df else 0),
        "total_trades": int(combined_trades.shape[0]) if not combined_trades.empty else 0,
        "total_points": round(float(combined_trades["points"].sum()), 2) if not combined_trades.empty else 0.0,
        "win_rate_pct": (round(float((combined_trades["points"] > 0).mean() * 100), 2)
                          if not combined_trades.empty else 0.0),
    }
    result = {"trades": combined_trades, "daily_summary": daily_summary_df, "overall": overall}
    if sector_leaderboards is not None:
        result["sector_leaderboards"] = sector_leaderboards
    return result


def run_backtest(data_service: FyersDataService, start_date: date, end_date: date,
                  direction: Optional[TradeDirection] = None) -> dict:
    """
    Runs the strategy over every weekday in [start_date, end_date].

    direction=None (the default): computed FRESH FOR EACH DAY from
    historical global sentiment as of that day's pre-market
    (bootstrap.decide_direction_for_date), exactly matching what live
    trading would have chosen - a day with no clear sentiment read (and no
    config.ALLOW_LONG_ON_NEUTRAL / ALLOW_SHORT_ON_NEUTRAL fallback
    configured) is skipped entirely, same as live.

    direction=<a fixed TradeDirection>: overrides the per-day decision and
    replays that ONE direction across the whole range regardless of
    sentiment - an explicit opt-in for comparing "what if this had always
    traded LONG/SHORT" against the real, sentiment-gated history; not the
    default, since it tests a different (simpler) strategy than what's
    actually deployed.

    Returns {"trades": combined trade-book DataFrame, "daily_summary":
    per-day DataFrame (includes the direction and sentiment actually used
    each day), "sector_leaderboards": {date_str: DataFrame}, "overall": aggregate dict}.
    """
    prefetched = _prefetch_equity_history(data_service, start_date, end_date)
    if not prefetched["trading_days"]:
        return {"trades": pd.DataFrame(), "daily_summary": pd.DataFrame(), "sector_leaderboards": {}, "overall": {}}

    all_trades, daily_summaries, sector_leaderboards = _run_backtest_days(data_service, prefetched, direction)
    return _aggregate_backtest_results(all_trades, daily_summaries, sector_leaderboards)


def sweep_or_candle_wick_threshold(data_service: FyersDataService, start_date: date, end_date: date,
                                    thresholds: Optional[list[float]] = None,
                                    direction: Optional[TradeDirection] = None) -> pd.DataFrame:
    """
    Finds the empirically-best config.MAX_OR_CANDLE_WICK_PCT (see that config value's docs for
    what it means) for YOUR equity universe, date range, and sentiment configuration, by
    replaying the SAME pre-fetched historical data once per candidate threshold and comparing
    results - this is the "optimum" referenced there, not a fixed number this codebase could
    hand you, since it depends on your actual historical performance.

    IMPORTANT: config.MAX_OR_CANDLE_WICK_PCT is ONE shared threshold now - NIFTY/BANKNIFTY
    options (options_engine.py) and MCX commodities (commodity_engine.py) read this exact same
    value live, not a separate one. This sweep only ever replays the EQUITY backtest path
    (_run_backtest_days()), so whatever threshold you pick from these results is what options
    and MCX will ALSO use the moment you set it - there's no equivalent MCX-only or
    options-only sweep yet. If your equity, options, and MCX universes have meaningfully
    different wick/volatility behavior, sanity-check the value you land on against
    backtest.run_mcx_backtest() results too before committing to it everywhere.

    The expensive part - fetching every universe symbol's full intraday + daily history for the
    date range - happens EXACTLY ONCE (via _prefetch_equity_history()), no matter how many
    thresholds are tried; only the cheap, local replay step (shortlist filtering + signal replay)
    repeats per threshold. This matters because the naive approach (calling run_backtest() once
    per threshold) would multiply the Fyers API-call cost by len(thresholds) for data that never
    changes between runs.

    thresholds defaults to [10, 20, 30, 40, 50, 60, 70, 80, 90, 100] - 100 is config.
    MAX_OR_CANDLE_WICK_PCT's own default (a no-op, i.e. "no wick filtering at all"), included so
    the sweep's own output shows you the filtered thresholds' effect RELATIVE TO the unfiltered
    baseline you're already running, not just relative to each other.

    direction has the same meaning as run_backtest()'s own `direction` parameter (None = per-day
    sentiment-driven, matching live; a fixed TradeDirection = force one side for the whole sweep).

    Returns a DataFrame, one row per threshold, sorted by threshold ascending, with columns
    ["wick_threshold_pct", "days_tested", "days_skipped_no_direction", "days_with_trades",
    "total_trades", "total_points", "win_rate_pct", "avg_points_per_trade"] - sort it by
    "total_points" or "win_rate_pct" yourself to see which threshold actually performed best
    (and sanity-check "total_trades" alongside it - a threshold so tight it only let 2 trades
    through for the whole range isn't a reliable "winner" even if both happened to win).

    IMPORTANT: this does NOT permanently change config.MAX_OR_CANDLE_WICK_PCT - it's restored to
    whatever it was before this function was called (even if it raises) once the sweep finishes.
    Set config.MAX_OR_CANDLE_WICK_PCT yourself afterwards to the value you pick.
    """
    if thresholds is None:
        thresholds = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

    prefetched = _prefetch_equity_history(data_service, start_date, end_date)
    if not prefetched["trading_days"]:
        return pd.DataFrame(columns=["wick_threshold_pct", "days_tested", "days_skipped_no_direction",
                                      "days_with_trades", "total_trades", "total_points", "win_rate_pct",
                                      "avg_points_per_trade"])

    original_threshold = config.MAX_OR_CANDLE_WICK_PCT
    rows = []
    try:
        for threshold in sorted(thresholds):
            config.MAX_OR_CANDLE_WICK_PCT = threshold
            all_trades, daily_summaries, _ = _run_backtest_days(data_service, prefetched, direction)
            agg = _aggregate_backtest_results(all_trades, daily_summaries)
            overall = agg["overall"]
            total_trades = overall.get("total_trades", 0)
            total_points = overall.get("total_points", 0.0)
            rows.append({
                "wick_threshold_pct": threshold,
                "days_tested": overall.get("days_tested", 0),
                "days_skipped_no_direction": overall.get("days_skipped_no_direction", 0),
                "days_with_trades": overall.get("days_with_trades", 0),
                "total_trades": total_trades,
                "total_points": total_points,
                "win_rate_pct": overall.get("win_rate_pct", 0.0),
                "avg_points_per_trade": round(total_points / total_trades, 2) if total_trades else 0.0,
            })
            logger.info("Wick threshold sweep: %.0f%% -> %d trade(s), %.2f total points, %.1f%% win rate.",
                        threshold, total_trades, total_points, overall.get("win_rate_pct", 0.0))
    finally:
        config.MAX_OR_CANDLE_WICK_PCT = original_threshold

    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# MCX commodity backtesting
# ----------------------------------------------------------------------------
# Mirrors the equity functions above, but MUCH simpler - no sector scan and no gain/loss-band
# shortlist (see commodity_engine.py's module docstring): config.MCX_COMMODITY_UNIVERSE IS
# the universe, traded directly and unconditionally every day, at config.
# MCX_CANDLE_INTERVAL_MINUTES resolution instead of equities' fixed 5-min bars (Task #6).
def _build_mcx_shortlist_for_date(symbols: list[str], intraday_by_symbol: dict,
                                   target_date: date, tz,
                                   direction: TradeDirection = TradeDirection.LONG) -> pd.DataFrame:
    """MCX counterpart to _build_shortlist_for_date() above - still NO gain/loss-band filtering
    and NO previous-close lookup (see commodity_engine.fetch_mcx_universe_opening_ranges, whose
    live logic this mirrors, sliced from pre-fetched data instead of fresh API calls), but DOES
    apply the SAME opening-range closing-strength/wick filter that live MCX trading now does -
    config.MAX_OR_CANDLE_WICK_PCT, the SAME shared threshold equities/options use (a no-op by
    default) - against `direction`, so a backtest's MCX universe never silently diverges from
    what live screening would do."""
    rows = []
    for symbol in symbols:
        day_candles = _candles_on_date(intraday_by_symbol.get(symbol, []), target_date, tz)
        if not day_candles:
            continue
        _, o, h, l, c, v = day_candles[0]
        if or_candle_wick_pct(o, h, l, c, direction) > config.MAX_OR_CANDLE_WICK_PCT:
            continue
        rows.append({"symbol": symbol, "open": o, "high": h, "low": l, "close": c, "volume": v})
    return pd.DataFrame(rows)


def _run_mcx_backtest_for_date(data_service: FyersDataService, target_date: date, symbols: list[str],
                                intraday_by_symbol: dict, tz, direction: TradeDirection,
                                lot_sizes: dict[str, int], capital: float) -> dict:
    import position_sizing  # local import: avoids a module-level dependency the equity path
                             # above doesn't need - matches commodity_engine.py's own style.

    shortlist = _build_mcx_shortlist_for_date(symbols, intraday_by_symbol, target_date, tz, direction=direction)
    if shortlist.empty:
        logger.info("%s: no MCX opening-range candle available for any universe symbol - skipping "
                    "(holiday, or a data gap).", target_date)
        return {"date": target_date, "shortlist": shortlist, "trades": pd.DataFrame(), "summary": {}}

    logger.info("%s: MCX %s universe -> %s", target_date, direction.value, list(shortlist["symbol"]))
    engine = SignalEngine(data_service, shortlist, direction=direction,
                           candle_resolution=str(config.MCX_CANDLE_INTERVAL_MINUTES))

    per_slot_capital = position_sizing.capital_per_slot(capital, len(engine.trackers))
    for symbol, tracker in engine.trackers.items():
        tracker.lot_size = lot_sizes.get(symbol, 1)
        tracker.capital_allocated = per_slot_capital

    # Seed volume-MA windows from the prior session(s), sliced out of the SAME pre-fetched
    # intraday data (zero extra API calls) - identical reasoning to the equity path above.
    seed_candles_by_symbol = {
        row["symbol"]: _seed_candles_before_date(intraday_by_symbol.get(row["symbol"], []), target_date, tz,
                                                  config.VOLUME_MA_PERIOD)
        for _, row in shortlist.iterrows()
    }
    seed_volume_windows_from_candles(engine.trackers, seed_candles_by_symbol)

    per_symbol_day_candles = {
        row["symbol"]: _candles_on_date(intraday_by_symbol.get(row["symbol"], []), target_date, tz)
        for _, row in shortlist.iterrows()
    }
    max_len = max((len(c) for c in per_symbol_day_candles.values()), default=0)

    # Start at index 1: index 0 is the opening-range candle itself, already seeded into
    # SignalEngine's VWAP above (see signal_engine.seed_opening_range_candle) - matches live.
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


def run_mcx_backtest(data_service: FyersDataService, start_date: date, end_date: date,
                      direction: Optional[TradeDirection] = None) -> dict:
    """
    MCX commodity counterpart to run_backtest() above - backtests config.MCX_COMMODITY_UNIVERSE
    directly (no sector/universe scan, no gain/loss-band shortlist - see commodity_engine.py's
    module docstring) over every weekday in [start_date, end_date], at config.
    MCX_CANDLE_INTERVAL_MINUTES resolution (see config.py/Task #6) rather than equities' fixed
    5-min bars.

    direction=None (the default): decided FRESH FOR EACH DAY from historical COMMODITY
    sentiment (bootstrap.decide_commodity_direction_for_date - gold/silver/crude/copper/
    natural-gas/dollar-index cues), exactly matching what live MCX trading would have chosen -
    deliberately NOT equities' decide_direction_for_date() (see that function's docstring for
    why the two sentiment reads are kept separate).

    direction=<a fixed TradeDirection>: overrides the per-day decision, same opt-in semantics
    as run_backtest()'s own `direction` parameter - replays one fixed side across the whole
    range regardless of commodity sentiment.

    Returns the same shape as run_backtest() minus "sector_leaderboards" (an NSE-equity-only
    concept with no MCX analogue): {"trades", "daily_summary", "overall"}.
    """
    symbols = [row["symbol"] for row in config.MCX_COMMODITY_UNIVERSE]
    lot_sizes = {row["symbol"]: int(row["lot_size"]) for row in config.MCX_COMMODITY_UNIVERSE}
    trading_days = trading_days_between(start_date, end_date)
    if not trading_days:
        logger.warning("No weekdays in the requested range (%s to %s).", start_date, end_date)
        return {"trades": pd.DataFrame(), "daily_summary": pd.DataFrame(), "overall": {}}

    resolution = str(config.MCX_CANDLE_INTERVAL_MINUTES)
    intraday_fetch_start = start_date - timedelta(days=INTRADAY_SEED_LOOKBACK_DAYS)
    intraday_chunks = len(_date_chunks(intraday_fetch_start, end_date, config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK))
    estimated_calls = len(symbols) * intraday_chunks
    logger.info("Pre-fetching %d-symbol MCX universe history (%s-min resolution) for %d trading "
                "day(s) (%s to %s). Estimated ~%d API calls.",
                len(symbols), resolution, len(trading_days), start_date, end_date, estimated_calls)

    intraday_by_symbol: dict[str, list] = {}
    for idx, symbol in enumerate(symbols, start=1):
        intraday_by_symbol[symbol] = _fetch_full_range_candles(
            data_service, symbol, resolution, intraday_fetch_start, end_date,
            config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK)
        logger.info("MCX pre-fetch progress: %d/%d symbols.", idx, len(symbols))

    tz = pytz.timezone(config.TIMEZONE)
    all_trades = []
    daily_summaries = []
    forced_direction = direction is not None

    for d in trading_days:
        if forced_direction:
            day_direction, sentiment_label, sentiment_score = direction, "FORCED", 0.0
        else:
            day_direction, sentiment_label, sentiment_score = decide_commodity_direction_for_date(d)

        if day_direction is None:
            logger.info("%s: commodity sentiment is %s (score %.2f) - no clear direction and no "
                        "ALLOW_LONG_ON_NEUTRAL/ALLOW_SHORT_ON_NEUTRAL fallback configured, "
                        "skipping (no MCX trades today, matching what live trading would do).",
                        d, sentiment_label, sentiment_score)
            daily_summaries.append({"date": str(d), "direction": None, "sentiment": sentiment_label,
                                     "sentiment_score": sentiment_score,
                                     "total_trades": 0, "total_points": 0.0, "win_rate_pct": 0.0})
            continue

        result = _run_mcx_backtest_for_date(data_service, d, symbols, intraday_by_symbol, tz,
                                             day_direction, lot_sizes, config.MCX_CAPITAL)
        if not result["trades"].empty:
            all_trades.append(result["trades"])
        daily_summaries.append({"date": str(d), "direction": day_direction.value, "sentiment": sentiment_label,
                                 "sentiment_score": sentiment_score, **result["summary"]})

    combined_trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    daily_summary_df = pd.DataFrame(daily_summaries)

    overall = {
        "days_tested": len(daily_summaries),
        "days_skipped_no_direction": int(daily_summary_df["direction"].isna().sum()) if "direction" in daily_summary_df else 0,
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

    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import fyersclient

    parser = argparse.ArgumentParser(
        description="Backtest the ORB strategy over a historical date range, with direction "
                    "decided per-day from historical sentiment by default (matching live).")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--market", default="equity", choices=["equity", "mcx"],
                         help="Which market to backtest: 'equity' (the NSE universe scan + "
                              "gain/loss-band shortlist - the default) or 'mcx' (config."
                              "MCX_COMMODITY_UNIVERSE, traded directly and unconditionally every "
                              "day, at config.MCX_CANDLE_INTERVAL_MINUTES resolution - see "
                              "run_mcx_backtest()).")
    parser.add_argument("--direction", default=None, choices=["LONG", "SHORT"],
                         help="Force one fixed direction across the whole range, overriding the "
                              "default per-day sentiment-based decision. Omit this to replay what "
                              "the live, sentiment-gated strategy would actually have done each day.")
    parser.add_argument("--out", default=None, help="Optional path to save the combined trade log as CSV")
    parser.add_argument("--sweep-wick", default=None, metavar="T1,T2,...",
                         help="Instead of a single backtest, sweep config.MAX_OR_CANDLE_WICK_PCT "
                              "(equity only - see sweep_or_candle_wick_threshold()) across this "
                              "comma-separated list of candidate thresholds (e.g. "
                              "'10,20,30,40,50,100') and print a comparison table, so you can pick "
                              "the empirically-best value for YOUR universe/date range instead of "
                              "guessing one. Omit to use the function's own default candidate range. "
                              "Ignored (with a warning) when --market mcx is also given.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    direction = TradeDirection[args.direction] if args.direction else None
    data_service = get_fyers_client()

    #results = run_backtest(data_service, start, end, direction=direction)
    if args.sweep_wick is not None and args.market == "mcx":
        print("--sweep-wick is equity-only (MCX has no wick/gain-loss shortlist filtering at "
              "all - see commodity_engine.py) - ignoring it and running a normal MCX backtest.")
        args.sweep_wick = None

    if args.sweep_wick is not None:
        thresholds = [float(t.strip()) for t in args.sweep_wick.split(",") if t.strip()]
        sweep_df = sweep_or_candle_wick_threshold(data_service, start, end, thresholds=thresholds,
                                                   direction=direction)
        print("\n=== Opening-Range Wick Threshold Sweep ===")
        print(sweep_df.to_string(index=False) if not sweep_df.empty else "No trading days in range.")
        if not sweep_df.empty:
            best_points = sweep_df.loc[sweep_df["total_points"].idxmax()]
            best_winrate = sweep_df.loc[sweep_df["win_rate_pct"].idxmax()]
            print(f"\nBest by total_points: wick_threshold_pct={best_points['wick_threshold_pct']:.0f} "
                  f"({best_points['total_points']:.2f} pts over {best_points['total_trades']:.0f} trade(s))")
            print(f"Best by win_rate_pct: wick_threshold_pct={best_winrate['wick_threshold_pct']:.0f} "
                  f"({best_winrate['win_rate_pct']:.1f}% over {best_winrate['total_trades']:.0f} trade(s))")
            print("Sanity-check trade counts before picking a winner - a tight threshold that only "
                  "let a couple of trades through isn't a reliable signal either way. Set config."
                  "MAX_OR_CANDLE_WICK_PCT to whichever value you choose.")
        if args.out:
            sweep_df.to_csv(args.out, index=False)
            print(f"\nSaved sweep results to {args.out}")
        raise SystemExit(0)

    if args.market == "mcx":
        results = run_mcx_backtest(data_service, start, end, direction=direction)
    else:
        results = run_backtest(data_service, start, end, direction=direction)

    print("\n=== Daily Summary ===")
    print(results["daily_summary"].to_string(index=False) if not results["daily_summary"].empty
          else "No trading days in range.")
    print("\n=== Overall ===")
    print(results["overall"])

    if args.out:
        results["trades"].to_csv(args.out, index=False)
        print(f"\nSaved combined trade log to {args.out}")
