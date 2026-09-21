"""
opening_range_scanner.py
--------------------------
At 09:20:02 (a 2-second buffer after the first 5-min candle, 09:15-09:20,
closes), this module:

  1. Pulls a proxy for that 09:15 candle's O/H/L/C/V for every stock in the
     universe, via ONE batched /quotes call per ~50 symbols (see "why
     quotes, not history" below) - roughly 10 API calls for a 500-stock
     universe, instead of ~1,000.
  2. Computes % change of that close vs. the previous day's close (i.e. how
     far the stock has moved so far today, gap included).
  3. Keeps stocks whose gain falls in (MIN_GAIN_PCT, MAX_GAIN_PCT].
  4. Returns the top N by gain%, as a DataFrame, for the signal engine to
     track for the rest of the session.

Why quotes, not the historical-candle endpoint
-------------------------------------------------
Calling get_5min_candle() + get_previous_close() once per stock (2 calls x
500 stocks = ~1,000 calls) blows straight through Fyers' per-second call cap
and would take minutes to work through even with a rate limiter - far too
slow for a job that needs to finish well before the next candle closes.

The trick: at 09:20:02, the exchange's cumulative "day so far" OHLC for a
stock IS the 09:15 candle - only one 5-minute bar has traded. The /quotes
endpoint returns exactly that (open_price/high_price/low_price/lp = the
day's running O/H/L/last-price, plus prev_close_price and volume) for many
symbols in a single batched call, via FyersDataService.get_quotes_batched().

This equivalence only holds for the FIRST candle of the day - don't reuse
this shortcut for any later candle (the signal engine correctly uses the
real historical-candle endpoint for that, since by then "day so far" and
"latest 5-min candle" are no longer the same thing).

Note: exact field names below (open_price, high_price, low_price, lp,
prev_close_price, volume) are standard Fyers quotes response keys - print
one raw response for your account once to confirm they match before relying
on this in production, since response shapes can shift between API
versions.
"""

from __future__ import annotations

import logging

import pandas as pd

import config
from fyers_service import FyersDataService

logger = logging.getLogger("orb_agent.opening_range")


def load_universe() -> list[str]:
    """
    Reads the NSE Nifty 500 constituent CSV (see README for the download
    link) and converts each symbol into Fyers' 'NSE:<SYMBOL>-EQ' format.
    """
    df = pd.read_csv(config.STOCK_UNIVERSE_CSV)
    symbol_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
    return [f"NSE:{str(s).strip()}-EQ" for s in df[symbol_col].tolist()]


def scan_opening_range(data_service: FyersDataService) -> pd.DataFrame:
    symbols = load_universe()
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

    df = pd.DataFrame(rows)
    if df.empty:
        logger.warning("No stocks matched the opening-range gain filter (%.1f%%, %.1f%%].",
                        config.MIN_GAIN_PCT, config.MAX_GAIN_PCT)
        return df

    df = df.sort_values("pct_change", ascending=False)
    top_n = df.head(config.TOP_N_STOCKS).reset_index(drop=True)

    logger.info("Opening-range shortlist (top %d):\n%s", config.TOP_N_STOCKS, top_n.to_string(index=False))
    return top_n
