import os
import json
import pandas as pd
from datetime import datetime, timedelta
import numpy as np

# Try to import yfinance as fallback
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

PORTFOLIO_FILE = "active_positions.csv"

def create_sample_portfolio():
    """Creates a sample portfolio CSV file if it doesn't exist."""
    if os.path.exists(PORTFOLIO_FILE):
        return
        
    # Sample data mimicking user holdings
    sample_data = [
        {
            "Symbol": "SHRIRAMFIN",
            "Entry_Date": (datetime.now() - timedelta(days=12)).strftime("%Y-%m-%d"),
            "Qty": 50,
            "Entry_Price": 1030.00,
            "Stop_Loss": 1010.00,
            "Target": 1200.00,
            "Est_Hold_Days": 15
        },
        {
            "Symbol": "TATASTEEL",
            "Entry_Date": (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d"),
            "Qty": 300,
            "Entry_Price": 184.50,
            "Stop_Loss": 178.00,
            "Target": 200.00,
            "Est_Hold_Days": 7
        },
        {
            "Symbol": "WIPRO",
            "Entry_Date": (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"),
            "Qty": 200,
            "Entry_Price": 171.00,
            "Stop_Loss": 164.00,
            "Target": 190.00,
            "Est_Hold_Days": 10
        }
    ]
    df = pd.DataFrame(sample_data)
    df.to_csv(PORTFOLIO_FILE, index=False)
    print(f"[INFO] Created a fresh sample portfolio at '{PORTFOLIO_FILE}'. Update this file with your real active trades!")

def fetch_latest_cmp_yfinance(symbols):
    """Fetches latest prices using yfinance fallback."""
    prices = {}
    if not YFINANCE_AVAILABLE:
        return prices
    try:
        # Batch download for efficiency
        tickers_str = " ".join([f"{s}.NS" for s in symbols])
        data = yf.download(tickers_str, period="1d", group_by="ticker", progress=False)
        for s in symbols:
            ns_sym = f"{s}.NS"
            if ns_sym in data:
                # Handle single vs multi-ticker dataframe structure of yfinance download
                try:
                    prices[s] = round(data[ns_sym]['Close'].iloc[-1], 2)
                except Exception:
                    prices[s] = round(data['Close'].iloc[-1], 2) if 'Close' in data else None
    except Exception as e:
        print(f"[WARNING] Fallback price fetch failed: {str(e)}")
    return prices

def monitor_portfolio():
    """Reads active positions, grabs live prices, and flags violations/exceeded hold times."""
    create_sample_portfolio()
    
    if not os.path.exists(PORTFOLIO_FILE):
        print("[ERROR] Portfolio file not found.")
        return
        
    df = pd.read_csv(PORTFOLIO_FILE)
    if df.empty:
        print("[INFO] Active portfolio is empty. Add positions to 'active_positions.csv'.")
        return
        
    symbols = df["Symbol"].unique().tolist()
    
    # Grab active prices
    # Fyers API setup can be integrated here similarly to scheduler-v6 using env variables
    prices = fetch_latest_cmp_yfinance(symbols)
    
    print("\n" + "="*85)
    print("                      SWING POSITION MONITORING DASHBOARD")
    print("="*85)
    print(f"Checked on: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}\n")
    
    headers = f"{'Symbol':<12} | {'Days Open':<9} | {'Est Max':<7} | {'Entry (₹)':<9} | {'CMP (₹)':<9} | {'Target (₹)':<10} | {'Return':<7} | {'Alert/Status':<15}"
    print(headers)
    print("-" * len(headers))
    
    for _, row in df.iterrows():
        symbol = row["Symbol"]
        entry_date = datetime.strptime(str(row["Entry_Date"]), "%Y-%m-%d")
        days_open = (datetime.now() - entry_date).days
        est_days = int(row["Est_Hold_Days"])
        entry_p = float(row["Entry_Price"])
        qty = int(row["Qty"])
        sl = float(row["Stop_Loss"])
        tgt = float(row["Target"])
        
        cmp = prices.get(symbol)
        
        # Fallback to entry price if market is offline/closed and yfinance returns empty
        if cmp is None or np.isnan(cmp):
            cmp = entry_p
            cmp_str = f"{cmp:,.2f}*"
        else:
            cmp_str = f"{cmp:,.2f}"
            
        unrealized_pct = ((cmp - entry_p) / entry_p) * 100
        sign = "+" if unrealized_pct >= 0 else ""
        ret_str = f"{sign}{unrealized_pct:.2f}%"
        
        # Decide status
        status = "🟢 Safe (Active)"
        if cmp >= tgt:
            status = "🎉 TARGET HIT!"
        elif cmp <= sl:
            status = "🚨 STOP-LOSS HIT!"
        elif days_open > est_days:
            status = "⏳ TIME EXCEEDED"
        elif days_open == est_days:
            status = "⚠️ LAST HOLD DAY"
            
        print(f"{symbol:<12} | {days_open:<9} | {est_days:<7} | {entry_p:<9,.2f} | {cmp_str:<9} | {tgt:<10,.2f} | {ret_str:<7} | {status:<15}")

    print("="*85)
    print("  *CMP represents mock/cached values if live API is throttled or offline.")
    print("  ⏳ TIME EXCEEDED: Position held longer than expected hold time. Consider structural exit.")
    print("="*85 + "\n")

if __name__ == "__main__":
    monitor_portfolio()
