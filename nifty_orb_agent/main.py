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
3b. bootstrap.bootstrap_mcx_catchup() - the SAME idea as step 3, but for MCX
    commodities (config.ENABLE_MCX_COMMODITY_TRADING), run UNCONDITIONALLY
    alongside step 3, not gated by it. MCX's own session (09:00-23:30 IST by
    default) is far longer than, and completely independent of, NSE's
    09:15-15:30 equity session - so starting this process at, say, 17:51 IST
    (NSE already closed, MCX still open) must still bootstrap and run MCX.
    Before this existed, a single equity-only market-phase check gated the
    WHOLE process's early exit, which silently prevented MCX from ever being
    scheduled on exactly this kind of late start - see bootstrap.py's
    bootstrap_mcx_catchup()/detect_mcx_market_phase() for the fix.
4. Exit only if BOTH sessions are done for the day (equity POST_CLOSE AND
   (MCX disabled OR MCX also POST_CLOSE)) - log why and return. An equity
   POST_CLOSE alone is no longer sufficient to exit while MCX might still be
   open.
5. Schedule the recurring/remaining jobs. Because bootstrap already handled
   catch-up as a one-time synchronous step, there's no next_run_time
   trickery needed here anymore - the one-off jobs just use their normal
   cron times, which correctly resolve to "tomorrow" via APScheduler's own
   semantics if today's occurrence has already passed. MCX's own candle
   checks run on a SEPARATE cron job/trigger (job_check_commodity_signals)
   from equities'/options' (job_check_signals), so MCX can run at its own
   configurable candle resolution (config.MCX_CANDLE_INTERVAL_MINUTES - see
   config.py) independent of equities' fixed CANDLE_INTERVAL_MINUTES.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dt_time, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import commodity_engine
import config
import options_engine
from bootstrap import (MarketPhase, _time_from_hms, bootstrap_and_catchup, bootstrap_mcx_catchup,
                        decide_todays_direction, decide_todays_commodity_direction,
                        fetch_and_seed_volume_windows)
                        
from global_sentiment import fetch_global_snapshot, score_global_bias, get_stock_news_ema_bias, evaluate_market_direction
from live_tick_stream import LiveTickStream
#from google_global_sentiment import fetch_global_snapshot, score_global_bias
from sector_scanner import get_index_snapshot, get_sector_leaderboard
from opening_range_scanner import scan_opening_range, scan_opening_range_accurate
from signal_engine import SignalEngine
from pathlib import Path
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fyersclient.fyers_client as fyres_client
from fyersclient.fyers_client import get_fyers_client, FyersDataService

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
# "commodity_signal_engine" are the opt-in (config.ENABLE_NIFTY_OPTIONS_TRADING /
# config.ENABLE_BANKNIFTY_OPTIONS_TRADING / config.ENABLE_MCX_COMMODITY_TRADING) parallel
# engines from options_engine.py (NIFTY and BANKNIFTY are two independent instances of the
# SAME NiftyOptionsEngine class, just constructed with different underlying/index/futures
# config) / commodity_engine.py - all four run completely independently (separate engine
# instances, separate trade books), so a failure or empty shortlist in one never affects
# the others.
state = {"data_service": None, "signal_engine": None,
         "options_signal_engine": None,          "banknifty_options_signal_engine": None,
         "commodity_signal_engine": None,
         "top5_gaining_sectors": None, "top5_losing_sectors": None,
         "global_sentiment": {}, "live_tick_stream": None}


def _cron_from_hms(hms: str) -> CronTrigger:
    h, m, s = map(int, hms.split(":"))
    corn_hms = CronTrigger(hour=h, minute=m, second=s, day_of_week="mon-fri", timezone=TZ)
    logger.info(f" corn job starts from {corn_hms}")
    return corn_hms
#    return CronTrigger(hour=h, minute=m, second=s, day_of_week="mon-fri", timezone=TZ)



