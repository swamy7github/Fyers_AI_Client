import os
import sys
import pandas as pd
from datetime import datetime, timedelta
import numpy as np
import requests

# Try to import yfinance as fallback
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

PORTFOLIO_FILE = "active_positions.csv"
JOURNAL_FILE = "trade_journal.xlsx"

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

def fetch_latest_cmp_fyers(symbols, app_id, access_token):
    """Fetches latest quotes from Fyers API."""
    prices = {}
    if not (app_id and access_token):
        return prices
    
    # Fyers v3 Quotes Endpoint supports multi-symbol comma separated
    fyers_symbols = ",".join([f"NSE:{s}-EQ" for s in symbols])
    url = "https://api-t1.fyers.in/data/quotes"
    params = {"symbols": fyers_symbols}
    headers = {"Authorization": f"{app_id}:{access_token}"}
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code == 200:
            res_data = response.json()
            if res_data.get("s") == "ok" and "d" in res_data:
                for item in res_data["d"]:
                    # Fyers returns key as 'NSE:SYMBOL-EQ'
                    raw_sym = item.get("n", "")
                    symbol = raw_sym.replace("NSE:", "").replace("-EQ", "")
                    # Extract last traded price (lp)
                    lp = item.get("v", {}).get("lp")
                    if lp is not None:
                        prices[symbol] = round(float(lp), 2)
            else:
                print(f"[WARNING] Fyers Quotes API returned status: {res_data.get('errmsg', 'Error')}")
        else:
            print(f"[WARNING] Fyers Quotes API HTTP Error {response.status_code}")
    except Exception as e:
        print(f"[ERROR] Exception during Fyers Quotes fetch: {str(e)}")
    return prices

def fetch_latest_cmp_yfinance(symbols):
    """Fetches latest prices using yfinance fallback."""
    prices = {}
    if not YFINANCE_AVAILABLE:
        return prices
    try:
        tickers_str = " ".join([f"{s}.NS" for s in symbols])
        data = yf.download(tickers_str, period="1d", group_by="ticker", progress=False)
        for s in symbols:
            ns_sym = f"{s}.NS"
            if ns_sym in data:
                try:
                    prices[s] = round(data[ns_sym]['Close'].iloc[-1], 2)
                except Exception:
                    prices[s] = round(data['Close'].iloc[-1], 2) if 'Close' in data else None
    except Exception as e:
        print(f"[WARNING] Fallback price fetch failed: {str(e)}")
    return prices

def log_to_trade_journal(closed_trades):
    """Appends closed trades to a beautifully styled Excel spreadsheet database."""
    if not closed_trades:
        return
        
    new_df = pd.DataFrame(closed_trades)
    
    # Check if journal already exists
    if os.path.exists(JOURNAL_FILE):
        try:
            old_df = pd.read_excel(JOURNAL_FILE, sheet_name="Trade Journal")
            combined_df = pd.concat([old_df, new_df], ignore_index=True)
        except Exception:
            combined_df = new_df
    else:
        combined_df = new_df

    # Write to Excel and apply beautiful styling using openpyxl
    try:
        with pd.ExcelWriter(JOURNAL_FILE, engine='openpyxl') as writer:
            combined_df.to_excel(writer, sheet_name="Trade Journal", index=False)
            
            # Format workbook
            workbook = writer.book
            worksheet = writer.sheets["Trade Journal"]
            
            # Formatting styles
            from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
            from openpyxl.utils import get_column_letter
            
            # Colors
            navy_header = PatternFill(start_color="1F3C4D", end_color="1F3C4D", fill_type="solid")
            white_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
            regular_font = Font(name="Segoe UI", size=10, color="333333")
            bold_font = Font(name="Segoe UI", size=10, bold=True, color="333333")
            green_fill = PatternFill(start_color="E2F0D9", end_color="E2F0D9", fill_type="solid")
            red_fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
            yellow_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
            
            # Center Alignments
            center_align = Alignment(horizontal="center", vertical="center")
            left_align = Alignment(horizontal="left", vertical="center")
            right_align = Alignment(horizontal="right", vertical="center")
            
            # Header Formatting
            for col_num in range(1, len(combined_df.columns) + 1):
                cell = worksheet.cell(row=1, column=col_num)
                cell.fill = navy_header
                cell.font = white_font
                cell.alignment = center_align
                
            # Content Formatting
            thin_border = Border(
                left=Side(style='thin', color='E0E0E0'),
                right=Side(style='thin', color='E0E0E0'),
                top=Side(style='thin', color='E0E0E0'),
                bottom=Side(style='thin', color='E0E0E0')
            )
            
            for row_idx in range(2, worksheet.max_row + 1):
                outcome = worksheet.cell(row=row_idx, column=10).value # Column 10: Outcome
                pnl_val = worksheet.cell(row=row_idx, column=7).value # Column 7: P&L (₹)
                
                # Default Font
                for col_idx in range(1, worksheet.max_column + 1):
                    cell = worksheet.cell(row=row_idx, column=col_idx)
                    cell.font = regular_font
                    cell.border = thin_border
                    
                    # Formatting alignments based on column index
                    if col_idx in [1, 2, 3, 10]:  # Symbol, Dates, Outcome
                        cell.alignment = center_align
                    elif col_idx in [4, 5, 6, 7, 8, 9]:  # Numbers
                        cell.alignment = right_align
                        if col_idx in [5, 6, 7]: # Money
                            cell.number_format = '₹#,##0.00'
                        elif col_idx == 8: # Profit %
                            cell.number_format = '0.00"%"'
                    
                # Color code based on outcome
                pnl_cell = worksheet.cell(row=row_idx, column=7)
                outcome_cell = worksheet.cell(row=row_idx, column=10)
                pct_cell = worksheet.cell(row=row_idx, column=8)
                
                pnl_cell.font = bold_font
                pct_cell.font = bold_font
                outcome_cell.font = bold_font
                
                if "Target" in str(outcome):
                    outcome_cell.fill = green_fill
                    pnl_cell.font = Font(name="Segoe UI", size=10, bold=True, color="2E7D32")
                elif "Stop-Loss" in str(outcome):
                    outcome_cell.fill = red_fill
                    pnl_cell.font = Font(name="Segoe UI", size=10, bold=True, color="C62828")
                else:
                    outcome_cell.fill = yellow_fill
            
            # Autofit column widths
            for col in worksheet.columns:
                max_len = max(len(str(cell.value or '')) for cell in col)
                col_letter = get_column_letter(col[0].column)
                worksheet.column_dimensions[col_letter].width = max(max_len + 4, 12)
                
        print(f"[SUCCESS] Beautifully logged {len(closed_trades)} position(s) to trade spreadsheet: '{JOURNAL_FILE}'")
    except Exception as e:
        print(f"[ERROR] Failed to format and write trade journal to Excel: {str(e)}")

