"""
main.py
--------
Entry point. Auth, one-shot bootstrap/catch-up, then hands off to the
scheduler for 5-min candle-level checks and the day's remaining one-off
jobs. Every entry, add-on, and exit decision is made purely from closed
5-minute candles (see signal_engine.py) - there is no tick-level risk
management, so no live tick WebSocket needs to run (live_tick_stream.py
still exists and is fully testable, but main.py no longer starts it; see
that module's docstring for why).

Direction (LONG/SHORT) for every market defaults to a global-sentiment read
(bootstrap.decide_todays_direction()/decide_todays_commodity_direction()).
config.USE_CANDLE_BASED_DIRECTION flips this to a price-action read instead -
today's direction comes "on the fly" from a reference instrument's own first
few opening candles (NIFTY50's for equities/NIFTY options, BANKNIFTY's for
BANKNIFTY options, one MCX_COMMODITY_UNIVERSE contract's for the whole MCX
universe) rather than global_sentiment.py's snapshot. This also shifts WHEN
each market's opening-range scan can first run (config.CANDLE_DIRECTION_SCAN_TIME/
MCX_CANDLE_DIRECTION_SCAN_TIME instead of OPENING_RANGE_SCAN_TIME/
MCX_OPENING_RANGE_SCAN_TIME, since direction literally can't be known before
the deciding candle(s) close) - see config.py's docstring on the flag and
bootstrap._decide_direction_from_candles() for the exact mechanics.

Run with:  python main.py
Stop with: Ctrl+C

Startup flow
-------------
1. Authenticate, build the data service.
2. Run the pre-market global sentiment check (informational, always runs once).
3. bootstrap.bootstrap_and_catchup() - the ONE place that decides what phase
   the NSE EQUITY market is in and, if started mid-session, synchronously does
   EVERYTHING needed to start seamlessly from "now": sector/breadth
   snapshot, gap-up shortlist, full 9:15-to-now candle replay, and broker
   position/order reconciliation. See bootstrap.py's module docstring for
   why this all needs to happen as one atomic step rather than the
   scattered per-job catch-up main.py used before this file existed.
   Opt-in like the other three strategies below (config.ENABLE_STOCK_TRADING,
   default False): this ALWAYS computes and returns the real market phase
   (needed for step 4's exit decision and NIFTY/BANKNIFTY options' shared
   session), but skips all the catch-up work above and returns a None
   signal_engine when disabled - see bootstrap_and_catchup()'s own gate.
3b. bootstrap.bootstrap_mcx_catchup() / bootstrap_mcx_options_catchup() - the SAME idea as
    step 3, but for MCX commodity FUTURES (config.ENABLE_MCX_COMMODITY_TRADING) and MCX
    commodity OPTIONS (config.ENABLE_MCX_OPTIONS_TRADING - mcx_options_engine.py; EACH
    commodity watches its OWN futures candles for a breakout in EITHER direction and buys
    that SAME commodity's own CE/PE, exactly like NIFTY/BANKNIFTY options - see that
    module's docstring), run UNCONDITIONALLY alongside step 3, not gated by it and not
    gated by each other (two completely independent engines/capital buckets/trade books,
    same as every other pair of strategies here). MCX's own session (09:00-23:30 IST by
    default) is far longer than, and completely independent of, NSE's 09:15-15:30 equity
    session - so starting this process at, say, 17:51 IST (NSE already closed, MCX still
    open) must still bootstrap and run MCX. Before bootstrap_mcx_catchup() existed, a single
    equity-only market-phase check gated the WHOLE process's early exit, which silently
    prevented MCX from ever being scheduled on exactly this kind of late start - see
    bootstrap.py's bootstrap_mcx_catchup()/detect_mcx_market_phase() for the fix.
3c. bootstrap.bootstrap_nifty_options_catchup() / bootstrap_banknifty_options_catchup() -
    the SAME idea as step 3, but for the two INDEX options engines
    (config.ENABLE_NIFTY_OPTIONS_TRADING / config.ENABLE_BANKNIFTY_OPTIONS_TRADING), run
    UNCONDITIONALLY alongside steps 3/3b. These two share the NSE EQUITY session (no
    separate phase concept of their own - see bootstrap._bootstrap_options_catchup()'s
    docstring), so they don't affect step 4's exit decision at all. Before this existed,
    job_nifty_options_scan()/job_banknifty_options_scan() were ONLY ever triggered by their
    fixed 09:20 cron job - a process started any time after that (even seconds after, let
    alone mid-session) silently never ran them for the rest of the day, since APScheduler
    resolves an already-passed cron time to "tomorrow". KNOWN LIMITATION (see
    bootstrap.py's _bootstrap_options_catchup()/bootstrap_mcx_options_catchup() docstrings):
    unlike steps 3/3b's FUTURES catch-up, none of the three OPTIONS catch-ups reconcile
    broker-held positions/orders - NiftyOptionsEngine's shape (single `.option` leg per
    market; mcx_options_engine.MCXOptionsEngine is N of those, one per commodity) isn't the
    flat `.trackers` dict bootstrap._reconcile_broker_state() walks, and doesn't support it yet.
4. Exit only once ALL THREE session-phase checks say POST_CLOSE for the day - equity
   (`result`), MCX futures (`mcx_result`), AND MCX options (`mcx_options_result`) - log why
   and return. An equity POST_CLOSE alone is no longer sufficient to exit while MCX might
   still be open, and (see bootstrap_mcx_options_catchup()'s own docstring) MCX futures being
   disabled/POST_CLOSE-faked alone is no longer sufficient either, if MCX options is enabled
   and the MCX session is genuinely still open. (NIFTY/BANKNIFTY options ride on the equity
   session and have no separate say in this - see step 3c.)
5. Schedule the recurring/remaining jobs. Because bootstrap already handled
   catch-up as a one-time synchronous step, there's no next_run_time
   trickery needed here anymore - the one-off jobs just use their normal
   cron times, which correctly resolve to "tomorrow" via APScheduler's own
   semantics if today's occurrence has already passed. job_nifty_options_scan()/
   job_banknifty_options_scan() are now ALSO guarded the same way
   job_opening_range_scan() always was - only cron-scheduled if step 3c didn't already
   produce an engine today (see the guard right before scheduler.add_job(job_nifty_options_scan...)
   below); job_mcx_options_scan() gets the same treatment, mirroring job_mcx_commodity_scan()'s
   own guard. MCX's own candle checks (BOTH futures and options) run on a SEPARATE cron
   job/trigger (job_check_commodity_signals) from equities'/options' (job_check_signals), so
   MCX can run at its own configurable candle resolution (config.MCX_CANDLE_INTERVAL_MINUTES -
   see config.py) independent of equities' fixed CANDLE_INTERVAL_MINUTES.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import commodity_engine
import config
import mcx_options_engine
import options_engine
from bootstrap import (MarketPhase, _time_from_hms, bootstrap_and_catchup, bootstrap_mcx_catchup,
                        bootstrap_mcx_options_catchup,
                        bootstrap_nifty_options_catchup, bootstrap_banknifty_options_catchup,
                        decide_todays_direction, decide_todays_commodity_direction,
                        fetch_and_seed_volume_windows)
from fyers_service import FyersAuthManager, FyersDataService
from global_sentiment import fetch_global_snapshot, score_global_bias, get_stock_news_ema_bias
from sector_scanner import get_index_snapshot, get_sector_leaderboard
from opening_range_scanner import scan_opening_range, scan_opening_range_accurate
from signal_engine import SignalEngine

# ----------------------------------------------------------------------------
# Logging: console + file handler, per the brief's "include logging" requirement
# ----------------------------------------------------------------------------
os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.FileHandler(config.LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger("orb_agent.main")

TZ = pytz.timezone(config.TIMEZONE)

# Shared mutable state the scheduled jobs read/write. "signal_engine" is the original
# NSE-equity engine; "options_signal_engine" / "banknifty_options_signal_engine" /
# "commodity_signal_engine" / "mcx_options_signal_engine" are the opt-in
# (config.ENABLE_NIFTY_OPTIONS_TRADING / config.ENABLE_BANKNIFTY_OPTIONS_TRADING /
# config.ENABLE_MCX_COMMODITY_TRADING / config.ENABLE_MCX_OPTIONS_TRADING) parallel engines
# from options_engine.py (NIFTY and BANKNIFTY are two independent instances of the SAME
# NiftyOptionsEngine class, just constructed with different underlying/index/futures config)
# / commodity_engine.py / mcx_options_engine.py - all five run completely independently
# (separate engine instances, separate trade books), so a failure or empty shortlist in one
# never affects the others. MCX futures and MCX options share the same MCX session clock (see
# job_check_commodity_signals()/job_mcx_square_off_and_report() below) but are still two
# completely separate engines/capital buckets/trade books, exactly like every other pair here.
state = {"data_service": None, "signal_engine": None,
         "options_signal_engine": None, "banknifty_options_signal_engine": None,
         "commodity_signal_engine": None, "mcx_options_signal_engine": None}


def _cron_from_hms(hms: str) -> CronTrigger:
    h, m, s = map(int, hms.split(":"))
    return CronTrigger(hour=h, minute=m, second=s, day_of_week="mon-fri", timezone=TZ)


def _should_use_fast_opening_range_path(now: datetime) -> bool:
    """job_opening_range_scan() only fires via its normal cron trigger now (see module
    docstring) - this stays as a safety net for the rare case where coalesce/
    misfire_grace_time lets it fire a little late, e.g. under transient runtime delay.
    Compares against config.CANDLE_DIRECTION_SCAN_TIME instead of OPENING_RANGE_SCAN_TIME
    when config.USE_CANDLE_BASED_DIRECTION is on, since THAT's the time it's actually
    scheduled at in that mode (see main()'s scheduler wiring) - otherwise this would always
    see itself as "late" (by ~10 minutes, every single day) and never take the fast path."""
    scan_time = config.CANDLE_DIRECTION_SCAN_TIME if config.USE_CANDLE_BASED_DIRECTION else config.OPENING_RANGE_SCAN_TIME
    t = _time_from_hms(scan_time)
    scheduled = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    return abs((now - scheduled).total_seconds()) <= config.OPENING_RANGE_FAST_PATH_TOLERANCE_SECONDS


# ----------------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------------
def job_global_sentiment():
    logger.info("Running pre-market global sentiment check...")
    snapshot = fetch_global_snapshot()
    label, score = score_global_bias(snapshot)
    logger.info("Global cues bias: %s (score %.2f) | Snapshot: %s", label, score, snapshot)
    print(f"\n=== Global Market Bias: {label} (score {score:.2f}) ===")
    print(snapshot)

    nifty_bias = get_stock_news_ema_bias("^NSEI", anthropic_api_key=config.ANTHROPIC_API_KEY)
    logger.info("Nifty 50 news+EMA bias: %s (ema=%s, news=%s)",
                nifty_bias["combined_bias"], nifty_bias["ema_bias"], nifty_bias["news_bias"])
    print(f"\n=== Nifty 50 News + EMA Bias: {nifty_bias['combined_bias']} ===")
    print(f"EMA: {nifty_bias['ema_bias']} {nifty_bias['ema_detail']}")
    if nifty_bias["news_bias"]:
        print(f"News: {nifty_bias['news_bias']} - {nifty_bias['news_reason']}")
        print(f"(from {len(nifty_bias['headlines_used'])} headline(s))")
    else:
        print("News: skipped (no ANTHROPIC_API_KEY configured, or no headlines returned)")

    # NEW: what today will actually trade, per global sentiment -> direction (see
    # bootstrap.decide_todays_direction / trade_direction.py) - or, if
    # config.USE_CANDLE_BASED_DIRECTION is on, a price-action read off NIFTY50's own opening
    # candles instead (purely informational here either way; called this early it will likely
    # come back WAITING_FOR_CANDLES in candle mode, since the deciding candles haven't closed
    # yet at this pre-market point). This is purely informational here - the SAME function is
    # called again, authoritatively, right before the shortlist is actually built
    # (job_opening_range_scan / bootstrap_and_catchup), since the reading can change between
    # this pre-market check and the actual scan time.
    direction, sentiment_label, sentiment_score = decide_todays_direction(state["data_service"])
    mode = "opening candles" if config.USE_CANDLE_BASED_DIRECTION else "global sentiment"
    print(f"\n=== Today's Trade Direction (from {mode}): "
          f"{direction.value if direction else 'NONE - no new positions today'} ===")
    print(f"({sentiment_label}, score {sentiment_score:.2f})")


def job_sector_scan(label: str = ""):
    logger.info("Running sector scan (%s)...", label or "unlabeled")
    snapshot = get_index_snapshot(state["data_service"])
    if snapshot.empty:
        return

    leaderboard = get_sector_leaderboard(snapshot)
    if not leaderboard.empty:
        print("\n=== Top Gaining Sectors ===")
        print(leaderboard.head(5).to_string(index=False))
        print("\n=== Top Losing Sectors ===")
        print(leaderboard.tail(5).to_string(index=False))

    context_names = ["Nifty 50", "Nifty 500", "India VIX"]
    context = snapshot[snapshot["index_name"].isin(context_names)]
    if not context.empty:
        print("\n=== Broader Market Context ===")
        print(context[["index_name", "ltp", "pct_change"]].to_string(index=False))


def job_opening_range_scan():
    """Opt-in (config.ENABLE_STOCK_TRADING) - fires via its normal cron trigger
    (config.OPENING_RANGE_SCAN_TIME, or config.CANDLE_DIRECTION_SCAN_TIME when
    config.USE_CANDLE_BASED_DIRECTION is on - see main()'s scheduler wiring) - see main()'s
    docstring for why the mid-session catch-up path no longer needs to call this directly.

    Computes today's direction fresh (via bootstrap.decide_todays_direction()) right before
    building the shortlist, rather than reusing job_global_sentiment()'s earlier read - the
    reading can move in the gap between the pre-market check and the actual scan time, and
    this is the call that actually decides what gets traded, so it should be as current as
    possible."""
    if not config.ENABLE_STOCK_TRADING:
        return
    direction, sentiment_label, sentiment_score = decide_todays_direction(state["data_service"])
    logger.info("Opening-range scan direction: %s (%s, score %.2f)",
                direction.value if direction else "NONE", sentiment_label, sentiment_score)
    if sentiment_label == "WAITING_FOR_CANDLES":
        logger.error("Opening-range scan fired but candle-based direction says it's STILL waiting "
                      "for its deciding candle(s) - config.CANDLE_DIRECTION_SCAN_TIME is probably "
                      "set earlier than the candles actually close; check it against "
                      "config.CANDLE_DIRECTION_NUM_CANDLES/MARKET_OPEN_TIME.")
        return
    if direction is None:
        logger.warning("No clear direction today (%s, score %.2f) - and config.ALLOW_LONG_ON_NEUTRAL "
                        "/ ALLOW_SHORT_ON_NEUTRAL are both False, so no new positions will be opened "
                        "today.", sentiment_label, sentiment_score)
        return

    now = datetime.now(TZ)
    if _should_use_fast_opening_range_path(now):
        logger.info("Running %s opening-range scan (on-time, quotes-proxy path)...", direction.value)
        shortlist = scan_opening_range(state["data_service"], direction=direction)
    else:
        logger.info("Running %s opening-range scan (fired late at %s IST - using the "
                    "slower but always-correct historical-candle path)...",
                    direction.value, now.strftime("%H:%M:%S"))
        shortlist = scan_opening_range_accurate(state["data_service"], direction=direction)

    if shortlist.empty:
        logger.warning("No stocks matched the %s opening-range filter today.", direction.value)
        return
    engine = SignalEngine(state["data_service"], shortlist, direction=direction)
    # Seed the volume-MA windows from the prior session so entries can fire starting the
    # 09:30 candle as documented, not after ~100 minutes of live-only warm-up - see
    # signal_engine.seed_volume_windows_from_candles's docstring.
    fetch_and_seed_volume_windows(engine, state["data_service"])
    state["signal_engine"] = engine
    print(f"\n=== Opening Range {direction.value} Shortlist (Top {config.TOP_N_STOCKS}) ===")
    print(shortlist.to_string(index=False))


def job_nifty_options_scan():
    """Opt-in (config.ENABLE_NIFTY_OPTIONS_TRADING) - builds today's single-source, BOTH-SIDES
    NIFTY options engine (options_engine.NiftyOptionsEngine): PRICE/VWAP/VOLUME signal all from
    config.NIFTY_FUTURES_SYMBOL, strike spot read from config.NIFTY_INDEX_SYMBOL AT SIGNAL TIME
    (not now), CE bought on an upside breakout / PE on a downside one - whichever the strategy's
    own entry criteria confirms first, not a pre-computed direction - see options_engine.py's
    module docstring for the full rule. Reuses today's ALREADY-COMPUTED equity direction
    (decide_todays_direction() is called once per day, not once per market) only to gate the
    opening-range quality filter, so equities and options can never silently disagree on
    what counts as a clean opening candle today."""
    if not config.ENABLE_NIFTY_OPTIONS_TRADING:
        return
    direction, sentiment_label, sentiment_score = decide_todays_direction(state["data_service"])
    if sentiment_label == "WAITING_FOR_CANDLES":
        logger.error("NIFTY options scan fired but candle-based direction says it's STILL waiting "
                      "for its deciding candle(s) - check config.CANDLE_DIRECTION_SCAN_TIME against "
                      "config.CANDLE_DIRECTION_NUM_CANDLES/MARKET_OPEN_TIME.")
        return
    if direction is None:
        logger.warning("No clear direction today (%s) - skipping NIFTY options for today.",
                        sentiment_label)
        return
    engine = options_engine.run_nifty_options_engine(state["data_service"], direction)
    if engine is None:
        logger.warning("NIFTY options engine could not be built today (see options_engine logs above) "
                        "- no options position will be tracked today.")
        return
    fetch_and_seed_volume_windows(engine, state["data_service"])
    state["options_signal_engine"] = engine
    print(f"\n=== NIFTY Options Engine Ready ===")
    print(f"Signal source: futures={engine.futures_tracker.symbol} "
          f"(OR high={engine.futures_tracker.or_high:.2f}, low={engine.futures_tracker.or_low:.2f}) - "
          f"watching BOTH directions; buys a CE on an upside breakout, a PE on a downside breakout, "
          f"whichever fires first (today's {direction.value} bias only gated this morning's "
          f"opening-range quality filter, it doesn't pick CE/PE any more).")
    print(f"Strike: mode={engine.strike_mode or config.OPTION_STRIKE_MODE}, "
          f"strikes_away={engine.strikes_away}, resolved fresh from index={config.NIFTY_INDEX_SYMBOL} "
          f"spot AT SIGNAL TIME (capital_allocated={engine.capital_allocated:.2f}) - no option leg "
          f"is known yet; one will appear here once an entry actually fires.")


def job_banknifty_options_scan():
    """Opt-in (config.ENABLE_BANKNIFTY_OPTIONS_TRADING) - identical single-source, BOTH-SIDES
    engine to job_nifty_options_scan() above (same NiftyOptionsEngine class, same rule set - see
    options_engine.py's module docstring), pointed at BANKNIFTY's own index/futures/strike
    universe (config.BANKNIFTY_*) instead of NIFTY's. Reuses the SAME today's-direction
    value as equities/NIFTY-options (one sentiment decision for the whole day, never
    recomputed per market) only to gate the opening-range quality filter - it no longer picks
    CE vs PE - and runs completely independently of the NIFTY options engine - both can be
    enabled at once, each with its own capital bucket and trade book."""
    if not config.ENABLE_BANKNIFTY_OPTIONS_TRADING:
        return
    # index_symbol=BANKNIFTY_INDEX_SYMBOL - only matters in config.USE_CANDLE_BASED_DIRECTION
    # mode, where BANKNIFTY reads its OWN index's opening candles rather than NIFTY50's (see
    # bootstrap.decide_todays_direction()'s docstring); ignored in sentiment mode, where this
    # still returns the SAME equity-cue sentiment value as job_nifty_options_scan().
    direction, sentiment_label, sentiment_score = decide_todays_direction(
        state["data_service"], index_symbol=config.BANKNIFTY_INDEX_SYMBOL)
    if sentiment_label == "WAITING_FOR_CANDLES":
        logger.error("BANKNIFTY options scan fired but candle-based direction says it's STILL "
                      "waiting for its deciding candle(s) - check config.CANDLE_DIRECTION_SCAN_TIME "
                      "against config.CANDLE_DIRECTION_NUM_CANDLES/MARKET_OPEN_TIME.")
        return
    if direction is None:
        logger.warning("No clear direction today (%s) - skipping BANKNIFTY options for today.",
                        sentiment_label)
        return
    engine = options_engine.run_banknifty_options_engine(state["data_service"], direction)
    if engine is None:
        logger.warning("BANKNIFTY options engine could not be built today (see options_engine logs "
                        "above) - no options position will be tracked today.")
        return
    fetch_and_seed_volume_windows(engine, state["data_service"])
    state["banknifty_options_signal_engine"] = engine
    print(f"\n=== BANKNIFTY Options Engine Ready ===")
    print(f"Signal source: futures={engine.futures_tracker.symbol} "
          f"(OR high={engine.futures_tracker.or_high:.2f}, low={engine.futures_tracker.or_low:.2f}) - "
          f"watching BOTH directions; buys a CE on an upside breakout, a PE on a downside breakout, "
          f"whichever fires first (today's {direction.value} bias only gated this morning's "
          f"opening-range quality filter, it doesn't pick CE/PE any more).")
    print(f"Strike: mode={engine.strike_mode or config.OPTION_STRIKE_MODE}, "
          f"strikes_away={engine.strikes_away}, resolved fresh from index={config.BANKNIFTY_INDEX_SYMBOL} "
          f"spot AT SIGNAL TIME (capital_allocated={engine.capital_allocated:.2f}) - no option leg "
          f"is known yet; one will appear here once an entry actually fires.")


def job_mcx_commodity_scan():
    """Opt-in (config.ENABLE_MCX_COMMODITY_TRADING) - mirrors job_nifty_options_scan() but
    for MCX commodity futures (commodity_engine.py), which - unlike options - trade
    today's bias direction AS-IS (long or short, same as equities), against the WHOLE
    config.MCX_COMMODITY_UNIVERSE directly (no sector/gain-band scan - see
    commodity_engine.py's module docstring). Direction comes from
    decide_todays_commodity_direction() - a COMMODITY-specific global sentiment read
    (gold/silver/crude/copper/natural-gas/dollar-index cues), deliberately a SEPARATE
    sentiment decision from equities'/NIFTY's/BANKNIFTY's equity-cue
    decide_todays_direction(), since MCX trades against global commodity prices, not
    global equity indices - the two can end up pointing different directions on the same
    day."""
    if not config.ENABLE_MCX_COMMODITY_TRADING:
        return
    direction, sentiment_label, sentiment_score = decide_todays_commodity_direction(state["data_service"])
    if sentiment_label == "WAITING_FOR_CANDLES":
        logger.error("MCX commodity scan fired but candle-based direction says it's STILL waiting "
                      "for its deciding candle(s) - check config.MCX_CANDLE_DIRECTION_SCAN_TIME "
                      "against config.MCX_CANDLE_DIRECTION_NUM_CANDLES/MCX_MARKET_OPEN_TIME.")
        return
    if direction is None:
        logger.warning("No clear direction today (%s) - skipping MCX commodities for today.",
                        sentiment_label)
        return
    engine = commodity_engine.run_mcx_commodity_engine(state["data_service"], direction)
    if engine is None:
        logger.warning("No opening-range candle was available for any MCX commodity today - "
                        "nothing tracked today.")
        return
    # Seed at MCX's own candle resolution (config.MCX_CANDLE_INTERVAL_MINUTES - see Task #6),
    # not equities' default "5" - the prior-session volume-MA warm-up has to be sliced at the
    # SAME width job_check_commodity_signals() will actually check live.
    fetch_and_seed_volume_windows(engine, state["data_service"],
                                   resolution=str(config.MCX_CANDLE_INTERVAL_MINUTES))
    state["commodity_signal_engine"] = engine
    print(f"\n=== MCX Commodity Universe ({direction.value}, commodity sentiment: {sentiment_label}) ===")
    print(list(engine.trackers))


def job_mcx_options_scan():
    """Opt-in (config.ENABLE_MCX_OPTIONS_TRADING) - the OPTIONS counterpart to
    job_mcx_commodity_scan() above, same MCX session/scan time, but builds
    mcx_options_engine.MCXOptionsEngine instead of a commodity_engine.SignalEngine: EVERY
    commodity in config.MCX_COMMODITY_UNIVERSE watches its OWN futures candles for a breakout
    in EITHER direction (not today's shared bias) and buys that SAME commodity's own CE/PE -
    see mcx_options_engine.py's module docstring for the full rule set (identical to NIFTY/
    BANKNIFTY options, just run independently per commodity instead of once for one index).
    Reuses the SAME decide_todays_commodity_direction() read job_mcx_commodity_scan() uses
    (never recomputed separately) only to gate each contract's own opening-range quality
    filter - it does NOT pick CE/PE for any commodity. Runs completely independently of
    job_mcx_commodity_scan() - both can be enabled at once, each with its own capital bucket
    and trade book."""
    if not config.ENABLE_MCX_OPTIONS_TRADING:
        return
    direction, sentiment_label, sentiment_score = decide_todays_commodity_direction(state["data_service"])
    if sentiment_label == "WAITING_FOR_CANDLES":
        logger.error("MCX options scan fired but candle-based direction says it's STILL waiting "
                      "for its deciding candle(s) - check config.MCX_CANDLE_DIRECTION_SCAN_TIME "
                      "against config.MCX_CANDLE_DIRECTION_NUM_CANDLES/MCX_MARKET_OPEN_TIME.")
        return
    if direction is None:
        logger.warning("No clear direction today (%s) - skipping MCX options for today.",
                        sentiment_label)
        return
    engine = mcx_options_engine.run_mcx_options_engine(state["data_service"], direction)
    if engine is None:
        logger.warning("No opening-range candle was available for any MCX commodity today (or "
                        "the MCX options symbol master is empty/unreadable) - nothing tracked "
                        "today.")
        return
    # Seed at MCX's own candle resolution, same reasoning as job_mcx_commodity_scan() above.
    fetch_and_seed_volume_windows(engine, state["data_service"],
                                   resolution=str(config.MCX_CANDLE_INTERVAL_MINUTES))
    state["mcx_options_signal_engine"] = engine
    print(f"\n=== MCX Options Universe ({direction.value}, commodity sentiment: {sentiment_label}) ===")
    print(f"Watching BOTH directions per commodity; buys that commodity's own CE on an upside "
          f"breakout, PE on a downside one, whichever fires first (today's {direction.value} "
          f"bias only gated each contract's own opening-range quality filter).")
    print(list(engine.trackers))


def job_check_signals():
    """Checks the equity engine (always) plus the NIFTY/BANKNIFTY options engines (if the
    opt-in scans above produced one) every CANDLE_INTERVAL_MINUTES, all sharing the NSE
    equity session's own SIGNAL_ENGINE_LAST_RUN (15:10) cutoff. MCX commodities are
    deliberately NOT checked here any more - see job_check_commodity_signals() below for why
    (MCX needs its own, independently-configurable candle resolution and a much later
    cutoff/cron window than equities/options)."""
    now = datetime.now(TZ)
    window_end = now.replace(second=0, microsecond=0)
    window_start = window_end - timedelta(minutes=config.CANDLE_INTERVAL_MINUTES)
    start_epoch, end_epoch = int(window_start.timestamp()), int(window_end.timestamp())

    if now.time() > _time_from_hms(config.SIGNAL_ENGINE_LAST_RUN):
        return  # past the NSE session's "keep checking" window - nothing left to do here today

    for label, engine in (
        ("equity", state["signal_engine"]),
        ("options", state["options_signal_engine"]),
        ("banknifty_options", state["banknifty_options_signal_engine"]),
    ):
        if engine is None:
            continue  # that market's opening-range scan hasn't run / found nothing yet
        engine.check_all(start_epoch, end_epoch)


def job_check_commodity_signals():
    """MCX's own candle-check job, split out from job_check_signals() above specifically so
    MCX can run on its OWN candle resolution (config.MCX_CANDLE_INTERVAL_MINUTES - see
    config.py/Task #6) independent of equities'/options' config.CANDLE_INTERVAL_MINUTES - one
    shared APScheduler CronTrigger can't fire at two different "every N minutes" cadences at
    once, so this needs its own trigger in main() below, not just its own function. Also
    keeps running (09:00 up to MCX_SIGNAL_ENGINE_LAST_RUN, typically 23:14:30) long after
    equities'/options' own job_check_signals has stopped for the day - see main()'s scheduler
    wiring and bootstrap.bootstrap_mcx_catchup()/detect_mcx_market_phase() for why MCX's
    session is treated as fully independent of NSE's throughout this codebase.

    Drives BOTH MCX engines - futures (commodity_signal_engine) AND options
    (mcx_options_signal_engine) - since they share this exact same session clock and candle
    resolution (mirrors job_check_signals()'s own equity/NIFTY-options/BANKNIFTY-options
    tuple-loop above); either, both, or neither may be active today depending on
    config.ENABLE_MCX_COMMODITY_TRADING/ENABLE_MCX_OPTIONS_TRADING."""
    now = datetime.now(TZ)
    if now.time() > _time_from_hms(config.MCX_SIGNAL_ENGINE_LAST_RUN):
        return  # past MCX's own "keep checking" window

    window_end = now.replace(second=0, microsecond=0)
    window_start = window_end - timedelta(minutes=config.MCX_CANDLE_INTERVAL_MINUTES)
    start_epoch, end_epoch = int(window_start.timestamp()), int(window_end.timestamp())

    for label, engine in (
        ("mcx_futures", state["commodity_signal_engine"]),
        ("mcx_options", state["mcx_options_signal_engine"]),
    ):
        if engine is None:
            continue  # that MCX engine's opening-range scan hasn't run / found nothing yet today
        engine.check_all(start_epoch, end_epoch)


def _report_and_save(engine: SignalEngine, label: str, csv_suffix: str = ""):
    engine.square_off_all()
    df = engine.trade_book.as_dataframe()
    summary = engine.trade_book.summary()

    print(f"\n=== {label} Trade Book ===")
    print(df.to_string(index=False) if not df.empty else "No trades were triggered today.")
    print(f"\n=== {label} Summary (points captured = profit/loss; pnl_amount = rupee P&L) ===")
    print(summary)

    out_path = config.TRADE_LOG_CSV.format(date=datetime.now(TZ).strftime("%Y%m%d") + csv_suffix)
    df.to_csv(out_path, index=False)
    logger.info("%s trade book saved to %s", label, out_path)


def job_square_off_and_report():
    """Squares off the equity engine, AND (if active) the NIFTY/BANKNIFTY options engines -
    all three run on the NSE equity session and close together at SQUARE_OFF_TIME. MCX runs
    its own, separate square-off job (job_mcx_square_off_and_report) much later in the day,
    since MCX's session stays open well past NSE's close."""
    engine = state["signal_engine"]
    if engine is None:
        logger.info("No equity signal engine was active today - nothing to square off.")
    else:
        _report_and_save(engine, "Equity")

    options_eng = state["options_signal_engine"]
    if options_eng is not None:
        _report_and_save(options_eng, "NIFTY Options", csv_suffix="_options")

    banknifty_options_eng = state["banknifty_options_signal_engine"]
    if banknifty_options_eng is not None:
        _report_and_save(banknifty_options_eng, "BANKNIFTY Options", csv_suffix="_banknifty_options")


def job_mcx_square_off_and_report():
    """Fires at config.MCX_SQUARE_OFF_TIME, well after NSE/equity close, since MCX's own
    session runs much later in the day. Squares off BOTH MCX engines - futures
    (config.ENABLE_MCX_COMMODITY_TRADING) and options (config.ENABLE_MCX_OPTIONS_TRADING) -
    independently (mirrors job_square_off_and_report()'s own per-engine pattern above, rather
    than one shared top-level gate): either, both, or neither may be active today, and one
    being absent never skips reporting the other."""
    engine = state["commodity_signal_engine"]
    if engine is None:
        logger.info("No MCX commodity engine was active today - nothing to square off.")
    else:
        _report_and_save(engine, "MCX Commodity", csv_suffix="_mcx")

    options_eng = state["mcx_options_signal_engine"]
    if options_eng is None:
        logger.info("No MCX options engine was active today - nothing to square off.")
    else:
        _report_and_save(options_eng, "MCX Options", csv_suffix="_mcx_options")


# ----------------------------------------------------------------------------
# Bootstrap + scheduler wiring
# ----------------------------------------------------------------------------
def main():
    logger.info("Starting ORB scanning agent (DRY_RUN=%s)...", config.DRY_RUN)

    auth = FyersAuthManager()
    access_token = auth.get_access_token()
    state["data_service"] = FyersDataService(access_token)

    job_global_sentiment()

    result = bootstrap_and_catchup(state["data_service"])
    logger.info("Bootstrap result: phase=%s | %s", result.phase.value, result.message)

    # MCX runs on a completely separate session/clock from NSE equities (see this module's
    # docstring) - its own catch-up ALWAYS runs here (self-gated internally on
    # config.ENABLE_MCX_COMMODITY_TRADING), never skipped just because equities are closed.
    mcx_result = bootstrap_mcx_catchup(state["data_service"])
    logger.info("MCX bootstrap result: phase=%s | %s", mcx_result.phase.value, mcx_result.message)

    # MCX OPTIONS - same MCX session/clock as mcx_result above, but a SEPARATE engine/flag
    # (config.ENABLE_MCX_OPTIONS_TRADING) - see bootstrap.bootstrap_mcx_options_catchup()'s own
    # docstring for why its phase genuinely needs to be checked separately below (rather than
    # assumed identical to mcx_result's): if MCX futures is disabled but MCX options is enabled,
    # mcx_result.phase is faked POST_CLOSE (futures' own "disabled" gate) even while MCX is
    # genuinely still open - mcx_options_result.phase is what correctly keeps the process alive
    # in exactly that case.
    mcx_options_result = bootstrap_mcx_options_catchup(state["data_service"])
    logger.info("MCX options bootstrap result: phase=%s | %s",
                mcx_options_result.phase.value, mcx_options_result.message)

    # NIFTY/BANKNIFTY options - same NSE equity session as `result` above (no separate phase
    # concept, see bootstrap._bootstrap_options_catchup()'s docstring), so these don't affect
    # the exit decision below at all; they just mean a late start (any time after 09:20:02, not
    # only before equities' own POST_CLOSE) now reconstructs these two instead of silently
    # missing them for the rest of the day - see bootstrap.py's bootstrap_nifty_options_catchup()/
    # bootstrap_banknifty_options_catchup() for what used to happen before this existed.
    nifty_options_result = bootstrap_nifty_options_catchup(state["data_service"])
    logger.info("NIFTY options bootstrap result: phase=%s | %s",
                nifty_options_result.phase.value, nifty_options_result.message)
    banknifty_options_result = bootstrap_banknifty_options_catchup(state["data_service"])
    logger.info("BANKNIFTY options bootstrap result: phase=%s | %s",
                banknifty_options_result.phase.value, banknifty_options_result.message)

    # Equities/NIFTY-options/BANKNIFTY-options all run on the NSE session - once it's
    # POST_CLOSE there's nothing left for THEM to do today. MCX (futures AND options) runs an
    # entirely separate, much longer session (09:00-23:30 by default) - so the whole process
    # only exits here once ALL THREE phases are done for the day (or each MCX engine isn't
    # enabled at all, in which case its own bootstrap call already reported it as POST_CLOSE
    # above - see bootstrap_mcx_options_catchup()'s docstring for exactly why checking BOTH mcx_
    # result.phase AND mcx_options_result.phase here, not just one, is what keeps this correct
    # for every combination of config.ENABLE_MCX_COMMODITY_TRADING/ENABLE_MCX_OPTIONS_TRADING).
    # An equity POST_CLOSE at, say, 17:51 IST must NOT by itself stop a still-open MCX session
    # from being scheduled - that was exactly the bug this decoupling fixes.
    if (result.phase == MarketPhase.POST_CLOSE and mcx_result.phase == MarketPhase.POST_CLOSE
            and mcx_options_result.phase == MarketPhase.POST_CLOSE):
        logger.info("Exiting - the equity session and MCX (futures and options) are all closed "
                    "(or disabled); nothing left to do today.")
        return

    if result.signal_engine is not None:
        state["signal_engine"] = result.signal_engine
    if mcx_result.signal_engine is not None:
        state["commodity_signal_engine"] = mcx_result.signal_engine
    if mcx_options_result.signal_engine is not None:
        state["mcx_options_signal_engine"] = mcx_options_result.signal_engine
    if nifty_options_result.signal_engine is not None:
        state["options_signal_engine"] = nifty_options_result.signal_engine
    if banknifty_options_result.signal_engine is not None:
        state["banknifty_options_signal_engine"] = banknifty_options_result.signal_engine

    common_kwargs = dict(coalesce=True, max_instances=1)
    scheduler = BackgroundScheduler(timezone=TZ)

    scheduler.add_job(job_sector_scan, _cron_from_hms(config.SECTOR_SCAN_TIME),
                       id="sector_scan_preopen", misfire_grace_time=60,
                       kwargs={"label": "09:10 pre-open snapshot - likely stale, see sector_scanner.py caveat"},
                       **common_kwargs)
    scheduler.add_job(job_sector_scan, _cron_from_hms(config.SECTOR_SCAN_TIME_POSTOPEN),
                       id="sector_scan_postopen", misfire_grace_time=60,
                       kwargs={"label": "09:16 post-open - real numbers"}, **common_kwargs)

    # config.USE_CANDLE_BASED_DIRECTION shifts WHEN direction can first be decided (the
    # deciding candle(s) have to close first - see config.py's docstring on this flag), so the
    # equity/options scan moves from OPENING_RANGE_SCAN_TIME to CANDLE_DIRECTION_SCAN_TIME, and
    # MCX's from MCX_OPENING_RANGE_SCAN_TIME to MCX_CANDLE_DIRECTION_SCAN_TIME, whenever it's on.
    scan_time = config.CANDLE_DIRECTION_SCAN_TIME if config.USE_CANDLE_BASED_DIRECTION else config.OPENING_RANGE_SCAN_TIME
    mcx_scan_time = (config.MCX_CANDLE_DIRECTION_SCAN_TIME if config.USE_CANDLE_BASED_DIRECTION
                      else config.MCX_OPENING_RANGE_SCAN_TIME)

    # Only schedule the opening-range job if bootstrap DIDN'T already produce a shortlist -
    # if it did (mid-session catch-up succeeded), today's shortlist is already built and
    # tracked; if bootstrap ran and found nothing (phase POST_OPEN, empty shortlist), the
    # underlying 09:15 candle is fixed and re-running this later today can't change that
    # answer, so it's still correctly skipped. It needs to actually run via cron for the
    # normal PRE_OPEN-start case, AND (config.USE_CANDLE_BASED_DIRECTION only) whenever
    # catch-up ran too early to decide direction yet (result.needs_scheduled_retry - see
    # BootstrapResult's docstring) - without this OR, a start between market open and the
    # candle-decision time would fall through both the "catch-up succeeded" case and the
    # PRE_OPEN-only guard, silently missing today's scan entirely even though its own
    # scheduled time hasn't passed yet.
    if result.signal_engine is None and (result.phase == MarketPhase.PRE_OPEN or result.needs_scheduled_retry):
        scheduler.add_job(job_opening_range_scan, _cron_from_hms(scan_time),
                           id="opening_range_scan", misfire_grace_time=30, **common_kwargs)

    # Opt-in (self-gated on their own config.ENABLE_* flag - see each job's docstring) AND,
    # same as the equity opening_range_scan guard above, only cron-scheduled if bootstrap
    # DIDN'T already produce an engine today via bootstrap_nifty_options_catchup()/
    # bootstrap_banknifty_options_catchup() (a mid-session catch-up start), OR that catch-up
    # ran too early to decide a candle-based direction yet (needs_scheduled_retry - same
    # reasoning as the equity guard above, checked against EACH options result's OWN flag,
    # not the shared equity `result`, since NIFTY/BANKNIFTY each decide their candle-based
    # direction from their OWN index's candles and could in principle become ready at
    # slightly different moments even though they share CANDLE_DIRECTION_NUM_CANDLES/
    # MARKET_OPEN_TIME today). Before this guard existed, these two were scheduled
    # unconditionally every run, which on a catch-up start would have left them scheduled to
    # run AGAIN at the fixed cron time - pointless (the opening-range candle can't change) and
    # would have clobbered the already-reconstructed engine in `state` with a fresh,
    # history-less one.
    # NIFTY/BANKNIFTY options share the NSE equity session's opening-range time.
    if state["options_signal_engine"] is None and (result.phase == MarketPhase.PRE_OPEN
                                                     or nifty_options_result.needs_scheduled_retry):
        scheduler.add_job(job_nifty_options_scan, _cron_from_hms(scan_time),
                           id="nifty_options_scan", misfire_grace_time=30, **common_kwargs)
    if state["banknifty_options_signal_engine"] is None and (result.phase == MarketPhase.PRE_OPEN
                                                               or banknifty_options_result.needs_scheduled_retry):
        scheduler.add_job(job_banknifty_options_scan, _cron_from_hms(scan_time),
                           id="banknifty_options_scan", misfire_grace_time=30, **common_kwargs)

    # Only schedule MCX's own cron-triggered opening-range scan if bootstrap_mcx_catchup()
    # didn't already produce an engine today (mirrors the equity opening_range_scan guard
    # above) - if MCX catch-up succeeded (a mid-session start), today's universe is already
    # built and tracked; if MCX is disabled entirely, or genuinely still pre-open for MCX (or,
    # config.USE_CANDLE_BASED_DIRECTION only, MCX catch-up ran too early to decide direction
    # yet), the cron job (itself self-gated on config.ENABLE_MCX_COMMODITY_TRADING) still
    # needs to fire at its normal time - this guard already re-schedules unconditionally
    # whenever state["commodity_signal_engine"] is None for any reason, so
    # mcx_result.needs_scheduled_retry doesn't need an explicit OR here the way the
    # phase-gated equity/options guards above do.
    if state["commodity_signal_engine"] is None:
        scheduler.add_job(job_mcx_commodity_scan, _cron_from_hms(mcx_scan_time),
                           id="mcx_commodity_scan", misfire_grace_time=30, **common_kwargs)

    # MCX OPTIONS counterpart to the guard immediately above - same reasoning, same
    # mcx_scan_time (MCX options share MCX futures' own opening-range/direction-decision
    # timing - there's no separate MCX_OPTIONS_OPENING_RANGE_SCAN_TIME, they read the exact
    # same opening-range candle(s) via the exact same commodity_engine.
    # fetch_mcx_universe_opening_ranges() call - see mcx_options_engine.run_mcx_options_engine()'s
    # docstring), self-gated on config.ENABLE_MCX_OPTIONS_TRADING inside job_mcx_options_scan()
    # itself, independent of the futures guard above (one engine's state being None never
    # affects whether the other gets (re)scheduled).
    if state["mcx_options_signal_engine"] is None:
        scheduler.add_job(job_mcx_options_scan, _cron_from_hms(mcx_scan_time),
                           id="mcx_options_scan", misfire_grace_time=30, **common_kwargs)

    # Equity/options candle-check cron - covers just the NSE session now (equities' own
    # MARKET_OPEN_TIME-to-SIGNAL_ENGINE_LAST_RUN hour span); job_check_signals() itself
    # already skips any engine that's None or past the NSE session's own cutoff.
    equity_start_hour = int(config.MARKET_OPEN_TIME.split(":")[0])
    equity_last_hour = int(config.SIGNAL_ENGINE_LAST_RUN.split(":")[0])
    check_signals_trigger = CronTrigger(minute=f"*/{config.CANDLE_INTERVAL_MINUTES}", second=2,
                                         hour=f"{equity_start_hour}-{equity_last_hour}",
                                         day_of_week="mon-fri", timezone=TZ)
    scheduler.add_job(job_check_signals, check_signals_trigger,
                       id="check_signals", misfire_grace_time=45, **common_kwargs)

    # MCX's OWN candle-check cron, on its OWN configurable resolution
    # (config.MCX_CANDLE_INTERVAL_MINUTES) and its OWN, much later hour span - completely
    # independent of the equity trigger above, which is exactly what lets MCX run any time
    # between MCX_MARKET_OPEN_TIME and MCX_SIGNAL_ENGINE_LAST_RUN regardless of what the NSE
    # equity session is doing. job_check_commodity_signals() drives BOTH MCX engines (futures
    # AND options - see that job's own docstring), so this is scheduled whenever EITHER is
    # enabled, not just futures - a futures-disabled/options-enabled setup (or vice versa)
    # would otherwise never get this cron job at all, silently leaving whichever one IS enabled
    # un-checked for the entire day.
    if config.ENABLE_MCX_COMMODITY_TRADING or config.ENABLE_MCX_OPTIONS_TRADING:
        mcx_start_hour = int(config.MCX_MARKET_OPEN_TIME.split(":")[0])
        mcx_last_hour = int(config.MCX_SIGNAL_ENGINE_LAST_RUN.split(":")[0])
        mcx_check_signals_trigger = CronTrigger(minute=f"*/{config.MCX_CANDLE_INTERVAL_MINUTES}", second=2,
                                                 hour=f"{mcx_start_hour}-{mcx_last_hour}",
                                                 day_of_week="mon-fri", timezone=TZ)
        scheduler.add_job(job_check_commodity_signals, mcx_check_signals_trigger,
                           id="mcx_check_signals", misfire_grace_time=45, **common_kwargs)

    scheduler.add_job(job_square_off_and_report, _cron_from_hms(config.SQUARE_OFF_TIME),
                       id="square_off", misfire_grace_time=60, **common_kwargs)
    scheduler.add_job(job_mcx_square_off_and_report, _cron_from_hms(config.MCX_SQUARE_OFF_TIME),
                       id="mcx_square_off", misfire_grace_time=60, **common_kwargs)

    scheduler.start()
    logger.info("Scheduler started. Waiting for scheduled jobs (Ctrl+C to stop)...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
