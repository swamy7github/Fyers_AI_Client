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
   the market is in and, if started mid-session, synchronously does
   EVERYTHING needed to start seamlessly from "now": sector/breadth
   snapshot, gap-up shortlist, full 9:15-to-now candle replay, and broker
   position/order reconciliation. See bootstrap.py's module docstring for
   why this all needs to happen as one atomic step rather than the
   scattered per-job catch-up main.py used before this file existed.
4. If the market is closed, log why and exit - nothing left to do today.
5. Schedule the recurring/remaining jobs. Because bootstrap already handled
   catch-up as a one-time synchronous step, there's no next_run_time
   trickery needed here anymore - the one-off jobs just use their normal
   cron times, which correctly resolve to "tomorrow" via APScheduler's own
   semantics if today's occurrence has already passed.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, time as dt_time, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import commodity_engine
import config
import options_engine
from bootstrap import (MarketPhase, _time_from_hms, bootstrap_and_catchup, decide_todays_direction,
                        fetch_and_seed_volume_windows)
from fyers_service import FyersAuthManager, FyersDataService
from global_sentiment import fetch_global_snapshot, score_global_bias, get_stock_news_ema_bias, evaluate_market_direction
from live_tick_stream import LiveTickStream
#from google_global_sentiment import fetch_global_snapshot, score_global_bias
from sector_scanner import get_index_snapshot, get_sector_leaderboard
from opening_range_scanner import scan_opening_range, scan_opening_range_accurate
from signal_engine import SignalEngine
from pathlib import Path

# 1. Dynamically parse the parent path ('C:\NS\Fyers_AI_Client')
# This gets the folder containing the current script and looks one level up
project_root = Path(__file__).resolve().parent.parent

