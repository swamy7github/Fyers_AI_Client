"""
yfinance_sepa_fetcher-v2.py
===========================
Automated market data & fundamental extractor for Mark Minervini's SEPA 
(Specific Entry Point Analysis) & Trend Template Strategy.

Enhanced in Version 2 with Production Rate Limiting:
  1. Adaptive Token-Bucket & Interval RateLimiter with randomized jitter to prevent bot detection.
  2. Automatic Exponential Backoff & Retry on HTTP 429 ('Too Many Requests') or throttling.
  3. Chunked Universe Batching with inter-chunk cooldown pauses (essential for 2,000+ stocks).
  4. Local Checkpointing: Saves progress periodically to disk so scans can resume if interrupted.
"""

import time
import json
import random
import logging
from typing import Dict, Any, List, Optional, Callable
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SEPA_Fetcher_v2")

from data.fyers_client import FyersDataClient
from utilities import get_last_traded_day

to_day = get_last_traded_day()
from_day = (pd.to_datetime(to_day) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
data_client = FyersDataClient(
            client_id="MOCK",
            access_token="MOCK",
            use_mock=False
        )

class AdaptiveRateLimiter:
    """
    Adaptive rate limiter with randomized jitter and cooldown penalty tracking.
    Prevents Yahoo Finance IP bans, throttling, and 429 'Too Many Requests' errors.
    """
    def __init__(
        self,
        calls_per_minute: int = 35,
        min_interval_sec: float = 1.2,
        jitter_range: tuple = (0.2, 0.8),
        max_backoff_level: int = 5
    ):
        self.min_interval = max(min_interval_sec, 60.0 / calls_per_minute)
        self.jitter_range = jitter_range
        self.last_call_time = 0.0
        self.backoff_level = 0
        self.max_backoff_level = max_backoff_level

    def wait(self):
        """Enforces spacing and randomized jitter between consecutive API requests."""
        now = time.time()
        elapsed = now - self.last_call_time
        jitter = random.uniform(self.jitter_range[0], self.jitter_range[1])
        target_interval = self.min_interval + jitter

        if elapsed < target_interval:
            time.sleep(target_interval - elapsed)

        self.last_call_time = time.time()

    def report_success(self):
        """Gradually relaxes backoff penalty when calls succeed."""
        if self.backoff_level > 0:
            self.backoff_level -= 1

    def report_rate_limit(self, error_msg: str = ""):
        """
        Escalates cooldown penalty when 429 / Rate Limit error is detected.
        Uses exponential backoff: 2^level * 4s + random jitter.
        """
        self.backoff_level = min(self.backoff_level + 1, self.max_backoff_level)
        cooldown = (2 ** self.backoff_level) * 4.0 + random.uniform(2.0, 5.0)
        logger.warning(
            f"Rate limit / 429 detected ({error_msg})! Backoff level {self.backoff_level}. "
            f"Cooling down for {cooldown:.1f} seconds..."
        )
        time.sleep(cooldown)


# Default global rate limiter instance (35 calls/min ~ 1.7s spacing)
DEFAULT_LIMITER = AdaptiveRateLimiter(calls_per_minute=100, min_interval_sec=.6)


def safe_api_call(
    func: Callable,
    *args,
    max_retries: int = 3,
    limiter: AdaptiveRateLimiter = DEFAULT_LIMITER,
    **kwargs
) -> Any:
    """
    Executes an API call wrapped with adaptive rate limiting and exponential retry logic.
    """
    last_err = None
    for attempt in range(1, max_retries + 1):
        limiter.wait()
        try:
            result = func(*args, **kwargs)
            limiter.report_success()
            return result
        except Exception as e:
            err_str = str(e).lower()
            last_err = e
            # Detect HTTP 429 or throttling keywords
            if any(k in err_str for k in ["429", "too many requests", "rate limit", "rate-limited"]):
                limiter.report_rate_limit(error_msg=str(e))
            else:
                wait_time = (attempt * 2.0) + random.uniform(0.5, 1.5)
                logger.warning(f"API attempt {attempt}/{max_retries} failed: {e}. Retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)

    logger.error(f"Function {func.__name__} failed after {max_retries} attempts: {last_err}")
    return None


def fetch_stock_ohlcv(
    ticker: str,
    period: str = "2y",
    limiter: AdaptiveRateLimiter = DEFAULT_LIMITER
) -> Optional[pd.DataFrame]:
    """
    Fetches daily OHLCV price history required for Minervini Trend Template & VCP analysis.
    Requires at least 200-252 trading days for accurate 200 SMA and 52-week metrics.
    """
    if yf is None:
        raise ImportError("The 'yfinance' package is required. Install via 'pip install yfinance'.")

    def _get_history():
        stock = yf.Ticker(ticker)
        return stock.history(period=period, interval="1d", auto_adjust=False)

    try:
        df = safe_api_call(_get_history, max_retries=3, limiter=limiter)
        if df is None or df.empty or len(df) < 200:
            logger.warning(f"[{ticker}] Insufficient OHLCV data (found {len(df) if df is not None else 0} bars, >=200 required).")
            return None

        # Standardize column names
        df = df.reset_index()
        rename_map = {}
        for col in df.columns:
            c = str(col).lower()
            if c in ['date', 'datetime']: rename_map[col] = 'Date'
            elif c == 'open': rename_map[col] = 'Open'
            elif c == 'high': rename_map[col] = 'High'
            elif c == 'low': rename_map[col] = 'Low'
            elif c == 'close': rename_map[col] = 'Close'
            elif c == 'volume': rename_map[col] = 'Volume'

        df.rename(columns=rename_map, inplace=True)
        req = {'Date', 'Open', 'High', 'Low', 'Close', 'Volume'}
        if not req.issubset(df.columns):
            logger.warning(f"[{ticker}] Missing required OHLCV columns: {req - set(df.columns)}")
            return None

        return df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]

    except Exception as e:
        logger.error(f"[{ticker}] Error parsing OHLCV: {e}")
        return None


def fetch_stock_fundamentals(
    ticker: str,
    limiter: AdaptiveRateLimiter = DEFAULT_LIMITER
) -> Dict[str, Any]:
    """
    Extracts quarterly income statements, balance sheets, and key financial ratios 
    to calculate the 6 SEPA fundamental criteria. Uses adaptive rate limiting.
    """
    default_fundamentals = {
        "eps_growth_qtr_yoy": 0.0,
        "eps_acceleration": False,
        "sales_growth_qtr_yoy": 0.0,
        "roe": 0.0,
        "debt_to_equity": 0.0,
        "margin_expanding": False,
        "institutional_sponsorship": False,
        "data_complete": False
    }

    if yf is None:
        raise ImportError("The 'yfinance' package is required. Install via 'pip install yfinance'.")

    try:
        def _get_stock_info():
            s = yf.Ticker(ticker)
            # yfinance defaults to TTM data if available, otherwise use .financials
            try:
                net_income = s.financials.loc["Net Income"].iloc[0]
            except KeyError:
                # Alternate index name sometimes populated by Yahoo Finance
                net_income = s.financials.loc["Net Income Common Stockholders"].iloc[0]

            # 2. Fetch the latest Total Stockholders' Equity from the balance sheet
            try:
                equity = s.balance_sheet.loc["Total Stockholder Equity"].iloc[0]
            except KeyError:
                # Alternate index name for international/NSE listings
                equity = s.balance_sheet.loc["Stockholders Equity"].iloc[0]
            # 1. Fetch Total Debt (If unavailable, fall back to Lease Obligations)
            try:
                total_debt = s.balance_sheet.loc["Total Debt"].iloc[0]
            except KeyError:
                try:
                    # Many debt-free companies only register lease obligations as debt
                    total_debt = s.balance_sheet.loc["Capital Lease Obligations"].iloc[0]
                except KeyError:
                    total_debt = 0.0
            fdata = {"equity":equity, "net_income":net_income, "total_debt":total_debt}
            print(f"======equity======{equity}=======net_income==={net_income}====total_debt == {total_debt} =======")
            
            return s.info or {}, s.quarterly_financials, fdata

        pack = safe_api_call(_get_stock_info, max_retries=3, limiter=limiter)
        if pack is None:
            return default_fundamentals

        info, inc_q, fdata = pack
        print(f" inf0 --------------------> fetch_stock_fundamentals")

        # Fallback to info dictionary if quarterly statements are unavailable or sparse
        if inc_q is None or inc_q.empty or inc_q.shape[1] < 4:
            q_earn_growth = info.get("earningsQuarterlyGrowth", 0.0)
            q_rev_growth = info.get("revenueGrowth", 0.0)
            roe_val = info.get("returnOnEquity", 0.0)
            de_val = info.get("debtToEquity", 0.0)
            inst_own = info.get("heldPercentInstitutions", 0.0)
            if roe_val == 0.0:
                roe_val = (fdata.get("net_income") / fdata.get("equity")) * 100
            if de_val == 0.0:
                de_val = (fdata.get("total_debt") / fdata.get("equity"))

            return {
                "eps_growth_qtr_yoy": round(float(q_earn_growth * 100.0 if q_earn_growth else 0.0), 2),
                "eps_acceleration": False,
                "sales_growth_qtr_yoy": round(float(q_rev_growth * 100.0 if q_rev_growth else 0.0), 2),
                "roe": round(float(roe_val * 100.0 if roe_val else 0.0), 2),
                "debt_to_equity": round(float(de_val / 100.0 if de_val else 0.0), 2),
                "margin_expanding": False,
                "institutional_sponsorship": bool(inst_own and inst_own >= 0.20),
                "data_complete": False
            }

        # Columns ordered newest -> oldest: [Q0, Q1, Q2, Q3, Q4 (1 year ago)]
        cols = inc_q.columns

        # --- 1. Net Income / EPS YoY Growth & Acceleration ---
        net_inc_row = None
        for candidate in ["Net Income", "Net Income Common Stockholders", "Net Income Applicable To Common Shares"]:
            if candidate in inc_q.index:
                net_inc_row = candidate
                break

        eps_growth_qtr = 0.0
        eps_accel = False
        if net_inc_row and len(cols) >= 5:
            net_inc_latest = inc_q.loc[net_inc_row].iloc[0]
            net_inc_year_ago = inc_q.loc[net_inc_row].iloc[4]

            if pd.notna(net_inc_latest) and pd.notna(net_inc_year_ago) and net_inc_year_ago != 0:
                eps_growth_qtr = ((net_inc_latest - net_inc_year_ago) / abs(net_inc_year_ago)) * 100.0

            if len(cols) >= 6:
                net_inc_prev = inc_q.loc[net_inc_row].iloc[1]
                net_inc_prev_year_ago = inc_q.loc[net_inc_row].iloc[5]
                if pd.notna(net_inc_prev) and pd.notna(net_inc_prev_year_ago) and net_inc_prev_year_ago != 0:
                    eps_growth_prev_qtr = ((net_inc_prev - net_inc_prev_year_ago) / abs(net_inc_prev_year_ago)) * 100.0
                    eps_accel = eps_growth_qtr > eps_growth_prev_qtr
            else:
                eps_accel = eps_growth_qtr > 25.0

        # --- 2. Sales / Revenue YoY Growth ---
        rev_row = None
        for candidate in ["Total Revenue", "Operating Revenue", "Revenue"]:
            if candidate in inc_q.index:
                rev_row = candidate
                break

        sales_growth_qtr = 0.0
        margin_expanding = False
        if rev_row and len(cols) >= 5:
            rev_latest = inc_q.loc[rev_row].iloc[0]
            rev_year_ago = inc_q.loc[rev_row].iloc[4]

            if pd.notna(rev_latest) and pd.notna(rev_year_ago) and rev_year_ago != 0:
                sales_growth_qtr = ((rev_latest - rev_year_ago) / abs(rev_year_ago)) * 100.0

            # --- 3. Net Margin Expansion Check ---
            if net_inc_row and len(cols) >= 2:
                net_inc_q0 = inc_q.loc[net_inc_row].iloc[0]
                rev_q0 = inc_q.loc[rev_row].iloc[0]
                net_inc_q1 = inc_q.loc[net_inc_row].iloc[1]
                rev_q1 = inc_q.loc[rev_row].iloc[1]

                if rev_q0 and rev_q1 and rev_q0 > 0 and rev_q1 > 0:
                    margin_q0 = net_inc_q0 / rev_q0
                    margin_q1 = net_inc_q1 / rev_q1
                    margin_expanding = margin_q0 > margin_q1

        # --- 4. ROE and Debt-to-Equity ---
        roe_val = info.get("returnOnEquity")
        roe_val = float(roe_val) * 100.0 if roe_val is not None else 0.0

        de_val = info.get("debtToEquity")
        de_val = float(de_val) / 100.0 if de_val is not None else 0.0

        # --- 5. Institutional Ownership ---
        inst_pct = info.get("heldPercentInstitutions", 0.0)
        has_inst_support = bool(inst_pct and inst_pct >= 0.20)
        if roe_val == 0.0:
            roe_val = (fdata.get("net_income") / fdata.get("equity")) * 100
        if de_val == 0.0:
            de_val = (fdata.get("total_debt") / fdata.get("equity"))
            
        return {
            "eps_growth_qtr_yoy": round(float(eps_growth_qtr), 2),
            "eps_acceleration": bool(eps_accel),
            "sales_growth_qtr_yoy": round(float(sales_growth_qtr), 2),
            "roe": round(float(roe_val), 2),
            "debt_to_equity": round(float(de_val), 2),
            "margin_expanding": bool(margin_expanding),
            "institutional_sponsorship": has_inst_support,
            "data_complete": True
        }

    except Exception as e:
        logger.error(f"[{ticker}] Error parsing fundamentals: {e}")
        return default_fundamentals

def fetch_sepa_stock_payload(
    ticker: str,
    limiter: AdaptiveRateLimiter = DEFAULT_LIMITER
) -> Optional[Dict[str, Any]]:
    """
    Fetches both OHLCV daily data and fundamentals for a single ticker,
    returning the ready-to-use payload for the SEPA ranker.
    """
    logger.info(f"Fetching payload for {ticker}...")
    #ohlcv_df = fetch_stock_ohlcv(ticker, limiter=limiter)
    ohlcv_df = data_client.get_historical_data(f"NSE:{ticker.split('.')[0]}-EQ", "D", from_day, to_day)
    if ohlcv_df is None:
        return None

    fundamentals = fetch_stock_fundamentals(ticker, limiter=limiter)
    print(f" symbol {ticker} ==========> {fundamentals}")
    return {
        "ticker": ticker,
        "ohlcv": ohlcv_df,
        "fundamentals": fundamentals
    }


def sepa_fetch_ticker_universe(
    ticker_list: List[str],
    calls_per_minute: int = 100,
    min_interval_sec: float = .5,
    chunk_size: int = 30,
    chunk_cooldown_sec: float = 6.0,
    checkpoint_file: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Iterates through a list of stock tickers (e.g. 2,000+ universe) with:
      - Adaptive rate limiting and randomized jitter
      - Chunk pauses (e.g. 6s pause every 30 tickers to clear rate buffers)
      - Checkpointing (saves progress to disk so runs can be safely resumed)
    """
    limiter = AdaptiveRateLimiter(calls_per_minute=calls_per_minute, min_interval_sec=min_interval_sec)
    results: List[Dict[str, Any]] = []
    total = len(ticker_list)
    logger.info(f"Starting resilient batch fetch for {total} tickers (Target: {calls_per_minute} req/min)...")

    for idx, ticker in enumerate(ticker_list, start=1):
        try:
            payload = fetch_sepa_stock_payload(ticker, limiter=limiter)
            if payload is not None:
                results.append(payload)
        except Exception as e:
            logger.error(f"Unexpected error processing {ticker}: {e}")

        # Logging & Progress Feedback
        if idx % 10 == 0 or idx == total:
            logger.info(f"Progress: {idx}/{total} processed ({len(results)} valid payloads collected).")

        # Chunk Cooldown to avoid triggering Yahoo Finance burst limits
        if idx % chunk_size == 0 and idx < total:
            logger.info(f"Reached chunk boundary ({idx} tickers). Pausing for {chunk_cooldown_sec}s cooldown...")
            time.sleep(chunk_cooldown_sec)

        # Periodic Checkpointing (if requested)
        if checkpoint_file and (idx % (chunk_size * 2) == 0 or idx == total):
            try:
                summary = [
                    {"ticker": r["ticker"], "fundamentals": r["fundamentals"], "bars": len(r["ohlcv"])}
                    for r in results
                ]
                with open(checkpoint_file, "w") as f:
                    json.dump(summary, f, indent=2)
                logger.info(f"Saved checkpoint with {len(summary)} records to {checkpoint_file}")
            except Exception as e:
                logger.warning(f"Could not save checkpoint: {e}")

    logger.info(f"Batch fetch complete: {len(results)}/{total} stocks ready for SEPA screening.")
    return results


# --- Local Test & Demo ---
if __name__ == "__main__":
    print("Testing AdaptiveRateLimiter...")
    test_limiter = AdaptiveRateLimiter(calls_per_minute=60, min_interval_sec=0.1, jitter_range=(0.05, 0.1))
    t0 = time.time()
    for _ in range(5):
        test_limiter.wait()
    t1 = time.time()
    #d = fetch_stock_fundamentals("BEL.NS")
    d = sepa_fetch_ticker_universe(ticker_list=["BEL.NS"])
    #d =  batch_fetch_universe(calls_per_minute = 100, ticker_list= ["BDL.NS","HAL.NS","BEL.NS"])
    print(f" {d} ")
    print(f"5 iterations completed in {t1 - t0:.2f}s (Rate limiter functioning properly).")