def _should_use_fast_opening_range_path(now: datetime) -> bool:
    """job_opening_range_scan() only fires via its normal 09:20:02 cron trigger now (see
    module docstring) - this stays as a safety net for the rare case where coalesce/
    misfire_grace_time lets it fire a little late, e.g. under transient runtime delay."""
    t = _time_from_hms(config.OPENING_RANGE_SCAN_TIME)
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
        logger.info("News: skipped (no ANTHROPIC_API_KEY configured, or no headlines returned)")
    sentiment_data = {
        "global_bias": label,
        "global_bias_score": score,
        "global_market": snapshot,
        "nse_bias": nifty_bias['ema_bias'],
        "nse_ema": nifty_bias['ema_detail'],
    }
    sentiment_label = label
    sentiment_score = score
    # NEW: what today will actually trade, per global sentiment -> direction (see
    # bootstrap.decide_todays_direction / trade_direction.py). This is purely informational
    # here - the SAME function is called again, authoritatively, right before the shortlist
    # is actually built (job_opening_range_scan / bootstrap_and_catchup), since sentiment
    # can move between this pre-market check and 09:20:02.
    #direction, sentiment_label, sentiment_score = decide_todays_direction()
    direction = evaluate_market_direction(sentiment_data)
    logger.info(f"\n=== Today's Trade Direction (from global sentiment): "
          f"{direction.value if direction else 'NONE - no new positions today'} ===")
    logger.info(f"(sentiment: {sentiment_label}, score {sentiment_score:.2f})")
    state['global_sentiment'] = {'label': sentiment_label, 'direction': direction, 'score': sentiment_score} 

def job_sector_scan(label: str = ""):
    logger.info("Running sector scan (%s)...", label or "unlabeled")
    snapshot = get_index_snapshot(state["data_service"])
    if snapshot.empty:
        return

    leaderboard = get_sector_leaderboard(snapshot)
    if not leaderboard.empty:
        logger.info("\n=== Top Gaining Sectors ===")
        state["top5_gaining_sectors"] = leaderboard.head(5)
        logger.info(state["top5_gaining_sectors"])
        logger.info("\n=== Top Losing Sectors ===")
        state["top5_losing_sectors"] = leaderboard.tail(5)
        logger.info(leaderboard.tail(5).to_string(index=False))

    context_names = ["Nifty 50", "Nifty 500", "India VIX"]
    context = snapshot[snapshot["index_name"].isin(context_names)]
    if not context.empty:
        print("\n=== Broader Market Context ===")
        print(context[["index_name", "ltp", "pct_change"]].to_string(index=False))


def job_opening_range_scan():
    """Only fires via its normal 09:20:02 cron trigger now - see main()'s docstring for why
    the mid-session catch-up path no longer needs to call this directly.

    Computes today's direction fresh (via bootstrap.decide_todays_direction()) right before
    building the shortlist, rather than reusing job_global_sentiment()'s earlier read -
    sentiment can move in the roughly 10 minutes between the pre-market check and 09:20:02,
    and this is the call that actually decides what gets traded, so it should be as current
    as possible."""
    direction, sentiment_label, sentiment_score = (state['global_sentiment']['direction'], state['global_sentiment']['label'], state['global_sentiment']['score'])  #decide_todays_direction()
    logger.info("Opening-range scan direction: %s (sentiment %s, score %.2f)",
                direction.value if direction else "NONE", sentiment_label, sentiment_score)
    if direction is None:
        logger.warning("Global sentiment is %s (score %.2f) - not a clear bullish/bearish read, "
                        "and config.ALLOW_LONG_ON_NEUTRAL / ALLOW_SHORT_ON_NEUTRAL are both False, "
                        "so no new positions will be opened today.", sentiment_label, sentiment_score)
        return

    now = datetime.now(TZ)
    if state["top5_gaining_sectors"] is None:
        job_sector_scan()
    if _should_use_fast_opening_range_path(now):
        logger.info("Running %s opening-range scan (on-time, quotes-proxy path)...", direction.value)
        shortlist = scan_opening_range(state, direction=direction)
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
    """Opt-in (config.ENABLE_NIFTY_OPTIONS_TRADING) - builds today's dual-source NIFTY options
    engine (options_engine.NiftyOptionsEngine): PRICE/VWAP signal from config.NIFTY_INDEX_SYMBOL,
    VOLUME signal from config.NIFTY_FUTURES_SYMBOL, execution on a single OTM option contract -
    see options_engine.py's module docstring for the full rule. Reuses today's
    ALREADY-COMPUTED equity direction (decide_todays_direction() is called once per day, not
    once per market) so equities and options can never silently disagree on today's
    sentiment-derived bias."""
    if not config.ENABLE_NIFTY_OPTIONS_TRADING:
        return
    direction, sentiment_label, sentiment_score = decide_todays_direction()
    if direction is None:
        logger.warning("No clear sentiment direction today (%s) - skipping NIFTY options for today.",
                        sentiment_label)
        return
    engine = options_engine.run_nifty_options_engine(state["data_service"], direction)
    if engine is None:
        logger.warning("NIFTY options engine could not be built today (see options_engine logs above) "
                        "- no options position will be tracked today.")
        return
    fetch_and_seed_volume_windows(engine, state["data_service"])
    state["options_signal_engine"] = engine
    print(f"\n=== NIFTY Options Engine Ready ({direction.value} bias) ===")
    print(f"Option leg: {engine.option.symbol} (lot_size={engine.option.lot_size}, "
          f"capital_allocated={engine.option.capital_allocated:.2f})")
    print(f"Signal sources: index={engine.index_tracker.symbol} "
          f"(OR high={engine.index_tracker.or_high:.2f}, low={engine.index_tracker.or_low:.2f}), "
          f"futures={engine.futures_tracker.symbol}")


