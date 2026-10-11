"""
config.py
----------
Centralised configuration for the Opening Range Breakout (ORB) scanning agent.

All secrets are read from environment variables (via a local `.env` file) so
credentials never get hard-coded or committed to source control. Copy
`.env.example` to `.env` and fill in your own values before running anything.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from dotenv import load_dotenv


# ----------------------------------------------------------------------------
# File paths
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

current_path = Path(BASE_DIR)
# Add the directory containing your module to the search path

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
STOCK_UNIVERSE_CSV = os.path.join(DATA_DIR, "ind_nifty500list.csv")     # see README for download link
#STOCK_UNIVERSE_CSV = Path(__file__).parent / 'data' / 'ind_niftytotalmarket_list.csv'

load_dotenv(".env.example")  # loads variables from a local .env file, if present

"""
APP_ID = "YFLRWVHU2G"       # Example: 'EGNI8CE27Q' YFLRWVHU2G-100
APP_SECRET = "HJ5RUXDPW2"   #HJ5RUXDPW2

#===================================================
APP_TYPE = "100"             # Usually '100'
FY_ID = "YY01479"      # Example: 'XY12345'
TOTP_KEY = "U6DLWH4CW64MFZJV4KOOI6F47WG55U4W" # 32-character base32 secret from Fyers account settings
PIN = "1436"      # e.g., '1234'
REDIRECT_URI = "http://127.0.0.1:5000/fyers/ai_callback/" # Example: 'https://trade.fyers.in/api-login/redirect-uri/index.html'
TOKEN_FILE = "fyers_token.txt"
"""

# ----------------------------------------------------------------------------
# Fyers API credentials (create an app at https://myapi.fyers.in)
# ----------------------------------------------------------------------------
FYERS_APP_ID = os.getenv("FYERS_APP_ID", "")            # e.g. "ABCDE12345-100"
FYERS_SECRET_KEY = os.getenv("FYERS_SECRET_KEY", "")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "")
FYERS_FY_ID = os.getenv("FYERS_FY_ID", "")               # your Fyers login id (e.g. XY01234)
FYERS_PIN = os.getenv("FYERS_PIN", "")                   # 4-digit trading PIN
FYERS_TOTP_SECRET = os.getenv("FYERS_TOTP_SECRET", "")   # from Fyers > Profile > 2FA setup
TOKEN_FILE = os.getenv("TOKEN_FILE")

# Optional: only needed if you enable the LLM-based macro/geopolitical news
# sentiment helper in global_sentiment.get_llm_news_sentiment()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

SECTOR_INDEX_MAP_CSV = os.path.join(DATA_DIR, "sector_index_map.csv")  # symbol -> sector mapping
LOG_FILE = os.path.join(LOG_DIR, "orb_agent.log")
TRADE_LOG_CSV = os.path.join(LOG_DIR, "trade_book_{date}.csv")

# ----------------------------------------------------------------------------
# Timing (all times are IST / Asia/Kolkata, weekdays only)
# ----------------------------------------------------------------------------
TIMEZONE = "Asia/Kolkata"

SECTOR_SCAN_TIME = "09:30:00"           # pre-open sector snapshot (see caveat in sector_scanner.py)
SECTOR_SCAN_TIME_POSTOPEN = "09:45:30"  # re-run shortly after the normal session starts, for real numbers
OPENING_RANGE_SCAN_TIME = "09:46:10"    # 2s buffer after the 09:15-09:20 candle closes
SIGNAL_ENGINE_LAST_RUN = "15:11:00"     # stop looking for NEW signals after this time
SQUARE_OFF_TIME = "15:11:30"            # force-close any open paper positions
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
INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK = 90
DAILY_HISTORY_MAX_DAYS_PER_CHUNK = 270
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

# ---- Directional gating from global sentiment (see trade_direction.py) ----
# When global_sentiment.score_global_bias() (or get_stock_news_ema_bias's combined_bias)
# comes back NEUTRAL/MIXED/UNKNOWN rather than a clear BULLISH or BEARISH, these decide
# whether to still trade a direction anyway. Both False (the default) means: no clear
# sentiment -> open no new positions that day, in either direction - a deliberately
# conservative default. Set either to True to fall back to that direction specifically
# when sentiment is unclear, rather than sitting out entirely.
ALLOW_LONG_ON_NEUTRAL = False
ALLOW_SHORT_ON_NEUTRAL = False

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
TRADING_CAPITAL = 200000.0

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
# DUAL-SOURCE signal, single-leg execution (see options_engine.py's module
# docstring for the full picture):
#   - PRICE/VWAP condition is read from NIFTY_INDEX_SYMBOL's own 5-min candles
#     (opening-range high/low from its 09:15 candle, running VWAP).
#   - VOLUME condition (> 20-period volume MA) is read from NIFTY_FUTURES_SYMBOL's
#     own 5-min candles instead - the index itself typically reports ZERO
#     volume in Fyers' feed (it isn't a tradable instrument), so a volume-MA
#     check against the index's own candles would be meaningless. VERIFY this
#     against a raw quote/candle for your NIFTY_INDEX_SYMBOL before enabling -
#     if your feed DOES report real index volume, the VWAP computed from it
#     is still literally what's used here either way.
#   - The instrument actually BOUGHT is a single OTM (out-of-the-money) NIFTY
#     option - CE when today's sentiment-derived bias is LONG, PE when it's
#     SHORT - BUY-ONLY, same reasoning as before (capped risk, no naked
#     writing). Off by default because it depends on a locally maintained F&O
#     symbol master file (below) that THIS repo cannot keep current for you -
#     read options_engine.py fully, and verify your own symbol master's
#     column names/values, before setting this True.
ENABLE_NIFTY_OPTIONS_TRADING = False

NIFTY_INDEX_SYMBOL = "NSE:NIFTY50-INDEX"   # PRICE/VWAP source - spot index, also used for OTM strike selection

# VOLUME source - the current front-month NIFTY index future. Unlike the option
# symbol (resolved fresh each day from the symbol master below), this is a
# plain string you must update yourself each time the contract rolls
# (monthly) - e.g. "NSE:NIFTY25DECFUT" is only valid for that one expiry
# cycle. If you'd rather this stay current automatically, extend
# options_engine.py to resolve it from the same symbol master file instead
# (the FUTURE rows are typically alongside the OPTION rows in Fyers' combined
# F&O master) - not done here since you only asked for the option leg to be
# master-resolved.
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

# How many strikes OUT of the money to buy: 1 = the first OTM strike (the
# listed strike immediately above spot for a CE, immediately below spot for
# a PE), 2 = the second, etc. Not specified precisely in the original
# request ("OTM CE"/"OTM PE") - 1 (the nearest OTM strike) is the most
# common reading and this repo's default; change it if you mean further OTM.
NIFTY_OTM_STRIKES_AWAY = 1

# Independent capital bucket for the options strategy - deliberately
# separate from TRADING_CAPITAL (equities) and MCX_CAPITAL (below) rather
# than shared, so running more than one of these strategies at once doesn't
# silently double-spend one real capital pool. If you only have ONE real
# pool of money, divide it across these yourself and don't run the
# strategies you're not funding.
NIFTY_OPTIONS_CAPITAL = 200000.0

# ----------------------------------------------------------------------------
# BANK NIFTY (NIFTYBANK) INDEX options trading - OFF by default
# ----------------------------------------------------------------------------
# The EXACT SAME dual-source engine as the NIFTY options block above
# (options_engine.py's NiftyOptionsEngine/run_index_options_engine - see its
# module docstring for the full rule set), just pointed at NIFTYBANK's own
# index/futures/strike universe instead of NIFTY's:
#   - PRICE/VWAP condition from BANKNIFTY_INDEX_SYMBOL's own 5-min candles.
#   - VOLUME condition (> 20-period volume MA) from BANKNIFTY_FUTURES_SYMBOL's
#     own 5-min candles, for the same reason as NIFTY's - the index itself
#     typically reports ZERO volume. VERIFY against your own raw feed before
#     enabling, same as the NIFTY caveat above.
#   - BUYS a single OTM (out-of-the-money) BANKNIFTY option - CE on a LONG
#     bias, PE on a SHORT bias - using the SAME today's sentiment-derived
#     direction equities/NIFTY-options use (never recomputed per market).
# Shares NIFTY_FO_SYMBOL_MASTER_CSV/NIFTY_FO_MASTER_COLUMNS above - Fyers'
# F&O symbol master is one combined file covering every NSE underlying
# (NIFTY, BANKNIFTY, FINNIFTY, single stocks, ...), so there's no separate
# "BANKNIFTY" master file to point at; options_engine.py just filters that
# same file's underlying_symbol column for "BANKNIFTY" rows instead of
# "NIFTY" ones. Off by default for the same reasons as NIFTY options - read
# options_engine.py fully, and verify your own symbol master's BANKNIFTY
# rows, before setting this True.
ENABLE_BANKNIFTY_OPTIONS_TRADING = False

BANKNIFTY_INDEX_SYMBOL = "NSE:NIFTYBANK-INDEX"  # PRICE/VWAP source - VERIFY this exact string
                                                  # against your own Fyers symbol master/quote
                                                  # before enabling; exchange symbol strings do
                                                  # change.

# VOLUME source - the current front-month BANKNIFTY future. Same manual-roll
# caveat as NIFTY_FUTURES_SYMBOL above: this is a plain string you must
# update yourself each time the contract rolls (monthly).
BANKNIFTY_FUTURES_SYMBOL = "NSE:BANKNIFTY25DECFUT"

# Same meaning as NIFTY_OTM_STRIKES_AWAY above, independently configurable
# since BANKNIFTY's strike gap (100) is wider than NIFTY's (50) - 1 = the
# nearest listed OTM strike.
BANKNIFTY_OTM_STRIKES_AWAY = 1

# Independent capital bucket, same reasoning as NIFTY_OPTIONS_CAPITAL above -
# deliberately separate so running NIFTY options and BANKNIFTY options at the
# same time doesn't silently double-spend one real capital pool.
BANKNIFTY_OPTIONS_CAPITAL = 200000.0

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
ENABLE_MCX_COMMODITY_TRADING = True

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
MCX_CANDLE_INTERVAL_MINUTES = 15

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
#    {"symbol": "MCX:GOLDTEN26OCTFUT", "lot_size": 10},     # Gold Mini - ~10g/lot (verify)
#    {"symbol": "MCX:COPPER26OCTFUT", "lot_size": 2500},   # Copper - ~2500 kg/lot (verify)
MCX_COMMODITY_UNIVERSE = [
    {"symbol": "MCX:CRUDEOILM26OCTFUT", "lot_size": 10},  # Crude Oil - ~10 barrels/lot (verify) Silver Mini 
    {"symbol": "MCX:GOLDPETAL26OCTFUT", "lot_size": 1},  # Gold Petals - ~1g/lot (verify)
    {"symbol": "MCX:NATGASMINI26OCTFUT", "lot_size": 250},  # Natural Gas - ~250 mmBtu/lot (verify)
    {"symbol": "MCX:SILVERMIC26NOVFUT", "lot_size": 1},    # Silver micro - ~1 kg/lot (verify)
    {"symbol": "MCX:ZINCMINI26OCTFUT", "lot_size": 100}   # Zinc Mini - ~1MT/lot (verify)
]

MCX_CAPITAL = 1000000.0  # see NIFTY_OPTIONS_CAPITAL's comment on why this is its own bucket

# Tick-level stop-loss/target/trailing-stop (STOP_LOSS_PCT/TARGET_PCT/TRAILING_SL_PCT)
# has been removed. Stop-loss is now purely candle/VWAP-driven: see
# CONSECUTIVE_CANDLES_FOR_STOPLOSS above and signal_engine.py's module docstring - the
# ONLY way a position closes before EOD is 2 consecutive 5-min candles confirming the
# move against it, the same conviction bar as entry, in both directions.
