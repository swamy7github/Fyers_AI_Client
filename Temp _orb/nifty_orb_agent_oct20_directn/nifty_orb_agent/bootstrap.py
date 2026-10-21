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
from typing import Callable, Optional

import pandas as pd
import pytz

import commodity_engine
import config
import options_engine
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
    needs_scheduled_retry: bool = False
    # True ONLY when config.USE_CANDLE_BASED_DIRECTION is on and this catch-up ran too early -
    # before its deciding candle(s) had closed (sentiment_label == "WAITING_FOR_CANDLES", see
    # _decide_direction_from_candles()'s docstring). Deliberately distinct from phase ==
    # MarketPhase.PRE_OPEN: the market IS open (phase is POST_OPEN here), there's just no
    # direction to read YET. main.py's scheduler wiring treats this the same as PRE_OPEN for
    # deciding whether to (re-)schedule that market's cron scan job - without this flag, a
    # start in this narrow window (e.g. 09:17 IST, between NIFTY's 09:15 session open and its
    # 09:30 3-candle decision point) would fall through BOTH paths - catch-up gives up because
    # there's genuinely no direction yet, and the old PRE_OPEN-only scheduling guard wouldn't
    # add the cron job either, since phase isn't PRE_OPEN - silently missing that market for
    # the rest of the day even though its own scheduled scan time hasn't passed yet.


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


def detect_mcx_market_phase(now: datetime) -> MarketPhase:
    """
    MCX-specific counterpart to detect_market_phase() above - MCX's own session
    (config.MCX_MARKET_OPEN_TIME/MCX_MARKET_CLOSE_TIME, commonly 09:00-23:30 IST) runs far
    longer than, and entirely INDEPENDENTLY of, NSE's 09:15-15:30 equity session. Before this
    existed, main.py used the single equity-only detect_market_phase() result to decide
    whether to exit the WHOLE process - which meant an NSE POST_CLOSE (e.g. starting the
    agent at 17:51 IST) would stop MCX from ever being scheduled, even though MCX was still
    open for another ~6 hours. See main.py's module docstring for the full story.

    Same MarketPhase enum as detect_market_phase() (PRE_OPEN/POST_OPEN/POST_CLOSE) - there's
    no need for a separate type, just a separate TIME WINDOW to evaluate it against. Does NOT
    check MCX trading holidays - same caveat as detect_market_phase().
    """
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return MarketPhase.POST_CLOSE
    t = now.time()
    if t < _time_from_hms(config.MCX_MARKET_OPEN_TIME):
        return MarketPhase.PRE_OPEN
    if t > _time_from_hms(config.MCX_MARKET_CLOSE_TIME):
        return MarketPhase.POST_CLOSE
    return MarketPhase.POST_OPEN


def _is_mcx_market_open_now(now: datetime) -> bool:
    return detect_mcx_market_phase(now) == MarketPhase.POST_OPEN


def _within_mcx_signal_window(now: datetime) -> bool:
    """MCX counterpart to _within_signal_window() above - POST_OPEN (for MCX) AND not yet
    past MCX's own config.MCX_SIGNAL_ENGINE_LAST_RUN cutoff, deliberately independent of
    equities' SIGNAL_ENGINE_LAST_RUN (15:10) - MCX keeps accepting fresh catch-up starts for
    many hours after equities/options have already stopped for the day."""
    return _is_mcx_market_open_now(now) and now.time() <= _time_from_hms(config.MCX_SIGNAL_ENGINE_LAST_RUN)


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


def _candle_decision_ready_time(session_open_hms: str, num_candles: int, resolution_minutes: int) -> dt_time:
    """The clock time at which the Nth (last) deciding candle has fully closed - e.g. 3
    candles of 5 minutes each from 09:15:00 closes its last candle at 09:30:00. Pure time
    arithmetic (ignores the actual date), shared by _decide_direction_from_candles() below
    and main.py's scheduler wiring (config.CANDLE_DIRECTION_SCAN_TIME/
    MCX_CANDLE_DIRECTION_SCAN_TIME are meant to equal this, plus a small buffer - kept as
    separate config values rather than computed automatically so they stay visible/obvious
    in config.py like every other scan time in this project, but see this function if you
    ever change NUM_CANDLES and need to recompute the matching scan time by hand)."""
    open_t = _time_from_hms(session_open_hms)
    anchor = datetime(2000, 1, 1, open_t.hour, open_t.minute, open_t.second)
    ready = anchor + timedelta(minutes=num_candles * resolution_minutes)
    return ready.time()