def job_banknifty_options_scan():
    """Opt-in (config.ENABLE_BANKNIFTY_OPTIONS_TRADING) - identical dual-source engine to
    job_nifty_options_scan() above (same NiftyOptionsEngine class, same rule set - see
    options_engine.py's module docstring), pointed at BANKNIFTY's own index/futures/strike
    universe (config.BANKNIFTY_*) instead of NIFTY's. Reuses the SAME today's-direction
    value as equities/NIFTY-options (one sentiment decision for the whole day, never
    recomputed per market) and runs completely independently of the NIFTY options engine -
    both can be enabled at once, each with its own capital bucket and trade book."""
    if not config.ENABLE_BANKNIFTY_OPTIONS_TRADING:
        return
    direction, sentiment_label, sentiment_score = decide_todays_direction()
    if direction is None:
        logger.warning("No clear sentiment direction today (%s) - skipping BANKNIFTY options for today.",
                        sentiment_label)
        return
    engine = options_engine.run_banknifty_options_engine(state["data_service"], direction)
    if engine is None:
        logger.warning("BANKNIFTY options engine could not be built today (see options_engine logs "
                        "above) - no options position will be tracked today.")
        return
    fetch_and_seed_volume_windows(engine, state["data_service"])
    state["banknifty_options_signal_engine"] = engine
    print(f"\n=== BANKNIFTY Options Engine Ready ({direction.value} bias) ===")
    print(f"Option leg: {engine.option.symbol} (lot_size={engine.option.lot_size}, "
          f"capital_allocated={engine.option.capital_allocated:.2f})")
    print(f"Signal sources: index={engine.index_tracker.symbol} "
          f"(OR high={engine.index_tracker.or_high:.2f}, low={engine.index_tracker.or_low:.2f}), "
          f"futures={engine.futures_tracker.symbol}")


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
    direction, sentiment_label, sentiment_score = decide_todays_commodity_direction()
    if direction is None:
        logger.warning("No clear commodity sentiment direction today (%s) - skipping MCX "
                        "commodities for today.", sentiment_label)
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
    session is treated as fully independent of NSE's throughout this codebase."""
    engine = state["commodity_signal_engine"]
    if engine is None:
        return  # MCX's opening-range scan/catch-up hasn't run or found nothing yet today

    now = datetime.now(TZ)
    if now.time() > _time_from_hms(config.MCX_SIGNAL_ENGINE_LAST_RUN):
        return  # past MCX's own "keep checking" window

    window_end = now.replace(second=0, microsecond=0)
    window_start = window_end - timedelta(minutes=config.MCX_CANDLE_INTERVAL_MINUTES)
    start_epoch, end_epoch = int(window_start.timestamp()), int(window_end.timestamp())
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
    """Opt-in (config.ENABLE_MCX_COMMODITY_TRADING) - fires at config.MCX_SQUARE_OFF_TIME,
    well after NSE/equity close, since MCX's own session runs much later in the day."""
    if not config.ENABLE_MCX_COMMODITY_TRADING:
        return
    engine = state["commodity_signal_engine"]
    if engine is None:
        logger.info("No MCX commodity engine was active today - nothing to square off.")
        return
    _report_and_save(engine, "MCX Commodity", csv_suffix="_mcx")


