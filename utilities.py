import pandas as pd
import requests
import requests_cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import sqlite3
import os
import csv
import json
import io

NSE_ALL_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"


def create_safe_session():  # is taken from safe_session_scraper.py
    """
    Creates a robust requests session with caching, rate limiting, and automatic retries.
    Optimized to handle connection drops, closed sockets, and fallback to stale cache.
    """
    
    # 1. Setup caching: Store API responses for 1 day
    # OPTIMIZATION: 'stale_if_error=True' ensures that if the socket closes 
    # or the network drops completely, the session will return the expired 
    # cached response rather than failing entirely.
    session = requests_cache.CachedSession(
        'http_internet_cache',
        expire_after=86400,
        stale_if_error=True 
    )
    
    # 2. CRITICAL FIX: Yahoo Finance aggressively blocks requests without a standard User-Agent.
    # We pretend to be a standard web browser (Chrome).
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    })
    
    # 3. Setup Retries: Automatically retry on 429, 50X errors, and dropped sockets
    retry = Retry(
        total=7,              # Total number of retries (increased for resilience)
        read=5,               # OPTIMIZATION: Retries if the socket closes mid-read (IncompleteRead)
        connect=5,            # OPTIMIZATION: Retries if the connection is refused/dropped initially
        backoff_factor=1.5,   # Exponential backoff multiplier (1.5s, 3.0s, 6.0s...)
        status_forcelist=[429, 500, 502, 503, 504], # Retry on these specific HTTP status codes
        allowed_methods=["HEAD", "GET", "OPTIONS"], # Explicitly allow retries on safe methods
        respect_retry_after_header=True # OPTIMIZATION: Respect 'Retry-After' header on 429 errors
    )
    
    # 4. Configure HTTP Adapter with connection pooling
    # OPTIMIZATION: pool_connections and pool_maxsize manage connection reuse gracefully.
    # If a socket drops, the pool will automatically discard it and create a new one.
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=20,  # Number of connection pools to cache
        pool_maxsize=20       # Maximum number of connections to save in the pool
    )
    
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    print("Safe session created: Caching (with stale fallback), Connection Pooling, and Resilient Retries enabled.")
    return session


def fetch_http_response(session, url=None):
    """
    Fetches the HTTP response from a given URL.
    """
    if url:
        try:
            if session:
                response = session.get(url, timeout=10)
                response.raise_for_status()  # Check for HTTP errors
                return response.text
            else:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                }
                response = requests.get(url, headers=headers, timeout=10)
                response.raise_for_status()  # Check for HTTP errors
                return response.text
        except requests.exceptions.RequestException as e:
            print(f"Error fetching URL: {e}")
            return None

def parse_response_text(response_text):
    """
    Parses the response text based on its format (JSON, plain text, etc.).
    This function handles JSON responses, assuming an array of objects.
    """
    try:
        data = json.loads(response_text)
        # Check if the parsed JSON is a list of dictionaries (tabular format)
        if isinstance(data, list) and all(isinstance(item, dict) for item in data):
            return data
        # If it's a single dictionary, wrap it in a list
        elif isinstance(data, dict):
            return [data]
        else:
            print("The JSON format is not supported for tabular CSV conversion.")
            return None
    except json.JSONDecodeError:
        if ',' in response_text and '\n' in response_text:
            print("Detected comma-separated text. Parsing as tabular CSV data...")
            # Using skipinitialspace to handle spaces after commas in headers like ' SERIES'
            reader = csv.DictReader(io.StringIO(response_text.strip()), skipinitialspace=True)
            print(f"=== {reader}") 
            return list(reader)
            
        print("Failed to decode JSON. The response might not be in JSON format.")
        # Fallback for plain text: split into lines
        lines = response_text.strip().split('\n')
        # Create a simple single-column data structure
        return [{'TextData': line} for line in lines if line.strip()]

def read_from_csv(filename):
    """
    Reads data from a CSV file and returns it as a dictionary of lists.
    """
    print(f"\n--- Reading data back from {filename} ---")
    columns_dict = {}
    
    try:
        with open(filename, mode='r', encoding='utf-8') as file:
            # Use DictReader to map information to dictionary keys
            reader = csv.DictReader(file)
            
            if reader.fieldnames:
                # Initialize empty lists for each column header
                columns_dict = {header: [] for header in reader.fieldnames}
            
            for row in reader:
                for key, value in row.items():
                    if key in columns_dict:
                        columns_dict[key].append(value)
                        
        return columns_dict
    except FileNotFoundError:
        print(f"Error: The file {filename} was not found.")
        return None
    except Exception as e:
        print(f"Error reading from file {filename}: {e}")
        return None
    
    
def save_to_csv(data, filename):
    """
    Saves the parsed data (a list of dictionaries) to a CSV file.
    """
    if not data:
        print("No data to save.")
        return

    try:
        # Extract headers from the keys of the first dictionary
        headers = data[0].keys()

        with open(filename, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=headers)
            writer.writeheader()
            
            # Write all rows
            for row in data:
                writer.writerow(row)
                
        print(f"Data successfully saved to {filename}")
    except IOError as e:
        print(f"Error writing to file {filename}: {e}")
    except Exception as e:
         print(f"An unexpected error occurred during CSV saving: {e}")


