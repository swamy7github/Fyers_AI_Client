"""
main.py
--------
Entry point. Wires together auth, data, sentiment, sector scan, opening-range
scan and the signal engine, and schedules everything with APScheduler in IST.

Run with:  python main.py
Stop with: Ctrl+C

Timeline it sets up (weekdays only):
  09:10:00        -> sector strength/weakness snapshot (pre-open, likely stale)
  09:16:00        -> sector strength/weakness snapshot (post-open, real numbers)
  09:20:02        -> opening-range scan -> builds the top-5 shortlist
  every 5 min     -> signal engine checks each shortlisted stock's latest candle
  15:10:00        -> stop looking for new signals
  15:10:30        -> square off any open paper positions, print + save trade book

Late-startup catch-up
-----------------------
If the process is started mid-session (e.g. 10:00 instead of before 09:00),
the jobs above whose scheduled time has already passed today would
otherwise sit idle until tomorrow's cron trigger. See _is_market_open_now(),
_within_signal_window() and the _should_catch_up_* helpers below: each
one-off job is given an immediate next_run_time on startup if its scheduled
time has already passed AND we're still within a window where running it
late is actually useful - see each helper's docstring for the exact
reasoning, particularly around why the opening-range/sector catch-up is
gated more conservatively than the plain "market is open" check used for
job_check_signals itself.
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

import config
from fyers_service import FyersAuthManager, FyersDataService
from global_sentiment import fetch_global_snapshot, score_global_bias
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

# Shared mutable state the scheduled jobs read/write
state = {"data_service": None, "signal_engine": None, "top5_gaining_sectors": None,
         "top5_losing_sectors": None, "global_sentiment":tuple()}


# ----------------------------------------------------------------------------
# Pure time-decision helpers - deliberately take `now` as a parameter (rather
# than calling datetime.now() internally) so they're independently testable
# with injected timestamps. See test_updates.py.
# ----------------------------------------------------------------------------
def _time_from_hms(hms: str) -> dt_time:
    h, m, s = map(int, hms.split(":"))
    return dt_time(h, m, s)


def _cron_from_hms(hms: str) -> CronTrigger:
    h, m, s = map(int, hms.split(":"))
    corn_hms = CronTrigger(hour=h, minute=m, second=s, day_of_week="mon-fri", timezone=TZ)
    logger.info(f" corn job starts from {corn_hms}")
    return corn_hms
#    return CronTrigger(hour=h, minute=m, second=s, day_of_week="mon-fri", timezone=TZ)


def _is_market_open_now(now: datetime) -> bool:
    """Weekday + within MARKET_OPEN_TIME..MARKET_CLOSE_TIME. Does NOT check NSE
    trading holidays - see the caveat in config.py next to MARKET_OPEN_TIME."""
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return _time_from_hms(config.MARKET_OPEN_TIME) <= now.time() <= _time_from_hms(config.MARKET_CLOSE_TIME)


def _within_signal_window(now: datetime) -> bool:
    """Market open AND not yet past the signal-checking cutoff. Deliberately
    narrower than _is_market_open_now(): this gates whether it's worth
    bootstrapping a FRESH opening-range shortlist / signal-tracking position
    on a late start. Creating a brand-new tracked position minutes before
    close, after the day's SQUARE_OFF_TIME cron trigger has already fired
    (or been skipped) uneventfully, would leave that position with no
    square-off safety net for the rest of the day - so we simply don't
    start anything new once we're past SIGNAL_ENGINE_LAST_RUN, even though
    the market is technically still open."""
    return _is_market_open_now(now) and now.time() <= _time_from_hms(config.SIGNAL_ENGINE_LAST_RUN)


def _should_catch_up_sector_postopen(now: datetime) -> bool:
    return _within_signal_window(now) and now.time() >= _time_from_hms(config.SECTOR_SCAN_TIME_POSTOPEN)


def _should_catch_up_opening_range(now: datetime) -> bool:
    return _within_signal_window(now) and now.time() >= _time_from_hms(config.OPENING_RANGE_SCAN_TIME)


def _should_run_immediate_signal_check(now: datetime) -> bool:
    # Deliberately the broader "market is open" check, matching the literal spec
    # ("if started within trading hours, execute job_check_signals() immediately").
    # job_check_signals() already no-ops internally once past SIGNAL_ENGINE_LAST_RUN,
    # so firing it late/uselessly in the 15:10-15:30 window is harmless.
    return _is_market_open_now(now)


def _should_use_fast_opening_range_path(now: datetime) -> bool:
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
    state['global_sentiment'] = (label, score)
    
    from google_global_sentiment import google_fetch_global_snapshot, google_score_global_bias
    logger.info("Running pre-market google global sentiment check...")
    gsnapshot = google_fetch_global_snapshot()
    glabel, gscore = google_score_global_bias(snapshot)
    logger.info("Google Global cues bias: %s (score %.2f) | Snapshot: %s", glabel, gscore, gsnapshot)
    print(f"\n===Google Global Market Bias: {glabel} (score {gscore:.2f}) ===")
    print(gsnapshot)
    state['g_global_sentiment'] = (glabel, gscore)

def job_sector_scan(label: str = ""):
    logger.info("Running sector scan (%s)...", label or "unlabeled")
    snapshot = get_index_snapshot(state["data_service"])
    if snapshot.empty:
        return

    leaderboard = get_sector_leaderboard(snapshot)
     
    if not leaderboard.empty:
        print("\n=== Top Gaining Sectors ===")
        state["top5_gaining_sectors"] = leaderboard.head(5)
        print(state["top5_gaining_sectors"])
        print("\n=== Top Losing Sectors ===")
        state["top5_losing_sectors"] = leaderboard.tail(5)
        print(leaderboard.tail(5).to_string(index=False))

    # Not sectors, but genuinely useful context - broad market + volatility gauge
    context_names = ["Nifty 50", "Nifty 500", "India VIX"]
    context = snapshot[snapshot["index_name"].isin(context_names)]
    if not context.empty:
        print("\n=== Broader Market Context ===")
        print(context[["index_name", "ltp", "pct_change"]].to_string(index=False))


def job_opening_range_scan():
    now = datetime.now(TZ)
    if _should_use_fast_opening_range_path(now):
        logger.info("Running opening-range scan (on-time, quotes-proxy path)...")
        if state["top5_gaining_sectors"] is None:
            job_sector_scan()
        shortlist = scan_opening_range(state)
    else:
        logger.info("Running opening-range scan (late/catch-up start at %s IST - using the "
                    "slower but always-correct historical-candle path instead of the "
                    "quotes-proxy shortcut, which is only valid right at %s)...",
                    now.strftime("%H:%M:%S"), config.OPENING_RANGE_SCAN_TIME)
        if state["top5_gaining_sectors"] is None:
            job_sector_scan()
        shortlist = scan_opening_range_accurate(state)

    if shortlist.empty:
        logger.warning("No stocks matched the opening-range gain filter today.")
        return
    state["signal_engine"] = SignalEngine(state["data_service"], shortlist)
    print(f"\n=== Opening Range Shortlist (Top {config.TOP_N_STOCKS}) ===")
    print(shortlist.to_string(index=False))


def job_check_signals():
    engine = state["signal_engine"]
    if engine is None:
        return  # opening-range scan hasn't run / found nothing yet

    now = datetime.now(TZ)
    if now.time() > _time_from_hms(config.SIGNAL_ENGINE_LAST_RUN):
        return  # past the "keep checking till 3:10pm" window

    window_end = now.replace(second=0, microsecond=0)
    window_start = window_end - timedelta(minutes=config.CANDLE_INTERVAL_MINUTES)
    engine.check_all(int(window_start.timestamp()), int(window_end.timestamp()))


def job_square_off_and_report():
    engine = state["signal_engine"]
    if engine is None:
        logger.info("No signal engine was active today - nothing to square off.")
        return

    engine.square_off_all()
    df = engine.trade_book.as_dataframe()
    summary = engine.trade_book.summary()

    print("\n=== Trade Book ===")
    print(df.to_string(index=False) if not df.empty else "No trades were triggered today.")
    print("\n=== Summary (points captured = profit/loss) ===")
    print(summary)

    out_path = config.TRADE_LOG_CSV.format(date=datetime.now(TZ).strftime("%Y%m%d"))
    df.to_csv(out_path, index=False)
    logger.info("Trade book saved to %s", out_path)


# ----------------------------------------------------------------------------
# Scheduler wiring
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

    state["data_service"] = FyersDataService(access_token=fyres.token)

    # One-off, informational: run once immediately regardless of the clock
    job_global_sentiment()
    job_sector_scan()
    job_opening_range_scan()
    job_check_signals()
    now = datetime.now(TZ)
    if not _is_market_open_now(now):
        logger.info("Starting outside trading hours (%s IST) - no catch-up needed, "
                    "the regular cron schedule below will handle everything.",
                    now.strftime("%H:%M:%S"))
    elif not _within_signal_window(now):
        logger.info("Starting at %s IST - market is open but past the %s signal-checking "
                    "cutoff, so no fresh opening-range/sector catch-up will run today "
                    "(see _within_signal_window's docstring for why).",
                    now.strftime("%H:%M:%S"), config.SIGNAL_ENGINE_LAST_RUN)
    else:
        logger.info("Starting mid-session at %s IST - catching up on anything already due today.",
                    now.strftime("%H:%M:%S"))

    # coalesce=True + max_instances=1 on every job: if execution is delayed (e.g. a slow
    # API call blocks the loop for a while), APScheduler runs the job ONCE to catch up
    # rather than firing a burst of every individually-missed trigger, and never overlaps
    # two runs of the same job. This is in addition to (not instead of) the explicit,
    # controlled catch-up below - misfire_grace_time/coalesce here is a safety net for
    # ONGOING transient delays, not the primary mechanism for a genuinely late startup.
    common_kwargs = dict(coalesce=True, max_instances=1)
    logger.info(f" common _ kwargs = {common_kwargs}")
    scheduler = BackgroundScheduler(timezone=TZ)
    #return

    # ---- sector scans ----
    scheduler.add_job(job_sector_scan, _cron_from_hms(config.SECTOR_SCAN_TIME),
                       id="sector_scan_preopen", misfire_grace_time=60,
                       kwargs={"label": "09:10 pre-open snapshot - likely stale, see sector_scanner.py caveat"},
                       **common_kwargs)
    # (the pre-open scan is only ever useful right at 09:10 per its own docstring caveat -
    # never worth an immediate catch-up run; just let it fire again tomorrow if missed today)

    postopen_kwargs = dict(id="sector_scan_postopen", misfire_grace_time=60,
                            kwargs={"label": "09:16 post-open - real numbers"}, **common_kwargs)
    if _should_catch_up_sector_postopen(now):
        logger.info("Late start - running the post-open sector scan immediately, once.")
        scheduler.add_job(job_sector_scan, _cron_from_hms(config.SECTOR_SCAN_TIME_POSTOPEN),
                           next_run_time=now, **postopen_kwargs)
    else:
        scheduler.add_job(job_sector_scan, _cron_from_hms(config.SECTOR_SCAN_TIME_POSTOPEN), **postopen_kwargs)

    # ---- opening-range scan ----
    or_kwargs = dict(id="opening_range_scan", misfire_grace_time=30, **common_kwargs)
    or_trigger = _cron_from_hms(config.OPENING_RANGE_SCAN_TIME)
    if _should_catch_up_opening_range(now):
        logger.info("Late start - running the opening-range scan immediately, once "
                    "(job_opening_range_scan auto-selects the always-correct historical-candle path).")
        scheduler.add_job(job_opening_range_scan, or_trigger, next_run_time=now, **or_kwargs)
    else:
        scheduler.add_job(job_opening_range_scan, or_trigger, **or_kwargs)

    # ---- signal checking, every 5 min ----
    check_signals_trigger = CronTrigger(minute=f"*/{config.CANDLE_INTERVAL_MINUTES}", second=2,
                                         hour="9-15", day_of_week="mon-fri", timezone=TZ)
    check_signals_kwargs = dict(id="check_signals", misfire_grace_time=45, **common_kwargs)
    if _should_run_immediate_signal_check(now):
        logger.info("Late start - running an immediate signal check in addition to the "
                    "regular 5-minute cadence (no-ops internally if there's no shortlist yet, "
                    "or if we're past the %s cutoff).", config.SIGNAL_ENGINE_LAST_RUN)
        scheduler.add_job(job_check_signals, check_signals_trigger, next_run_time=now, **check_signals_kwargs)
    else:
        scheduler.add_job(job_check_signals, check_signals_trigger, **check_signals_kwargs)

    # ---- EOD square-off ----
    scheduler.add_job(job_square_off_and_report, _cron_from_hms(config.SQUARE_OFF_TIME),
                       id="square_off", misfire_grace_time=60, **common_kwargs)

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
