"""
options_engine.py
--------------------
Applies the EXACT SAME ORB + VWAP + 20-period-volume-MA entry/exit logic
from signal_engine.py to NIFTY INDEX options, instead of inventing a
separate options-specific strategy. SignalEngine/StockTracker/TradeBook are
reused completely unchanged - this module's only job is to decide WHICH
single option contract to track today, size it correctly (lot_size > 1 -
see position_sizing.py), and hand SignalEngine a one-row shortlist for it,
exactly like opening_range_scanner.py does for equities.

Off by default (config.ENABLE_NIFTY_OPTIONS_TRADING = False) - read this
whole module and the config block above NIFTY_INDEX_SYMBOL before turning
it on.

Design decisions
-------------------
1. BUY-ONLY, always TradeDirection.LONG. This agent only ever BUYS an
   option (a call when today's sentiment-derived bias is bullish, a put
   when bearish) - it never WRITES/SELLS an option naked, which has a
   completely different (much larger, margin-based, theoretically
   uncapped) risk profile the rest of this codebase was never designed
   for. Because risk is capped to the premium paid either way, the engine
   always tracks the chosen contract with direction=LONG: a RISING premium
   (price above the contract's own opening-range high, above the
   contract's own VWAP, on rising volume) is the entry/add signal, a
   FALLING premium (2 consecutive candles below the contract's own VWAP on
   volume > its 20-period MA) is the weakness/exit signal - identical
   rules to an equity LONG, just applied to the OPTION CONTRACT'S OWN
   candles, not the index's.

2. Direction for the DAY (which strike/option-type to trade) still comes
   from the SAME sentiment pipeline equities use - bootstrap.decide_todays_direction().
   Pass that result straight into run_nifty_options_engine() rather than
   recomputing sentiment here, so equities and options always agree on
   what today's bias actually is. LONG bias -> buy an ATM CALL (CE). SHORT
   bias -> buy an ATM PUT (PE). The engine itself is then always built
   with direction=TradeDirection.LONG regardless (see point 1) - only the
   CONTRACT chosen differs, not the tracking direction.

3. Strike, expiry, lot size, and the exact Fyers trading-symbol string are
   ALL resolved from Fyers' own published F&O symbol master file (a CSV
   Fyers publishes and updates), NOT hard-coded here. NIFTY's expiry day,
   lot size, and strike gap have all changed more than once under
   SEBI/exchange rule changes - hard-coding any of them as a Python string
   template is exactly the kind of thing that goes silently stale. Point
   config.NIFTY_FO_SYMBOL_MASTER_CSV at a locally downloaded, periodically
   refreshed copy of that file before enabling this. The column names this
   module looks for (config.NIFTY_FO_MASTER_COLUMNS) are a best-effort
   guess - PRINT the actual file's columns once and correct that config
   dict before trusting this in production, exactly like
   opening_range_scanner.py's own "verify quotes field names" caveat.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import pytz

import config
from fyers_service import FyersDataService
from signal_engine import SignalEngine
from trade_direction import TradeDirection

logger = logging.getLogger("orb_agent.options")


def load_fo_symbol_master(csv_path: str | None = None) -> pd.DataFrame:
    """
    Loads Fyers' NSE F&O instrument master CSV from disk - download/refresh
    it yourself periodically (see README) and point
    config.NIFTY_FO_SYMBOL_MASTER_CSV at it. Column names are normalised
    via config.NIFTY_FO_MASTER_COLUMNS, a best-effort {logical name: actual
    column name} mapping you MUST verify against your own downloaded
    file's real header before relying on this (see this module's
    docstring, point 3).

    Returns an empty DataFrame (never raises) if the file doesn't exist
    yet or is missing an expected column, so a misconfigured/missing
    master file degrades to "no options traded today" rather than
    crashing the whole agent.
    """
    path = csv_path or config.NIFTY_FO_SYMBOL_MASTER_CSV
    try:
        df = pd.read_csv(path)
    except (FileNotFoundError, OSError) as exc:
        logger.error("NIFTY F&O symbol master not found at %s (%s) - options trading disabled "
                      "for today. Download/refresh it and set config.NIFTY_FO_SYMBOL_MASTER_CSV.",
                      path, exc)
        return pd.DataFrame()

    cols = config.NIFTY_FO_MASTER_COLUMNS
    missing = [logical for logical, actual in cols.items() if actual not in df.columns]
    if missing:
        logger.error("NIFTY F&O symbol master at %s is missing expected column(s) %s (looked for "
                      "%s) - check config.NIFTY_FO_MASTER_COLUMNS against this file's real header "
                      "(columns found: %s). Options trading disabled for today.",
                      path, missing, {m: cols[m] for m in missing}, list(df.columns))
        return pd.DataFrame()
    return df


def _fetch_nifty_spot(data_service: FyersDataService) -> float | None:
    quote = data_service.get_quotes([config.NIFTY_INDEX_SYMBOL])
    try:
        return next(item["v"]["lp"] for item in quote.get("d", [])
                    if item.get("n") == config.NIFTY_INDEX_SYMBOL)
    except (StopIteration, KeyError, TypeError):
        logger.error("Could not fetch NIFTY spot LTP via %s - can't resolve an ATM strike.",
                      config.NIFTY_INDEX_SYMBOL)
        return None


def resolve_atm_option_symbol(data_service: FyersDataService, option_type: str,
                               symbol_master: pd.DataFrame | None = None) -> tuple[str, int] | None:
    """
    option_type: "CE" or "PE". Returns (fyers_trading_symbol, lot_size) for
    the NEAREST-EXPIRY, AT-THE-MONEY NIFTY contract of that type, or None
    if it couldn't be resolved (missing/empty master file, no NIFTY LTP, or
    no matching row) - callers must treat None as "skip options trading
    today", not retry indefinitely.

    ATM = the available strike CLOSEST to the current NIFTY spot LTP
    (fetched fresh via a quotes call on config.NIFTY_INDEX_SYMBOL) - not
    rounded to a hard-coded strike gap, since the master file's own listed
    strikes are the ground truth for what's actually tradable today.
    lot_size is read straight from the master file too, not hard-coded -
    see this module's docstring, point 3.
    """
    master = symbol_master if symbol_master is not None else load_fo_symbol_master()
    if master.empty:
        return None

    cols = config.NIFTY_FO_MASTER_COLUMNS
    underlying_col, opttype_col = cols["underlying"], cols["option_type"]
    expiry_col, strike_col = cols["expiry"], cols["strike"]
    symbol_col, lot_col = cols["trading_symbol"], cols["lot_size"]

    spot = _fetch_nifty_spot(data_service)
    if spot is None:
        return None

    candidates = master[(master[underlying_col].astype(str).str.upper() == "NIFTY") &
                         (master[opttype_col].astype(str).str.upper() == option_type.upper())]
    if candidates.empty:
        logger.error("No %s NIFTY option rows found in the symbol master.", option_type)
        return None

    nearest_expiry = candidates[expiry_col].min()
    candidates = candidates[candidates[expiry_col] == nearest_expiry]

    candidates = candidates.assign(_dist=(candidates[strike_col] - spot).abs())
    best = candidates.sort_values("_dist").iloc[0]

    symbol = str(best[symbol_col])
    lot_size = int(best[lot_col])
    logger.info("Resolved ATM %s: spot=%.2f -> strike=%s, expiry=%s, symbol=%s, lot_size=%d",
                option_type, spot, best[strike_col], nearest_expiry, symbol, lot_size)
    return symbol, lot_size


def build_single_symbol_shortlist(data_service: FyersDataService, symbol: str) -> pd.DataFrame:
    """
    Fetches `symbol`'s own 09:15-09:20 opening-range candle (the CONTRACT'S
    OWN first 5-min bar, not the index's) and wraps it into the same
    {symbol, open, high, low, close, volume} one-row shape
    opening_range_scanner.py produces for equities - SignalEngine's
    constructor only ever reads `symbol`/`high`/`low` from this, so it's a
    drop-in regardless of which market produced the row.

    Returns an empty DataFrame (never raises) if that candle isn't
    available yet (contract just listed today with no trades, a holiday, a
    feed gap, etc).
    """
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    start = tz.localize(datetime(now.year, now.month, now.day, 9, 15, 0))
    end = tz.localize(datetime(now.year, now.month, now.day, 9, 20, 0))
    candles = data_service.get_5min_candle(symbol, now.strftime("%Y-%m-%d"),
                                            int(start.timestamp()), int(end.timestamp()))
    if not candles:
        logger.warning("No opening-range candle yet for %s - can't build today's options shortlist.", symbol)
        return pd.DataFrame()

    _, o, h, l, c, v = candles[0]
    return pd.DataFrame([{"symbol": symbol, "open": o, "high": h, "low": l, "close": c, "volume": v}])


def run_nifty_options_engine(data_service: FyersDataService, bias_direction: TradeDirection,
                              symbol_master: pd.DataFrame | None = None) -> SignalEngine | None:
    """
    bias_direction: today's sentiment-derived directional bias - pass in
    EXACTLY what bootstrap.decide_todays_direction() returned for equities
    today (main.py's job_opening_range_scan() already computes this once;
    reuse that same value here rather than calling sentiment twice, so the
    two strategies can never silently disagree on what today's bias is).

    LONG bias -> buy an ATM call (CE). SHORT bias -> buy an ATM put (PE).
    Either way the returned engine tracks that single contract with
    direction=TradeDirection.LONG (see module docstring, point 1) - this
    function hides that mapping so callers never have to think about it.

    Returns None (never raises) if the ATM contract couldn't be resolved
    or its opening-range candle isn't available yet - callers should treat
    that exactly like opening_range_scanner's "empty shortlist" case for
    equities: skip options trading for today, equities (and/or MCX) keep
    going independently.
    """
    option_type = "CE" if bias_direction == TradeDirection.LONG else "PE"
    resolved = resolve_atm_option_symbol(data_service, option_type, symbol_master)
    if resolved is None:
        return None
    symbol, lot_size = resolved

    shortlist = build_single_symbol_shortlist(data_service, symbol)
    if shortlist.empty:
        return None

    engine = SignalEngine(data_service, shortlist, direction=TradeDirection.LONG)
    tracker = engine.trackers[symbol]
    tracker.lot_size = lot_size
    tracker.capital_allocated = config.NIFTY_OPTIONS_CAPITAL
    logger.info("NIFTY options engine ready: BUY-ONLY %s %s, lot_size=%d, capital_allocated=%.2f",
                option_type, symbol, lot_size, tracker.capital_allocated)
    return engine
