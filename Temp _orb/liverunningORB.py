import sys
import logging
import datetime
import pytz
import pandas as pd
import numpy as np
from apscheduler.schedulers.blocking import BlockingScheduler
from fyers_apiv3 import fyersModel

from pathlib import Path

# 1. Dynamically parse the parent path ('C:\NS\Fyers_AI_Client')
# This gets the folder containing the current script and looks one level up
project_root = Path(__file__).resolve().parent.parent

# 2. Add 'C:\NS\Fyers_AI_Client' to Python's search path
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))
    
# =====================================================================
# 1. CONFIGURATION & LOGGING SETUP
# =====================================================================
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler("fyers_scanner.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("QuantAgent")

IST = pytz.timezone("Asia/Kolkata")

# Replace with your production credentials
FYERS_CLIENT_ID = "YOUR_APP_ID-100"       # e.g., "XCXXXXX-100"
FYERS_ACCESS_TOKEN = "YOUR_ACCESS_TOKEN"  # Generated daily access token

# Sector Indices to monitor at 9:10 AM
SECTOR_SYMBOLS = [
    "NSE:NIFTYBANK-INDEX",
    "NSE:NIFTYIT-INDEX",
    "NSE:NIFTYAUTO-INDEX",
    "NSE:NIFTYFMCG-INDEX",
    "NSE:NIFTYMETAL-INDEX",
    "NSE:NIFTYPHARMA-INDEX",
    "NSE:NIFTYREALTY-INDEX",
    "NSE:NIFTYENERGY-INDEX",
    "NSE:NIFTYINDDEFENCE-INDEX",
    "NSE:NIFTYINFRA-INDEX",
    "NSE:NIFTYPSE-INDEX",
    "NSE:NIFTYCONSUMPTION-INDEX",
    "NSE:FINNIFTY-INDEX",
    "NSE:NIFTYMEDIA-INDEX",
]

# Predefined Stock Universe (Add your full Nifty 500 / Liquid basket here)
STOCK_UNIVERSE = [
    "AEGISLOG", "TATACHEM", "GRAPHITE", "ACMESOLAR", "CARBORUNIV", 
    "FINCABLES", "CHENNPETRO", "JYOTICNC", "NIACL", "APARINDS", 
    "ENGINERSIN", "GABRIEL", "JSWINFRA", "MEESHO", "PFOCUS", 
    "SYRMA", "TEGA", "USHAMART", "PVRINOX", "PINELABS", 
    "BRIGADE", "URBANCO", "WELCORP", "PATANJALI", "WELSPUNLIV", 
    "ACE", "ABDL", "BANDHANBNK", "HOMEFIRST", "MANKIND", 
    "CYIENT", "BEML", "PCBL", "TATAINVEST", "SPLPETRO", 
    "JUBLPHARMA", "AEGISVOPAK", "BELRISE", "SOLARINDS", "WOCKPHARMA", 
    "KIRLOSENG", "PAYTM", "DLF", "CEMPRO", "OBEROIRLTY", 
    "CAPLIPOINT", "SWANCORP", "IKS", "CPPLUS"
]
STOCK_UNIVERSE = [f"NSE:{s}-EQ" for s in  STOCK_UNIVERSE]
# =====================================================================
# 2. FYERS API CLIENT WRAPPER
# =====================================================================
class FyersClient:
    """Wrapper class handling API connections and queries."""
    def __init__(self, client_id: str, access_token: str):
        try:
            self.fyers = fyersModel.FyersModel(
                client_id=client_id,
                token=access_token,
                is_async=False,
                log_path=""
            )
            logger.info("Fyers client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Fyers client: {e}")
            raise

    def get_quotes(self, symbols: list) -> dict:
        """Fetch quotes for a list of symbols in batches (max 50 per batch)."""
        all_data = {}
        batch_size = 45
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            data = {"symbols": ",".join(batch)}
            try:
                response = self.fyers.quotes(data=data)
                if response.get("s") == "ok":
                    for item in response.get("d", []):
                        sym = item.get("n")
                        all_data[sym] = item.get("v", {})
                else:
                    logger.warning(f"Quotes API response not OK: {response.get('message')}")
            except Exception as e:
                logger.error(f"Error fetching quotes batch: {e}")
        return all_data

    def get_5m_candles(self, symbol: str) -> pd.DataFrame:
        """Fetch today's 5-minute OHLCV historical candles."""
        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        payload = {
            "symbol": symbol,
            "resolution": "5",
            "date_format": "1",
            "range_from": today_str,
            "range_to": today_str,
            "cont_flag": "1"
        }
        try:
            response = self.fyers.history(data=payload)
            if response.get("s") == "ok" and "candles" in response:
                candles = response["candles"]
                if not candles:
                    return pd.DataFrame()
                
                df = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
                df["datetime"] = pd.to_datetime(df["epoch"], unit="s").dt.tz_localize("UTC").dt.tz_convert(IST)
                return df
            else:
                logger.warning(f"Failed to fetch history for {symbol}: {response.get('message')}")
                return pd.DataFrame()
        except Exception as e:
            logger.error(f"History API error for {symbol}: {e}")
            return pd.DataFrame()

# =====================================================================
# 3. TRADING AGENT & STRATEGY ENGINE
# =====================================================================
class QuantTradingAgent:
    def __init__(self, client: FyersClient):
        self.client = client
        self.top_5_stocks = []
        self.benchmark_first_candle = {}  # {symbol: {"high": float, "low": float, ...}}
        
        # Position Tracker: {symbol: {"status": "OPEN"/"CLOSED", "entry_price": float, "exit_price": float, "pnl": float}}
        self.trade_book = {}

    @staticmethod
    def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """Calculates cumulative Intraday VWAP and Volume 20-period Moving Average."""
        if df.empty:
            return df

        # Typical Price = (High + Low + Close) / 3
        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        cum_volume = df["volume"].cumsum()
        cum_vol_price = (typical_price * df["volume"]).cumsum()
        
        df["vwap"] = np.where(cum_volume > 0, cum_vol_price / cum_volume, df["close"])
        df["vol_ma20"] = df["volume"].rolling(window=20, min_periods=1).mean()
        return df

    # -------------------------------------------------------------
    # 09:10 AM SCAN: SECTORS & UNIVERSE FILTER
    # -------------------------------------------------------------
    def run_910am_scan(self):
        logger.info("========== RUNNING 09:10 AM SECTOR & UNIVERSE SCAN ==========")
        
        # 1. Sector Scan
        sector_quotes = self.client.get_quotes(SECTOR_SYMBOLS)
        sector_perf = []
        for sym, data in sector_quotes.items():
            chp = data.get("chp", 0.0)  # Percentage change
            lp = data.get("lp", 0.0)
            sector_perf.append({"Sector": sym.replace("NSE:", "").replace("-INDEX", ""), "LTP": lp, "% Change": chp})
        
        sec_df = pd.DataFrame(sector_perf).sort_values(by="% Change", ascending=False)
        print("\n--- SECTOR PERFORMANCE AT 09:10 AM ---")
        print(sec_df.to_string(index=False))

        # 2. Universe Stock Scan: Filter up by > 0.5% and < 2.5%
        stock_quotes = self.client.get_quotes(STOCK_UNIVERSE)
        candidates = []
        for sym, data in stock_quotes.items():
            chp = data.get("chp", 0.0)
            lp = data.get("lp", 0.0)
            if 0.5 < chp < 2.5:
                candidates.append({"symbol": sym, "LTP": lp, "% Change": chp})

        cand_df = pd.DataFrame(candidates)
        if not cand_df.empty:
            # Rank descending and pick top 5
            cand_df = cand_df.sort_values(by="% Change", ascending=False).head(5)
            self.top_5_stocks = cand_df["symbol"].tolist()
            print("\n--- TOP 5 STOCKS SELECTED (+0.5% to +2.5%) ---")
            print(cand_df.to_string(index=False))
        else:
            logger.warning("No stocks found within the 0.5% - 2.5% window. Defaulting to top 5 liquid symbols.")
            self.top_5_stocks = STOCK_UNIVERSE[:5]

        logger.info(f"Final 5 Focus Stocks for the day: {self.top_5_stocks}")

    # -------------------------------------------------------------
    # 09:20:02 AM SCAN: FIRST 5-MIN CANDLE (9:15 - 9:20)
    # -------------------------------------------------------------
    def run_920am_first_candle_scan(self):
        logger.info("========== RUNNING 09:20:02 AM FIRST CANDLE SCAN ==========")
        first_candle_records = []
        if not self.top_5_stocks:
            self.run_910am_scan()
        for sym in self.top_5_stocks:
            df = self.client.get_5m_candles(sym)
            if df.empty or len(df) < 1:
                logger.warning(f"Could not retrieve 9:15 candle for {sym}")
                continue

            # First candle corresponds to index 0 (09:15 AM - 09:20 AM)
            c1 = df.iloc[0]
            self.benchmark_first_candle[sym] = {
                "open": c1["open"],
                "high": c1["high"],
                "low": c1["low"],
                "close": c1["close"],
                "volume": c1["volume"]
            }

            first_candle_records.append({
                "Symbol": sym,
                "Open": c1["open"],
                "High": c1["high"],
                "Low": c1["low"],
                "Close": c1["close"],
                "Volume": c1["volume"],
                "Setup": "Open==Low" if c1["open"] == c1["low"] else ("Open==High" if c1["open"] == c1["high"] else "Neutral")
            })

        summary_df = pd.DataFrame(first_candle_records)
        print("\n--- 9:15 - 9:20 AM FIRST CANDLE BENCHMARKS ---")
        print(summary_df.to_string(index=False))

    # -------------------------------------------------------------
    # CONTINUOUS 5-MIN SCAN (09:25 AM to 03:10 PM)
    # -------------------------------------------------------------
    def monitor_live_candles(self):
        now_time = datetime.datetime.now(IST).strftime("%H:%M:%S")
        logger.info(f"Checking 5m candle signals at {now_time} IST...")

        for sym in self.top_5_stocks:
            if sym not in self.benchmark_first_candle:
                continue

            df = self.client.get_5m_candles(sym)
            if df.empty or len(df) < 3:
                continue

            # Compute technical indicators
            df = self.calculate_indicators(df)

            ref_high = self.benchmark_first_candle[sym]["high"]
            
            # Identify the last two COMPLETED 5-minute candles
            # iloc[-1] is the just-closed candle; iloc[-2] is the candle before it
            last_c = df.iloc[-1]
            prev_c = df.iloc[-2]

            current_pos = self.trade_book.get(sym, {}).get("status", "NONE")

            # ---------------- BUY SIGNAL CRITERIA ----------------
            # 1. Any two consecutive candles closed above 9:15-9:20 high
            # 2. Current candle close > VWAP
            # 3. Current candle volume > Volume MA(20)
            is_two_consecutive_above_high = (last_c["close"] > ref_high) and (prev_c["close"] > ref_high)
            is_above_vwap = (last_c["close"] > last_c["vwap"]) and (prev_c["close"] > prev_c["vwap"])
            is_volume_breakout = (last_c["volume"] > last_c["vol_ma20"]) #and (prev_c["volume"] > prev_c["vol_ma20"])

            if current_pos != "OPEN":
                if is_two_consecutive_above_high and is_above_vwap and is_volume_breakout:
                    entry_price = last_c["close"]
                    self.trade_book[sym] = {
                        "status": "OPEN",
                        "entry_price": entry_price,
                        "exit_price": 0.0,
                        "pnl": 0.0,
                        "entry_time": last_c["datetime"].strftime("%H:%M:%S")
                    }
                    logger.info(f"🚨 [BUY SIGNAL] {sym} entered at {entry_price:.2f} | Time: {last_c['datetime']}")

            # ---------------- SELL / EXIT SIGNAL CRITERIA ----------------
            # 1. Position must be OPEN
            # 2. Two consecutive candle closes below VWAP: Close[t] < VWAP[t] AND Close[t-1] < VWAP[t-1]
            # 3. Confirmation candle volume > Volume MA(20)
            elif current_pos == "OPEN":
                is_two_consecutive_below_vwap = (
                    (last_c["close"] < last_c["vwap"]) and 
                    (prev_c["close"] < prev_c["vwap"])
                )
                is_exit_volume_spike = (last_c["volume"] > last_c["vol_ma20"]) #and (prev_c["volume"] > prev_c["vol_ma20"])

                if is_two_consecutive_below_vwap and is_exit_volume_spike:
                    exit_price = last_c["close"]
                    entry_price = self.trade_book[sym]["entry_price"]
                    pnl = exit_price - entry_price

                    self.trade_book[sym]["status"] = "CLOSED"
                    self.trade_book[sym]["exit_price"] = exit_price
                    self.trade_book[sym]["pnl"] = pnl
                    self.trade_book[sym]["exit_time"] = last_c["datetime"].strftime("%H:%M:%S")

                    logger.info(
                        f"🛑 [SELL/EXIT CONFIRMED] {sym} exited at {exit_price:.2f} "
                        f"(Two closes below VWAP + Vol Spike) | Points: {pnl:+.2f}"
                    )

        self.display_trade_book()

    # -------------------------------------------------------------
    # 03:10 PM: FORCE SQUARE-OFF & FINAL SUMMARY
    # -------------------------------------------------------------
    def close_all_positions_eod(self):
        logger.info("========== 03:10 PM REACHED: INTRADAY AUTO-SQUARE OFF ==========")
        for sym, trade in self.trade_book.items():
            if trade.get("status") == "OPEN":
                df = self.client.get_5m_candles(sym)
                exit_price = df.iloc[-1]["close"] if not df.empty else trade["entry_price"]
                pnl = exit_price - trade["entry_price"]

                trade["status"] = "CLOSED"
                trade["exit_price"] = exit_price
                trade["pnl"] = pnl
                trade["exit_time"] = "15:10:00 (EOD)"
                logger.info(f"[EOD SQUARED OFF] {sym} at {exit_price:.2f} | Points: {pnl:+.2f}")

        self.display_trade_book()

    def display_trade_book(self):
        """Displays current execution performance in a tabular format."""
        if not self.trade_book:
            return

        records = []
        total_pnl = 0.0
        for sym, trade in self.trade_book.items():
            records.append({
                "Symbol": sym,
                "Status": trade["status"],
                "Entry Time": trade.get("entry_time", "-"),
                "Entry Price": trade.get("entry_price", 0.0),
                "Exit Time": trade.get("exit_time", "-"),
                "Exit Price": trade.get("exit_price", 0.0),
                "Points Captured (P&L)": round(trade.get("pnl", 0.0), 2)
            })
            total_pnl += trade.get("pnl", 0.0)

        df_report = pd.DataFrame(records)
        print("\n================ LIVE TRADE BOOK ================")
        print(df_report.to_string(index=False))
        print(f"Total Cumulative Strategy Points: {total_pnl:+.2f}\n")

# =====================================================================
# 4. PRECISION SCHEDULER SETUP (APScheduler)
# =====================================================================
def main():
    from fyers_auto_login_v2 import APP_ID, get_fyers_session_handle
    
    fyres = get_fyers_session_handle()

    try:

        fyers_client = FyersClient(client_id=fyres.client_id, access_token=fyres.token)
    except Exception as e:
        logger.critical(f"Exiting due to authentication failure: {e}")
        return

    agent = QuantTradingAgent(client=fyers_client)
    scheduler = BlockingScheduler(timezone=IST)

    # 1. 09:10:00 AM IST: Pre-market sector & gap scan
    scheduler.add_job(
        agent.run_910am_scan,
        trigger="cron",
        hour=9,
        minute=10,
        second=0,
        id="job_910am_scan"
    )

    # 2. 09:20:02 AM IST: Exact completion of 9:15 - 9:20 AM candle
    scheduler.add_job(
        agent.run_920am_first_candle_scan,
        trigger="cron",
        hour=9,
        minute=20,
        second=2,
        id="job_920am_first_candle"
    )

    # 3. Every 5 minutes from 09:25:02 to 15:05:02 IST (Runs immediately after each 5m candle closes)
    scheduler.add_job(
        agent.monitor_live_candles,
        trigger="cron",
        day_of_week="mon-fri",
        hour="9-14",
        minute="25,30,35,40,45,50,55",
        second=2,
        id="job_5m_loop_morning"
    )
    scheduler.add_job(
        agent.monitor_live_candles,
        trigger="cron",
        day_of_week="mon-fri",
        hour="10-14",
        minute="0,5,10,15,20,25,30,35,40,45,50,55",
        second=2,
        id="job_5m_loop_midday"
    )
    scheduler.add_job(
        agent.monitor_live_candles,
        trigger="cron",
        day_of_week="mon-fri",
        hour=15,
        minute="0,5",
        second=2,
        id="job_5m_loop_afternoon"
    )

    # 4. 03:10:00 PM IST: Force square-off
    scheduler.add_job(
        agent.close_all_positions_eod,
        trigger="cron",
        hour=15,
        minute=10,
        second=0,
        id="job_eod_squareoff"
    )

    logger.info("Scheduler started successfully. Waiting for market schedule triggers...")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Engine terminated manually by user.")

if __name__ == "__main__":
    main()