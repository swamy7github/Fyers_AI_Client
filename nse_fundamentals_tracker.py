import sqlite3
import pandas as pd
import requests
import io
import yfinance as yf
import time
import json
from datetime import datetime

DB_NAME = "nse_fundamentals.db"
NSE_CSV_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

def init_db():
    """Initializes the SQLite database and creates necessary tables."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Create table to store the raw JSON data instead of specific columns
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fundamentals_raw (
            symbol TEXT PRIMARY KEY,
            raw_info TEXT,
            last_updated TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def fetch_nse_symbols():
    """Fetches the latest active equity symbols directly from the NSE website."""
    print("Fetching latest symbols from NSE...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/csv"
    }
    
    try:
        response = requests.get(NSE_CSV_URL, headers=headers, timeout=10)
        response.raise_for_status()
        df = pd.read_csv(io.StringIO(response.text))
        
        # Filter for normal equities (Series == 'EQ')
        # Note: CSV headers from NSE sometimes have leading spaces, so we strip them
        df.columns = df.columns.str.strip()
        equity_df = df[df['SERIES'].str.strip() == 'EQ']
        
        symbols = equity_df['SYMBOL'].tolist()
        print(f"Successfully fetched {len(symbols)} active NSE equity symbols.")
        return symbols
    except Exception as e:
        print(f"Error fetching symbols from NSE: {e}")
        return []

def update_fundamentals(symbols, limit=None):
    """
    Fetches fundamental data from Yahoo Finance and updates the database.
    :param symbols: List of NSE symbols (without .NS)
    :param limit: Integer to limit the number of stocks processed (useful for testing)
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    symbols_to_process = symbols[:limit] if limit else symbols
    total = len(symbols_to_process)
    
    print(f"Starting fundamental data fetch for {total} stocks...")
    
    for idx, symbol in enumerate(symbols_to_process, 1):
        yf_symbol = f"{symbol}.NS"
        print(f"[{idx}/{total}] Fetching data for {yf_symbol}...", end=" ", flush=True)
        
        try:
            ticker = yf.Ticker(yf_symbol)
            info = ticker.info
            
            # Serialize the entire Yahoo Finance dictionary into a JSON string
            info_json = json.dumps(info)
            
            # Upsert into database
            cursor.execute('''
                INSERT OR REPLACE INTO fundamentals_raw 
                (symbol, raw_info, last_updated)
                VALUES (?, ?, ?)
            ''', (
                symbol, info_json, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
            conn.commit()
            print("Success")
            
        except Exception as e:
            print(f"Failed. Reason: {e}")
            
        # Add a small delay to prevent rate-limiting by Yahoo Finance
        time.sleep(0.5)
        
    conn.close()
    print("Database update complete.")

def get_database_as_dataframe(requested_fields=None):
    """
    Retrieves fundamental data from the database into a Pandas DataFrame.
    :param requested_fields: List of string keys to extract from the JSON (e.g., ['marketCap', 'trailingPE']).
                             If None, it extracts every available key.
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Fetch from the new table
    try:
        cursor.execute("SELECT symbol, raw_info, last_updated FROM fundamentals_raw")
        rows = cursor.fetchall()
    except sqlite3.OperationalError:
        print("Table not found. Please run init_db() and update_fundamentals() first.")
        return pd.DataFrame()
    finally:
        conn.close()
    
    data = []
    for row in rows:
        symbol = row[0]
        try:
            # Parse the JSON string back into a Python dictionary
            info = json.loads(row[1])
        except json.JSONDecodeError:
            info = {}
            
        last_updated = row[2]
        
        # Always include base identifiers
        extracted_row = {'symbol': symbol, 'last_updated': last_updated}
        
        # If user specified fields, extract only those
        if requested_fields:
            for field in requested_fields:
                extracted_row[field] = info.get(field, None)
        else:
            # If no fields specified, dump the entire dictionary into the DataFrame columns
            extracted_row.update(info)
            
        data.append(extracted_row)
        
    return pd.DataFrame(data)

if __name__ == "__main__":
    # 1. Initialize the database (Creates file if it doesn't exist)
    init_db()
    
    # 2. Fetch latest symbols
    nse_symbols = fetch_nse_symbols()
    
    if nse_symbols:
        # 3. Update fundamentals in the DB
        # WARNING: We are setting limit=10 for demonstration.
        update_fundamentals(nse_symbols, limit=1)
        
        # 4. Retrieve specific parameters from the JSON database
        print("\n--- Retrieving Data from Database ---")
        
        # Define the exact Yahoo Finance dictionary keys you want to extract
        desired_parameters = [
            'longName', 'sector', 'industry', 'marketCap', 
            'trailingPE', 'priceToBook', 'returnOnEquity', 
            'debtToEquity', 'dividendYield', 'beta', 'fiftyTwoWeekHigh'
        ]
        
        # Pass the desired parameters to the function
        df = get_database_as_dataframe(requested_fields=desired_parameters)
        
        # Example Analysis
        if not df.empty:
            # Filter out empty market caps
            valid_df = df[df['marketCap'] > 0].copy()
            
            # Convert market cap to Crores for easier Indian reading
            valid_df['market_cap_cr'] = valid_df['marketCap'] / 10000000 
            
            # Sort by lowest P/E ratio
            value_stocks = valid_df.sort_values(by='trailingPE').head(10)
            
            print("\nTop Processed Stocks by Lowest P/E:")
            # Displaying the dynamically extracted columns
            print(value_stocks[['symbol', 'longName', 'trailingPE', 'market_cap_cr', 'beta']].to_string(index=False))
        else:
            print("Database is currently empty.")