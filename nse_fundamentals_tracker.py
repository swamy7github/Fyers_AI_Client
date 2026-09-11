import sqlite3
import pandas as pd
import requests
import io
# Try to import yfinance strictly as a secondary fallback for price series if Fyers API is not configured
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
import time
import json
import os
import smtplib
import re
from datetime import datetime, timedelta
import numpy as np
import requests
from bs4 import BeautifulSoup

DB_NAME = "nse_fundamentals.db"
NSE_CSV_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

# =============================================================================
# 2. SCREENER.IN FUNDAMENTAL DATA ENGINE (REPLACES YFINANCE)
# =============================================================================


def parse_roe_table(table):
    """
    Parses the Return on Equity (ROE) table from Screener.in and extracts key metrics.
    :param table: BeautifulSoup object representing the ROE table.
    :return: Dictionary containing extracted ROE metrics."""
    
    extracted_data = {}
    
    if table:
        rows = table.find_all('tr')
        
        # We start from index 1 because the first row (index 0) is the header "Return on Equity"
        for row in rows[1:]:
            cols = row.find_all('td')
            
            if len(cols) == 2:
                key = cols[0].text.strip().replace(':', '')
                value = cols[1].text.strip()
                
                extracted_data[key] = value
                
    return extracted_data


def fetch_fundamental_data_screener(symbol):
    """
    Fetches company fundamental metrics directly from Screener.in using BeautifulSoup.
    Parses key financial ratios: P/E, Price to Book, ROE, ROCE, Debt to Equity, OPM %, and Sales Growth.
    Includes grounded fallback data if offline or rate-limited.
    """
    # Clean symbol mapping for Screener URL slugs
    clean_symbol = symbol.replace("&", "").replace("-", "").upper()
    
    urls = [
        f"https://www.screener.in/company/{clean_symbol}/consolidated/",
        f"https://www.screener.in/company/{clean_symbol}/"
    ]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.screener.in/"
    }
    
    scraped_data = {}
    
    for url in urls:
        try:
            response = requests.get(url, headers=headers, timeout=8)
            if response.status_code == 200:
                print(f"[INFO] Successfully fetched data from Screener.in for {symbol} at {url}")
                soup = BeautifulSoup(response.text, 'html.parser')
                #print(f"[INFO] Parsing fundamental data for {soup}...")
                
                # 1. Parse Top Ratios section (#top-ratios li)
                ratio_items = soup.find_all('li', class_=lambda c: c and 'flex' in c)
                
                parsed_ratios = {}
                for item in ratio_items:
                    name_span = item.find('span', class_='name')
                    val_span = item.find('span', class_='number') or item.find('span', class_='value')
                    if name_span and val_span:
                        name_text = name_span.text.strip().lower()
                        val_text = val_span.text.strip().replace(',', '')
                        parsed_ratios[name_text] = val_text
                print(f"[DEBUG] Found ratio: { parsed_ratios}")        
                # Extract Top Ratios
                market_cap = float(parsed_ratios.get('market cap', 0) or 0)
                cmp_val = float(parsed_ratios.get('current price', 0) or 0)
                book_val = float(parsed_ratios.get('book value', 0) or 0)
                pe_val = float(parsed_ratios.get('stock p/e', 0) or 0)
                roce_val = float(parsed_ratios.get('roce', 0) or 0)
                roe_val = float(parsed_ratios.get('roe', 0) or 0)
                de_val = float(parsed_ratios.get('debt to equity', 0) or 0)                
                pb_val = round(cmp_val / book_val, 2) if (cmp_val and book_val) else 2.5
                
                # 2. Extract Operating Profit Margin (OPM %) & Sales Growth from Tables
                margin_val = 15.0
                growth_val = 12.0
                
                # Search Profit & Loss or Quarters table for OPM % and Sales Growth
                tables = soup.find_all('table', class_='ranges-table')
        
                for table in tables:
                    text_content = table.text
                    if not text_content:
                        print(f"[DEBUG] Skipping empty table: {table}")
                        continue
                    if "Return on Equity" in text_content:
                        return_on_equity = float(parse_roe_table(table).get("3 Years").split("%")[0])
                        print(f"Treturn_on_equity: {return_on_equity}...")  # Print first 100 chars for brevity
                    elif "Stock Price CAGR" in text_content:
                        stock_CAGR = float(parse_roe_table(table).get("3 Years").split("%")[0])
                        print(f"Stock Price CAGR: {stock_CAGR}...")  # Print first 100 chars for brevity
                    elif "Compounded Profit Growth" in text_content:
                        comp_profit_grt = float(parse_roe_table(table).get("3 Years").split("%")[0])
                        print(f"Compounded Profit Growth: {comp_profit_grt}...")  # Print first 100 chars for brevity
                    elif "Compounded Sales Growth" in text_content:
                        comp_sales_grt = float(parse_roe_table(table).get("3 Years").split("%")[0])
                        print(f"Compounded Sales Growth: {comp_sales_grt}...")  # Print first 100 chars for brevity
                                                                                                
                    '''
                    if "OPM %" in text_content or "Sales" in text_content:
                        rows = table.find_all('tr')
                        for row in rows:
                            row_title = row.find('td', class_='title') or row.find('td')
                            if row_title:
                                t_text = row_title.text.strip().lower()
                                if 'opm %' in t_text or 'operating profit margin' in t_text:
                                    cells = row.find_all('td')
                                    if len(cells) > 1:
                                        last_val = cells[-1].text.strip().replace('%', '').replace(',', '')
                                        try: margin_val = float(last_val)
                                        except ValueError: pass
                                elif 'sales growth' in t_text or 'compounded sales growth' in t_text:
                                    cells = row.find_all('td')
                                    print(f"[DEBUG] Found Sales Growth row: {cells}")
                                    if len(cells) > 1:
                                        last_val = cells[-1].text.strip().replace('%', '').replace(',', '')
                                        try: growth_val = float(last_val)
                                        except ValueError: pass
                    '''
                scraped_data = {
                    "market_cap": round(market_cap, 2),
                    "pe": round(pe_val, 2),
                    "pb": round(pb_val, 2),
                    "roe": round(roe_val, 2),
                    "roce": round(roce_val, 2),
                    "de": round(de_val, 2),
                    "comp_sales_grt": round(comp_sales_grt, 2),
                    "comp_profit_grt": round(comp_profit_grt, 2),
                    "stock_CAGR": round(stock_CAGR, 2),
                    "return_on_equity": round(return_on_equity, 2)
                }
                
                if scraped_data.get("pe") > 0 or scraped_data.get("roe") > 0:
                    print(f"[SUCCESS] Screener.in parsed fundamentals for {symbol}: {scraped_data}")
                    return scraped_data
        except Exception as e:
            print(f"[WARNING] Failed to fetch or parse Screener.in data for {symbol} at {url}: {e}")
            continue
    return {}
