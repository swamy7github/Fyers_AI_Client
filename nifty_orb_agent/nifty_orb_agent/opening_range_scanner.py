"""
opening_range_scanner.py
--------------------------
At 09:20:02 (a 2-second buffer after the first 5-min candle, 09:15-09:20,
closes), this module:

  1. Pulls that 09:15 candle's O/H/L/C/V for every stock in the universe.
  2. Computes % change of that candle's close vs. the previous day's close
     (i.e. how far the stock has moved so far today, gap included).
  3. Keeps stocks whose gain falls in (MIN_GAIN_PCT, MAX_GAIN_PCT].
  4. Returns the top N by gain%, as a DataFrame, for the signal engine to
     track for the rest of the session.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import pytz

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


def _today_915_window_epochs() -> tuple[int, int]:
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    start = tz.localize(datetime(now.year, now.month, now.day, 9, 15, 0))
    end = tz.localize(datetime(now.year, now.month, now.day, 9, 20, 0))
    return int(start.timestamp()), int(end.timestamp())


def scan_opening_range(data_service: FyersDataService) -> pd.DataFrame:
    symbols = load_universe()
    from_epoch, to_epoch = _today_915_window_epochs()
    today_str = datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%Y-%m-%d")

    rows = []
    for symbol in symbols:
        try:
            candles = data_service.get_5min_candle(symbol, today_str, from_epoch, to_epoch)
            if not candles:
                continue
            _, o, h, l, c, v = candles[0]  # exactly one 5-min candle in this window

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
            logger.error("Opening-range fetch failed for %s: %s", symbol, exc)
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
