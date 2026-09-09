import json
import time

try:
    import yfinance as yf
    import requests_cache
    from requests import Session
    from requests_ratelimiter import LimiterSession
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    import sqlite3
    import pandas as pd
    import requests
    import io
    import time
    from datetime import datetime

except ImportError:
    print("Please install required libraries using:")
    print("pip install yfinance requests-cache requests-ratelimiter")
    exit()
    
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

def create_safe_session():
    """
    Creates a robust requests session with caching, rate limiting, and automatic retries.
    This prevents HTTP 429 Too Many Requests errors from Yahoo Finance.
    """
    # 1. Setup caching: Store API responses for 1 day
    # Fundamental data doesn't change rapidly, so caching is highly effective.
    # It creates a local SQLite database named 'yfinance_cache.sqlite'
    session = requests_cache.CachedSession('yfinance_cache', expire_after=86400)
    
    # 2. CRITICAL FIX: Yahoo Finance aggressively blocks requests without a standard User-Agent.
    # We must pretend to be a standard web browser (Chrome).
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    })
    
    # 3. Setup Retries: Automatically retry on 429 (Too Many Requests) and 50X errors
    retry = Retry(
        total=5,              # Total number of retries
        read=5,               # Number of read retries
        connect=5,            # Number of connection-related retries
        backoff_factor=1.5,   # Exponential backoff multiplier
        status_forcelist=[429, 500, 502, 503, 504], # Retry on these specific HTTP status codes
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    print("Safe session created with caching, rate limiting, and retries.")
    return session


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
    
def update_fundamentals_sqlite_db(symbols, custom_session=create_safe_session(), limit=None):
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
            ticker = yf.Ticker(yf_symbol, session=custom_session)
            info = ticker.info
            # Check if the info dictionary is empty (can happen if the symbol is invalid or heavily rate-limited)
            if not info:
                print(f"Warning: No data returned for {symbol}. Verify the ticker symbol.")
                return None
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
            time.sleep(1)  # 1 second delay between requests
            continue  # Continue to the next symbol even if one fails
            
        # Add a small delay to prevent rate-limiting by Yahoo Finance
        time.sleep(1)  # 1 second delay between requests
        
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


'''    
def get_indian_stock_fundamentals(symbol, custom_session=None):
    """
    Fetches fundamental data for an Indian stock using Yahoo Finance.
    Symbol should be the NSE ticker (e.g., 'RELIANCE').
    """
    # Append .NS to the symbol to fetch from the National Stock Exchange of India
    yahoo_symbol = f"{symbol}.NS"
    print(f"Fetching fundamental data for {yahoo_symbol}...")
    
    # Initialize ticker with our custom, safe session
    ticker = yf.Ticker(yahoo_symbol, session=custom_session)
    
    try:
        # Get all info dictionary
        info = ticker.info
        
        # Check if the info dictionary is empty (can happen if the symbol is invalid or heavily rate-limited)
        if not info:
            print(f"Warning: No data returned for {symbol}. Verify the ticker symbol.")
            return None

        # Extracting key fundamental metrics safely using .get()
        fundamentals = {
            "Company Name": info.get("longName", "N/A"),
            "Sector": info.get("sector", "N/A"),
            "Industry": info.get("industry", "N/A"),
            "Market Cap (₹)": info.get("marketCap", "N/A"),
            "Current Price (₹)": info.get("currentPrice", "N/A"),
            "52 Week High (₹)": info.get("fiftyTwoWeekHigh", "N/A"),
            "52 Week Low (₹)": info.get("fiftyTwoWeekLow", "N/A"),
            
            # Valuation Metrics
            "Trailing P/E": info.get("trailingPE", "N/A"),
            "Forward P/E": info.get("forwardPE", "N/A"),
            "Price to Book (P/B)": info.get("priceToBook", "N/A"),
            "PEG Ratio": info.get("pegRatio", "N/A"),
            
            # Profitability & Margins
            "Return on Equity (ROE) %": info.get("returnOnEquity", 0) * 100 if info.get("returnOnEquity") else "N/A",
            "Return on Assets (ROA) %": info.get("returnOnAssets", 0) * 100 if info.get("returnOnAssets") else "N/A",
            "Operating Margin %": info.get("operatingMargins", 0) * 100 if info.get("operatingMargins") else "N/A",
            "Profit Margin %": info.get("profitMargins", 0) * 100 if info.get("profitMargins") else "N/A",
            
            # Financial Health & Debt
            "Total Debt (₹)": info.get("totalDebt", "N/A"),
            "Debt to Equity": info.get("debtToEquity", "N/A"), # Usually represented as a percentage in yf
            "Current Ratio": info.get("currentRatio", "N/A"),
            
            # Dividends
            "Dividend Yield %": info.get("dividendYield", 0) * 100 if info.get("dividendYield") else "N/A",
            
            # Growth (YoY)
            "Revenue Growth %": info.get("revenueGrowth", 0) * 100 if info.get("revenueGrowth") else "N/A",
            "Earnings Growth %": info.get("earningsGrowth", 0) * 100 if info.get("earningsGrowth") else "N/A"
        }
        
        return fundamentals
        
    except Exception as e:
        print(f"Error fetching data for {symbol}: {e}")
        return None
'''
if __name__ == "__main__":
    # Create the safe, rate-limited, cached session once before starting the loop
    #safe_session = create_safe_session()

    # 1. Initialize the database (Creates file if it doesn't exist)
    init_db()
    
    # 2. Fetch latest symbols
    nse_symbols = fetch_nse_symbols()
    
    print(f"Processing {len(nse_symbols)} ...")  # Displaying a sample of symbols for brevity
    
    if nse_symbols:
        # 3. Update fundamentals in the DB
        # WARNING: We are setting limit=10 for demonstration.
        update_fundamentals_sqlite_db(nse_symbols, limit=1)
        
        # 4. Retrieve specific parameters from the JSON database
        print("\n--- Retrieving Data from Database ---")
        
        # Define the exact Yahoo Finance dictionary keys you want to extract
        desired_parameters = [
            'longName', 'sector', 'industry', 'marketCap', 
            'trailingPE', 'priceToBook', 'returnOnEquity', 
            'debtToEquity', 'dividendYield', 'beta', 'fiftyTwoWeekHigh'
        ]
        #(symbol, company_name, sector, industry, market_cap, pe_ratio, pb_ratio, roe, debt_to_equity, dividend_yield, last_updated)
        
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
    '''

    for i, stock_symbol in enumerate(stock_symbols):
        data = get_indian_stock_fundamentals(stock_symbol, custom_session=safe_session)
        
        if data:
            print(f"\n--- FUNDAMENTAL DATA FOR {stock_symbol} ---")
            for key, value in data.items():
                # Formatting numeric values for better readability
                if isinstance(value, float):
                    print(f"{key:25}: {value:,.2f}")
                elif isinstance(value, int) and "Cap" in key or "Debt" in key:
                    # Format large numbers like Market Cap and Debt with commas
                    print(f"{key:25}: ₹ {value:,}")
                else:
                    print(f"{key:25}: {value}")
            
            # Fetching recent quarterly earnings
            print("\n--- RECENT QUARTERLY EARNINGS ---")
            ticker = yf.Ticker(f"{stock_symbol}.NS", session=safe_session)
            try:
                # You can also access balance_sheet, cashflow, etc.
                income_stmt = ticker.quarterly_income_stmt
                if not income_stmt.empty:
                    # Display the first few rows (like Total Revenue, Net Income)
                    print(income_stmt.head())
                else:
                    print("No recent quarterly earnings found.")
            except Exception as e:
                print(f"Error fetching earnings: {e}")
        
        # Hard delay to respect Yahoo Finance's strict limits.
        # We only sleep if it's not the last item in the list.
        if i < len(stock_symbols) - 1:
            print(f"\nWaiting 3 seconds before next request to avoid rate limits...")
            time.sleep(3)
        '''