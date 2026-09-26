"""
opening_range_scanner.py
--------------------------
At 09:20:02 (a 2-second buffer after the first 5-min candle, 09:15-09:20,
closes), this module:

  1. Loads the stock universe from STOCK_UNIVERSE_CSV, filtered down to only
     the stocks whose `Industry` matches one of the `sector` labels loaded
     from SECTOR_INDEX_MAP_CSV (see load_universe() below).
  2. Pulls a proxy for that 09:15 candle's O/H/L/C/V for every remaining
     stock, via ONE batched /quotes call per ~50 symbols (see "why quotes,
     not history" below).
  3. Computes % change of that close vs. the previous day's close (i.e. how
     far the stock has moved so far today, gap included).
  4. Keeps stocks whose gain falls in (MIN_GAIN_PCT, MAX_GAIN_PCT].
  5. Returns the top N by gain%, as a DataFrame, for the signal engine to
     track for the rest of the session.

Why quotes, not the historical-candle endpoint (for the ON-TIME path)
--------------------------------------------------------------------------
Calling get_5min_candle() + get_previous_close() once per stock blows
straight through Fyers' per-second call cap and would take minutes to work
through even with a rate limiter - far too slow for a job that needs to
finish well before the next candle closes.

The trick: AT 09:20:02, the exchange's cumulative "day so far" OHLC for a
stock IS the 09:15 candle - only one 5-minute bar has traded. The /quotes
endpoint returns exactly that in a single batched call.

This equivalence ONLY holds right around 09:20:02 - if this module is asked
to run significantly later (e.g. an application that started mid-session at
10:00, catching up on a missed run), "day so far" and "the 09:15 candle" are
no longer the same thing, and the quotes shortcut would silently return the
wrong numbers. For that case, use scan_opening_range_accurate() instead,
which fetches the real 09:15-09:20 candle via the historical-candle
endpoint regardless of when it's called - slower (bounded by the rate
limiter, roughly a minute or two for a full universe) but always correct.
main.py's job_opening_range_scan() picks between the two automatically
based on how close "now" is to config.OPENING_RANGE_SCAN_TIME.

Note: exact field names below (open_price, high_price, low_price, lp,
prev_close_price, volume) are standard Fyers quotes response keys - print
one raw response for your account once to confirm they match before relying
on this in production, since response shapes can shift between API
versions.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import pytz
from pathlib import Path
import config
import sys
from fyers_service import FyersDataService
from sector_scanner import load_sector_map

# 1. Dynamically parse the parent path ('C:\NS\Fyers_AI_Client')
# This gets the folder containing the current script and looks one level up
project_root = Path(__file__).resolve().parent.parent

# 2. Add 'C:\NS\Fyers_AI_Client' to Python's search path
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))
    
logger = logging.getLogger("orb_agent.opening_range_scanner")


def load_valid_sectors() -> set[str]:
    """
    Case-insensitive, trimmed, upper-cased set of `sector` labels loaded
    from SECTOR_INDEX_MAP_CSV (via sector_scanner.load_sector_map()) that
    are eligible for universe filtering. Rows with a blank sector (broad
    market indices, VIX, G-Secs, cross-sector baskets) are excluded here,
    not because they failed to load, but because they were never meant to
    match a single Industry value - see the column docs in
    sector_scanner.py.
    """
    sector_df = load_sector_map()
    if sector_df.empty:
        return set()
    sectors = sector_df["sector"].dropna().astype(str).str.strip()
    sectors = sectors[sectors != ""]
    return set(sectors.str.upper())


def load_universe(top_sectors: pd.DataFrame) -> list[str]:
    """
    Reads the NSE Nifty 500 constituent CSV (see README for the download
    link), filters it down to stocks whose `Industry` matches a loaded
    `sector` (case-insensitive, trimmed), and converts each remaining
    symbol into Fyers' 'NSE:<SYMBOL>-EQ' format.

    Falls back to scanning the FULL, unfiltered universe (with a warning)
    if the CSV has no `Industry` column, or if no valid sectors were
    loaded at all - a misconfiguration here should never silently shrink
    the universe to zero stocks.
    """
    logger.info(f" STOCK_UNIVERSE_CSV file is @ {config.STOCK_UNIVERSE_CSV} ")
    dfsuc = pd.read_csv(config.STOCK_UNIVERSE_CSV)
    column_lookup = {c.strip().lower(): c for c in dfsuc.columns}
    #for 
    symbol_col = column_lookup.get("symbol", dfsuc.columns[0])
    sector_col = column_lookup.get("industry", dfsuc.columns[0])

    total_universe = len(dfsuc)
    sec_symbols = pd.DataFrame()
    sectors = []
    from utilities import get_nifty_stock_universe, save_to_csv
    for top_sector in top_sectors.itertuples(index=True):  # for row in df.itertuples(index=True)
        if top_sector.sector not in ["add all"]:
            sectors.append(top_sector.sector)
        else:
            sec_symbols = pd.concat([sec_symbols, get_nifty_stock_universe(url=top_sector.sector_url)], ignore_index=True) 
    
    filtered_dfsuc = dfsuc[dfsuc[sector_col].isin(sectors)]
    sec_symbols = pd.concat([sec_symbols, filtered_dfsuc], ignore_index=True)
    sec_symbols.drop_duplicates(subset=[symbol_col], keep='last', inplace=True)
                      
    # 2. Overwrite old rows by keeping the last occurrence of the ID
    # Filter the DataFrame
    #keys 
    #filtered_df = df[df['column_name'].isin(target_values)]
    #df_combined.drop_duplicates(subset=['id'], keep='last', inplace=True)
    
    '''
    valid_sectors = load_valid_sectors()
    logger.info("Loaded %d valid sector(s) from %s for universe filtering: %s",
                len(valid_sectors), config.SECTOR_INDEX_MAP_CSV, sorted(valid_sectors))

    if industry_col is None:
        logger.warning("No 'Industry' column found in %s - skipping sector-based filtering, "
                        "scanning the full universe (%d stocks).",
                        config.STOCK_UNIVERSE_CSV, total_universe)
        filtered = df
    elif not valid_sectors:
        logger.warning("No valid sectors loaded from %s - skipping sector-based filtering, "
                        "scanning the full universe (%d stocks). Check the 'sector' column "
                        "in that file.", config.SECTOR_INDEX_MAP_CSV, total_universe)
        filtered = df
    else:
        # Missing/null Industry values become the literal string "nan" after .astype(str),
        # which won't match any real sector label - safely excluded, not a crash.
        industry_normalized = df[industry_col].astype(str).str.strip().str.upper()
        mask = industry_normalized.isin(valid_sectors)
        filtered = df[mask]

        # Diagnostic: per-sector match counts, so a wrong guess in sector_index_map.csv's
        # `sector` column (see its docs) is obvious immediately rather than silently
        # shrinking the universe.
        match_counts = industry_normalized[mask].value_counts()
        for sector in sorted(valid_sectors):
            count = int(match_counts.get(sector, 0))
            if count == 0:
                logger.warning("Sector '%s' matched ZERO stocks in %s's Industry column - "
                                "likely a text mismatch, see sector_scanner.py's column docs "
                                "for how to fix it.", sector, config.STOCK_UNIVERSE_CSV)
            else:
                logger.info("Sector '%s': %d stock(s) matched.", sector, count)

    logger.info("Stock universe: %d total -> %d retained after sector filtering.",
                total_universe, len(filtered))
'''
    from swing_trading_email_scheduler_v14 import scan_and_research 
    sec_symbols = dict(zip(sec_symbols[symbol_col], sec_symbols[sector_col]))
        # Convert keys to uppercase
    nse_symbols = {
        key.upper(): value for key, value in sec_symbols.items()
    }
    print(f"[SUCCESS] Successfully loaded {len(sec_symbols)} tickers from the sectors constitu")   
    #sec_symbols = scan_and_research(only_tech_scan=True, nse_symbols=nse_symbols)
    symbols = []
    if sec_symbols:
        #symbols = [item[symbol_col] for item in sec_symbols]
        symbols = sec_symbols.keys()
        symbols = [f"NSE:{str(s).strip()}-EQ" for s in symbols]
    return symbols


def _today_915_window_epochs(now) -> tuple[int, int]:
    tz = pytz.timezone(config.TIMEZONE)
    #now = datetime.now(tz)
    
    start = tz.localize(datetime(now.year, now.month, now.day, 9, 15, 0))
    end = tz.localize(datetime(now.year, now.month, now.day, 9, 20, 0))
    return int(start.timestamp()), int(end.timestamp())


def _apply_gain_filter(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        logger.warning("No stocks matched the opening-range gain filter (%.1f%%, %.1f%%].",
                        config.MIN_GAIN_PCT, config.MAX_GAIN_PCT)
        return df
    df = df.sort_values("pct_change", ascending=False)
    # select only grean candles 
    df = df[df['open'] < df['close']]
    top_n = df.head(config.TOP_N_STOCKS).reset_index(drop=True)
    logger.info("Opening-range shortlist (top %d):\n%s", config.TOP_N_STOCKS, top_n.to_string(index=False))
    return top_n


def scan_opening_range(state) -> pd.DataFrame:
    """Fast path: valid only right around config.OPENING_RANGE_SCAN_TIME - see module docstring."""
    data_service: FyersDataService = state['data_service']
    symbols = load_universe(state['top5_gaining_sectors'])
    quote_lookup = data_service.get_quotes_batched(symbols)

    rows = []
    for symbol in symbols:
        quote = quote_lookup.get(symbol)
        if not quote:
            continue
        try:
            o = quote["open_price"]
            h = quote["high_price"]
            l = quote["low_price"]
            c = quote["lp"]                    # last price so far today == the 09:15 candle's close
            v = quote.get("volume", 0)
            prev_close = quote.get("prev_close_price")
            if not prev_close:
                continue

            pct_change = (c - prev_close) / prev_close * 100
            if config.MIN_GAIN_PCT < pct_change <= config.MAX_GAIN_PCT:
                rows.append({
                    "symbol": symbol, "open": o, "high": h, "low": l, "close": c,
                    "volume": v, "prev_close": prev_close, "pct_change": round(pct_change, 2),
                })
        except (KeyError, TypeError) as exc:
            logger.error("Unexpected quote shape for %s (%s) - skipping.", symbol, exc)
            continue

    return _apply_gain_filter(rows)


def scan_opening_range_accurate(state) -> pd.DataFrame:
    """
    Slow-but-always-correct path: fetches the REAL 09:15-09:20 historical
    candle per stock, regardless of what time it's actually called. Use
    this instead of scan_opening_range() for any late/catch-up start,
    where "day so far" no longer equals "the 09:15 candle".
    """
    symbols = load_universe(state['top5_gaining_sectors'])
    from utilities import get_last_traded_day
    today_str = get_last_traded_day()
    
    from_epoch, to_epoch = _today_915_window_epochs(datetime.strptime(today_str, "%Y-%m-%d"))
    #today_str = datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%Y-%m-%d")
        
    data_service = state['data_service']
    rows = []
    for symbol in symbols:
        try:
            candles = data_service.get_5min_candle(symbol, today_str, from_epoch, to_epoch)
            if not candles:
                continue
            _, o, h, l, c, v = candles[0]

            prev_close = data_service.get_previous_close(symbol)
            if not prev_close:
                continue

            pct_change = (c - prev_close) / prev_close * 100
            if config.MIN_GAIN_PCT < pct_change <= config.MAX_GAIN_PCT:
                rows.append({
                    "symbol": symbol, "open": o, "high": h, "low": l, "close": c,
                    "volume": v, "prev_close": prev_close, "pct_change": round(pct_change, 2),
                })
        except Exception as exc:
            logger.error("Opening-range (accurate) fetch failed for %s: %s", symbol, exc)
            continue

    return _apply_gain_filter(rows)
