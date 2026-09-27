"""
main.py
--------
Entry point. Wires together auth, data, sentiment, sector scan, opening-range
scan and the signal engine, and schedules everything with APScheduler in IST.

Run with:  python main.py
Stop with: Ctrl+C

Timeline it sets up (weekdays only):
  09:10:00        -> sector strength/weakness snapshot
  09:20:02        -> opening-range scan -> builds the top-5 shortlist
  every 5 min     -> signal engine checks each shortlisted stock's latest candle
  15:10:00        -> stop looking for new signals
  15:10:30        -> square off any open paper positions, print + save trade book
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
from fyers_service import FyersAuthManager, FyersDataService
from global_sentiment import fetch_global_snapshot, score_global_bias
from sector_scanner import scan_sectors
from opening_range_scanner import scan_opening_range
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
state = {"data_service": None, "signal_engine": None}


def _cron_from_hms(hms: str) -> CronTrigger:
    h, m, s = map(int, hms.split(":"))
    return CronTrigger(hour=h, minute=m, second=s, day_of_week="mon-fri", timezone=TZ)


def job_global_sentiment():
    logger.info("Running pre-market global sentiment check...")
    snapshot = fetch_global_snapshot()
    label, score = score_global_bias(snapshot)
    logger.info("Global cues bias: %s (score %.2f) | Snapshot: %s", label, score, snapshot)
    print(f"\n=== Global Market Bias: {label} (score {score:.2f}) ===")
    print(snapshot)


def job_sector_scan():
    logger.info("Running 09:10 sector scan...")
    sectors_df = scan_sectors(state["data_service"])
    if sectors_df.empty:
        return
    print("\n=== Top Gaining Sectors ===")
    print(sectors_df.head(5).to_string(index=False))
    print("\n=== Top Losing Sectors ===")
    print(sectors_df.tail(5).to_string(index=False))


def job_opening_range_scan():
    logger.info("Running 09:20:02 opening-range scan...")
    shortlist = scan_opening_range(state["data_service"])
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
    cutoff_h, cutoff_m, cutoff_s = map(int, config.SIGNAL_ENGINE_LAST_RUN.split(":"))
    cutoff = now.replace(hour=cutoff_h, minute=cutoff_m, second=cutoff_s, microsecond=0)
    if now > cutoff:
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


def main():
    logger.info("Starting ORB scanning agent (DRY_RUN=%s)...", config.DRY_RUN)

    auth = FyersAuthManager()
    access_token = auth.get_access_token()
    state["data_service"] = FyersDataService(access_token)

    # One-off, informational: run once immediately regardless of the clock
    job_global_sentiment()

    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(job_sector_scan, _cron_from_hms(config.SECTOR_SCAN_TIME),
                       id="sector_scan", misfire_grace_time=60)
    scheduler.add_job(job_opening_range_scan, _cron_from_hms(config.OPENING_RANGE_SCAN_TIME),
                       id="opening_range_scan", misfire_grace_time=30)
    scheduler.add_job(job_check_signals,
                       CronTrigger(minute=f"*/{config.CANDLE_INTERVAL_MINUTES}", second=2,
                                   hour="9-15", day_of_week="mon-fri", timezone=TZ),
                       id="check_signals", misfire_grace_time=30)
    scheduler.add_job(job_square_off_and_report, _cron_from_hms(config.SQUARE_OFF_TIME),
                       id="square_off", misfire_grace_time=60)

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
