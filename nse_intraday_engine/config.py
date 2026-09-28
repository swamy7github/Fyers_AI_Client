"""
Configuration module for the NSE Intraday Trading & Backtesting Engine.

Loads environment variables from `.env` and exposes all strategy,
execution and infrastructure parameters as a single `Config` object.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


@dataclass
class Config:
    # ---- Execution mode -----------------------------------------------------
    MODE: str = os.getenv("MODE", "").upper()  
    # "BACKTEST" | "PAPER_TRADING"

    # ---- Fyers API credentials -----------------------------------------------
    FYERS_APP_ID: str = os.getenv("FYERS_APP_ID", "")
    FYERS_APP_SECRET: str = os.getenv("FYERS_APP_SECRET", "")
    FYERS_REDIRECT_URI: str = os.getenv("FYERS_REDIRECT_URI", "https://127.0.0.1")
    FYERS_ACCESS_TOKEN_PATH: str = os.getenv(
        "FYERS_ACCESS_TOKEN_PATH", str(BASE_DIR / "./YFLRWVHU2G_fyers_token.txt")
    )
    FYERS_ACCESS_TOKEN: str = os.getenv("FYERS_ACCESS_TOKEN", "")
    # ---- Universe / screener --------------------------------------------------
    NIFTY500_URL: str = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
    SECTOR_INDEX_CSV_BASE: str = "https://archives.nseindia.com/content/indices/"
    UNIVERSE_TOP_N: int = int(os.getenv("UNIVERSE_TOP_N", 25))
    TURNOVER_LOOKBACK_DAYS: int = 10
    MAX_SPREAD_PCT: float = 0.15  # tight bid-ask spread filter, % of LTP

    # ---- 9:15 candle filter -----------------------------------------------------
    FIRST_CANDLE_START: str = "09:15"
    FIRST_CANDLE_END: str = "09:20"
    CANDLE_FILTER_PCT: float = 3.0  # reject moves beyond +/-3%

    # ---- Signal generation --------------------------------------------------------
    ATR_PERIOD: int = 14
    VOLUME_SMA_PERIOD: int = 20
    VOLUME_SPIKE_MULTIPLE: float = 1.5
    OPEN_EXTREME_TOLERANCE_PCT: float = 0.1  # open ~ low/high tolerance
    VWAP_SUSTAIN_BARS: int = 2               # bars required to confirm "sustained" VWAP side

    # ---- Position sizing & lifecycle -------------------------------------------------
    RISK_PER_TRADE_PCT: float = 1.0  # % of equity risked per trade
    PORTFOLIO_EQUITY: float = float(os.getenv("PORTFOLIO_EQUITY", 1_000_000))
    STOP_LOSS_ATR_MULT: float = 1.0
    PYRAMID_TRIGGER_ATR_MULT: float = 1.0
    PYRAMID_QTY_PCT: float = 0.5
    TARGET1_ATR_MULT: float = 1.5
    TARGET1_BOOK_PCT: float = 0.5
    TRAIL_ATR_MULT: float = 1.5
    SQUARE_OFF_TIME: str = "15:15"
    MARKET_CLOSE: str = "15:30"

    # ---- Costs & slippage ---------------------------------------------------------
    SLIPPAGE_PCT: float = 0.05         # % applied to every simulated fill
    BROKERAGE_PER_ORDER: float = 20.0  # flat Rs. cap per executed order (discount-broker style)
    STT_SELL_PCT: float = 0.025        # STT on sell side, intraday equity, % of turnover
    EXCHANGE_TXN_PCT: float = 0.00297  # NSE transaction charges, % of turnover
    SEBI_CHARGES_PCT: float = 0.0001
    STAMP_DUTY_BUY_PCT: float = 0.003  # stamp duty on buy side, % of turnover
    GST_PCT: float = 18.0              # GST on (brokerage + exchange txn charges)

    # ---- Infra -----------------------------------------------------------------------
    DB_PATH: str = os.getenv("DB_PATH", str(BASE_DIR / "trading_engine.db"))
    LOG_DIR: str = os.getenv("LOG_DIR", str(BASE_DIR / "logs"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    API_MIN_INTERVAL_SEC: float = float(os.getenv("API_MIN_INTERVAL_SEC", 0.25))
    MAX_RETRIES: int = 5
    BACKOFF_BASE_SEC: float = 1.0
    BACKOFF_MAX_SEC: float = 30.0

    # ---- Backtest window (only used when MODE == BACKTEST) ----------------------------
    BACKTEST_FROM: str = os.getenv("BACKTEST_FROM", "2024-01-01")
    BACKTEST_TO: str = os.getenv("BACKTEST_TO", "2024-12-31")


config = Config()