# 2. Add 'C:\NS\Fyers_AI_Client' to Python's search path
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

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
# NSE-equity engine; "options_signal_engine" / "commodity_signal_engine" are the opt-in
# (config.ENABLE_NIFTY_OPTIONS_TRADING / config.ENABLE_MCX_COMMODITY_TRADING) parallel
# engines from options_engine.py / commodity_engine.py - all three run completely
# independently (separate SignalEngine instances, separate trade books), so a failure or
# empty shortlist in one never affects the others.
state = {"data_service": None, "signal_engine": None,
         "options_signal_engine": None, "commodity_signal_engine": None,
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
    logger.info(f"\n=== Global Market Bias: {label} (score {score:.2f}) ===")
    logger.info(snapshot)

    nifty_bias = get_stock_news_ema_bias("^NSEI", anthropic_api_key=config.ANTHROPIC_API_KEY)
    logger.info("Nifty 50 news+EMA bias: %s (ema=%s, news=%s)",
                nifty_bias["combined_bias"], nifty_bias["ema_bias"], nifty_bias["news_bias"])
    logger.info(f"\n=== Nifty 50 News + EMA Bias: {nifty_bias['combined_bias']} ===")
    logger.info(f"EMA: {nifty_bias['ema_bias']} {nifty_bias['ema_detail']}")
    
    if nifty_bias["news_bias"]:
        logger.info(f"News: {nifty_bias['news_bias']} - {nifty_bias['news_reason']}")
        logger.info(f"(from {len(nifty_bias['headlines_used'])} headline(s))")
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
        logger.info("\n=== Broader Market Context ===")
        logger.info(context[["index_name", "ltp", "pct_change"]].to_string(index=False))


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
    logger.info(f"\n=== Opening Range {direction.value} Shortlist (Top {config.TOP_N_STOCKS}) ===")
    logger.info(shortlist.to_string(index=False))

def job_nifty_options_scan():
    """Opt-in (config.ENABLE_NIFTY_OPTIONS_TRADING) - runs the SAME ORB logic against a
    single ATM NIFTY option contract. Reuses today's ALREADY-COMPUTED equity direction
    (decide_todays_direction() is called once per day, not once per market) so equities and
    options can never silently disagree on today's sentiment-derived bias - see
    options_engine.py's module docstring for why the engine itself always tracks
    direction=LONG regardless (BUY-ONLY: calls on a LONG bias, puts on a SHORT bias)."""
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
    print(engine.trackers)


def job_mcx_commodity_scan():
    """Opt-in (config.ENABLE_MCX_COMMODITY_TRADING) - mirrors job_nifty_options_scan() but
    for MCX commodity futures (commodity_engine.py), which - unlike options - trade
    today's bias direction AS-IS (long or short, same as equities)."""
    if not config.ENABLE_MCX_COMMODITY_TRADING:
        return
    direction = state['global_sentiment']['direction']
    sentiment_label = state['global_sentiment']['label']
    sentiment_score = state['global_sentiment']['score']  #decide_todays_direction()
    if direction is None:
        logger.warning("No clear sentiment direction today (%s) - skipping MCX commodities for today.",
                        sentiment_label)
        return
    engine = commodity_engine.run_mcx_commodity_engine(state["data_service"], direction)
    if engine is None:
        logger.warning("No MCX commodity matched today's opening-range filter - nothing tracked today.")
        return
    fetch_and_seed_volume_windows(engine, state["data_service"])
    state["commodity_signal_engine"] = engine
    print(f"\n=== MCX Commodity Shortlist ({direction.value}) ===")
    print(list(engine.trackers))


def job_check_signals():
    """Checks the equity engine (always) plus the options/commodity engines (if the
    opt-in scans above produced one) every CANDLE_INTERVAL_MINUTES. Each engine stops
    being checked past its OWN "last run" cutoff - equities/options share the NSE
    session's SIGNAL_ENGINE_LAST_RUN (15:10), MCX runs later in the day and uses its own
    MCX_SIGNAL_ENGINE_LAST_RUN - so a still-open MCX position doesn't go unmanaged for
    hours after equities/options have already finished for the day."""
    now = datetime.now(TZ)
    window_end = now.replace(second=0, microsecond=0)
    window_start = window_end - timedelta(minutes=config.CANDLE_INTERVAL_MINUTES)
    start_epoch, end_epoch = int(window_start.timestamp()), int(window_end.timestamp())

    for label, engine, last_run_hms in (
        ("equity", state["signal_engine"], config.SIGNAL_ENGINE_LAST_RUN),
        ("options", state["options_signal_engine"], config.SIGNAL_ENGINE_LAST_RUN),
        ("commodity", state["commodity_signal_engine"], config.MCX_SIGNAL_ENGINE_LAST_RUN),
    ):
        if engine is None:
            continue  # that market's opening-range scan hasn't run / found nothing yet
        if now.time() > _time_from_hms(last_run_hms):
            continue  # past that market's "keep checking" window
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
    """Squares off the equity engine, AND (if active) the options engine - both run on
    the NSE equity session and close together at SQUARE_OFF_TIME. MCX runs its own,
    separate square-off job (job_mcx_square_off_and_report) much later in the day, since
    MCX's session stays open well past NSE's close."""
    engine = state["signal_engine"]
    if engine is None:
        logger.info("No equity signal engine was active today - nothing to square off.")
    else:
        _report_and_save(engine, "Equity")

    options_eng = state["options_signal_engine"]
    if options_eng is not None:
        _report_and_save(options_eng, "NIFTY Options", csv_suffix="_options")


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

    from fyers_auto_login_v2 import APP_ID, get_fyers_session_handle

    fyres = get_fyers_session_handle()
    if not fyres:
        logger.error(f"Failed to get fyres tocken and authenticat for the APP {APP_ID}")
        return
    '''
     auth = FyersAuthManager()

     access_token = auth.get_access_token()
    '''
    #access_token = fyres.token
    access_token = fyres.token
    state["data_service"] = FyersDataService(access_token)

    job_global_sentiment()
    #job_sector_scan()
    #job_opening_range_scan()
    #job_check_signals()
    job_mcx_commodity_scan()
    
    result = bootstrap_and_catchup(state)
    logger.info("Bootstrap result: phase=%s | %s", result.phase.value, result.message)

    if result.phase == MarketPhase.POST_CLOSE:
        logger.info("Exiting - market is closed, nothing left to do today.")
        return

    if result.signal_engine is not None:
        state["signal_engine"] = result.signal_engine

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

    # Both opt-in (self-gated on their own config.ENABLE_* flag - see each job's docstring)
    # so these are always scheduled; they simply no-op immediately if their flag is False.
    # Options shares the NSE equity session's opening-range time; MCX uses its own, later one.
    scheduler.add_job(job_nifty_options_scan, _cron_from_hms(config.OPENING_RANGE_SCAN_TIME),
                       id="nifty_options_scan", misfire_grace_time=30, **common_kwargs)
    scheduler.add_job(job_mcx_commodity_scan, _cron_from_hms(config.MCX_OPENING_RANGE_SCAN_TIME),
                       id="mcx_commodity_scan", misfire_grace_time=30, **common_kwargs)

    # The candle-check cron's hour range has to stretch to cover MCX's much later session
    # close when MCX trading is enabled - job_check_signals() itself already skips any
    # engine that's None or past ITS OWN last-run cutoff, so widening this range is only
    # ever a no-op for equities/options, never a correctness risk for them.
    last_hour = (int(config.MCX_SIGNAL_ENGINE_LAST_RUN.split(":")[0])
                 if config.ENABLE_MCX_COMMODITY_TRADING else 15)
    check_signals_trigger = CronTrigger(minute=f"*/{config.CANDLE_INTERVAL_MINUTES}", second=2,
                                         hour=f"9-{last_hour}", day_of_week="mon-fri", timezone=TZ)
    scheduler.add_job(job_check_signals, check_signals_trigger,
                       id="check_signals", misfire_grace_time=45, **common_kwargs)

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
