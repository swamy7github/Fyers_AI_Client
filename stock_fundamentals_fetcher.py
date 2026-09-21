import sqlite3
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import screener_in_fundmntls
from utilities import get_nifty_stock_universe
DB_NAME = "./data/nse_universe_fundamentals.sqlite"
NSE_ALL_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_250_URL = "https://archives.nseindia.com/content/indices/ind_niftylargemidcap250list.csv"
NSE_LMS500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500LargeMidSmallEqualCapWeighted_list.csv"


class StockFundamentalsDB:
    """
    A robust database module for managing stock fundamental metrics.
    Handles connections, schema setup, and queries internally.
    """
    
    def __init__(self, db_path: str = DB_NAME):
        """
        Initializes the database connection.
        
        Args:
            db_path (str): Path to the SQLite database file. 
                           Defaults to ':memory:' for transient storage.
        """
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        self._create_tables()
        self.screener = screener_in_fundmntls.ScreenerClient()
     
    def __del__(self):
        self.close()
        
    def _create_tables(self):
        """Creates necessary tables if they do not exist."""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS fundamentals_raw (
                symbol TEXT PRIMARY KEY,
                raw_info TEXT,
                last_updated TEXT
            )
        ''')
        self.conn.commit()

    def get_update_fundamentals(self, symbol: str):
        """
        Upserts (Inserts or Replaces) a stock's fundamental metrics.
        
        Args:
            symbol (str): The stock ticker symbol.
            metrics (Dict[str, Any]): Dictionary containing the fundamental metrics.
            
        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            dp_data = self.get_fundamentals(symbol=symbol)
            
            if len(dp_data) != 0 and len(dp_data[0]) > 2:
                print(f"===========>len {len(dp_data[0])} ------ {dp_data[0]['last_updated']}")                
                lastupdated = dp_data[0]['last_updated']
                last_updated_time = datetime.strptime(lastupdated, "%Y-%m-%d %H:%M:%S")
                # Check if the difference is less than 7 days
                print(f" ==> {datetime.now()},{last_updated_time},{timedelta(days=7)}")
                if datetime.now() - last_updated_time < timedelta(days=7):
                    print(f"Data is fresh. Last updated on: {lastupdated}")
                return dp_data[0]["metrics"]
                
            metrics = self.screener.get_company_ratios(symbol)
            print(f"[INFO] metrics ---> {metrics}")
            # 1. Define the key mapping
            key_mapping = {
                "Stock P/E": "pe",
                "ROCE": "roec",
                "ROE": "roe",
                "comp_sales_grt": "comp_sales_grt_3y",
                "comp_profit_grt": "comp_profit_grt_3y",
                "stock_CAGR": "stock_CAGR_3y",
                "return_on_equity": "return_on_equity_3y",
            }
            # 2. Rename keys using a dictionary comprehension
            # It replaces the key if it's in the mapping, otherwise keeps the original key
            updated_metrics = {
                key_mapping.get(k, k): v for k, v in metrics.items()
            }

            info_json = json.dumps(updated_metrics)
            
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            self.cursor.execute('''
                INSERT OR REPLACE INTO fundamentals_raw 
                (symbol, raw_info, last_updated)
                VALUES (?, ?, ?)
            ''', (symbol, info_json, current_time))
            self.conn.commit()
            
            print(f"Successfully updated fundamentals for {symbol}")
            return updated_metrics
            
        except Exception as e:
            print(f"Error updating database for {symbol}: {e}")
            return {}

    def get_fundamentals(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieves fundamental metrics from the database.
        
        Args:
            symbol (str, optional): The specific stock ticker to fetch. 
                                    If None, fetches all records.
                                    
        Returns:
            List[Dict[str, Any]]: A list of structured dictionaries with database records.
        """
        results = []
        try:
            if symbol:
                self.cursor.execute(
                    "SELECT symbol, raw_info, last_updated FROM fundamentals_raw WHERE symbol = ?", 
                    (symbol,)
                )
            else:
                self.cursor.execute(
                    "SELECT symbol, raw_info, last_updated FROM fundamentals_raw"
                )
                
            rows = self.cursor.fetchall()
            print(f' len = {len(rows)}')
            for row in rows:
                db_symbol, raw_info_str, last_updated = row
                metrics_dict = json.loads(raw_info_str)
                
                results.append({
                    'symbol': db_symbol,
                    'metrics': metrics_dict,
                    'last_updated': last_updated
                })
            print(f"get_fundamentals = {results} ")
               
        except Exception as e:
            print(f"Error fetching from database: {e}")
            
        return results

    def close(self):
        """Closes the database connection cleanly."""
        self.conn.close()

if __name__ == "__main__":
    # Example of how you would use this module in another file:
    # from database_operations import StockFundamentalsDB
    
    # 1. Initialize the module (using in-memory DB for demo)
    
    db = StockFundamentalsDB()
    
    # 2. Prepare the sample data
    sample_metrics = {
        'market_cap': 50000.5,
        'pe': 25.4,
        'pb': 3.2,
        'roe': 15.6,
        'roce': 18.1,
        'de': 0.4,
        'comp_sales_grt': 12.5,
        'comp_profit_grt': 20.0,
        'stock_CAGR': 14.2,
        'return_on_equity': 15.6 
    }
    
    nse_symbols = get_nifty_stock_universe(session=db.screener.session, url=NSE_ALL_URL)  # get_nifty_stock_universe(url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv")
    print(f"[INFO] {len(nse_symbols)} stocks to be checked ")
    
    for symbol in nse_symbols:
        martics = db.get_update_fundamentals(symbol)
        print(f"===>{martics}")
    
    #print(f"--- Testing Module Updates ---{db.get_update_fundamentals("HAL")}")
    #db.get_update_fundamentals("HAL")
    #print(f"--- Testing Module Updates ---{db.get_update_fundamentals("BEL")}")
    # Intentionally updating a second one to show retrieval of all records
    sample_metrics['market_cap'] = 25000.0
    #db.get_update_fundamentals("BEL")
    sym = ['HAL', 'BEL']
    for i in sym:
        print("\n--- Testing Module Retrieval ---")
        fetched_data = db.get_fundamentals(i)
        
        for item in fetched_data:
            print(f"\nSymbol: {item['symbol']} (Updated: {item['last_updated']})")
            print(f"{item}")
            
    # 3. Clean up
    db.close()