def _decide_direction_from_candles(data_service: Optional[FyersDataService], symbol: str,
                                    session_open_hms: str, num_candles: int, resolution: str,
                                    now: Optional[datetime] = None
                                    ) -> tuple[Optional[TradeDirection], str, float]:
    """
    config.USE_CANDLE_BASED_DIRECTION's price-action counterpart to the sentiment pipeline
    above - decides today's direction from `symbol`'s OWN first `num_candles` candles (each
    `resolution` minutes wide) starting at session_open_hms, instead of global market
    sentiment: if the LAST of those candles' CLOSE is above the FIRST candle's own OPEN,
    direction is LONG; below, SHORT; exactly equal (flat), the usual
    config.ALLOW_LONG_ON_NEUTRAL/ALLOW_SHORT_ON_NEUTRAL fallback applies (see
    _apply_neutral_fallback()) - same policy as a NEUTRAL sentiment reading, reused rather
    than duplicated.

    Returns the SAME (direction_or_None, label, score) 3-tuple shape decide_todays_direction()/
    decide_todays_commodity_direction() always have, so every existing caller keeps working
    with no further changes - but TWO label values carry extra meaning worth checking for:
      - "WAITING_FOR_CANDLES": called before the deciding candle(s) have closed (e.g. at
        09:17 IST when NIFTY needs its 09:25-09:30 candle). direction is always None here,
        but this is NOT "no clear direction today" - it's "don't know YET". The fallback
        flags above are deliberately NOT applied in this case (doing so could open a
        position before the very candles meant to decide its direction have even closed).
        The catch-up functions below turn this into BootstrapResult.needs_scheduled_retry=True
        so main.py's scheduler still gives this market a fresh shot once ready.
      - "CANDLE_ERROR": the candle data couldn't be fetched at all (no data_service, or the
        fetch raised) - treated like a sentiment fetch failure, no retry implied.
    Any other label starts with "CANDLE:" and carries the actual open/close numbers that
    drove the decision - score is the net percentage move across the window.
    """
    now = now or datetime.now(TZ)
    resolution_minutes = int(resolution)
    ready_time = _candle_decision_ready_time(session_open_hms, num_candles, resolution_minutes)
    if now.time() < ready_time:
        logger.info("Candle-based direction for %s: called at %s IST, but its deciding candle(s) "
                    "aren't closed until %s IST (%d x %d-min candle(s) from %s) - not ready yet.",
                    symbol, now.strftime("%H:%M:%S"), ready_time.strftime("%H:%M:%S"),
                    num_candles, resolution_minutes, session_open_hms)
        return None, "WAITING_FOR_CANDLES", 0.0

    if data_service is None:
        logger.error("config.USE_CANDLE_BASED_DIRECTION is True but no data_service was given to "
                      "decide %s's direction from candles - treating as undecidable today.", symbol)
        return None, "CANDLE_ERROR", 0.0

    today_str = now.strftime("%Y-%m-%d")
    try:
        candles_by_symbol = data_service.get_candles_bulk([symbol], resolution, today_str, today_str)
    except Exception as exc:
        logger.error("Could not fetch %s's opening candle(s) for the candle-based direction "
                      "decision: %s", symbol, exc)
        return None, "CANDLE_ERROR", 0.0

    open_t = _time_from_hms(session_open_hms)
    session_open_dt = TZ.localize(datetime(now.year, now.month, now.day,
                                            open_t.hour, open_t.minute, open_t.second))
    window_end_dt = session_open_dt + timedelta(minutes=num_candles * resolution_minutes)

    candles = sorted((c for c in candles_by_symbol.get(symbol, [])
                       if session_open_dt.timestamp() <= c[0] < window_end_dt.timestamp()),
                      key=lambda c: c[0])
    if len(candles) < num_candles:
        logger.warning("Candle-based direction for %s: expected %d candle(s) from %s but only "
                        "got %d - treating as not ready yet (a data gap, or called right at the "
                        "boundary before the feed has the last candle).",
                        symbol, num_candles, session_open_hms, len(candles))
        return None, "WAITING_FOR_CANDLES", 0.0

    first_open = candles[0][1]
    last_close = candles[-1][4]
    pct_change = ((last_close - first_open) / first_open * 100) if first_open else 0.0
    label = (f"CANDLE: {symbol} {num_candles}x{resolution_minutes}min {session_open_hms}->"
             f"{window_end_dt.strftime('%H:%M:%S')} open={first_open:.2f}->close={last_close:.2f} "
             f"({pct_change:+.2f}%)")

    if last_close > first_open:
        return TradeDirection.LONG, label, pct_change
    if last_close < first_open:
        return TradeDirection.SHORT, label, pct_change
    # Exactly flat - a real (if rare) reading, not a "come back later" signal, so it gets the
    # SAME neutral-fallback treatment sentiment's NEUTRAL/MIXED/UNKNOWN readings already get.
    return _apply_neutral_fallback(None), label + " [FLAT]", pct_change


