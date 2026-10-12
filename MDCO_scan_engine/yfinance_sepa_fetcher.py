"""
yfinance_sepa_fetcher.py
========================
Automated market data & fundamental extractor for Mark Minervini's SEPA 
(Specific Entry Point Analysis) & Trend Template Strategy.

Extracts:
  1. OHLCV Daily Price Data (1-2 years history for 50/150/200 SMAs & 52-week metrics)
  2. The 6 Key Fundamental Velocity Metrics:
     - 'eps_growth_qtr_yoy': Quarterly Net Income / EPS YoY growth rate (%)
     - 'eps_acceleration': Boolean (True if current YoY growth % > previous quarter YoY %)
     - 'sales_growth_qtr_yoy': Quarterly Revenue YoY growth rate (%)
     - 'margin_expanding': Boolean (True if current net margin > previous quarter net margin)
     - 'roe': Return on Equity (%)
     - 'debt_to_equity': Debt-to-Equity ratio (< 0.5 preferred)
     - 'institutional_sponsorship': Institutional ownership ratio / flag
"""

import time
import logging
from typing import Dict, Any, List, Optional
import pandas as pd
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def fetch_stock_ohlcv(ticker: str, period: str = "2y") -> Optional[pd.DataFrame]:
    """
    Fetches daily OHLCV price history required for Minervini Trend Template & VCP analysis.
    Requires at least 252-300 trading days for accurate 200 SMA and 52-week calculations.
    """
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period=period, interval="1d", auto_adjust=False)
        
        if df.empty or len(df) < 200:
            logger.warning(f"[{ticker}] Insufficient OHLCV data ({len(df)} bars found, >=200 required).")
            return None

        # Clean index and column formatting
        df = df.reset_index()
        # Keep standard column names
        rename_map = {}
        for col in df.columns:
            if str(col).lower() in ['date', 'datetime']:
                rename_map[col] = 'Date'
            elif str(col).lower() == 'open':
                rename_map[col] = 'Open'
            elif str(col).lower() == 'high':
                rename_map[col] = 'High'
            elif str(col).lower() == 'low':
                rename_map[col] = 'Low'
            elif str(col).lower() == 'close':
                rename_map[col] = 'Close'
            elif str(col).lower() == 'volume':
                rename_map[col] = 'Volume'

        df.rename(columns=rename_map, inplace=True)
        req = {'Date', 'Open', 'High', 'Low', 'Close', 'Volume'}
        if not req.issubset(df.columns):
            logger.warning(f"[{ticker}] Missing required columns: {req - set(df.columns)}")
            return None

        return df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]

    except Exception as e:
        logger.error(f"[{ticker}] Error fetching OHLCV: {e}")
        return None


