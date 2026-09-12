import os
import requests
from bs4 import BeautifulSoup
from utilities import create_safe_session
from time import sleep
class ScreenerClient:
    def __init__(self, session=create_safe_session(), username="ns.motog@gmail.com", password="Swamy@0684"):
        self.session = session
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.screener.in/login/"
        })
        self.username = username
        self.password = password
        self.is_authenticated = False
        self.login()
        
    def login(self):
        """
        Logs into Screener.in using CSRF token authentication.
        """
        login_url = "https://www.screener.in/login/"
        
        # Step 1: Fetch the login page to acquire the CSRF token
        try:
            response = self.session.get(login_url, timeout=10)
            if response.status_code != 200:
                print(f"[ERROR] Unable to reach Screener login page. HTTP {response.status_code}")
                return False

            soup = BeautifulSoup(response.text, "html.parser")
            csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
            
            # Extract CSRF token from input field or cookies
            csrf_token = csrf_input["value"] if csrf_input else self.session.cookies.get("csrftoken")
            
            if not csrf_token:
                print("[ERROR] Could not extract CSRF token from Screener login form.")
                return False

            # Step 2: POST credentials with CSRF middleware token
            payload = {
                "username": self.username,
                "password": self.password,
                "csrfmiddlewaretoken": csrf_token
            }

            post_response = self.session.post(login_url, data=payload, timeout=10)

            # Step 3: Verify session authentication
            if "sessionid" in self.session.cookies or "/dash/" in post_response.url or post_response.status_code == 200:
                self.is_authenticated = True
                print("[SUCCESS] Authenticated with Screener.in!")
                return True
            else:
                print("[ERROR] Screener.in authentication failed. Please check your username and password.")
                return False

        except Exception as e:
            print(f"[ERROR] Screener login exception: {str(e)}")
            return False
    

    def parse_roe_table(self, table):
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

    def get_company_ratios(self, symbol):
        """
        Fetches key financial metrics for a specific NSE symbol.
        """
        clean_symbol = symbol.replace("&", "").replace("-", "").strip().upper()
        sleep(2)
        url = f"https://www.screener.in/company/{clean_symbol}/consolidated/"
        
        response = self.session.get(url)
        if response.status_code == 404:
            # Fallback to standalone financials if consolidated is unavailable
            url = f"https://www.screener.in/company/{clean_symbol}/"
            response = self.session.get(url)

        if response.status_code != 200:
            print(f"[WARNING] Could not retrieve data for {symbol}. Status: {response.status_code}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        ratios = {'symbol': symbol}

        # Extract top ratios (<ul id="top-ratios">)
        ratio_items = soup.find_all("li", class_="flex flex-space-between")
        for item in ratio_items:
            name_elem = item.find("span", class_="name")
            val_elem = item.find("span", class_="number")
            if name_elem and val_elem:
                name = name_elem.text.strip()
                val = val_elem.text.strip().replace(",", "")
                ratios[name] = round(float(val), 2)
        # Search Profit & Loss or Quarters table for OPM % and Sales Growth
        tables = soup.find_all('table', class_='ranges-table')
        return_on_equity = 0
        stock_CAGR = 0
        comp_profit_grt = 0
        comp_sales_grt =0
        for table in tables:
            text_content = table.text
            if not text_content:
                print(f"[DEBUG] Skipping empty table: {table}")
                continue
            if "Return on Equity" in text_content:
                value_str = self.parse_roe_table(table).get("3 Years", "").split("%")[0]
                return_on_equity = float(value_str) if value_str != '' else 0.0
                print(f"Treturn_on_equity: {return_on_equity}...")  # Print first 100 chars for brevity
            elif "Stock Price CAGR" in text_content:
                value_str = self.parse_roe_table(table).get("3 Years", "").split("%")[0]
                stock_CAGR = float(value_str) if value_str != '' else 0.0
                print(f"Stock Price CAGR: {stock_CAGR}...")  # Print first 100 chars for brevity
            elif "Compounded Profit Growth" in text_content:
                value_str = self.parse_roe_table(table).get("3 Years", "").split("%")[0]
                comp_profit_grt = float(value_str) if value_str != '' else 0.0
                print(f"Compounded Profit Growth: {comp_profit_grt}...")  # Print first 100 chars for brevity
            elif "Compounded Sales Growth" in text_content:
                value_str = self.parse_roe_table(table).get("3 Years", "").split("%")[0]
                comp_sales_grt = float(value_str) if value_str != '' else 0.0
                print(f"Compounded Sales Growth: {comp_sales_grt}...")  # Print first 100 chars for brevity
        ratios.update({"comp_sales_grt": round(comp_sales_grt, 2)})
        ratios.update({"comp_profit_grt": round(comp_profit_grt, 2)})
        ratios.update({"stock_CAGR": round(stock_CAGR, 2)})
        ratios.update({"return_on_equity": round(return_on_equity, 2)})
        return ratios

# =============================================================================
# USAGE EXAMPLE
# =============================================================================
if __name__ == "__main__":
    # Best practice: Load credentials from environment variables
    SCREENER_USER = os.environ.get("SCREENER_USERNAME", "your_email@example.com")
    SCREENER_PASS = os.environ.get("SCREENER_PASSWORD", "your_password")

    screener = ScreenerClient()
    # Fetch company ratios
    data = screener.get_company_ratios("NATIONALUM")
    print("\n=== Screener Ratios Extracted ===")
    for ratio_name, value in data.items():
        print(f"{ratio_name:<25} : {value}")