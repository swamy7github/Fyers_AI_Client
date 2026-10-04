"""
main.py
--------
Entry point. Auth, one-shot bootstrap/catch-up, then hands off to the
scheduler (for 5-min candle-level checks and the day's remaining one-off
jobs) and the live tick WebSocket (for fast stop-loss/target/trailing-SL).

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
5. If bootstrap produced a signal engine (either because it caught up
   mid-session, or because a previous run already had one - it doesn't,
   since this is a fresh process, but the point stands for anything built
   later that reuses this), start the live tick WebSocket for it
   immediately, before anything else.
6. Schedule the recurring/remaining jobs. Because bootstrap already handled
   catch-up as a one-time synchronous step, there's no next_run_time
   trickery needed here anymore - the one-off jobs just use their normal
   cron times, which correctly resolve to "tomorrow" via APScheduler's own
   semantics if today's occurrence has already passed.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from bootstrap import (MarketPhase, _time_from_hms, bootstrap_and_catchup, decide_todays_direction,
                        fetch_and_seed_volume_windows)
from fyers_service import FyersAuthManager, FyersDataService
from global_sentiment import fetch_global_snapshot, score_global_bias, get_stock_news_ema_bias
from live_tick_stream import LiveTickStream
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

# Shared mutable state the scheduled jobs read/write
state = {"data_service": None, "signal_engine": None, "live_tick_stream": None}


def _cron_from_hms(hms: str) -> CronTrigger:
    h, m, s = map(int, hms.split(":"))
    return CronTrigger(hour=h, minute=m, second=s, day_of_week="mon-fri", timezone=TZ)


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
        print("News: skipped (no ANTHROPIC_API_KEY configured, or no headlines returned)")

    # NEW: what today will actually trade, per global sentiment -> direction (see
    # bootstrap.decide_todays_direction / trade_direction.py). This is purely informational
    # here - the SAME function is called again, authoritatively, right before the shortlist
    # is actually built (job_opening_range_scan / bootstrap_and_catchup), since sentiment
    # can move between this pre-market check and 09:20:02.
    direction, sentiment_label, sentiment_score = decide_todays_direction()
    print(f"\n=== Today's Trade Direction (from global sentiment): "
          f"{direction.value if direction else 'NONE - no new positions today'} ===")
    print(f"(sentiment: {sentiment_label}, score {sentiment_score:.2f})")


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


def _start_live_tick_stream(access_token: str, engine: SignalEngine) -> None:
    stream = LiveTickStream(access_token, engine)
    state["live_tick_stream"] = stream
    stream.start()
    logger.info("Live tick stream started for %d tracked symbol(s).", len(engine.trackers))


def job_opening_range_scan(access_token: str):
    """Only fires via its normal 09:20:02 cron trigger now - see main()'s docstring for why
    the mid-session catch-up path no longer needs to call this directly.

    Computes today's direction fresh (via bootstrap.decide_todays_direction()) right before
    building the shortlist, rather than reusing job_global_sentiment()'s earlier read -
    sentiment can move in the roughly 10 minutes between the pre-market check and 09:20:02,
    and this is the call that actually decides what gets traded, so it should be as current
    as possible."""
    direction, sentiment_label, sentiment_score = decide_todays_direction()
    logger.info("Opening-range scan direction: %s (sentiment %s, score %.2f)",
                direction.value if direction else "NONE", sentiment_label, sentiment_score)
    if direction is None:
        logger.warning("Global sentiment is %s (score %.2f) - not a clear bullish/bearish read, "
                        "and config.ALLOW_LONG_ON_NEUTRAL / ALLOW_SHORT_ON_NEUTRAL are both False, "
                        "so no new positions will be opened today.", sentiment_label, sentiment_score)
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
    _start_live_tick_stream(access_token, engine)


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
    if state["live_tick_stream"] is not None:
        state["live_tick_stream"].stop()

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

    if result.phase == MarketPhase.POST_CLOSE:
        logger.info("Exiting - market is closed, nothing left to do today.")
        return

    if result.signal_engine is not None:
        state["signal_engine"] = result.signal_engine
        _start_live_tick_stream(access_token, result.signal_engine)

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
                           id="opening_range_scan", misfire_grace_time=30,
                           kwargs={"access_token": access_token}, **common_kwargs)

    check_signals_trigger = CronTrigger(minute=f"*/{config.CANDLE_INTERVAL_MINUTES}", second=2,
                                         hour="9-15", day_of_week="mon-fri", timezone=TZ)
    scheduler.add_job(job_check_signals, check_signals_trigger,
                       id="check_signals", misfire_grace_time=45, **common_kwargs)

    scheduler.add_job(job_square_off_and_report, _cron_from_hms(config.SQUARE_OFF_TIME),
                       id="square_off", misfire_grace_time=60, **common_kwargs)

    scheduler.start()
    logger.info("Scheduler started. Waiting for scheduled jobs (Ctrl+C to stop)...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        if state["live_tick_stream"] is not None:
            state["live_tick_stream"].stop()
        scheduler.shutdown()


if __name__ == "__main__":
    main()
