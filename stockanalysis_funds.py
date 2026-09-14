import requests
from bs4 import BeautifulSoup
import json

class StockAnalysisScraper:
    def __init__(self):
        """
        Initializes the scraper with standard browser headers to bypass basic
        bot-protection (WAF) mechanisms used by financial websites.
        """
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1'
        }

    def get_statistics(self, symbol, exchange="nse"):
        """
        Fetches and extracts all fundamental statistics for a given symbol.
        
        Args:
            symbol (str): The stock ticker (e.g., 'HAL', 'RELIANCE').
            exchange (str): The exchange to query (default is 'nse'). Use 'BSE' for Bombay or leave blank for US stocks.
            
        Returns:
            dict: A dictionary containing all extracted statistical categories and metrics.
        """
        # Construct the target URL based on exchange and symbol
        if exchange.lower() in ['nse', 'bse']:
            url = f"https://stockanalysis.com/quote/{exchange.lower()}/{symbol.upper()}/statistics/"
        else:
            # For US stocks (e.g., AAPL, TSLA), the URL structure doesn't use the exchange prefix
            url = f"https://stockanalysis.com/quote/{symbol.upper()}/statistics/"
            
        print(f"[INFO] Fetching fundamental statistics from: {url}")
        
        try:
            response = requests.get(url, headers=self.headers, timeout=15)
            
            if response.status_code != 200:
                print(f"[ERROR] Failed to fetch data. HTTP Status Code: {response.status_code}")
                return None
                
            soup = BeautifulSoup(response.text, "html.parser")
            
            # This dictionary will hold all our extracted data
            all_statistics = {}
            
            # StockAnalysis uses <table> elements for their statistic grids.
            # We will find all tables and extract the rows.
            tables = soup.find_all("table")
            
            if not tables:
                print("[WARNING] No tables found on the page. The website structure might have changed.")
                return None
                
            for table in tables:
                # Try to find the section header (usually an h2 or h3 tag right before the table)
                # This helps us categorize the data (e.g., "Valuation Ratios", "Financial Position")
                category_name = "General Statistics"
                
                # Navigate up the DOM to find a relevant header for this specific table
                parent_div = table.find_parent("div")
                if parent_div:
                    header = parent_div.find_previous_sibling(["h2", "h3"])
                    if header:
                        category_name = header.get_text(strip=True)
                
                if category_name not in all_statistics:
                    all_statistics[category_name] = {}
                    
                # Loop through all rows in the current table
                rows = table.find_all("tr")
                for row in rows:
                    # Look for table data cells (td)
                    cols = row.find_all("td")
                    
                    # A valid statistics row usually has exactly 2 columns: Label and Value
                    if len(cols) == 2:
                        metric_name = cols[0].get_text(strip=True)
                        metric_value = cols[1].get_text(strip=True)
                        
                        # Store in our dictionary
                        if metric_name:
                            all_statistics[category_name][metric_name] = metric_value
                            
            print(f"[SUCCESS] Extracted {sum(len(v) for v in all_statistics.values())} data points across {len(all_statistics)} categories.")
            return all_statistics
            
        except Exception as e:
            print(f"[ERROR] An exception occurred while scraping: {e}")
            return None

if __name__ == "__main__":
    # Initialize the scraper
    scraper = StockAnalysisScraper()
    
    # Define the stock we want to look up
    TARGET_STOCK = "HAL"
    EXCHANGE = "nse"
    
    print("-" * 50)
    print(f"Starting Data Extraction for {TARGET_STOCK.upper()}...")
    print("-" * 50)
    
    # Call the extraction function
    stats_data = scraper.get_statistics(TARGET_STOCK, EXCHANGE)
    
    # Format and print the results beautifully
    if stats_data:
        print("\n=== EXTRACTED FUNDAMENTALS ===\n")
        
        # Iterate through the categories and their respective metrics
        for category, metrics in stats_data.items():
            print(f"[{category}]")
            
            for metric, value in metrics.items():
                # Format with fixed spacing so the colon aligns nicely
                print(f"  {metric:<40} : {value:>12}")
                
            print() # Empty line between categories
            
        # Optional: Save to a JSON file for later use!
        # with open(f"{TARGET_STOCK}_stats.json", "w") as f:
        #     json.dump(stats_data, f, indent=4)
        # print(f"\n[INFO] Data can also be saved to {TARGET_STOCK}_stats.json")