def get_url_response_save(session, url, output_filename):
    # Example URL (Returns JSON data: a list of user objects)
    
    print(f"Fetching data from: {url}")
    
    # 1. Fetch the HTTP response
    response_text = fetch_http_response(session, url)
    
    if response_text:
        # 2. Parse the response text into a tabular format
        print("Parsing response data...")
        parsed_data = parse_response_text(response_text)
        
        # 3. Store the parsed data in a CSV file
        if parsed_data:
             save_to_csv(parsed_data, output_filename)
             print(f" saving the dowload file to {output_filename}")
        else:
             print("Could not parse data into a format suitable for CSV.")
    else:
        print("Failed to retrieve the HTTP response.")


def export_sqlite_to_csv(db_path, output_dir="."):
    """
    Connects to a SQLite database and exports every table to a separate CSV file.
    Specifically handles binary BLOBs (like requests_cache payloads) safely.
    """
    if not os.path.exists(db_path):
        print(f"Error: Database file '{db_path}' not found.")
        print("Make sure you have run the scraper to generate the cache first.")
        return

    # Ensure the output directory exists
    os.makedirs(output_dir, exist_ok=True)

    conn = None
    try:
        #        print(f"Connecting to database: {db_path}")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        #        # Query the sqlite_master table to find all user-created tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()

        if not tables:
            print(f"No tables found in '{db_path}'. The database might be empty.")
            return

        for table_name in tables:
            table_name = table_name[0]
            output_file = os.path.join(output_dir, f"{table_name}.csv")
            print(f"Exporting table '{table_name}' to {output_file}...")
            
            # Fetch all rows from the current table
            cursor.execute(f"SELECT * FROM {table_name}")

            # Extract column headers from the cursor description
            headers = [description[0] for description in cursor.description]

            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(headers) # Write the header row

                # Iterate through rows and clean up binary data
                for row in cursor.fetchall():
                    processed_row = []
                    for item in row:
                        # requests_cache stores the HTTP response as a pickled binary BLOB.
                        # Writing raw bytes to a CSV will corrupt it, so we replace it with a placeholder.
                        if isinstance(item, bytes):
                            processed_row.append("<Binary Data / BLOB>")
                        else:
                            processed_row.append(item)
                    
                    writer.writerow(processed_row)

        print(f"\nSuccess! All tables have been exported to the '{output_dir}' directory.")

    except sqlite3.Error as e:
        print(f"An SQLite error occurred: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        if conn:
            conn.close()

    
def get_nifty_stock_universe(session=None, url=None, csv_file_path=None):
    """
    Attempts to download the official Nifty list from NSE.
    If the live download fails, falls back to reading from a local CSV file 
    containing columns: "Company Name,Industry,Symbol,Series,ISIN Code".
    Returns a dictionary of {symbol: industry}.
    """
    print(f" args = 1 {session}  2{url} 3{csv_file_path}")
    if url:
        csv_file_path = f"./data/{os.path.basename(url.strip())}"
        print(f"====> {csv_file_path} ")
        print(f"URL ={url}")
        try:
           # 1. Fetch the HTTP response save to CSV
            get_url_response_save(session, url, csv_file_path)
            
        except Exception as e:
            print(f"[ERROR] Failed to write  {csv_file_path}: {str(e)}")
                
    nse_stock_symbols = {}    
    # Check if the fallback CSV file exists in the directory
    if csv_file_path and os.path.exists(csv_file_path):
        print(f"[INFO] Reading the csv file {csv_file_path}" )
        try:
            # Using csv.DictReader is safer because it maps the exact column headers you specified 
            # ("Company Name,Industry,Symbol,Series,ISIN Code") and handles commas inside company names safely.
             # DictReader maps the information in each row to a dict whose keys are the header names
            nse_stock_symbols = pd.read_csv(csv_file_path)
            
        except Exception as e:
            print(f"[ERROR] Failed to read from {csv_file_path}: {str(e)}")
    else:
        print(f"[ERROR] The local fallback file '{csv_file_path}' was not found in the current directory.")

    return nse_stock_symbols

def get_last_traded_day():
    from datetime import datetime
    import pytz
    import pandas_market_calendars as mcal

    # 1. Get today's date
    tz = pytz.timezone("Asia/Kolkata")
    today_str = datetime.now(tz).strftime("%Y-%m-%d")

    # 2. Fetch the market calendar (e.g., NYSE, NASDAQ, LSE)
    nyse = mcal.get_calendar('NSE')

    # 3. Look back slightly further than today to catch holidays/weekends
    schedule = nyse.schedule(start_date='2020-01-01', end_date=today_str)

    # 4. Get the last valid trading day before or including today
    # (Filter out today if the market hasn't closed/opened yet, or keep it if it's currently a trading day)
    valid_days = schedule.index.strftime('%Y-%m-%d').tolist()

    # If today is in the list but you specifically want the PREVIOUS completed session:
    #if today_str in valid_days:
    #    valid_days.remove(today_str)

    last_traded_str = valid_days[-1]
    return last_traded_str

if __name__ == "__main__":
    print(f"{get_last_traded_day()}")