# ----------------------------------------------------------------------------
# Bootstrap + scheduler wiring
# ----------------------------------------------------------------------------
def main():
    logger.info("Starting ORB scanning agent (DRY_RUN=%s)...", config.DRY_RUN)

    state["data_service"] = get_fyers_client()

    job_global_sentiment()
    #job_sector_scan()
    #job_opening_range_scan()
    #job_check_signals()
    job_mcx_commodity_scan()
    
    result = bootstrap_and_catchup(state)
    logger.info("Bootstrap result: phase=%s | %s", result.phase.value, result.message)

    # MCX runs on a completely separate session/clock from NSE equities (see this module's
    # docstring) - its own catch-up ALWAYS runs here (self-gated internally on
    # config.ENABLE_MCX_COMMODITY_TRADING), never skipped just because equities are closed.
    mcx_result = bootstrap_mcx_catchup(state["data_service"])
    logger.info("MCX bootstrap result: phase=%s | %s", mcx_result.phase.value, mcx_result.message)

    # Equities/NIFTY-options/BANKNIFTY-options all run on the NSE session - once it's
    # POST_CLOSE there's nothing left for THEM to do today. MCX runs an entirely separate,
    # much longer session (09:00-23:30 by default) - so the whole process only exits here
    # once BOTH sessions are done for the day (or MCX isn't enabled at all, in which case
    # bootstrap_mcx_catchup() already reported it as POST_CLOSE above). An equity POST_CLOSE
    # at, say, 17:51 IST must NOT by itself stop a still-open MCX session from being
    # scheduled - that was exactly the bug this decoupling fixes.
    if result.phase == MarketPhase.POST_CLOSE and mcx_result.phase == MarketPhase.POST_CLOSE:
        logger.info("Exiting - both the equity session and MCX are closed (or MCX is disabled); "
                    "nothing left to do today.")
        return

    if result.signal_engine is not None:
        state["signal_engine"] = result.signal_engine
    if mcx_result.signal_engine is not None:
        state["commodity_signal_engine"] = mcx_result.signal_engine

    common_kwargs = dict(coalesce=True, max_instances=1)
    scheduler = BackgroundScheduler(timezone=TZ)

    scheduler.add_job(job_sector_scan, _cron_from_hms(config.SECTOR_SCAN_TIME),
                       id="sector_scan_preopen", misfire_grace_time=60,
                       kwargs={"label": "09:10 pre-open snapshot - likely stale, see sector_scanner.py caveat"},
                       **common_kwargs)
    scheduler.add_job(job_sector_scan, _cron_from_hms(config.SECTOR_SCAN_TIME_POSTOPEN),
                       id="sector_scan_postopen", misfire_grace_time=60,
                       kwargs={"label": "09:16 post-open - real numbers"}, **common_kwargs)

    # Only schedule the opening-range job if bootstrap DIDN'T already produce a shortlist -
    # if it did (mid-session catch-up succeeded), today's shortlist is already built and
    # tracked; if bootstrap ran and found nothing (phase POST_OPEN, empty shortlist), the
    # underlying 09:15 candle is fixed and re-running this later today can't change that
    # answer, so it's still correctly skipped. It only needs to actually run via cron for
    # the normal PRE_OPEN-start case.
    if result.signal_engine is None and result.phase == MarketPhase.PRE_OPEN:
        scheduler.add_job(job_opening_range_scan, _cron_from_hms(config.OPENING_RANGE_SCAN_TIME),
                           id="opening_range_scan", misfire_grace_time=30, **common_kwargs)

    # All opt-in (self-gated on their own config.ENABLE_* flag - see each job's docstring)
    # so these are always scheduled; they simply no-op immediately if their flag is False.
    # NIFTY/BANKNIFTY options share the NSE equity session's opening-range time.
    scheduler.add_job(job_nifty_options_scan, _cron_from_hms(config.OPENING_RANGE_SCAN_TIME),
                       id="nifty_options_scan", misfire_grace_time=30, **common_kwargs)
    scheduler.add_job(job_banknifty_options_scan, _cron_from_hms(config.OPENING_RANGE_SCAN_TIME),
                       id="banknifty_options_scan", misfire_grace_time=30, **common_kwargs)

    # Only schedule MCX's own cron-triggered opening-range scan if bootstrap_mcx_catchup()
    # didn't already produce an engine today (mirrors the equity opening_range_scan guard
    # above) - if MCX catch-up succeeded (a mid-session start), today's universe is already
    # built and tracked; if MCX is disabled entirely, or genuinely still pre-open for MCX,
    # the cron job (itself self-gated on config.ENABLE_MCX_COMMODITY_TRADING) still needs to
    # fire at its normal time.
    if state["commodity_signal_engine"] is None:
        scheduler.add_job(job_mcx_commodity_scan, _cron_from_hms(config.MCX_OPENING_RANGE_SCAN_TIME),
                           id="mcx_commodity_scan", misfire_grace_time=30, **common_kwargs)

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
    # equity session is doing. Only scheduled when MCX trading is enabled at all.
    if config.ENABLE_MCX_COMMODITY_TRADING:
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
