"""
config.py
----------
Centralised configuration for the Opening Range Breakout (ORB) scanning agent.

All secrets are read from environment variables (via a local `.env` file) so
credentials never get hard-coded or committed to source control. Copy
`.env.example` to `.env` and fill in your own values before running anything.
"""
"""
============================================================================
-----------------------------MASTER SYMBOL LINK---------------------------------
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
https://fyers.in/api-docs/v3/api/appendix/broker-config/symbol-master-csv/
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()  # loads variables from a local .env file, if present

# ----------------------------------------------------------------------------
# Fyers API credentials (create an app at https://myapi.fyers.in)
# ----------------------------------------------------------------------------
FYERS_APP_ID = os.getenv("FYERS_APP_ID", "")            # e.g. "ABCDE12345-100"
FYERS_SECRET_KEY = os.getenv("FYERS_SECRET_KEY", "")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "")
FYERS_FY_ID = os.getenv("FYERS_FY_ID", "")               # your Fyers login id (e.g. XY01234)
FYERS_PIN = os.getenv("FYERS_PIN", "")                   # 4-digit trading PIN
FYERS_TOTP_SECRET = os.getenv("FYERS_TOTP_SECRET", "")   # from Fyers > Profile > 2FA setup

# Optional: only needed if you enable the LLM-based macro/geopolitical news
# sentiment helper in global_sentiment.get_llm_news_sentiment()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ----------------------------------------------------------------------------
# File paths
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

STOCK_UNIVERSE_CSV = os.path.join(DATA_DIR, "ind_nifty500list.csv")     # see README for download link
SECTOR_INDEX_MAP_CSV = os.path.join(DATA_DIR, "sector_index_map.csv")  # symbol -> sector mapping
LOG_FILE = os.path.join(LOG_DIR, "orb_agent.log")
TRADE_LOG_CSV = os.path.join(LOG_DIR, "trade_book_{date}.csv")

# ----------------------------------------------------------------------------
# Timing (all times are IST / Asia/Kolkata, weekdays only)
# ----------------------------------------------------------------------------
TIMEZONE = "Asia/Kolkata"

SECTOR_SCAN_TIME = "09:10:00"           # pre-open sector snapshot (see caveat in sector_scanner.py)
SECTOR_SCAN_TIME_POSTOPEN = "09:16:00"  # re-run shortly after the normal session starts, for real numbers
OPENING_RANGE_SCAN_TIME = "09:20:02"    # 2s buffer after the 09:15-09:20 candle closes
SIGNAL_ENGINE_LAST_RUN = "15:10:00"     # stop looking for NEW signals after this time
SQUARE_OFF_TIME = "15:10:30"            # force-close any open paper positions
CANDLE_INTERVAL_MINUTES = 5

# NSE's actual normal-session hours. Used only to decide whether a late
# application startup falls "within trading hours" for the catch-up logic in
# main.py - does NOT account for NSE trading holidays, which is a separate
# concern this scaffold doesn't cover (a holiday calendar check would need
# to be layered on top before relying on this for unattended daily runs).
MARKET_OPEN_TIME = "09:15:00"
MARKET_CLOSE_TIME = "15:30:00"

# job_opening_range_scan() uses the fast quotes-proxy method (valid only
# right at 09:20:02, see opening_range_scanner.py) when called within this
# many seconds of OPENING_RANGE_SCAN_TIME, and automatically falls back to
# the slower-but-always-correct historical-candle method otherwise (e.g. on
# a late/catch-up start). See job_opening_range_scan in main.py.
OPENING_RANGE_FAST_PATH_TOLERANCE_SECONDS = 180

# Fyers' documented per-request limits for the historical /history endpoint
# (confirmed via their own API docs and community forum, including an
# actual -339 error for exceeding the daily one): up to 100 days per request
# for intraday resolutions (1/2/3/5/10/15/20/30/45/60/120/180/240 min), and
# up to ~365-366 days per request for daily ('D') resolution. backtest.py
# chunks any longer requested range into calls no larger than these.
# Verify against your own account/plan before relying on it for a long
# backtest - these are Fyers-side limits that could change.
INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK = 100
DAILY_HISTORY_MAX_DAYS_PER_CHUNK = 365

# Master switch for the agent's original/core strategy: scanning the NSE equity universe for
# opening-range gap gainers/losers and trading the shortlisted stocks themselves (as opposed to
# the NIFTY/BANKNIFTY options or MCX commodity engines below, each gated by their own
# ENABLE_*_TRADING flag). main.job_opening_range_scan() and the equity catch-up path inside
# bootstrap.bootstrap_and_catchup() both self-gate on this flag (same "check first, no-op if
# off" pattern as ENABLE_NIFTY_OPTIONS_TRADING/ENABLE_BANKNIFTY_OPTIONS_TRADING/
# ENABLE_MCX_COMMODITY_TRADING) - main.py still schedules job_opening_range_scan
# unconditionally; it just returns immediately once called if this is False. Equity market-phase
# detection (detect_market_phase()/PRE_OPEN/POST_OPEN/POST_CLOSE) is UNAFFECTED by this flag and
# keeps reflecting the real NSE session clock either way, since NIFTY/BANKNIFTY options ride on
# that same session and main()'s whole-process exit decision depends on it being accurate
# regardless of whether equities themselves are enabled.
#
# Default is False, matching the other three strategies' own defaults - set it True to turn
# equities back on. NOTE: with ENABLE_STOCK_TRADING, ENABLE_NIFTY_OPTIONS_TRADING,
# ENABLE_BANKNIFTY_OPTIONS_TRADING and ENABLE_MCX_COMMODITY_TRADING all defaulting to False out
# of the box, the agent will trade NOTHING until you explicitly turn at least one of them on.
ENABLE_STOCK_TRADING = False

# ----------------------------------------------------------------------------
# Strategy parameters
# ----------------------------------------------------------------------------
MIN_GAIN_PCT = 0.5       # lower bound (exclusive) for the opening-range gainer filter (LONG side)
MAX_GAIN_PCT = 2.5       # upper bound (inclusive) for the opening-range gainer filter (LONG side)

# Mirror of the above for the SHORT side: a stock down between MIN_LOSS_PCT and MAX_LOSS_PCT
# (both positive numbers here, representing the magnitude of the decline) is eligible for
# the gap-down/breakdown screen. See opening_range_scanner.scan_opening_range(direction=...).
MIN_LOSS_PCT = 0.5
MAX_LOSS_PCT = 2.5

TOP_N_STOCKS = 5          # how many stocks to shortlist and track intraday
VOLUME_MA_PERIOD = 20     # number of 5-min candles for the ENTRY volume moving average
CONSECUTIVE_CANDLES_FOR_BUY = 2  # candles required to confirm a breakout entry

# Opening-range candle "closing strength" filter - a SECOND, independent screen on the 09:15
# candle itself, applied on top of the prev-close gain/loss band above. It measures how far the
# candle's CLOSE sits away from the extreme that matters for a breakout in that direction, as a
# percentage of the candle's own (high - low) range:
#   LONG:  wick_pct = (high  - close) / (high - low) * 100   (distance from close UP to the high)
#   SHORT: wick_pct = (close - low)   / (high - low) * 100   (distance from close DOWN to the low)
# wick_pct is always in [0, 100] by construction (close sits between the candle's high and low):
#   0%   = closed exactly at the relevant extreme - the strongest possible candle for that side.
#   100% = closed at the OPPOSITE extreme - a full reversal wick working against the trade (e.g.
#          for LONG, a candle that spiked up then sold all the way back down to its low by the
#          close - a weak sign for a breakout that needs the stock to keep going UP).
# A stock is kept only if wick_pct <= MAX_OR_CANDLE_WICK_PCT. A flat candle (high == low) always
# passes (wick_pct is defined as 0.0 - there's no wick to measure).
#
# 100.0 (the default below) is a NO-OP: every candle passes, since wick_pct can never exceed 100
# - this preserves old behaviour (no wick-based filtering at all) until you deliberately tune it
# down. There's no universally "right" value - it depends on your universe and the gain/loss
# band above - so don't hand-pick one from intuition alone. Use
# backtest.sweep_or_candle_wick_threshold() (or `python backtest.py --sweep-wick ...` from the
# CLI) to replay your own historical data across a range of candidate thresholds and see which
# one actually produces the best total_points/win_rate for YOUR universe and sentiment config -
# that empirical result is the "optimum", not a number copied from here. As a starting point for
# the sweep's own candidate range, many opening-range strategies land somewhere around 30-50
# (close in roughly the half-to-two-thirds of the range closest to the breakout side).
#
# Applies to EVERY engine this agent runs, ONE shared threshold (merged from two separate
# settings - equities/options and MCX used to be tuned independently - on the understanding
# that you'd rather have one knob than remember to update two):
#   - the NSE equity shortlist (opening_range_scanner.py/backtest.py's equity path - per-STOCK,
#     part of the shortlist filter)
#   - the NIFTY/BANKNIFTY options engines (options_engine.py - a per-DAY go/no-go gate on the
#     underlying FUTURE's own opening-range candle: too wide a reversal wick there skips
#     options entirely for the day, same as "no opening-range candle available yet")
#   - MCX commodities (commodity_engine.py - applied PER CONTRACT in
#     fetch_mcx_universe_opening_ranges(), and mirrored in backtest.py's MCX path: a contract
#     whose own opening-range candle closed with too wide a reversal wick against today's
#     bias_direction is left out of the traded universe for the day, logged the same way a
#     genuine data gap already is)
# All three use the exact same formula (or_candle_wick_pct() in opening_range_scanner.py,
# reused as-is everywhere) against their OWN opening-range candle - only the candle's width
# differs per market (NSE's fixed 5-min bar vs config.MCX_CANDLE_INTERVAL_MINUTES), not the
# threshold. If MCX's typical intraday volatility ever turns out to need a genuinely different
# bar than equities/options do, split this back into two config values rather than hand-waving
# a shared number across markets with different wick behavior - nothing stops you, this is just
# this project's current default.
MAX_OR_CANDLE_WICK_PCT = 100.0

# ---- Directional gating from global sentiment (see trade_direction.py) ----
# When global_sentiment.score_global_bias() (or get_stock_news_ema_bias's combined_bias)
# comes back NEUTRAL/MIXED/UNKNOWN rather than a clear BULLISH or BEARISH, these decide
# whether to still trade a direction anyway. Both False (the default) means: no clear
# sentiment -> open no new positions that day, in either direction - a deliberately
# conservative default. Set either to True to fall back to that direction specifically
# when sentiment is unclear, rather than sitting out entirely.
ALLOW_LONG_ON_NEUTRAL = False
ALLOW_SHORT_ON_NEUTRAL = False

# ---- Candle-based direction (alternative to global sentiment above) ----
# Off by default - bootstrap.decide_todays_direction()/decide_todays_commodity_direction()
# (used by EVERY catch-up/scan path: equities, NIFTY options, BANKNIFTY options, MCX) use
# global-sentiment-derived direction as documented above. Flip this to True to switch them
# ALL to a PRICE-ACTION read instead: today's direction is decided "on the fly" from a
# reference instrument's OWN first few opening candles, rather than from
# global_sentiment.py's index-proxy snapshot. Rule (same for every market): take the first
# CANDLE_DIRECTION_NUM_CANDLES (or MCX_CANDLE_DIRECTION_NUM_CANDLES for MCX) candles from
# session open; if the LAST candle's CLOSE is above the FIRST candle's own OPEN, direction is
# LONG; below, SHORT; exactly flat (equal - rare but possible), the SAME
# ALLOW_LONG_ON_NEUTRAL/ALLOW_SHORT_ON_NEUTRAL fallback above applies (both False by default
# -> sit out that day), reusing that existing policy rather than adding a separate flag for
# it. See bootstrap._decide_direction_from_candles()'s docstring for the exact mechanics,
# including the "called before the deciding candle has even closed" WAITING_FOR_CANDLES case
# (NOT the same as a flat reading - a late/mid-session start before the window closes gets
# automatically retried via the scheduled scan job once the candles ARE in, rather than being
# treated as "no clear direction today").
#
# IMPORTANT - this also shifts WHEN each market's opening-range scan job can first run, since
# the direction literally cannot be known until the deciding candle(s) close: main.py
# schedules the equity/NIFTY-options/BANKNIFTY-options scan at CANDLE_DIRECTION_SCAN_TIME
# instead of OPENING_RANGE_SCAN_TIME, and MCX's scan at MCX_CANDLE_DIRECTION_SCAN_TIME instead
# of MCX_OPENING_RANGE_SCAN_TIME, whenever this flag is True - see main()'s scheduler wiring.
#
# NOTE: the opening-range HIGH/LOW used as each stock's/index's breakout trigger line is NOT
# affected by this flag - it stays the first 5-min candle only (unchanged), same as sentiment
# mode. Only the DIRECTION decision reads the extra candles; the trigger prices do not widen
# to span them.
#
# NOTE: backtest.py's decide_direction_for_date()/decide_commodity_direction_for_date() do
# NOT currently support this mode - they always replay the sentiment-based decision regardless
# of this flag (and log a warning if it's on), since teaching the backtester to slice its own
# pre-fetched intraday candles the same way is a separate, not-yet-built piece of work. Ask for
# it if you want backtests to reflect candle-based direction too.
USE_CANDLE_BASED_DIRECTION = False

# NIFTY/BANKNIFTY options (and, since they share decide_todays_direction(), equities too):
# how many of config.MARKET_OPEN_TIME's own 5-min candles to wait for/read. 3 candles from
# 09:15 means the decision is only possible once the 09:25-09:30 candle has closed, i.e. at
# or after 09:30:00 - see CANDLE_DIRECTION_SCAN_TIME below, which must stay in sync with this
# if you change it (CANDLE_DIRECTION_SCAN_TIME = MARKET_OPEN_TIME + this many candles' worth
# of minutes, plus a small buffer).
CANDLE_DIRECTION_NUM_CANDLES = 3
CANDLE_DIRECTION_SCAN_TIME = "09:30:02"   # 2s buffer after the 3rd 09:15-session candle closes (09:30:00)

# MCX counterpart - 2 candles from MCX_MARKET_OPEN_TIME (09:00 by default) means ready at
# 09:10:00. Kept independent of CANDLE_DIRECTION_NUM_CANDLES/CANDLE_DIRECTION_SCAN_TIME above
# (unlike MAX_OR_CANDLE_WICK_PCT, which IS now shared across every market - see its own docs)
# because MCX's own session/candle width genuinely differs from NSE's (config.
# MCX_CANDLE_INTERVAL_MINUTES, not NSE's fixed 5-min bar), so the TIME this becomes decidable
# can't be a single shared clock value the way a dimensionless wick PERCENTAGE can be a single
# shared threshold.
MCX_CANDLE_DIRECTION_NUM_CANDLES = 2
MCX_CANDLE_DIRECTION_SCAN_TIME = "09:10:02"  # 2s buffer after the 2nd MCX-session candle closes (09:10:00)

# Which ONE MCX_COMMODITY_UNIVERSE contract's own candles decide direction for the WHOLE MCX
# FUTURES universe (commodity_engine.py only - see mcx_options_engine.py's own note below for
# why MCX OPTIONS does NOT use this) - mirrors how commodity SENTIMENT already decides one
# shared direction for the whole universe today. None (the default) means "the first entry in
# MCX_COMMODITY_UNIVERSE" - set an explicit symbol string here (e.g. "MCX:GOLD25DECFUT") to
# pick a specific reference contract instead, independent of ordering in the universe list.
#
# NOTE this is a SINGLE shared value, not configurable per-symbol - each commodity in
# MCX_COMMODITY_UNIVERSE still trades the SAME direction (LONG or SHORT, decided from this one
# reference contract's candles, or from commodity sentiment when USE_CANDLE_BASED_DIRECTION is
# off) when traded as FUTURES via commodity_engine.run_mcx_commodity_engine(). If you want each
# commodity to decide ITS OWN direction from ITS OWN candles instead (bidirectional, CE-or-PE,
# exactly like NIFTY/BANKNIFTY options already do - see options_engine.py's module docstring),
# that's what MCX OPTIONS trading (ENABLE_MCX_OPTIONS_TRADING, mcx_options_engine.py) is for:
# its engine watches EVERY commodity's own futures breakout independently and buys that SAME
# commodity's own CE/PE, with no shared reference symbol or shared direction involved at all -
# this MCX_DIRECTION_REFERENCE_SYMBOL/commodity-sentiment bias only gates each commodity's own
# opening-range quality filter there (config.MAX_OR_CANDLE_WICK_PCT), the same demoted role
# bias_direction plays for NIFTY/BANKNIFTY options.
MCX_DIRECTION_REFERENCE_SYMBOL = None

# ---- Stop-loss / square-off & position-sizing (add-on) parameters ----
# Weakness/breakdown is detected with the SAME 20-period volume MA
# (VOLUME_MA_PERIOD above) used for entries - two consecutive candles closing
# against the position, with volume above that 20-period average, confirm
# the exit/add-on signal. (Earlier revisions used a separate, faster 2-period
# volume average here; that has been replaced so entry and exit share one
# volume-conviction bar.)
CONSECUTIVE_CANDLES_FOR_STOPLOSS = 2

# The FIRST time the weakness pattern appears after a buy (no square-off has
# fired yet on this position), the engine adds to the position instead of
# exiting - up to MAX_POSITION_ADDS times. Any weakness event beyond that
# triggers a full square-off of the whole position (all adds included).
# Set MAX_POSITION_ADDS = 0 to disable adding and square off on the very
# first weakness event instead.
MAX_POSITION_ADDS = 1

# Retry / resilience for API calls
API_MAX_RETRIES = 3
API_RETRY_BACKOFF_SECONDS = 2

# Rate-limit-SPECIFIC retry handling (see fyers_service._looks_like_rate_limit_error),
# separate from the generic linear-backoff retries above - a rejection that's
# genuinely "you're being rate limited" warrants more attempts with an exponentially
# growing wait, since a short fixed backoff often isn't enough for a per-minute-style
# limit to actually clear. This matters most during the morning bulk historical fetch
# (fyers_service.FyersDataService.get_candles_bulk), where many symbols are fetched
# back-to-back right after market open.
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_BACKOFF_BASE_SECONDS = 3  # doubles each attempt: 3s, 6s, 12s, 24s, 48s

# Fyers caps API calls per second per account/plan (you mentioned 9/sec for
# yours). Every single call FyersDataService makes - quotes, history,
# retries included - goes through fyers_service.RateLimiter using this
# value. Set it a little under your actual documented limit, not exactly at
# it, to leave headroom for clock jitter and any other process sharing the
# same API key (e.g. a separate WebSocket connection).
FYERS_MAX_CALLS_PER_SECOND = 8

# How many symbols to pack into a single batched /quotes call. Fyers caps
# how many comma-separated symbols one quotes request accepts - verify the
# current limit for your plan in the API docs before changing this.
QUOTES_BATCH_SIZE = 50

# Dry-run: when True (default) the agent only *simulates* trades in memory and
# CSV, and NEVER calls fyers.place_order(). Only flip to False after you have
# paper-traded the logic for a good stretch and understand the risk involved
# (see README "Before you go live").
DRY_RUN = True

# ---- Capital-based position sizing (see position_sizing.py) ----
# Total capital this agent will work with for EQUITY trading (paper, via
# DRY_RUN=True, or real via your Fyers broker account once DRY_RUN=False -
# both paths use the exact same sizing logic below; DRY_RUN only decides
# whether the resulting order is logged or actually sent to the broker, see
# fyers_service.FyersDataService.place_order and
# signal_engine.SignalEngine._maybe_place_live_order).
TRADING_CAPITAL = 200_000.0

# Equal-split allocation: with TOP_N_STOCKS shortlisted stocks tracked
# concurrently (see opening_range_scanner.py), each gets an equal share of
# TRADING_CAPITAL as its per-trade budget - position_sizing.calculate_quantity()
# then buys as many whole shares as that budget affords at the entry price.
# A position "ADD" (see CONSECUTIVE_CANDLES_FOR_STOPLOSS/MAX_POSITION_ADDS
# below) spends this SAME per-slot amount again, so a stock that adds once
# can end up using up to 2x its slot's share of TRADING_CAPITAL - this is a
# deliberate choice (an add-on is a fresh, separately-justified signal, not
# a top-up of the original size) but worth knowing before relying on it.
# Change the allocation rule itself (equal split vs risk-weighted vs
# volatility-adjusted, etc.) in position_sizing.py if equal-split isn't
# what you want.
CAPITAL_PER_TRADE = TRADING_CAPITAL / TOP_N_STOCKS if TOP_N_STOCKS else TRADING_CAPITAL

# Last-resort fallback quantity, used by position_sizing.calculate_quantity()
# callers ONLY if a computed capital-based quantity comes back 0 (e.g. a
# single share/lot costs more than the allocated capital) - NOT the normal
# sizing path any more. Kept so a mis-sized slot logs loudly and falls back
# to a tiny, deliberately-visible order instead of silently placing nothing;
# see _maybe_place_live_order's qty-resolution comment.
DEFAULT_ORDER_QTY = 1

# ----------------------------------------------------------------------------
# NIFTY INDEX options trading (see options_engine.py) - OFF by default
# ----------------------------------------------------------------------------
# SINGLE-SOURCE signal (NIFTY_FUTURES_SYMBOL), single-leg execution (see
# options_engine.py's module docstring for the full picture):
#   - BOTH the PRICE condition (opening-range high/low from its own 09:15
#     candle, running VWAP) AND the VOLUME condition (> 20-period volume MA)
#     are read from NIFTY_FUTURES_SYMBOL's own 5-min candles - the index
#     itself typically reports ZERO volume in Fyers' feed (it isn't a
#     tradable instrument), so NEITHER a volume-MA check NOR a VWAP
#     (cumulative price*volume / cumulative volume) computed against the
#     index's own candles would be meaningful - VWAP would silently collapse
#     to 0 if the index's reported volume is genuinely always zero. The
#     future is a real, continuously-traded proxy that tracks the index
#     almost 1:1, so it supplies the whole signal by itself. VERIFY the
#     zero-volume assumption against a raw quote/candle for your own
#     NIFTY_INDEX_SYMBOL before relying on this - if your feed DOES report
#     real index volume, you could point the engine back at the index
#     instead, but this repo defaults to the safer/proven futures-only read.
#   - NIFTY_INDEX_SYMBOL is used for exactly ONE thing: fetching the spot LTP
#     to resolve WHICH strike to buy (ITM/ATM/OTM - see OPTION_STRIKE_MODE
#     below, and resolve_option_symbol() in options_engine.py) - it plays no
#     part in the entry/exit signal itself. That spot is read FRESH at the
#     moment an entry signal actually fires, NOT back at today's ~09:20
#     engine-setup time - spot can move meaningfully in between, and the
#     strike should reflect where the market actually is when the trade is
#     taken, not where it was when this engine was first built.
#   - The instrument actually BOUGHT is a single NIFTY option, CE on an
#     upside futures breakout or PE on a downside one - decided by the
#     STRATEGY'S OWN entry criteria at the moment it fires, NOT by today's
#     sentiment-derived bias direction any more (bias direction still gates
#     the morning opening-range quality filter below - see
#     run_index_options_engine() - but no longer pre-selects CE vs PE or a
#     strike). BUY-ONLY, same reasoning as before (capped risk, no naked
#     writing). Off by default because it depends on a locally maintained F&O
#     symbol master file (below) that THIS repo cannot keep current for you -
#     read options_engine.py fully, and verify your own symbol master's
#     column names/values, before setting this True.
ENABLE_NIFTY_OPTIONS_TRADING = False

NIFTY_INDEX_SYMBOL = "NSE:NIFTY50-INDEX"   # Strike-resolution spot source ONLY - not part of the
                                            # entry/exit signal (see block comment above)

# PRICE + VWAP + VOLUME source - the current front-month NIFTY index future, the
# ONLY instrument the entry/exit signal is read from. Unlike the option symbol
# (resolved fresh each day from the symbol master below), this is a plain
# string you must update yourself each time the contract rolls (monthly) -
# e.g. "NSE:NIFTY25DECFUT" is only valid for that one expiry cycle. If you'd
# rather this stay current automatically, extend options_engine.py to resolve
# it from the same symbol master file instead (the FUTURE rows are typically
# alongside the OPTION rows in Fyers' combined F&O master) - not done here
# since you only asked for the option leg to be master-resolved.
NIFTY_FUTURES_SYMBOL = "NSE:NIFTY25DECFUT"

# Fyers publishes a downloadable NSE F&O instrument/symbol master file
# (check their current API docs for the exact URL - it has moved before)
# listing every live contract's trading symbol, strike, expiry, option type
# and lot size. Download/refresh it periodically and point this at your
# local copy; options_engine.load_fo_symbol_master() reads strike/expiry/lot
# size/trading-symbol straight from THIS file rather than from any
# hard-coded format string here, specifically because NIFTY's expiry day,
# lot size, and strike gap have all changed more than once under SEBI/
# exchange rule changes - hard-coding any of them would silently go stale.
NIFTY_FO_SYMBOL_MASTER_CSV = os.path.join(DATA_DIR, "nse_fo_symbol_master.csv")

# Best-effort {logical name: actual column name} mapping for the file above -
# VERIFY against your own downloaded file's real header (print df.columns
# once, exactly like opening_range_scanner.py's quotes-field caveat) and
# correct these before enabling ENABLE_NIFTY_OPTIONS_TRADING. Wrong column
# names here fail loudly (see load_fo_symbol_master) rather than silently
# resolving the wrong contract.
NIFTY_FO_MASTER_COLUMNS = {
    "underlying": "underlying_symbol",
    "option_type": "option_type",      # expected values "CE" / "PE"
    "expiry": "expiry_date",
    "strike": "strike_price",
    "trading_symbol": "trading_symbol",  # the exact string Fyers' history/order APIs expect
    "lot_size": "lot_size",
}

# Which strike to BUY for NIFTY/BANKNIFTY options entries: "ITM", "ATM", or "OTM" - ONE
# shared knob for BOTH options engines (same pattern as MAX_OR_CANDLE_WICK_PCT's merge
# below), read by resolve_option_symbol() in options_engine.py:
#   "OTM" (this repo's ORIGINAL/default behaviour): out-of-the-money, NIFTY_OTM_STRIKES_AWAY/
#         BANKNIFTY_OTM_STRIKES_AWAY strikes away from spot (away = above spot for a CALL,
#         below spot for a PUT).
#   "ITM": the mirror image - that many strikes IN the money instead (below spot for a CALL,
#         above spot for a PUT). Costs meaningfully more premium (more intrinsic value and
#         delta) than OTM at the same strikes-away count - re-check capital_allocated/lot
#         sizing before switching to this.
#   "ATM": always the SINGLE strike nearest to spot, whichever side it falls on -
#         NIFTY_OTM_STRIKES_AWAY/BANKNIFTY_OTM_STRIKES_AWAY is ignored in this mode.
# Resolved FRESH every time an entry signal actually fires, from spot AT THAT MOMENT - not
# once at today's ~09:20 engine setup (see the NIFTY INDEX options block comment above).
OPTION_STRIKE_MODE = "OTM"

# How many strikes away from the at-the-money strike to go, in whichever direction
# OPTION_STRIKE_MODE above implies (OTM: away from spot; ITM: toward/through spot). 1 = the
# nearest listed strike in that direction, 2 = the second, etc. Ignored entirely when
# OPTION_STRIKE_MODE="ATM" (which always takes the single nearest strike, full stop). Not
# specified precisely in the original request ("OTM CE"/"OTM PE") - 1 (the nearest strike) is
# the most common reading and this repo's default; change it if you mean further away.
NIFTY_OTM_STRIKES_AWAY = 1

# Independent capital bucket for the options strategy - deliberately
# separate from TRADING_CAPITAL (equities) and MCX_CAPITAL (below) rather
# than shared, so running more than one of these strategies at once doesn't
# silently double-spend one real capital pool. If you only have ONE real
# pool of money, divide it across these yourself and don't run the
# strategies you're not funding.
NIFTY_OPTIONS_CAPITAL = 200_000.0

# ----------------------------------------------------------------------------
# BANK NIFTY (NIFTYBANK) INDEX options trading - OFF by default
# ----------------------------------------------------------------------------
# The EXACT SAME single-source engine as the NIFTY options block above
# (options_engine.py's NiftyOptionsEngine/run_index_options_engine - see its
# module docstring for the full rule set), just pointed at NIFTYBANK's own
# index/futures/strike universe instead of NIFTY's:
#   - PRICE + VWAP + VOLUME condition all from BANKNIFTY_FUTURES_SYMBOL's own
#     5-min candles, for the same reason as NIFTY's - the index itself
#     typically reports ZERO volume, which would also break a VWAP computed
#     from it. VERIFY against your own raw feed before enabling, same as the
#     NIFTY caveat above.
#   - BANKNIFTY_INDEX_SYMBOL is used for exactly ONE thing: the spot LTP,
#     read FRESH at signal time, to resolve which strike to buy (per the
#     SHARED OPTION_STRIKE_MODE above) - no part of the entry/exit signal.
#   - BUYS a single BANKNIFTY option - CE on an upside futures breakout, PE
#     on a downside one, decided by the strategy's own entry criteria at
#     signal time (today's sentiment-derived direction still gates the
#     opening-range quality filter, same as NIFTY, but no longer picks CE
#     vs PE - see options_engine.py's module docstring).
# Shares NIFTY_FO_SYMBOL_MASTER_CSV/NIFTY_FO_MASTER_COLUMNS above - Fyers'
# F&O symbol master is one combined file covering every NSE underlying
# (NIFTY, BANKNIFTY, FINNIFTY, single stocks, ...), so there's no separate
# "BANKNIFTY" master file to point at; options_engine.py just filters that
# same file's underlying_symbol column for "BANKNIFTY" rows instead of
# "NIFTY" ones. Off by default for the same reasons as NIFTY options - read
# options_engine.py fully, and verify your own symbol master's BANKNIFTY
# rows, before setting this True.
ENABLE_BANKNIFTY_OPTIONS_TRADING = False

BANKNIFTY_INDEX_SYMBOL = "NSE:NIFTYBANK-INDEX"  # OTM-strike spot source ONLY - VERIFY this
                                                  # exact string against your own Fyers symbol
                                                  # master/quote before enabling; exchange symbol
                                                  # strings do change.

# PRICE + VWAP + VOLUME source - the current front-month BANKNIFTY future, the
# ONLY instrument the entry/exit signal is read from. Same manual-roll
# caveat as NIFTY_FUTURES_SYMBOL above: this is a plain string you must
# update yourself each time the contract rolls (monthly).
BANKNIFTY_FUTURES_SYMBOL = "NSE:BANKNIFTY25DECFUT"

# Same meaning as NIFTY_OTM_STRIKES_AWAY above (direction governed by the SHARED
# OPTION_STRIKE_MODE), independently configurable since BANKNIFTY's strike gap (100) is wider
# than NIFTY's (50) - 1 = the nearest listed strike in that direction.
BANKNIFTY_OTM_STRIKES_AWAY = 1

# Independent capital bucket, same reasoning as NIFTY_OPTIONS_CAPITAL above -
# deliberately separate so running NIFTY options and BANKNIFTY options at the
# same time doesn't silently double-spend one real capital pool.
BANKNIFTY_OPTIONS_CAPITAL = 200_000.0

# ----------------------------------------------------------------------------
# MCX commodities trading (see commodity_engine.py) - OFF by default
# ----------------------------------------------------------------------------
# Applies the SAME ORB + VWAP + volume-MA strategy directly to MCX commodity
# FUTURES contracts (not commodity options - MCX options liquidity is thin
# for most contracts, and this keeps the "same logic as equities" mapping
# exact: a futures contract has its own continuously-traded OHLCV series,
# just like a stock). Off by default - see commodity_engine.py and verify
# every value below against Fyers' current MCX contract specs before
# enabling.
ENABLE_MCX_COMMODITY_TRADING = False

# MCX's session runs well past NSE's 09:15-15:30 (into the evening,
# currently commonly 09:00-23:30 IST with a seasonal 23:00 close during
# daylight-saving months abroad that affect international commodity
# benchmarks) - VERIFY current timings on MCX's own site/circulars, they
# are NOT fixed and this scaffold cannot track that for you.
MCX_MARKET_OPEN_TIME = "09:00:00"
MCX_MARKET_CLOSE_TIME = "23:30:00"
MCX_OPENING_RANGE_SCAN_TIME = "09:05:02"   # mirrors OPENING_RANGE_SCAN_TIME's "2s after the first candle" buffer
MCX_SIGNAL_ENGINE_LAST_RUN = "23:14:30"    # mirrors SIGNAL_ENGINE_LAST_RUN - stop looking for NEW signals
MCX_SQUARE_OFF_TIME = "23:15:00"           # mirrors SQUARE_OFF_TIME - force-close before session close

# MCX's own candle resolution, deliberately INDEPENDENT of equities'/options'
# CANDLE_INTERVAL_MINUTES above - set to "5", "15", "30", etc. to change how wide each MCX
# opening-range/signal-check candle is, without touching the NSE equity session's own
# timeframe at all. Must be one of Fyers' documented intraday resolution strings when
# stringified ("1", "2", "3", "5", "10", "15", "20", "30", "45", "60", "120", "180", "240").
# Used by commodity_engine.py (opening-range window + per-tracker candle resolution) and by
# main.py's separate MCX candle-check cron job (job_check_commodity_signals) - see that
# job's docstring for why MCX needs its own cron trigger rather than sharing equities'.
MCX_CANDLE_INTERVAL_MINUTES = 5

# Fixed commodity universe - MCX has nothing equivalent to the NSE-500 +
# sector-filter screen equities use, and doesn't need one: EVERY symbol
# listed here is traded directly, every day, with NO sector scan and NO
# gain/loss-band shortlisting step (unlike equities - see
# commodity_engine.py's module docstring). This list IS the universe.
# `symbol` MUST be the exact current front-month Fyers trading symbol
# (these roll monthly/bi-monthly - e.g. "MCX:GOLDM25DECFUT" is only valid
# for one expiry cycle) - update this list yourself before each contract
# rolls. `lot_size` values below are illustrative placeholders
# (approximate, commonly-cited MCX contract sizes as of this scaffold's own
# knowledge) - VERIFY every one against MCX's current contract
# specifications before trusting them for real position sizing; MCX
# revises lot sizes too.
#
# `option_underlying` is used ONLY by MCX OPTIONS trading (ENABLE_MCX_OPTIONS_TRADING,
# mcx_options_engine.py, below) - it's the underlying_symbol code MCX_FO_MASTER_COLUMNS'
# "underlying" column uses for THIS commodity's own option contracts in MCX_FO_SYMBOL_MASTER_CSV
# (commonly, but not always, the same root as the futures trading symbol minus the expiry/"FUT"
# suffix - e.g. "GOLDM" for "MCX:GOLDM25DECFUT") - VERIFY each one against your own downloaded
# MCX options master file's real underlying_symbol values before enabling
# ENABLE_MCX_OPTIONS_TRADING; a wrong value here just means that ONE commodity resolves no
# option rows (logged loudly) and is skipped, not a crash. Ignored entirely by the FUTURES
# engine (commodity_engine.py) above - MCX_COMMODITY_UNIVERSE's `symbol` is all that needs.
MCX_COMMODITY_UNIVERSE = [
    {"symbol": "MCX:GOLDM25DECFUT", "lot_size": 10, "option_underlying": "GOLDM"},        # Gold Mini - ~10 grams/lot (verify)
    {"symbol": "MCX:SILVERM25DECFUT", "lot_size": 5, "option_underlying": "SILVERM"},     # Silver Mini - ~5 kg/lot (verify)
    {"symbol": "MCX:CRUDEOIL25DECFUT", "lot_size": 100, "option_underlying": "CRUDEOIL"},  # Crude Oil - ~100 barrels/lot (verify)
    {"symbol": "MCX:NATURALGAS25DECFUT", "lot_size": 1250, "option_underlying": "NATURALGAS"},  # Natural Gas - ~1250 mmBtu/lot (verify)
    {"symbol": "MCX:COPPER25DECFUT", "lot_size": 2500, "option_underlying": "COPPER"},    # Copper - ~2500 kg/lot (verify)
]

MCX_CAPITAL = 200_000.0  # see NIFTY_OPTIONS_CAPITAL's comment on why this is its own bucket

# ----------------------------------------------------------------------------
# MCX commodity OPTIONS trading (see mcx_options_engine.py) - OFF by default
# ----------------------------------------------------------------------------
# Unlike the FUTURES engine above (commodity_engine.py - trades config.MCX_COMMODITY_UNIVERSE
# directly, long or short, on ONE shared daily direction), this applies the exact same
# single-source, BOTH-SIDES, BUY-ONLY signal/resolution rules options_engine.py already uses
# for NIFTY/BANKNIFTY options - see that module's docstring - to EVERY commodity in
# MCX_COMMODITY_UNIVERSE INDEPENDENTLY: each commodity's OWN futures contract supplies its OWN
# PRICE/VWAP/VOLUME breakout signal, in EITHER direction, and whichever side confirms first
# BUYS that SAME commodity's own CE (upside) or PE (downside) - never a naked short, never
# gated by MCX_DIRECTION_REFERENCE_SYMBOL/a shared daily bias (see that value's own note above
# for the full contrast). Today's commodity-sentiment bias (decide_todays_commodity_direction()
# - the SAME read the futures engine uses) still gates each commodity's own opening-range
# "closing strength" quality filter (config.MAX_OR_CANDLE_WICK_PCT, shared across every market),
# exactly as NIFTY/BANKNIFTY options' own bias_direction does - it just doesn't pick CE/PE.
#
# Can run at the SAME time as the futures engine (ENABLE_MCX_COMMODITY_TRADING) - two
# completely independent engines/trade books/capital buckets, same as every other pair of
# strategies in this agent - or on its own. Off by default - read mcx_options_engine.py and
# this whole block (especially MCX_FO_SYMBOL_MASTER_CSV/MCX_FO_MASTER_COLUMNS and each
# MCX_COMMODITY_UNIVERSE entry's option_underlying above) before turning it on.
ENABLE_MCX_OPTIONS_TRADING = False

# MCX publishes its OWN F&O instrument master file - a SEPARATE download from NSE's
# (NIFTY_FO_SYMBOL_MASTER_CSV above covers NSE equity/index derivatives only; MCX is a
# different exchange segment entirely) - download/refresh it periodically and point this at
# your local copy. mcx_options_engine.load_mcx_fo_symbol_master() reads strike/expiry/lot
# size/trading-symbol straight from THIS file, exactly like options_engine.load_fo_symbol_master()
# does for NSE (same generic loader, see that function's docstring - it's parameterised over
# both the CSV path and the column mapping specifically so this module could reuse it).
MCX_FO_SYMBOL_MASTER_CSV = os.path.join(DATA_DIR, "mcx_fo_symbol_master.csv")

# Best-effort {logical name: actual column name} mapping for the file above - VERIFY against
# your own downloaded file's real header (same caveat as NIFTY_FO_MASTER_COLUMNS) and correct
# these before enabling ENABLE_MCX_OPTIONS_TRADING. "underlying" is matched against each
# MCX_COMMODITY_UNIVERSE entry's own option_underlying above, not the futures trading symbol.
MCX_FO_MASTER_COLUMNS = {
    "underlying": "underlying_symbol",
    "option_type": "option_type",      # expected values "CE" / "PE"
    "expiry": "expiry_date",
    "strike": "strike_price",
    "trading_symbol": "trading_symbol",  # the exact string Fyers' history/order APIs expect
    "lot_size": "lot_size",
}

# Same meaning as NIFTY_OTM_STRIKES_AWAY/BANKNIFTY_OTM_STRIKES_AWAY (direction governed by the
# SHARED OPTION_STRIKE_MODE above - ONE knob for every options market in this agent, NIFTY/
# BANKNIFTY/MCX alike) - ONE shared strikes-away count across the WHOLE MCX_COMMODITY_UNIVERSE
# (gold, crude, natural gas, ... don't each get their own independent count - their strike
# gaps already differ enough that "N strikes away" scales naturally per contract without a
# separate knob per commodity). 1 = the nearest listed strike in that direction.
MCX_OTM_STRIKES_AWAY = 1

# Independent capital bucket, same reasoning as NIFTY_OPTIONS_CAPITAL/MCX_CAPITAL above -
# deliberately separate so running MCX options alongside MCX futures (or any other strategy)
# never silently double-spends one real capital pool. Split EVENLY across however many
# commodities actually had an opening-range candle available today (position_sizing.
# capital_per_slot - mirrors MCX_CAPITAL's own per-contract split for futures), not per
# commodity ahead of time, so a data gap on one contract doesn't leave money idle.
MCX_OPTIONS_CAPITAL = 200_000.0

# Tick-level stop-loss/target/trailing-stop (STOP_LOSS_PCT/TARGET_PCT/TRAILING_SL_PCT)
# has been removed. Stop-loss is now purely candle/VWAP-driven: see
# CONSECUTIVE_CANDLES_FOR_STOPLOSS above and signal_engine.py's module docstring - the
# ONLY way a position closes before EOD is 2 consecutive 5-min candles confirming the
# move against it, the same conviction bar as entry, in both directions.