'''
    # High-fidelity Grounded Fallback Database for Nifty constituents
    grounded_screener_db = {
        "ADANIPORTS": {"pe": 28.5, "pb": 4.2, "roe": 16.8, "roce": 15.2, "de": 0.85, "margin": 24.5, "rev_growth": 18.2},
        "SHRIRAMFIN": {"pe": 15.4, "pb": 2.1, "roe": 18.2, "roce": 19.5, "de": 1.10, "margin": 14.2, "rev_growth": 18.5},
        "ACME": {"pe": 34.2, "pb": 4.5, "roe": 12.8, "roce": 11.2, "de": 0.85, "margin": 8.4, "rev_growth": 25.0},
        "WIPRO": {"pe": 13.22, "pb": 2.10, "roe": 15.11, "roce": 17.84, "de": 0.12, "margin": 16.2, "rev_growth": -2.1},
        "NATIONALUM": {"pe": 11.45, "pb": 1.78, "roe": 26.83, "roce": 27.46, "de": 0.00, "margin": 16.3, "rev_growth": 90.8},
        "WAAREE": {"pe": 45.10, "pb": 12.40, "roe": 38.50, "roce": 85.38, "de": 0.12, "margin": 18.5, "rev_growth": 237.5},
        "BEL": {"pe": 32.40, "pb": 6.80, "roe": 27.55, "roce": 28.10, "de": 0.00, "margin": 18.2, "rev_growth": 19.4},
        "TATASTEEL": {"pe": 22.40, "pb": 1.85, "roe": 8.45, "roce": 9.10, "de": 0.95, "margin": 7.8, "rev_growth": 5.2},
        "AUROPHARMA": {"pe": 16.20, "pb": 1.95, "roe": 12.40, "roce": 14.10, "de": 0.15, "margin": 11.4, "rev_growth": 9.8},
        "KOTAKBANK": {"pe": 19.10, "pb": 2.80, "roe": 14.50, "roce": 13.80, "de": 0.90, "margin": 18.1, "rev_growth": 15.4},
        "TECHM": {"pe": 28.50, "pb": 4.10, "roe": 17.50, "roce": 23.10, "de": 0.05, "margin": 12.3, "rev_growth": 28.45},
        "HAL": {"pe": 38.40, "pb": 8.90, "roe": 24.30, "roce": 26.20, "de": 0.00, "margin": 21.4, "rev_growth": 14.2},
        "DIVISLAB": {"pe": 54.20, "pb": 8.10, "roe": 15.40, "roce": 17.50, "de": 0.00, "margin": 28.2, "rev_growth": 11.8},
    }
    
    if symbol in grounded_screener_db:
        return grounded_screener_db[symbol]
        
    return {"pe": 22.0, "pb": 2.5, "roe": 14.0, "roce": 15.0, "de": 0.50, "margin": 12.0, "rev_growth": 10.0}
'''


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
    temp = fetch_fundamental_data_screener("TATASTEEL")  # Example call to fetch Screener.in data for a specific symbol
    print(f"Sample Screener.in data for TATASTEEL: {temp}")
    exit()

    if nse_symbols:
        # 3. Update fundamentals in the DB
        # WARNING: We are setting limit=10 for demonstration.
        update_fundamentals(nse_symbols, limit=None)  # Set limit=None to process all symbols, or a number for testing
        
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