def manual_close_trade(symbol, exit_price=None):
    """Manually closes a trade from the active positions file."""
    if not os.path.exists(PORTFOLIO_FILE):
        print(f"[ERROR] Portfolio file '{PORTFOLIO_FILE}' does not exist.")
        return
        
    df = pd.read_csv(PORTFOLIO_FILE)
    if df.empty:
        print("[INFO] Active portfolio is empty.")
        return
        
    # Case insensitive check
    df['Symbol_upper'] = df['Symbol'].str.upper()
    target_symbol = symbol.upper()
    
    if target_symbol not in df['Symbol_upper'].values:
        print(f"[ERROR] Stock '{symbol}' is not currently in your active portfolio.")
        return
        
    # Get trade details
    trade_row = df[df['Symbol_upper'] == target_symbol].iloc[0]
    symbol_actual = trade_row['Symbol']
    entry_p = float(trade_row['Entry_Price'])
    qty = int(trade_row['Qty'])
    entry_date_str = str(trade_row['Entry_Date'])
    
    if exit_price is None:
        # Prompt for exit price if not supplied via command line
        try:
            exit_price_input = input(f"Enter Exit Price (₹) for {symbol_actual} (Entry: ₹{entry_p:,.2f}): ")
            exit_price = float(exit_price_input)
        except ValueError:
            print("[ERROR] Invalid exit price entered. Aborting manual close.")
            return

    # Calculate metrics
    exit_p = float(exit_price)
    pnl = round((exit_p - entry_p) * qty, 2)
    ret_pct = round(((exit_p - entry_p) / entry_p) * 100, 2)
    days_open = (datetime.now() - datetime.strptime(entry_date_str, "%Y-%m-%d")).days
    
    closed_trade = {
        "Symbol": symbol_actual,
        "Entry_Date": entry_date_str,
        "Exit_Date": datetime.now().strftime("%Y-%m-%d"),
        "Qty": qty,
        "Entry_Price": entry_p,
        "Exit_Price": exit_p,
        "P&L (₹)": pnl,
        "Return (%)": ret_pct,
        "Duration (Days)": days_open,
        "Outcome": "Manual Exit"
    }
    
    # Remove from active positions and save
    df = df[df['Symbol_upper'] != target_symbol].drop(columns=['Symbol_upper'])
    df.to_csv(PORTFOLIO_FILE, index=False)
    
    # Append to Journal
    log_to_trade_journal([closed_trade])
    print(f"[INFO] Manually closed {symbol_actual} at ₹{exit_p:,.2f}. Realized P&L: {'+' if pnl >= 0 else ''}₹{pnl:,.2f} ({ret_pct:.2f}%).")

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
    
    # Try fetching Fyers credentials
    app_id = os.environ.get("FYERS_APP_ID")
    access_token = os.environ.get("FYERS_ACCESS_TOKEN")
    
    prices = {}
    if app_id and access_token:
        print("[INFO] Fyers credentials detected. Fetching live quotes from Fyers API...")
        prices = fetch_latest_cmp_fyers(symbols, app_id, access_token)
        
    # If Fyers returned empty, fall back to yfinance
    if not prices:
        print("[INFO] Fetching live quotes from Yahoo Finance...")
        prices = fetch_latest_cmp_yfinance(symbols)
        
    print("\n" + "="*95)
    print("                      SWING POSITION MONITORING DASHBOARD")
    print("="*95)
    print(f"Checked on: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}\n")
    
    headers = f"{'Symbol':<12} | {'Days Open':<9} | {'Est Max':<7} | {'Entry (₹)':<9} | {'CMP (₹)':<9} | {'Target (₹)':<10} | {'Return':<7} | {'Alert/Status':<15}"
    print(headers)
    print("-" * len(headers))
    
    closed_trades = []
    active_rows = []
    
    for _, row in df.iterrows():
        symbol = row["Symbol"]
        entry_date_str = str(row["Entry_Date"])
        entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d")
        days_open = (datetime.now() - entry_date).days
        est_days = int(row["Est_Hold_Days"])
        entry_p = float(row["Entry_Price"])
        qty = int(row["Qty"])
        sl = float(row["Stop_Loss"])
        tgt = float(row["Target"])
        
        cmp = prices.get(symbol)
        
        # Fallback if live price is unavailable
        if cmp is None or np.isnan(cmp):
            cmp = entry_p
            cmp_str = f"{cmp:,.2f}*"
        else:
            cmp_str = f"{cmp:,.2f}"
            
        unrealized_pct = ((cmp - entry_p) / entry_p) * 100
        sign = "+" if unrealized_pct >= 0 else ""
        ret_str = f"{sign}{unrealized_pct:.2f}%"
        
        # Decide status and exits
        status = "🟢 Safe (Active)"
        is_closed = False
        outcome = ""
        
        if cmp >= tgt:
            status = "🎉 TARGET HIT!"
            is_closed = True
            outcome = "Target Hit"
        elif cmp <= sl:
            status = "🚨 STOP-LOSS HIT!"
            is_closed = True
            outcome = "Stop-Loss Hit"
        elif days_open > est_days:
            status = "⏳ TIME EXCEEDED"
            # We alert but do not auto-close unless they hit stop/target. 
            # We let the trader decide on manual time stops.
        elif days_open == est_days:
            status = "⚠️ LAST HOLD DAY"
            
        print(f"{symbol:<12} | {days_open:<9} | {est_days:<7} | {entry_p:<9,.2f} | {cmp_str:<9} | {tgt:<10,.2f} | {ret_str:<7} | {status:<15}")
        
        if is_closed:
            pnl = round((cmp - entry_p) * qty, 2)
            closed_trades.append({
                "Symbol": symbol,
                "Entry_Date": entry_date_str,
                "Exit_Date": datetime.now().strftime("%Y-%m-%d"),
                "Qty": qty,
                "Entry_Price": entry_p,
                "Exit_Price": cmp,
                "P&L (₹)": pnl,
                "Return (%)": round(unrealized_pct, 2),
                "Duration (Days)": days_open,
                "Outcome": outcome
            })
        else:
            active_rows.append(row)

    print("="*95)
    print("  *CMP represents mock/cached values if live API is throttled or offline.")
    print("  ⏳ TIME EXCEEDED: Position held longer than expected hold time. Consider manual exit.")
    print("="*95 + "\n")
    
    # Process auto-exits if any occurred
    if closed_trades:
        print("\n" + "!"*50)
        print("          AUTO-EXIT EVENTS DETECTED")
        print("!"*50)
        for trade in closed_trades:
            pnl_sign = "+" if trade["P&L (₹)"] >= 0 else ""
            print(f"👉 {trade['Symbol']} exited via {trade['Outcome'].upper()} at ₹{trade['Exit_Price']:.2f}")
            print(f"   Realized Profit/Loss: {pnl_sign}₹{trade['P&L (₹)']:,} ({trade['Return (%)']:.2f}% over {trade['Duration (Days)']} days)")
        print("!"*50 + "\n")
        
        # Log to Excel
        log_to_trade_journal(closed_trades)
        
        # Update the active positions CSV (saving only those that didn't exit)
        updated_df = pd.DataFrame(active_rows)
        updated_df.to_csv(PORTFOLIO_FILE, index=False)
        print(f"[INFO] Updated active portfolio in '{PORTFOLIO_FILE}' (removed {len(closed_trades)} exited positions).")

if __name__ == "__main__":
    # Check if manual close command line argument is passed
    # Usage: python monitor.py --close SYMBOL [PRICE]
    if len(sys.argv) >= 3 and sys.argv[1] == "--close":
        sym = sys.argv[2]
        price = float(sys.argv[3]) if len(sys.argv) >= 4 else None
        manual_close_trade(sym, price)
    else:
        monitor_portfolio()
