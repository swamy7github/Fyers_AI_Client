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
#STOCK_UNIVERSE_CSV = os.path.join(DATA_DIR, "ind_nifty500list.csv")     # see README for download link
STOCK_UNIVERSE_CSV = Path(__file__).parent / 'data' / 'ind_nifty500list.csv'

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

# ----------------------------------------------------------------------------
# Strategy parameters
# ----------------------------------------------------------------------------
MIN_GAIN_PCT = 0.5       # lower bound (exclusive) for the opening-range gainer filter
MAX_GAIN_PCT = 2.5       # upper bound (inclusive) for the opening-range gainer filter
TOP_N_STOCKS = 5          # how many stocks to shortlist and track intraday
VOLUME_MA_PERIOD = 20     # number of 5-min candles for the ENTRY volume moving average
CONSECUTIVE_CANDLES_FOR_BUY = 2  # candles required to confirm a breakout entry

# ---- Stop-loss / square-off & position-sizing (add-on) parameters ----
# Weakness/breakdown is detected using a much faster, 2-period volume average
# than the entry's volMA(20) - the idea is entry needs sustained conviction,
# exit should react quickly.
VOLUME_SMA_STOPLOSS_PERIOD = 2
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

# Fyers caps API calls per second per account/plan (you mentioned 9/sec for
# yours). Every single call FyersDataService makes - quotes, history,
# retries included - goes through fyers_service.RateLimiter using this
# value. Set it a little under your actual documented limit, not exactly at
# it, to leave headroom for clock jitter and any other process sharing the
# same API key (e.g. a separate WebSocket connection).
FYERS_MAX_CALLS_PER_SECOND = 5

# How many symbols to pack into a single batched /quotes call. Fyers caps
# how many comma-separated symbols one quotes request accepts - verify the
# current limit for your plan in the API docs before changing this.
QUOTES_BATCH_SIZE = 50

# Dry-run: when True (default) the agent only *simulates* trades in memory and
# CSV, and NEVER calls fyers.place_order(). Only flip to False after you have
# paper-traded the logic for a good stretch and understand the risk involved
# (see README "Before you go live").
DRY_RUN = True