def decide_todays_direction(data_service: Optional[FyersDataService] = None,
                             index_symbol: Optional[str] = None,
                             now: Optional[datetime] = None) -> tuple[Optional[TradeDirection], str, float]:
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

    config.USE_CANDLE_BASED_DIRECTION (default False) swaps the above for a PRICE-ACTION read
    instead - see _decide_direction_from_candles()'s docstring for the exact rule. `symbol`
    used is `index_symbol` if given, else config.NIFTY_INDEX_SYMBOL - equities and NIFTY
    options (which don't pass index_symbol) read NIFTY50's own candles; BANKNIFTY options'
    catch-up/scan pass index_symbol=config.BANKNIFTY_INDEX_SYMBOL explicitly so it reads its
    OWN index's candles instead of piggybacking on NIFTY50's (see
    _bootstrap_options_catchup()/main.job_banknifty_options_scan()). `data_service` is
    REQUIRED in candle mode (every caller already has one in scope); both new parameters are
    silently ignored in sentiment mode (the default), so passing them is always safe
    regardless of which mode is active - no call site needs an if/else for this.
    """
    if config.USE_CANDLE_BASED_DIRECTION:
        symbol = index_symbol or config.NIFTY_INDEX_SYMBOL
        return _decide_direction_from_candles(data_service, symbol, config.MARKET_OPEN_TIME,
                                               config.CANDLE_DIRECTION_NUM_CANDLES,
                                               resolution="5", now=now)
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

    KNOWN LIMITATION: always uses the SENTIMENT pipeline, even if
    config.USE_CANDLE_BASED_DIRECTION is True live - backtest.py's pre-fetched intraday
    candles aren't yet wired through to _decide_direction_from_candles() here (that fetches
    fresh via data_service.get_candles_bulk(), which isn't how backtest.py sources its
    historical data - see backtest.py's own module docstring). Logged once per call so a
    candle-mode backtest doesn't silently diverge from live without at least a loud warning.
    """
    if config.USE_CANDLE_BASED_DIRECTION:
        logger.warning("config.USE_CANDLE_BASED_DIRECTION is True, but decide_direction_for_date() "
                        "(backtest.py) does not support candle-based direction yet - replaying the "
                        "SENTIMENT-based decision for %s instead. Live trading and this backtest will "
                        "diverge if you rely on this result.", as_of_date)
    snapshot = global_sentiment.fetch_global_snapshot_as_of(as_of_date)
    label, score = global_sentiment.score_global_bias(snapshot)
    direction = _apply_neutral_fallback(decide_trade_direction_from_sentiment(label))
    return direction, label, score


def decide_todays_commodity_direction(data_service: Optional[FyersDataService] = None,
                                       now: Optional[datetime] = None
                                       ) -> tuple[Optional[TradeDirection], str, float]:
    """
    Commodity-specific counterpart to decide_todays_direction() - used ONLY by
    main.py's job_mcx_commodity_scan(), never by equities/NIFTY/BANKNIFTY
    options. Turns global_sentiment.score_commodity_global_bias() (gold,
    silver, crude, copper, natural gas, dollar-index cues - see
    global_sentiment.COMMODITY_GLOBAL_TICKERS) into today's MCX trading
    direction: BULLISH -> LONG, BEARISH -> SHORT, same NEUTRAL/MIXED/UNKNOWN
    -> None (subject to the same ALLOW_LONG_ON_NEUTRAL/ALLOW_SHORT_ON_NEUTRAL
    fallback) as the equity-cue version. Deliberately a SEPARATE sentiment
    read, not a reuse of decide_todays_direction()'s equity-cue result -
    MCX commodities trade against global commodity prices, not global
    equity indices, so the two markets can (and sometimes will) end up
    trading in different directions on the same day.

    config.USE_CANDLE_BASED_DIRECTION switches this to a price-action read too, mirroring
    decide_todays_direction()'s candle mode exactly, but from
    config.MCX_DIRECTION_REFERENCE_SYMBOL (or the FIRST entry in config.MCX_COMMODITY_UNIVERSE
    if that's not set) at config.MCX_MARKET_OPEN_TIME, config.MCX_CANDLE_DIRECTION_NUM_CANDLES
    candles wide at MCX's own config.MCX_CANDLE_INTERVAL_MINUTES resolution - the SAME one
    reference contract's candles decide direction for the WHOLE MCX_COMMODITY_UNIVERSE
    (mirrors how commodity sentiment already decides one shared direction for the whole
    universe today - a deliberate judgment call to NOT also rearchitect MCX into independent
    per-contract directions, which nothing asked for; say so if you'd rather have that).
    `data_service` is REQUIRED in candle mode; ignored (safe to pass regardless) in sentiment
    mode.
    """
    if config.USE_CANDLE_BASED_DIRECTION:
        if config.MCX_DIRECTION_REFERENCE_SYMBOL:
            symbol = config.MCX_DIRECTION_REFERENCE_SYMBOL
        elif config.MCX_COMMODITY_UNIVERSE:
            symbol = config.MCX_COMMODITY_UNIVERSE[0]["symbol"]
        else:
            logger.error("config.USE_CANDLE_BASED_DIRECTION is True but MCX_COMMODITY_UNIVERSE is "
                          "empty and MCX_DIRECTION_REFERENCE_SYMBOL isn't set - nothing to read a "
                          "candle from.")
            return None, "CANDLE_ERROR", 0.0
        return _decide_direction_from_candles(data_service, symbol, config.MCX_MARKET_OPEN_TIME,
                                               config.MCX_CANDLE_DIRECTION_NUM_CANDLES,
                                               resolution=str(config.MCX_CANDLE_INTERVAL_MINUTES),
                                               now=now)
    snapshot = global_sentiment.fetch_commodity_global_snapshot()
    label, score = global_sentiment.score_commodity_global_bias(snapshot)
    direction = _apply_neutral_fallback(decide_trade_direction_from_sentiment(label))
    return direction, label, score


def decide_commodity_direction_for_date(as_of_date: date) -> tuple[Optional[TradeDirection], str, float]:
    """Historical counterpart to decide_todays_commodity_direction(), mirroring
    decide_direction_for_date()'s role for the equity-cue version - called per-day by
    backtest.run_mcx_backtest() (Task #7) so MCX backtests pick direction from historical
    COMMODITY sentiment, exactly mirroring what live MCX trading would have chosen.

    KNOWN LIMITATION: same as decide_direction_for_date() - always uses the SENTIMENT
    pipeline even if config.USE_CANDLE_BASED_DIRECTION is True live; candle-based direction
    isn't wired into backtest.py yet. Logged once per call rather than silently diverging.
    """
    if config.USE_CANDLE_BASED_DIRECTION:
        logger.warning("config.USE_CANDLE_BASED_DIRECTION is True, but "
                        "decide_commodity_direction_for_date() (backtest.py) does not support "
                        "candle-based direction yet - replaying the SENTIMENT-based decision for "
                        "%s instead. Live trading and this backtest will diverge if you rely on "
                        "this result.", as_of_date)
    snapshot = global_sentiment.fetch_commodity_global_snapshot_as_of(as_of_date)
    label, score = global_sentiment.score_commodity_global_bias(snapshot)
    direction = _apply_neutral_fallback(decide_trade_direction_from_sentiment(label))
    return direction, label, score


# ----------------------------------------------------------------------------
# Historical replay
# ----------------------------------------------------------------------------
def fetch_and_seed_volume_windows(engine: SignalEngine, data_service: FyersDataService,
                                   reference_date: Optional[date] = None, lookback_days: int = 7,
                                   resolution: str = "5") -> None:
    """
    Live counterpart to backtest.py's own pre-fetched-data seeding (see
    signal_engine.seed_volume_windows_from_candles for what this fixes and
    why): fetches the most recent trading session(s) before reference_date
    (today, if not given) via one bulk call per lookback day actually
    needed, and seeds every tracked symbol's volume windows from it.

    resolution defaults to "5" (equities'/options' own candle width) - bootstrap_mcx_catchup()
    below passes config.MCX_CANDLE_INTERVAL_MINUTES (stringified) instead, so the prior-session
    volume-MA warm-up is sliced at the SAME candle width the live engine will actually check
    (see config.MCX_CANDLE_INTERVAL_MINUTES/Task #6) rather than always assuming 5-min bars.

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
            candles_by_symbol = data_service.get_candles_bulk(symbols, resolution, day_str, day_str)
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
                              symbols: list[str], now: datetime, *,
                              market_open_time: Optional[str] = None,
                              candle_interval_minutes: Optional[int] = None,
                              resolution: Optional[str] = None) -> int:
    """
    Fetches every symbol's candles for today in one rate-limit-hardened
    bulk call, then feeds each FULLY CLOSED candle from market open up to
    "now" through engine.feed_candle(is_backfill=True), in chronological
    order per symbol. Returns how many candles were replayed in total.

    "Fully closed" matters: a candle whose window hasn't finished yet (i.e.
    its close-time is still in the future relative to `now`) is still
    forming - feeding a partial bar would corrupt VWAP/volume-MA state with
    data that hasn't actually happened yet.

    market_open_time/candle_interval_minutes/resolution default to equities' own config
    (config.MARKET_OPEN_TIME / config.CANDLE_INTERVAL_MINUTES / "5") - bootstrap_mcx_catchup()
    below passes MCX's own values instead, so this SAME replay logic serves both markets
    without duplicating it (see config.MCX_MARKET_OPEN_TIME/MCX_CANDLE_INTERVAL_MINUTES).

    The skip boundary is candle_open_time <= market_open (not strictly <): the very FIRST
    candle of the session (whose open time EQUALS market open) is the opening-range candle,
    and it's already seeded into cum_pv/cum_vol by SignalEngine.__init__ at construction time
    (see signal_engine.seed_opening_range_candle) - replaying it again here would double-count
    it in VWAP. Everything strictly after market open is replayed exactly as before.
    """
    if not symbols:
        return 0

    market_open_time = market_open_time or config.MARKET_OPEN_TIME
    candle_interval_minutes = candle_interval_minutes or config.CANDLE_INTERVAL_MINUTES
    resolution = resolution or "5"

    today_str = now.strftime("%Y-%m-%d")
    candles_by_symbol = data_service.get_candles_bulk(symbols, resolution, today_str, today_str)

    market_open_t = _time_from_hms(market_open_time)
    market_open_dt = TZ.localize(datetime(now.year, now.month, now.day,
                                           market_open_t.hour, market_open_t.minute, market_open_t.second))
    candle_span = timedelta(minutes=candle_interval_minutes)

    replayed = 0
    for symbol in symbols:
        candles = sorted(candles_by_symbol.get(symbol, []), key=lambda c: c[0])
        for candle in candles:
            candle_open_time = datetime.fromtimestamp(candle[0], TZ)
            candle_close_time = candle_open_time + candle_span
            if candle_open_time <= market_open_dt or candle_close_time > now:
                continue  # at/before today's open (already seeded at construction - see
                          # docstring above), or not fully closed yet - skip
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
        engine.adopt_broker_position(), which puts it under the same
        candle-level weakness detector as any other open position from the
        next candle check onward (there is no tick-level risk management
        any more - see signal_engine.py's module docstring).
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

    # 2. Today's direction - global sentiment by default, or (config.USE_CANDLE_BASED_DIRECTION)
    #    a price-action read off NIFTY50's own first few opening candles instead - see
    #    decide_todays_direction()'s docstring. Computed before the (potentially expensive) gap
    #    screen below, so an unclear-direction day short-circuits early without spending API
    #    calls scanning a universe we're not going to trade anyway.
    direction, sentiment_label, sentiment_score = decide_todays_direction(data_service, now=now)
    logger.info("Direction decision: %s (score %.2f) -> trade direction: %s",
                sentiment_label, sentiment_score, direction.value if direction else "NONE")
    if sentiment_label == "WAITING_FOR_CANDLES":
        msg = (f"Catch-up: started at {now:%H:%M:%S} IST, but config.USE_CANDLE_BASED_DIRECTION is "
               f"True and today's deciding candle(s) haven't closed yet - the scheduled "
               f"opening_range_scan job (config.CANDLE_DIRECTION_SCAN_TIME) will decide direction "
               f"and build today's shortlist once they do.")
        logger.info(msg)
        return BootstrapResult(phase, None, leaderboard, breadth, msg, needs_scheduled_retry=True,
                                sentiment_label=sentiment_label, sentiment_score=sentiment_score)
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


# ----------------------------------------------------------------------------
# MCX orchestration - decoupled from the equity session entirely
# ----------------------------------------------------------------------------
def bootstrap_mcx_catchup(data_service: FyersDataService) -> BootstrapResult:
    """
    MCX counterpart to bootstrap_and_catchup() above, decoupled from the NSE equity session
    entirely - see detect_mcx_market_phase()/_within_mcx_signal_window() above and main.py's
    module docstring for why: MCX's own session runs 09:00-23:30 IST, far longer than and
    completely independent of NSE's 09:15-15:30, so an equity POST_CLOSE (e.g. starting the
    agent at 17:51 IST) must never prevent THIS from running while MCX is still open. main.py
    calls this unconditionally alongside (not instead of) bootstrap_and_catchup() - the two
    markets' bootstrap results are evaluated and acted on completely independently.

    Mirrors bootstrap_and_catchup()'s overall shape, but notably simpler - no sector/breadth
    snapshot (an NSE-equity-only concept with no MCX analogue) and no gain/loss-band
    shortlist screen: MCX trades config.MCX_COMMODITY_UNIVERSE directly and unconditionally
    every day (see commodity_engine.py's module docstring), so there's nothing to scan or
    filter down to.

      1. detect_mcx_market_phase()           - PRE_OPEN / POST_OPEN / POST_CLOSE, MCX's OWN
         session window (config.MCX_MARKET_OPEN_TIME/MCX_MARKET_CLOSE_TIME)
      2. decide_todays_commodity_direction() - COMMODITY-specific global sentiment (gold/
         silver/crude/copper/natural-gas/dollar-index cues), deliberately separate from
         equities' own decide_todays_direction() (see that function's docstring)
      3. commodity_engine.run_mcx_commodity_engine() - builds the engine from the WHOLE
         universe's own opening-range candles (no shortlist step), at MCX's own configurable
         candle_resolution (config.MCX_CANDLE_INTERVAL_MINUTES - see Task #6)
      4. fetch_and_seed_volume_windows()     - prior-session volume-MA warm-up, sliced at
         MCX's own candle resolution
      5. _replay_intraday_history()          - MCX_MARKET_OPEN_TIME-to-now replay, using
         MCX's own open time/interval/resolution (see that function's docstring)
      6. _reconcile_broker_state()           - broker truth overrides replay, identical to
         equities (already fully generic over any engine.trackers - no MCX-specific change
         needed there at all)

    Returns a BootstrapResult - the SAME dataclass bootstrap_and_catchup() returns (its
    `signal_engine` field holds the commodity engine here, not an equity one) rather than a
    separate MCX-specific type, so main.py can handle both results with identical code.
    """
    if not config.ENABLE_MCX_COMMODITY_TRADING:
        return BootstrapResult(MarketPhase.POST_CLOSE, None,
                                message="MCX commodity trading is disabled (config.ENABLE_MCX_COMMODITY_TRADING "
                                        "= False) - nothing to bootstrap.")

    now = datetime.now(TZ)
    phase = detect_mcx_market_phase(now)

    if phase == MarketPhase.PRE_OPEN:
        msg = (f"MCX: started pre-open at {now:%H:%M:%S} IST - nothing to catch up on; the "
               f"scheduled MCX job in main.py will run at its normal time.")
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg)

    if phase == MarketPhase.POST_CLOSE:
        day_kind = "a weekend" if now.weekday() >= 5 else "after today's MCX market close"
        msg = f"MCX: market is closed ({day_kind}, {now:%Y-%m-%d %H:%M:%S} IST) - exiting, nothing to bootstrap."
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg)

    # phase == POST_OPEN
    if not _within_mcx_signal_window(now):
        msg = (f"MCX: started at {now:%H:%M:%S} IST - market is open but past the "
               f"{config.MCX_SIGNAL_ENGINE_LAST_RUN} signal-checking cutoff, so no fresh catch-up "
               f"will run today (see _within_mcx_signal_window's docstring for why).")
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg)

    logger.info("=== ENTERING MCX CATCH-UP MODE at %s IST ===", now.strftime("%H:%M:%S"))

    direction, sentiment_label, sentiment_score = decide_todays_commodity_direction(data_service, now=now)
    logger.info("MCX direction decision: %s (score %.2f) -> trade direction: %s",
                sentiment_label, sentiment_score, direction.value if direction else "NONE")
    if sentiment_label == "WAITING_FOR_CANDLES":
        msg = (f"MCX catch-up: started at {now:%H:%M:%S} IST, but config.USE_CANDLE_BASED_DIRECTION "
               f"is True and today's deciding candle(s) haven't closed yet - the scheduled "
               f"job_mcx_commodity_scan job (config.MCX_CANDLE_DIRECTION_SCAN_TIME) will decide "
               f"direction once they do.")
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg, needs_scheduled_retry=True,
                                sentiment_label=sentiment_label, sentiment_score=sentiment_score)
    if direction is None:
        msg = (f"MCX catch-up: commodity sentiment is {sentiment_label} (score {sentiment_score:.2f}) - "
               f"not a clear bullish/bearish read, so no new MCX positions will be opened today.")
        logger.warning(msg)
        return BootstrapResult(phase, None, message=msg, direction=None,
                                sentiment_label=sentiment_label, sentiment_score=sentiment_score)

    engine = commodity_engine.run_mcx_commodity_engine(data_service, direction)
    if engine is None:
        msg = ("MCX catch-up: no opening-range candle was available for any MCX commodity today - "
               "nothing to track.")
        logger.warning(msg)
        return BootstrapResult(phase, None, message=msg, direction=direction,
                                sentiment_label=sentiment_label, sentiment_score=sentiment_score)

    resolution = str(config.MCX_CANDLE_INTERVAL_MINUTES)

    # 4. Seed volume-MA windows from the prior session, BEFORE replay - same reasoning as
    #    equities' own step 4 above, just at MCX's own candle resolution.
    fetch_and_seed_volume_windows(engine, data_service, reference_date=now.date(), resolution=resolution)

    # 5. Replay MCX_MARKET_OPEN_TIME -> now for every universe symbol (backfill-flagged).
    _replay_intraday_history(engine, data_service, list(engine.trackers), now,
                              market_open_time=config.MCX_MARKET_OPEN_TIME,
                              candle_interval_minutes=config.MCX_CANDLE_INTERVAL_MINUTES,
                              resolution=resolution)

    # 6. Broker truth overrides replay wherever they disagree - identical to equities.
    _reconcile_broker_state(engine, data_service)

    open_count = sum(1 for t in engine.trackers.values() if t.position_open)
    msg = (f"MCX catch-up complete: tracking {len(engine.trackers)} {direction.value} commodity "
           f"contract(s), {open_count} with an open position reconstructed from today so far.")
    logger.info(msg)
    return BootstrapResult(phase, engine, message=msg, direction=direction,
                            sentiment_label=sentiment_label, sentiment_score=sentiment_score)


# ----------------------------------------------------------------------------
# NIFTY/BANKNIFTY options catch-up - decoupled from the scheduled-cron-only path they used to
# rely on exclusively (see main.py's module docstring and this module's own top-of-file history
# for MCX's equivalent bug: a late start used to miss these two entirely)
# ----------------------------------------------------------------------------
# NOTE: this used to need its own bespoke _replay_options_intraday_history() here, because an
# earlier version of NiftyOptionsEngine read its PRICE signal from the index and its VOLUME
# signal from the future - two symbols paired by epoch, fed together through
# engine.feed_candles(index_candle, futures_candle, ...). Now that the whole signal (price,
# VWAP, AND volume) reads from the futures symbol alone (see options_engine.py's module
# docstring), NiftyOptionsEngine.trackers/.feed_candle(symbol, candle, now_str, is_backfill)
# has the exact same one-symbol-at-a-time shape signal_engine.SignalEngine does, so
# _bootstrap_options_catchup() below just calls the generic _replay_intraday_history() above
# directly - no options-specific replay function needed any more.


def _bootstrap_options_catchup(data_service: FyersDataService, *, enabled: bool, label: str,
                                index_symbol: str,
                                run_engine: Callable[[FyersDataService, TradeDirection],
                                                      "Optional[options_engine.NiftyOptionsEngine]"]
                                ) -> BootstrapResult:
    """
    Shared implementation behind bootstrap_nifty_options_catchup()/
    bootstrap_banknifty_options_catchup() below - NIFTY and BANKNIFTY options are two
    independent instances of the exact same catch-up shape (same NSE equity session, same
    options_engine.NiftyOptionsEngine class, same .trackers/.square_off_all() duck-typed
    surface as the equity/MCX engines - see options_engine.py's module docstring), differing
    only in which config/engine-builder they're pointed at - factored out rather than
    copy-pasted twice.

    Deliberately reuses detect_market_phase()/_within_signal_window() (the EQUITY session
    window), not a separate options-specific phase: NIFTY/BANKNIFTY options trade during NSE's
    regular 09:15-15:30 session, exactly like equities, so there's no independent session clock
    to detect here the way MCX needed (MCX's session genuinely runs on different hours - see
    detect_mcx_market_phase()'s docstring for why THAT one needed its own).

    Before this existed, job_nifty_options_scan()/job_banknifty_options_scan() in main.py were
    ONLY ever triggered by their fixed 09:20:02 cron job - with no catch-up/replay path, a start
    any time after that (even while equities' own catch-up still worked fine) silently never
    built either options engine for the rest of the day, since APScheduler resolves an
    already-passed cron time to tomorrow. This mirrors bootstrap_and_catchup()'s/
    bootstrap_mcx_catchup()'s shape to close that same gap here.

    KNOWN LIMITATION: unlike bootstrap_and_catchup()/bootstrap_mcx_catchup(), this does NOT call
    _reconcile_broker_state() - that helper walks engine.trackers' StockTracker.position_open
    flags and calls engine.adopt_broker_position(symbol, ...), both of which assume a
    multi-symbol SignalEngine. NiftyOptionsEngine's single option leg is bookkept in
    `.option` (an _OptionPosition), not a StockTracker, and it exposes no
    adopt_broker_position() method, so that reconciliation step genuinely doesn't apply here
    as-is. If this agent is ever reconciled against a real broker's option positions, that
    needs an options-specific version of _reconcile_broker_state() added here first - until
    then, a catch-up start trusts the REPLAYED state alone for the option leg, same as before
    this function existed for the on-time (09:20:02) path.
    """
    if not enabled:
        return BootstrapResult(MarketPhase.POST_CLOSE, None,
                                message=f"{label} options trading is disabled - nothing to bootstrap.")

    now = datetime.now(TZ)
    phase = detect_market_phase(now)

    if phase == MarketPhase.PRE_OPEN:
        msg = (f"{label} options: started pre-open at {now:%H:%M:%S} IST - nothing to catch up "
               f"on; the scheduled job in main.py will run at its normal time.")
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg)

    if phase == MarketPhase.POST_CLOSE:
        day_kind = "a weekend" if now.weekday() >= 5 else "after today's market close"
        msg = (f"{label} options: market is closed ({day_kind}, {now:%Y-%m-%d %H:%M:%S} IST) - "
               f"nothing to bootstrap.")
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg)

    # phase == POST_OPEN
    if not _within_signal_window(now):
        msg = (f"{label} options: started at {now:%H:%M:%S} IST - market is open but past the "
               f"{config.SIGNAL_ENGINE_LAST_RUN} signal-checking cutoff, so no fresh catch-up "
               f"will run today.")
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg)

    logger.info("=== ENTERING %s OPTIONS CATCH-UP MODE at %s IST ===", label, now.strftime("%H:%M:%S"))

    # Direction is recomputed fresh here, deliberately NOT reused from equities' own catch-up
    # result - this matches how the existing on-time jobs already behave (job_opening_range_scan/
    # job_nifty_options_scan/job_banknifty_options_scan each call decide_todays_direction()
    # independently, since sentiment can move between calls and each job wants the freshest read
    # at the moment it actually commits to a shortlist/position - see job_opening_range_scan's
    # own docstring). A catch-up start is no different: it's standing in for whichever job would
    # have run at this moment, so it gets the same "as current as possible" treatment they do.
    # index_symbol matters only in config.USE_CANDLE_BASED_DIRECTION mode (sentiment mode
    # ignores it) - NIFTY passes config.NIFTY_INDEX_SYMBOL, BANKNIFTY passes its own
    # config.BANKNIFTY_INDEX_SYMBOL, so each reads its OWN index's opening candles rather than
    # both silently sharing NIFTY50's (see decide_todays_direction()'s docstring).
    direction, sentiment_label, sentiment_score = decide_todays_direction(
        data_service, index_symbol=index_symbol, now=now)
    logger.info("%s options: direction decision %s (score %.2f) -> trade direction: %s",
                label, sentiment_label, sentiment_score, direction.value if direction else "NONE")
    if sentiment_label == "WAITING_FOR_CANDLES":
        msg = (f"{label} options: started at {now:%H:%M:%S} IST, but config."
               f"USE_CANDLE_BASED_DIRECTION is True and today's deciding candle(s) haven't closed "
               f"yet - the scheduled scan job (config.CANDLE_DIRECTION_SCAN_TIME) will decide "
               f"direction once they do.")
        logger.info(msg)
        return BootstrapResult(phase, None, message=msg, needs_scheduled_retry=True,
                                sentiment_label=sentiment_label, sentiment_score=sentiment_score)
    if direction is None:
        msg = (f"{label} options catch-up: global sentiment is {sentiment_label} (score "
               f"{sentiment_score:.2f}) - not a clear bullish/bearish read, so no new {label} "
               f"options position will be opened today.")
        logger.warning(msg)
        return BootstrapResult(phase, None, message=msg, direction=None,
                                sentiment_label=sentiment_label, sentiment_score=sentiment_score)

    engine = run_engine(data_service, direction)
    if engine is None:
        msg = (f"{label} options catch-up: engine could not be built today (see options_engine "
               f"logs above - no OTM strike resolved, no opening-range candle yet, or it failed "
               f"the opening-range wick filter) - no {label} options position will be tracked "
               f"today.")
        logger.warning(msg)
        return BootstrapResult(phase, None, message=msg, direction=direction,
                                sentiment_label=sentiment_label, sentiment_score=sentiment_score)

    # Seed the futures tracker's volume-MA window from the prior session, BEFORE replay - same
    # reasoning as equities'/MCX's own step. engine.trackers exposes the ONE futures tracker
    # (see NiftyOptionsEngine.trackers' docstring) - its VWAP was already seeded from its own
    # opening-range candle inside run_engine() above.
    fetch_and_seed_volume_windows(engine, data_service, reference_date=now.date())

    # Replay 09:15 -> now for the futures symbol - NiftyOptionsEngine is now a single-source,
    # single-symbol signal (see options_engine.py's module docstring), so it has the exact same
    # engine.trackers/.feed_candle(symbol, candle, now_str, is_backfill) shape
    # signal_engine.SignalEngine does, and this generic replay helper (equities'/MCX's own)
    # works unchanged - no options-specific paired-candle replay function needed any more.
    _replay_intraday_history(engine, data_service, list(engine.trackers), now)

    # No _reconcile_broker_state() call here - see this function's own docstring, "KNOWN
    # LIMITATION", for why.

    if engine.option.is_open:
        msg = (f"{label} options catch-up complete: reconstructed an OPEN "
               f"{engine.option.entry_direction.value} position in {engine.option.symbol} from "
               f"today so far.")
    else:
        msg = (f"{label} options catch-up complete: flat, watching {engine.futures_tracker.symbol} "
               f"for a breakout in EITHER direction (CE on an upside breakout, PE on a downside "
               f"one - today's {direction.value} bias only gated this morning's opening-range "
               f"quality filter) - the strike will be resolved fresh from spot whenever an entry "
               f"actually fires.")
    logger.info(msg)
    return BootstrapResult(phase, engine, message=msg, direction=direction,
                            sentiment_label=sentiment_label, sentiment_score=sentiment_score)


def bootstrap_nifty_options_catchup(data_service: FyersDataService) -> BootstrapResult:
    """NIFTY counterpart to bootstrap_mcx_catchup() - see _bootstrap_options_catchup()'s
    docstring for the full shape/rationale. Self-gated on config.ENABLE_NIFTY_OPTIONS_TRADING,
    runs on NSE's regular equity session window, builds via options_engine.run_nifty_options_engine().
    index_symbol=config.NIFTY_INDEX_SYMBOL - only read in config.USE_CANDLE_BASED_DIRECTION mode."""
    return _bootstrap_options_catchup(data_service, enabled=config.ENABLE_NIFTY_OPTIONS_TRADING,
                                       label="NIFTY", index_symbol=config.NIFTY_INDEX_SYMBOL,
                                       run_engine=options_engine.run_nifty_options_engine)


def bootstrap_banknifty_options_catchup(data_service: FyersDataService) -> BootstrapResult:
    """BANKNIFTY counterpart to bootstrap_mcx_catchup() - see _bootstrap_options_catchup()'s
    docstring for the full shape/rationale. Self-gated on config.ENABLE_BANKNIFTY_OPTIONS_TRADING,
    runs on NSE's regular equity session window (SAME as NIFTY options and equities - BANKNIFTY
    options are not a separate session), builds via options_engine.run_banknifty_options_engine().
    index_symbol=config.BANKNIFTY_INDEX_SYMBOL - its OWN index, not NIFTY50's - only read in
    config.USE_CANDLE_BASED_DIRECTION mode (both share the same equity-cue SENTIMENT read in
    sentiment mode, unaffected by this parameter)."""
    return _bootstrap_options_catchup(data_service, enabled=config.ENABLE_BANKNIFTY_OPTIONS_TRADING,
                                       label="BANKNIFTY", index_symbol=config.BANKNIFTY_INDEX_SYMBOL,
                                       run_engine=options_engine.run_banknifty_options_engine)
