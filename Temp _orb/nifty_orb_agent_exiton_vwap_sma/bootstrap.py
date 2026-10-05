"""
bootstrap.py
-------------
Market-phase detection and the catch-up/warm-up pipeline that lets main.py
start at ANY time before market close and pick up seamlessly from there.

This is the file signal_engine.py's docstrings already referred to
(adopt_broker_position, is_backfill, feed_candle) before it existed - the
hooks were built first; this wires them together.

The core entry point is bootstrap_and_catchup(), which does ALL of the
one-time, at-startup work synchronously, before the scheduler in main.py
starts anything recurring:

  1. detect_market_phase()      - PRE_OPEN / POST_OPEN / POST_CLOSE
  2. sector leaderboard + market breadth snapshot (informational)
  3. decide_todays_direction()  - global sentiment (BULLISH/BEARISH) decides
     whether today trades LONG or SHORT; NEUTRAL/MIXED/UNKNOWN sentiment
     means no new positions today unless config.ALLOW_LONG_ON_NEUTRAL /
     ALLOW_SHORT_ON_NEUTRAL says otherwise (see trade_direction.py)
  4. gap screen -> today's shortlist: gap-UP for LONG, gap-DOWN for SHORT
     (via the always-correct historical path, since we don't know how late
     "late" is)
  5. fetch_and_seed_volume_windows() - pre-fills each tracker's 20-period
     volume MA (and the fast 2-period one) from the prior session's
     candles, BEFORE replay. Without this, the volume MA needed for entries
     stays None for the first 20 candles fed to a fresh tracker - roughly
     100 minutes - silently pushing the earliest possible entry to ~11:00
     AM instead of the documented ~09:30, on every single day this ran
     without it. See signal_engine.seed_volume_windows_from_candles's
     docstring for the full story.
  6. _replay_intraday_history() - feeds every 5-min candle from market open
     up to "now" through SignalEngine.feed_candle(is_backfill=True), so any
     entry/add/stop-loss that would have fired earlier today (in whichever
     direction today is trading) is reconstructed into the engine's state
     WITHOUT placing a live order for it (that's what is_backfill does -
     see signal_engine.py's _maybe_place_live_order docstring for exactly
     why: a signal detected in a replay is not a live decision, and the
     price that justified it has moved on by the time we're replaying it)
  7. _reconcile_broker_state()  - broker positions are the source of truth
     (including which DIRECTION a position actually is, from the broker's
     own qty sign - see adopt_broker_position); adopts anything the broker
     reports that replay missed, and clears anything replay thinks is open
     that the broker says isn't

Why this matters (see also the conversation this was written in for the
full audit): a late start that skips steps 3-5 and just starts checking
NEW candles from "now" forward - which is what main.py's catch-up used to
do before this file existed - silently MISSES any breakout that already
happened between market open and startup. That's not a hypothetical edge
case for an intraday strategy; it's the normal case for any start after
09:30.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from enum import Enum
from typing import Optional

import pandas as pd
import pytz

import config
from fyers_service import FyersDataService
import global_sentiment
from opening_range_scanner import load_universe, scan_opening_range_accurate
from sector_scanner import compute_market_breadth, get_index_snapshot, get_sector_leaderboard
from signal_engine import SignalEngine, seed_volume_windows_from_candles
from trade_direction import TradeDirection, decide_trade_direction_from_sentiment

logger = logging.getLogger("orb_agent.bootstrap")

TZ = pytz.timezone(config.TIMEZONE)


class MarketPhase(Enum):
    PRE_OPEN = "PRE_OPEN"      # before today's market open
    POST_OPEN = "POST_OPEN"    # market is open and trading right now
    POST_CLOSE = "POST_CLOSE"  # market has closed for the day (includes weekends)


@dataclass
class BootstrapResult:
    phase: MarketPhase
    signal_engine: Optional[SignalEngine]
    sector_leaderboard: pd.DataFrame = field(default_factory=pd.DataFrame)
    market_breadth: dict = field(default_factory=dict)
    message: str = ""
    direction: Optional[TradeDirection] = None
    sentiment_label: str = ""
    sentiment_score: float = 0.0


# ----------------------------------------------------------------------------
# Pure time-decision helpers - take `now` as a parameter (rather than calling
# datetime.now() internally) so they're independently testable with injected
# timestamps. Moved here from main.py now that bootstrap.py owns "what phase
# is it and is catch-up worth doing" as a single concern; main.py imports
# these rather than redefining them.
# ----------------------------------------------------------------------------
def _time_from_hms(hms: str) -> dt_time:
    h, m, s = map(int, hms.split(":"))
    return dt_time(h, m, s)


def detect_market_phase(now: datetime) -> MarketPhase:
    """Does NOT check NSE trading holidays - see the caveat next to
    config.MARKET_OPEN_TIME. A holiday will be misdetected as POST_OPEN if
    it falls on a weekday within market hours; layer a holiday calendar on
    top of this before relying on it for unattended daily runs."""
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return MarketPhase.POST_CLOSE
    t = now.time()
    if t < _time_from_hms(config.MARKET_OPEN_TIME):
        return MarketPhase.PRE_OPEN
    if t > _time_from_hms(config.MARKET_CLOSE_TIME):
        return MarketPhase.POST_CLOSE
    return MarketPhase.POST_OPEN


def _is_market_open_now(now: datetime) -> bool:
    return detect_market_phase(now) == MarketPhase.POST_OPEN


def _within_signal_window(now: datetime) -> bool:
    """POST_OPEN AND not yet past the signal-checking cutoff. Deliberately
    narrower than _is_market_open_now(): this gates whether it's worth
    bootstrapping a FRESH shortlist / signal-tracking position on a late
    start. Creating a brand-new tracked position minutes before close,
    after today's SQUARE_OFF_TIME cron trigger has already fired (or been
    skipped) uneventfully, would leave that position with no square-off
    safety net for the rest of the day - so catch-up simply doesn't start
    anything new once we're past SIGNAL_ENGINE_LAST_RUN, even though the
    market is technically still open."""
    return _is_market_open_now(now) and now.time() <= _time_from_hms(config.SIGNAL_ENGINE_LAST_RUN)


# ----------------------------------------------------------------------------
# Sentiment -> direction decision. Public (no leading underscore) because
# main.py's own on-time (09:20:02) path needs the exact same decision, not a
# second copy of it - see main.job_opening_range_scan().
# ----------------------------------------------------------------------------
def _apply_neutral_fallback(direction: Optional[TradeDirection]) -> Optional[TradeDirection]:
    """
    Shared by decide_todays_direction() (live) and decide_direction_for_date()
    (historical, for backtest.py) so the NEUTRAL/MIXED/UNKNOWN fallback
    policy - config.ALLOW_LONG_ON_NEUTRAL / ALLOW_SHORT_ON_NEUTRAL, both
    default False - lives in exactly ONE place and can never drift between
    what a backtest replays and what live trading would actually do.
    """
    if direction is not None:
        return direction
    if config.ALLOW_LONG_ON_NEUTRAL:
        return TradeDirection.LONG
    if config.ALLOW_SHORT_ON_NEUTRAL:
        return TradeDirection.SHORT
    return None


def decide_todays_direction() -> tuple[Optional[TradeDirection], str, float]:
    """
    Turns global_sentiment.score_global_bias() into today's trading
    direction: BULLISH -> LONG, BEARISH -> SHORT. Anything else (NEUTRAL,
    MIXED, UNKNOWN, or a fetch failure) returns None UNLESS
    config.ALLOW_LONG_ON_NEUTRAL / ALLOW_SHORT_ON_NEUTRAL says to fall back
    to a specific direction anyway - see _apply_neutral_fallback().

    Returns (direction_or_None, sentiment_label, sentiment_score) so callers
    can log/report the raw sentiment that drove the decision, not just the
    final direction - "SHORT because BEARISH (score -1.4)" is a lot more
    auditable than just "SHORT".
    """
    snapshot = global_sentiment.fetch_global_snapshot()
    label, score = global_sentiment.score_global_bias(snapshot)
    direction = _apply_neutral_fallback(decide_trade_direction_from_sentiment(label))
    return direction, label, score


def decide_direction_for_date(as_of_date: date) -> tuple[Optional[TradeDirection], str, float]:
    """
    Historical counterpart to decide_todays_direction(), for backtest.py:
    what direction WOULD have been chosen on the morning of as_of_date,
    using only global market data available strictly before that date (see
    global_sentiment.fetch_global_snapshot_as_of). Same
    _apply_neutral_fallback() policy as the live decision, so a backtest's
    direction choice never silently diverges from what live trading would
    have done for the same sentiment reading.
    """
    snapshot = global_sentiment.fetch_global_snapshot_as_of(as_of_date)
    label, score = global_sentiment.score_global_bias(snapshot)
    direction = _apply_neutral_fallback(decide_trade_direction_from_sentiment(label))
    return direction, label, score


# ----------------------------------------------------------------------------
# Historical replay
# ----------------------------------------------------------------------------
def fetch_and_seed_volume_windows(engine: SignalEngine, data_service: FyersDataService,
                                   reference_date: Optional[date] = None, lookback_days: int = 7) -> None:
    """
    Live counterpart to backtest.py's own pre-fetched-data seeding (see
    signal_engine.seed_volume_windows_from_candles for what this fixes and
    why): fetches the most recent trading session(s) before reference_date
    (today, if not given) via one bulk call per lookback day actually
    needed, and seeds every tracked symbol's volume windows from it.

    Looks back up to `lookback_days` CALENDAR days, skipping weekends (NOT
    NSE holidays - same known limitation as the rest of this project),
    stopping once every symbol has at least config.VOLUME_MA_PERIOD candles
    collected or the lookback budget runs out. Never raises - a
    data-service failure here degrades to "no seed, warms up live" rather
    than blocking engine construction, since a missing volume-MA warm-up
    optimization should never be the reason the whole catch-up pipeline
    fails.

    Call this BEFORE _replay_intraday_history() so the seed data is already
    in place when today's own candles start getting replayed on top of it -
    the deque's maxlen naturally phases out yesterday's oldest entries as
    today's real candles accumulate, exactly as if the live system had been
    running continuously since market open.
    """
    symbols = list(engine.trackers.keys())
    if not symbols:
        return
    reference_date = reference_date or datetime.now(TZ).date()
    needed = config.VOLUME_MA_PERIOD
    collected: dict[str, list] = {s: [] for s in symbols}

    day = reference_date
    days_checked = 0
    while days_checked < lookback_days and any(len(v) < needed for v in collected.values()):
        day = day - timedelta(days=1)
        if day.weekday() >= 5:  # Saturday/Sunday - skip, not a trading day
            continue
        days_checked += 1
        day_str = day.strftime("%Y-%m-%d")
        try:
            candles_by_symbol = data_service.get_candles_bulk(symbols, "5", day_str, day_str)
        except Exception as exc:
            logger.warning("Could not fetch %s history for volume-MA seeding: %s", day_str, exc)
            continue
        for symbol in symbols:
            if len(collected[symbol]) >= needed:
                continue
            day_candles = sorted(candles_by_symbol.get(symbol, []), key=lambda c: c[0])
            collected[symbol] = day_candles + collected[symbol]  # an older lookback day goes BEFORE
            # whatever's already collected, so multi-day lookback stays in chronological order.

    seed_volume_windows_from_candles(engine.trackers, collected)


def _replay_intraday_history(engine: SignalEngine, data_service: FyersDataService,
                              symbols: list[str], now: datetime) -> int:
    """
    Fetches every symbol's 5-min candles for today in one rate-limit-hardened
    bulk call, then feeds each FULLY CLOSED candle from market open up to
    "now" through engine.feed_candle(is_backfill=True), in chronological
    order per symbol. Returns how many candles were replayed in total.

    "Fully closed" matters: a candle whose 5-minute window hasn't finished
    yet (i.e. its close-time is still in the future relative to `now`) is
    still forming - feeding a partial bar would corrupt VWAP/volume-MA
    state with data that hasn't actually happened yet.
    """
    if not symbols:
        return 0

    today_str = now.strftime("%Y-%m-%d")
    candles_by_symbol = data_service.get_candles_bulk(symbols, "5", today_str, today_str)

    market_open_t = _time_from_hms(config.MARKET_OPEN_TIME)
    market_open_dt = TZ.localize(datetime(now.year, now.month, now.day,
                                           market_open_t.hour, market_open_t.minute, market_open_t.second))
    candle_span = timedelta(minutes=config.CANDLE_INTERVAL_MINUTES)

    replayed = 0
    for symbol in symbols:
        candles = sorted(candles_by_symbol.get(symbol, []), key=lambda c: c[0])
        for candle in candles:
            candle_open_time = datetime.fromtimestamp(candle[0], TZ)
            candle_close_time = candle_open_time + candle_span
            if candle_open_time < market_open_dt or candle_close_time > now:
                continue  # before today's open, or not fully closed yet - skip
            try:
                engine.feed_candle(symbol, candle, candle_open_time.strftime("%H:%M:%S"), is_backfill=True)
                replayed += 1
            except KeyError:
                # Shouldn't happen - candles_by_symbol was built from this same shortlist -
                # but skip defensively rather than crash the whole catch-up over one symbol.
                logger.error("Replay tried to feed a candle for untracked symbol %s - skipping.", symbol)

    logger.info("Replayed %d historical candle(s) across %d symbol(s) for catch-up.", replayed, len(symbols))
    return replayed


# ----------------------------------------------------------------------------
# Broker state reconciliation
# ----------------------------------------------------------------------------
def _reconcile_broker_state(engine: SignalEngine, data_service: FyersDataService) -> None:
    """
    Broker-reported positions are the source of truth, in BOTH directions:

      - Broker reports an open position in a symbol replay did NOT
        reconstruct as open (a different strategy version, a manual trade,
        a gap in this engine's own detection rules, or a position that
        survived a previous crash) -> adopt it via
        engine.adopt_broker_position(), which enables full live tick-level
        risk management for it immediately.
      - Replay reconstructed a position as open, but the broker does NOT
        report it -> someone/something already closed it for real (or it
        never actually filled) - clear the local state rather than
        managing a position that doesn't exist.

    NOTE: pos.get("symbol")/("netQty")/("netAvg") below are Fyers v3's
    documented netPositions field names as of when this was written -
    print one raw get_positions() response for your account once to
    confirm these match before trusting this in production; broker API
    response shapes are exactly the kind of thing that shifts between SDK
    versions without much warning.
    """
    try:
        positions = data_service.get_positions()
    except Exception as exc:
        logger.error("Could not fetch broker positions during catch-up reconciliation: %s. "
                     "Proceeding with replay-reconstructed state ONLY - verify manually against "
                     "your broker terminal before trusting this session with live orders.", exc)
        return

    now_str = datetime.now(TZ).strftime("%H:%M:%S")
    open_broker_symbols: set[str] = set()

    for pos in positions:
        symbol = pos.get("symbol")
        net_qty = pos.get("netQty", 0)
        avg_price = pos.get("netAvg") or pos.get("avgPrice")
        if not symbol or not net_qty or not avg_price:
            continue  # a flat/closed position line, or an unrecognized shape - skip, don't guess
        open_broker_symbols.add(symbol)

        tracker = engine.trackers.get(symbol)
        if tracker is None:
            continue  # adopt_broker_position() would log this same case clearly if we called it
        if tracker.position_open:
            logger.info("Broker confirms replay-reconstructed position in %s (broker avg=%.2f).",
                        symbol, avg_price)
            continue
        logger.warning("Broker reports an OPEN position in %s that replay did NOT reconstruct - "
                        "adopting the broker's version as the source of truth.", symbol)
        engine.adopt_broker_position(symbol, float(avg_price), int(net_qty), now_str)

    for symbol, tracker in engine.trackers.items():
        if tracker.position_open and symbol not in open_broker_symbols:
            logger.warning("Replay reconstructed an open position in %s that the broker does NOT "
                            "report - marking it flat locally rather than managing a position "
                            "that doesn't actually exist.", symbol)
            tracker.position_open = False
            tracker.weakness_events_on_position = 0
            tracker.entry_price_for_risk = None
            tracker.highest_price_since_entry = None


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def bootstrap_and_catchup(data_service: FyersDataService) -> BootstrapResult:
    now = datetime.now(TZ)
    phase = detect_market_phase(now)

    if phase == MarketPhase.PRE_OPEN:
        msg = (f"Started pre-open at {now:%H:%M:%S} IST - nothing to catch up on; "
               f"the scheduled jobs in main.py will run at their normal times.")
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg)

    if phase == MarketPhase.POST_CLOSE:
        day_kind = "a weekend" if now.weekday() >= 5 else "after today's market close"
        msg = f"Market is closed ({day_kind}, {now:%Y-%m-%d %H:%M:%S} IST) - exiting, nothing to bootstrap."
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg)

    # phase == POST_OPEN
    if not _within_signal_window(now):
        msg = (f"Started at {now:%H:%M:%S} IST - market is open but past the "
               f"{config.SIGNAL_ENGINE_LAST_RUN} signal-checking cutoff, so no fresh catch-up "
               f"will run today (see _within_signal_window's docstring for why).")
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg)

    logger.info("=== ENTERING CATCH-UP MODE at %s IST ===", now.strftime("%H:%M:%S"))

    # 1. Sector leaderboard + market breadth - informational, always computed regardless
    #    of the direction decision below, since it's cheap and useful either way.
    snapshot = get_index_snapshot(data_service)
    leaderboard = get_sector_leaderboard(snapshot)
    universe = load_universe()
    breadth = compute_market_breadth(data_service, universe)
    logger.info("Market breadth snapshot: %s", breadth)

    # 2. Global sentiment -> today's direction. Computed before the (potentially
    #    expensive) gap screen below, so an unclear-sentiment day short-circuits early
    #    without spending API calls scanning a universe we're not going to trade anyway.
    direction, sentiment_label, sentiment_score = decide_todays_direction()
    logger.info("Global sentiment: %s (score %.2f) -> trade direction: %s",
                sentiment_label, sentiment_score, direction.value if direction else "NONE")
    if direction is None:
        msg = (f"Catch-up: global sentiment is {sentiment_label} (score {sentiment_score:.2f}) - "
               f"not a clear bullish/bearish read, and config.ALLOW_LONG_ON_NEUTRAL / "
               f"ALLOW_SHORT_ON_NEUTRAL are both False, so no new positions will be opened today.")
        logger.warning(msg)
        return BootstrapResult(phase, None, leaderboard, breadth, msg,
                                direction=None, sentiment_label=sentiment_label, sentiment_score=sentiment_score)

    # 3. Gap screen -> shortlist (gap-UP for LONG, gap-DOWN for SHORT), via the
    #    always-correct historical path (we don't know how late "late" is, so the
    #    quotes-proxy shortcut is never safe here)
    shortlist = scan_opening_range_accurate(data_service, direction=direction)
    if shortlist.empty:
        msg = f"Catch-up: no stocks matched the {direction.value} opening-range filter today - nothing to track."
        logger.warning(msg)
        return BootstrapResult(phase, None, leaderboard, breadth, msg,
                                direction=direction, sentiment_label=sentiment_label, sentiment_score=sentiment_score)

    engine = SignalEngine(data_service, shortlist, direction=direction)

    # 4. Seed volume-MA windows from the prior session, BEFORE replay - without this, no
    #    entry could fire before ~100 minutes of live warm-up (see
    #    signal_engine.seed_volume_windows_from_candles's docstring)
    fetch_and_seed_volume_windows(engine, data_service, reference_date=now.date())

    # 5. Replay 9:15 -> now for every shortlisted symbol (backfill-flagged: reconstructs
    #    state, never places a live order for what it finds)
    _replay_intraday_history(engine, data_service, shortlist["symbol"].tolist(), now)

    # 6. Broker truth overrides replay wherever they disagree (adopt_broker_position
    #    detects LONG vs SHORT from the broker's own qty sign, independent of `direction`
    #    above - broker reality always wins over what we intended to trade today)
    _reconcile_broker_state(engine, data_service)

    open_count = sum(1 for t in engine.trackers.values() if t.position_open)
    msg = (f"Catch-up complete: tracking {len(engine.trackers)} {direction.value} symbol(s), "
           f"{open_count} with an open position reconstructed from today so far.")
    logger.info(msg)
    return BootstrapResult(phase, engine, leaderboard, breadth, msg,
                            direction=direction, sentiment_label=sentiment_label, sentiment_score=sentiment_score)