def fetch_stock_fundamentals(ticker: str) -> Dict[str, Any]:
    """
    Extracts quarterly income statements, balance sheets, and key financial ratios 
    to calculate the 6 SEPA fundamental criteria.
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

    try:
        stock = yf.Ticker(ticker)
        info = stock.info or {}
        
        # 1. Fetch quarterly financial statements
        inc_q = stock.quarterly_financials
        
        # Fallback to info dictionary if quarterly statements are unavailable or sparse
        if inc_q is None or inc_q.empty or inc_q.shape[1] < 4:
            q_earn_growth = info.get("earningsQuarterlyGrowth", 0.0)
            q_rev_growth = info.get("revenueGrowth", 0.0)
            roe_val = info.get("returnOnEquity", 0.0)
            de_val = info.get("debtToEquity", 0.0)
            inst_own = info.get("heldPercentInstitutions", 0.0)

            return {
                "eps_growth_qtr_yoy": round(float(q_earn_growth * 100.0 if q_earn_growth else 0.0), 2),
                "eps_acceleration": False,
                "sales_growth_qtr_yoy": round(float(q_rev_growth * 100.0 if q_rev_growth else 0.0), 2),
                "roe": round(float(roe_val * 100.0 if roe_val else 0.0), 2),
                "debt_to_equity": round(float(de_val / 100.0 if de_val else 0.0), 2),
                "margin_expanding": False,
                "institutional_sponsorship": bool(inst_own and inst_own > 0.20),
                "data_complete": False
            }

        # Columns are ordered newest -> oldest: [Q0, Q1, Q2, Q3, Q4 (1 year ago)]
        cols = inc_q.columns

        # --- A. Net Income / EPS YoY Growth & Acceleration ---
        net_inc_row = None
        for candidate in ["Net Income", "Net Income Common Stockholders", "Net Income Applicable To Common Shares"]:
            if candidate in inc_q.index:
                net_inc_row = candidate
                break

        eps_growth_qtr = 0.0
        eps_accel = False
        if net_inc_row and len(cols) >= 5:
            net_inc_latest = inc_q.loc[net_inc_row].iloc[0]
            net_inc_year_ago = inc_q.loc[net_inc_row].iloc[4]  # 4 quarters ago

            if pd.notna(net_inc_latest) and pd.notna(net_inc_year_ago) and net_inc_year_ago != 0:
                eps_growth_qtr = ((net_inc_latest - net_inc_year_ago) / abs(net_inc_year_ago)) * 100.0

            # Prior quarter YoY growth (Q1 vs Q5 if available, else Q1 vs Q4)
            if len(cols) >= 6:
                net_inc_prev = inc_q.loc[net_inc_row].iloc[1]
                net_inc_prev_year_ago = inc_q.loc[net_inc_row].iloc[5]
                if pd.notna(net_inc_prev) and pd.notna(net_inc_prev_year_ago) and net_inc_prev_year_ago != 0:
                    eps_growth_prev_qtr = ((net_inc_prev - net_inc_prev_year_ago) / abs(net_inc_prev_year_ago)) * 100.0
                    eps_accel = eps_growth_qtr > eps_growth_prev_qtr
            else:
                eps_accel = eps_growth_qtr > 25.0  # Proxy if 6th quarter not available

        # --- B. Sales / Revenue YoY Growth ---
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

            # --- C. Net Profit Margin Expansion ---
            if net_inc_row and len(cols) >= 2:
                net_inc_q0 = inc_q.loc[net_inc_row].iloc[0]
                rev_q0 = inc_q.loc[rev_row].iloc[0]
                net_inc_q1 = inc_q.loc[net_inc_row].iloc[1]
                rev_q1 = inc_q.loc[rev_row].iloc[1]

                if rev_q0 and rev_q1 and rev_q0 > 0 and rev_q1 > 0:
                    margin_q0 = net_inc_q0 / rev_q0
                    margin_q1 = net_inc_q1 / rev_q1
                    margin_expanding = margin_q0 > margin_q1

        # --- D. ROE and Debt-to-Equity from info ---
        roe_val = info.get("returnOnEquity")
        if roe_val is not None:
            roe_val = float(roe_val) * 100.0
        else:
            roe_val = 0.0

        de_val = info.get("debtToEquity")
        if de_val is not None:
            # yfinance reports debtToEquity as a percentage (e.g. 45.0 for 0.45)
            de_val = float(de_val) / 100.0
        else:
            de_val = 0.0

        # --- E. Institutional Sponsorship ---
        inst_pct = info.get("heldPercentInstitutions", 0.0)
        has_inst_support = bool(inst_pct and inst_pct >= 0.20)

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


def fetch_sepa_stock_payload(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Fetches both OHLCV daily data and fundamentals for a single ticker,
    returning the ready-to-use payload for the SEPA ranker.
    """
    logger.info(f"Processing {ticker}...")
    ohlcv_df = fetch_stock_ohlcv(ticker)
    if ohlcv_df is None:
        return None

    fundamentals = fetch_stock_fundamentals(ticker)
    return {
        "ticker": ticker,
        "ohlcv": ohlcv_df,
        "fundamentals": fundamentals
    }


def batch_fetch_universe(ticker_list: List[str], delay_sec: float = 0.5) -> List[Dict[str, Any]]:
    """
    Iterates through a list of stock tickers (e.g., 2,000+ universe),
    fetches OHLCV + fundamentals with rate limiting, and returns valid payloads.
    """
    results = []
    total = len(ticker_list)
    logger.info(f"Starting batch fetch for {total} tickers...")

    for idx, ticker in enumerate(ticker_list, start=1):
        try:
            payload = fetch_sepa_stock_payload(ticker)
            if payload:
                results.append(payload)
        except Exception as e:
            logger.error(f"Failed to process {ticker}: {e}")

        if idx % 10 == 0 or idx == total:
            logger.info(f"Progress: {idx}/{total} processed ({len(results)} valid).")

        if delay_sec > 0:
            time.sleep(delay_sec)

    return results


# --- Local Test & Demo ---
if __name__ == "__main__":
    test_tickers = ["RELIANCE.NS", "TCS.NS"]
    print("Fetching single stock sample...")
    for item in test_tickers:
        
        sample = fetch_sepa_stock_payload(item)
        if sample:
            print("\n=== Fundamentals Extracted ===")
            import json
            print(json.dumps(sample["fundamentals"], indent=2))
            print("\n=== OHLCV Shape ===")
            print(sample["ohlcv"].tail())
