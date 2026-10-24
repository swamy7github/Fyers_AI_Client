"""
commodity_engine.py
----------------------
Applies the SAME ORB + VWAP + volume-MA strategy from signal_engine.py
directly to MCX commodity FUTURES contracts, rather than inventing a
separate commodities-specific strategy. A futures contract has its own
continuously-traded OHLCV series just like a stock, so - unlike NIFTY
options (see options_engine.py, which is BUY-ONLY because of options' very
different risk profile) - commodities keep this agent's original
LONG-AND-SHORT behaviour unchanged: today's sentiment-derived bias is used
exactly as-is, long or short, same as equities - except that bias comes
from bootstrap.decide_todays_commodity_direction() (a COMMODITY-specific
global sentiment read - gold/silver/crude/copper/natural-gas/dollar-index
cues, see global_sentiment.COMMODITY_GLOBAL_TICKERS), deliberately NOT the
equity-cue sentiment equities/NIFTY/BANKNIFTY options use, since MCX trades
against global commodity prices, not global equity indices.

Off by default (config.ENABLE_MCX_COMMODITY_TRADING = False) - read this
module and the MCX config block in config.py (especially
MCX_COMMODITY_UNIVERSE's per-contract symbol/lot_size, which roll and
change respectively and are NOT kept current by this scaffold) before
turning it on.

Why futures, not commodity options
-------------------------------------
MCX commodity OPTIONS exist too, but liquidity is thin for most strikes on
most contracts, and trading futures keeps the equities-to-commodities
mapping exact: one tradable instrument per commodity, its own opening
range, VWAP, and volume, no strike/expiry-selection step at all (unlike
NIFTY options, which need exactly that - see options_engine.py). If you
specifically want MCX options instead, options_engine.py's OTM-resolution
pattern (resolve everything from a symbol master file, never hard-code a
strike/expiry format) is the template to extend from, not this module.

Why the FULL fixed universe, unfiltered - no scan, no shortlist
---------------------------------------------------------------------
Equities scan hundreds of NSE stocks (opening_range_scanner.load_universe())
and filter by sector + today's opening gain/loss band down to a shortlist,
because the starting universe is far too large to trade all of it at once.
MCX has nothing equivalent to scan, and doesn't need one:
config.MCX_COMMODITY_UNIVERSE IS the universe - an explicit, short,
hand-maintained list of the handful of contracts the account actually
trades (see config.py's comment). There is still no sector step and no
gain/loss-band (prev-close % move) shortlist here: every symbol in
MCX_COMMODITY_UNIVERSE is CONSIDERED every day, directly, with no price-move
narrowing step in between. The one thing that CAN now leave a contract out
(see fetch_mcx_universe_opening_ranges() below) is the opening-range
"closing strength"/wick filter - config.MAX_OR_CANDLE_WICK_PCT (the SAME
shared threshold equities/options use, not an MCX-specific one - see that
config value's docs), a no-op by default - which screens the SHAPE of a
contract's own opening-range candle, not its price move, so it's a quality
check on THAT contract's candle, not a cross-universe ranking the way
equities' gain/loss band is. Besides that, a symbol is only ever left out
because its own opening-range candle wasn't available (a data problem,
logged loudly), never because of its price move.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytz
from datetime import datetime

import config
from fyers_service import FyersDataService
from opening_range_scanner import or_candle_wick_pct
from signal_engine import SignalEngine
from trade_direction import TradeDirection

logger = logging.getLogger("orb_agent.commodities")


def _mcx_915_window_epochs() -> tuple[int, int]:
    """MCX's session opens later than NSE's 09:15 - see config.MCX_MARKET_OPEN_TIME. The
    'opening range' candle for MCX is the first MCX_CANDLE_INTERVAL_MINUTES bar from ITS OWN
    session open, not NSE's 09:15 (and not necessarily the same width as NSE's own
    CANDLE_INTERVAL_MINUTES - see config.MCX_CANDLE_INTERVAL_MINUTES/Task #6), so this can't
    reuse opening_range_scanner._today_915_window_epochs() as-is."""
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    open_h, open_m, open_s = (int(p) for p in config.MCX_MARKET_OPEN_TIME.split(":"))
    start = tz.localize(datetime(now.year, now.month, now.day, open_h, open_m, open_s))
    end = start + pd.Timedelta(minutes=config.MCX_CANDLE_INTERVAL_MINUTES)
    return int(start.timestamp()), int(end.timestamp())


def _lot_size_lookup() -> dict[str, int]:
    return {row["symbol"]: int(row["lot_size"]) for row in config.MCX_COMMODITY_UNIVERSE}


def fetch_mcx_universe_opening_ranges(data_service: FyersDataService,
                                       direction: TradeDirection = TradeDirection.LONG) -> pd.DataFrame:
    """
    Fetches EVERY symbol in config.MCX_COMMODITY_UNIVERSE's own opening-range candle (the
    first CANDLE_INTERVAL_MINUTES bar from MCX's own session open - see
    _mcx_915_window_epochs()) and returns them ALL as a DataFrame - still NO gain/loss-band
    shortlisting step, unlike equities' scan_opening_range_accurate()/
    opening_range_scanner.load_universe() (MCX_COMMODITY_UNIVERSE is already the full,
    deliberately short, hand-picked trading universe - see this module's docstring, there's
    nothing to screen by price move) - but AS OF this parameter, one quality screen does apply:
    the SAME opening-range "closing strength" check equities/options use, and the SAME shared
    threshold too (see config.MAX_OR_CANDLE_WICK_PCT's docs), applied PER CONTRACT against
    `direction`. Defaults to its no-op (100.0), so this stays a pure pass-through until you
    deliberately tune it down - and tuning it affects every market at once now, not just MCX.

    A symbol is left out of the returned DataFrame when its opening-range candle simply isn't
    available yet (contract not listed today, feed gap, etc - a genuine data-availability
    problem, logged loudly) OR when it fails the wick filter above (a strategy decision, logged
    separately so the two reasons are never conflated) - either way, every OTHER symbol in
    MCX_COMMODITY_UNIVERSE is still traded.
    """
    symbols = [row["symbol"] for row in config.MCX_COMMODITY_UNIVERSE]
    from_epoch, to_epoch = _mcx_915_window_epochs()
    today_str = datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%Y-%m-%d")
    resolution = str(config.MCX_CANDLE_INTERVAL_MINUTES)
    candles_by_symbol = data_service.get_candles_bulk(symbols, resolution, today_str, today_str)

    rows = []
    missing = []
    wick_filtered = []
    for symbol in symbols:
        candles = candles_by_symbol.get(symbol, [])
        window_candle = next((c for c in candles if from_epoch <= c[0] < to_epoch), None)
        if window_candle is None:
            missing.append(symbol)
            continue
        _, o, h, l, c, v = window_candle
        wick_pct = or_candle_wick_pct(o, h, l, c, direction)
        if wick_pct > config.MAX_OR_CANDLE_WICK_PCT:
            wick_filtered.append((symbol, wick_pct))
            continue
        rows.append({"symbol": symbol, "open": o, "high": h, "low": l, "close": c, "volume": v})

    if missing:
        logger.warning("No opening-range candle available yet for %s - skipped today (a data "
                        "availability gap, not a strategy filter; every OTHER symbol in "
                        "MCX_COMMODITY_UNIVERSE is still traded).", missing)
    if wick_filtered:
        logger.info("Opening-range closing-strength filter excluded %s today (wick_pct > config."
                    "MAX_OR_CANDLE_WICK_PCT=%.1f%%, %s bias): %s - every OTHER symbol in "
                    "MCX_COMMODITY_UNIVERSE is still traded.",
                    [s for s, _ in wick_filtered], config.MAX_OR_CANDLE_WICK_PCT, direction.value,
                    [(s, round(p, 1)) for s, p in wick_filtered])
    return pd.DataFrame(rows)


def run_mcx_commodity_engine(data_service: FyersDataService,
                              bias_direction: TradeDirection) -> SignalEngine | None:
    """
    bias_direction: today's COMMODITY-specific sentiment-derived directional bias - pass in
    EXACTLY what bootstrap.decide_todays_commodity_direction() returned (NOT
    decide_todays_direction(), which is equities'/NIFTY-options' equity-cue read - see this
    module's docstring for why they're deliberately separate). Unlike options, this trades
    bias_direction AS-IS (LONG or SHORT, same as equities).

    Trades EVERY symbol in config.MCX_COMMODITY_UNIVERSE directly (see
    fetch_mcx_universe_opening_ranges() - no gain/loss-band shortlisting, unlike equities).
    Capital is split equally across whichever of those symbols actually had an
    opening-range candle available today (see position_sizing.capital_per_slot) - normally
    the whole universe, unless one symbol hit a genuine data gap.

    Returns None only if NOT A SINGLE symbol in the universe had its opening-range candle
    available (a data/connectivity problem - there's no filter left to fail) - callers
    should treat that like equities' "empty shortlist" case: skip MCX trading for today,
    other markets keep going independently.
    """
    import position_sizing  # local import: avoids a module-level dependency equities/options
                             # don't need; keeps this module's import list scoped to what it uses.

    universe_df = fetch_mcx_universe_opening_ranges(data_service, direction=bias_direction)
    if universe_df.empty:
        logger.warning("No opening-range candle was available for ANY symbol in "
                        "MCX_COMMODITY_UNIVERSE today - nothing to trade.")
        return None

    engine = SignalEngine(data_service, universe_df, direction=bias_direction,
                           candle_resolution=str(config.MCX_CANDLE_INTERVAL_MINUTES))
    lot_sizes = _lot_size_lookup()
    per_slot_capital = position_sizing.capital_per_slot(config.MCX_CAPITAL, len(engine.trackers))
    for symbol, tracker in engine.trackers.items():
        tracker.lot_size = lot_sizes.get(symbol, 1)
        tracker.capital_allocated = per_slot_capital
        if symbol not in lot_sizes:
            logger.error("%s has no lot_size entry in config.MCX_COMMODITY_UNIVERSE - defaulting to "
                          "1, which is almost certainly WRONG for a commodity future. Add it before "
                          "trusting this contract's sizing.", symbol)
    logger.info("MCX commodity engine ready: %s, %d/%d universe contract(s) tracked, %.2f capital/slot.",
                bias_direction.value, len(engine.trackers), len(config.MCX_COMMODITY_UNIVERSE), per_slot_capital)
    return engine
