"""
commodity_engine.py
----------------------
Applies the SAME ORB + VWAP + volume-MA strategy from signal_engine.py
directly to MCX commodity FUTURES contracts, rather than inventing a
separate commodities-specific strategy. A futures contract has its own
continuously-traded OHLCV series just like a stock, so - unlike NIFTY
options (see options_engine.py, which is BUY-ONLY because of options' very
different risk profile) - commodities keep this agent's original
LONG-AND-SHORT behaviour unchanged: config.decide_todays_direction()'s
sentiment-derived bias for the day is used exactly as-is, long or short,
same as equities.

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
specifically want MCX options instead, options_engine.py's ATM-resolution
pattern (resolve everything from a symbol master file, never hard-code a
strike/expiry format) is the template to extend from, not this module.

Why a fixed universe instead of a scanned one
--------------------------------------------------
Equities scan hundreds of NSE stocks (opening_range_scanner.load_universe())
and filter by sector + gain/loss band down to a shortlist. MCX has nothing
equivalent to scan - there is no broad "commodity universe" CSV, and most
accounts only trade a handful of liquid contracts anyway. So
config.MCX_COMMODITY_UNIVERSE is an explicit, short, hand-maintained list
instead: this module reuses
opening_range_scanner.scan_symbols_accurate() (factored out of
scan_opening_range_accurate() specifically so this doesn't duplicate that
logic) directly against that list, rather than load_universe()'s
NSE-equity-specific CSV scan.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytz
from datetime import datetime

import config
from fyers_service import FyersDataService
from opening_range_scanner import scan_symbols_accurate
from signal_engine import SignalEngine
from trade_direction import TradeDirection

logger = logging.getLogger("orb_agent.commodities")


def _mcx_915_window_epochs() -> tuple[int, int]:
    """MCX's session opens later than NSE's 09:15 - see config.MCX_MARKET_OPEN_TIME. The
    'opening range' candle for MCX is the first CANDLE_INTERVAL_MINUTES bar from ITS OWN
    session open, not NSE's 09:15, so this can't reuse
    opening_range_scanner._today_915_window_epochs() as-is."""
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    open_h, open_m, open_s = (int(p) for p in config.MCX_MARKET_OPEN_TIME.split(":"))
    start = tz.localize(datetime(now.year, now.month, now.day, open_h, open_m, open_s))
    end = start + pd.Timedelta(minutes=config.CANDLE_INTERVAL_MINUTES)
    return int(start.timestamp()), int(end.timestamp())


def _lot_size_lookup() -> dict[str, int]:
    return {row["symbol"]: int(row["lot_size"]) for row in config.MCX_COMMODITY_UNIVERSE}


def scan_mcx_opening_range(data_service: FyersDataService,
                            direction: TradeDirection = TradeDirection.LONG) -> pd.DataFrame:
    """
    MCX equivalent of opening_range_scanner.scan_opening_range_accurate(),
    scanned against the fixed config.MCX_COMMODITY_UNIVERSE instead of the
    NSE-equity CSV universe, using MCX's own session-open window instead of
    NSE's 09:15. Reuses the exact same gain/loss-band filtering
    (config.MIN_GAIN_PCT/MAX_GAIN_PCT/MIN_LOSS_PCT/MAX_LOSS_PCT,
    config.TOP_N_STOCKS) as equities - change those in config.py if MCX
    should use different thresholds than equities do.
    """
    symbols = [row["symbol"] for row in config.MCX_COMMODITY_UNIVERSE]
    return scan_symbols_accurate(data_service, symbols, direction, window_epochs=_mcx_915_window_epochs())


def run_mcx_commodity_engine(data_service: FyersDataService,
                              bias_direction: TradeDirection) -> SignalEngine | None:
    """
    bias_direction: today's sentiment-derived directional bias - pass in
    EXACTLY what bootstrap.decide_todays_direction() returned for equities
    today (same reasoning as options_engine.run_nifty_options_engine: reuse
    the one sentiment read, don't recompute it per-market). Unlike options,
    this trades bias_direction AS-IS (LONG or SHORT, same as equities) -
    see this module's docstring for why.

    Builds a SignalEngine exactly like equities do, then sets each
    tracker's lot_size from config.MCX_COMMODITY_UNIVERSE and
    capital_allocated from an equal split of config.MCX_CAPITAL across
    however many commodities actually made the shortlist today (NOT a
    fixed len(MCX_COMMODITY_UNIVERSE) split, since an under-filled
    shortlist shouldn't leave capital idle - see
    position_sizing.capital_per_slot).

    Returns None if nothing in the MCX universe passed today's gain/loss
    filter - callers should treat that like equities' "empty shortlist"
    case: skip MCX trading for today, other markets keep going
    independently.
    """
    import position_sizing  # local import: avoids a module-level dependency equities/options
                             # don't need; keeps this module's import list scoped to what it uses.

    shortlist = scan_mcx_opening_range(data_service, bias_direction)
    if shortlist.empty:
        logger.warning("No MCX commodity matched today's %s opening-range filter.", bias_direction.value)
        return None

    engine = SignalEngine(data_service, shortlist, direction=bias_direction)
    lot_sizes = _lot_size_lookup()
    per_slot_capital = position_sizing.capital_per_slot(config.MCX_CAPITAL, len(engine.trackers))
    for symbol, tracker in engine.trackers.items():
        tracker.lot_size = lot_sizes.get(symbol, 1)
        tracker.capital_allocated = per_slot_capital
        if symbol not in lot_sizes:
            logger.error("%s has no lot_size entry in config.MCX_COMMODITY_UNIVERSE - defaulting to "
                          "1, which is almost certainly WRONG for a commodity future. Add it before "
                          "trusting this contract's sizing.", symbol)
    logger.info("MCX commodity engine ready: %s, %d contract(s), %.2f capital/slot.",
                bias_direction.value, len(engine.trackers), per_slot_capital)
    return engine
