import requests
from bs4 import BeautifulSoup

class ScreenerClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.session = requests.Session()
        
        # Mimic a real browser to bypass Cloudflare and WAFs
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Origin': 'https://www.screener.in',
            'Referer': 'https://www.screener.in/login/',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1'
        })
        self.is_authenticated = False
        self.login()

    def login(self):
        """
        Logs into Screener.in using CSRF token authentication.
        """
        login_url = "https://www.screener.in/login/"
        
        try:
            # Step 1: Fetch the login page to acquire the CSRF token
            response = self.session.get(login_url, timeout=10)
            if response.status_code != 200:
                print(f"[ERROR] Unable to reach Screener login page. HTTP {response.status_code}")
                return False

            soup = BeautifulSoup(response.text, "html.parser")
            
            # Extract CSRF token
            csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
            csrf_token = csrf_input["value"] if csrf_input else self.session.cookies.get("csrftoken")
            
            if not csrf_token:
                print("[ERROR] Could not extract CSRF token from Screener login form.")
                return False

            # Extract the 'next' hidden field if it exists
            next_input = soup.find("input", {"name": "next"})
            next_val = next_input["value"] if next_input else "/"

            # Step 2: POST credentials with CSRF middleware token
            payload = {
                "login": self.username,
                "username": self.username,
                "email": self.username,
                "password": self.password,
                "csrfmiddlewaretoken": csrf_token,
                "next": next_val
            }

            post_response = self.session.post(login_url, data=payload, timeout=10)

            # Step 3: Verify session authentication
            if "sessionid" in self.session.cookies.get_dict():
                self.is_authenticated = True
                print("[SUCCESS] Authenticated with Screener.in!")
                return True
                
            # If we reach here, authentication failed.
            print(f"[ERROR] Authentication failed. HTTP Status: {post_response.status_code}")
            
            # Scrape the page for Django form errors
            error_soup = BeautifulSoup(post_response.text, "html.parser")
            error_elements = error_soup.find_all(class_=lambda x: x and ('error' in x.lower() or 'alert' in x.lower()))
            
            if error_elements:
                print("[DEBUG] The website responded with these form errors:")
                for err in error_elements:
                    clean_err = err.get_text(separator=" ", strip=True)
                    if clean_err:
                        print(f" -> {clean_err}")
            else:
                print("[DEBUG] No obvious error text found.")
            
            return False
                
        except Exception as e:
            print(f"[ERROR] Exception occurred during login: {e}")
            return False

    def get_stock_info(self, symbol):
        """
        Fetches fundamental information for a given stock symbol.
        """
        if not self.is_authenticated:
            print("[WARNING] You are not logged in. Some premium data might be hidden.")

        url = f"https://www.screener.in/company/{symbol}/consolidated/"
        
        try:
            print(f"[INFO] Fetching fundamentals for {symbol}...")
            response = self.session.get(url, timeout=10)
            
            if response.status_code != 200:
                url = f"https://www.screener.in/company/{symbol}/"
                response = self.session.get(url, timeout=10)
                if response.status_code != 200:
                    print(f"[ERROR] Failed to fetch data for {symbol}.")
                    return None

            soup = BeautifulSoup(response.text, "html.parser")
            fundamentals = {}
            
            # Extract Company Name
            company_name_tag = soup.find("h1", class_="margin-0") or soup.find("h1")
            if company_name_tag:
                fundamentals["Company Name"] = company_name_tag.text.strip()

            # Extract Top Ratios
            ratios_ul = soup.find("ul", id="top-ratios")
            if ratios_ul:
                for li in ratios_ul.find_all("li"):
                    name_elem = li.find("span", class_="name")
                    value_elem = li.find("span", class_="number")
                    
                    if name_elem and value_elem:
                        name = name_elem.text.strip()
                        value = value_elem.text.strip()
                        fundamentals[name] = value

            return fundamentals
            
        except Exception as e:
            print(f"[ERROR] Exception while fetching stock info: {e}")
            return None

    def get_quick_ratios(self, symbol):
        """
        Fetches advanced fundamentals including the quick_ratios API data, 
        shareholding patterns, and historical ratios.
        """
        if not self.is_authenticated:
            print("[WARNING] You are not logged in. Advanced data may be limited.")

        url = f"https://www.screener.in/company/{symbol}/consolidated/"
        
        try:
            print(f"[INFO] Fetching advanced quick ratios & shareholding for {symbol}...")
            response = self.session.get(url, timeout=10)
            
            if response.status_code != 200:
                url = f"https://www.screener.in/company/{symbol}/"
                response = self.session.get(url, timeout=10)
                if response.status_code != 200:
                    return None

            soup = BeautifulSoup(response.text, "html.parser")
            advanced_data = {}
            
            # 1. Hit the Quick Ratios API
            # First, we need the internal company ID or warehouse ID from the HTML
            company_div = soup.find(attrs={"data-company-id": True})
            print(f"{company_div}")
            
            if company_div:
                company_id = company_div.get("data-company-id")
                warehouse_id = company_div.get("data-warehouse-id")
                
                # Screener sometimes relies on warehouse_id for the ratio API
                target_id = warehouse_id if warehouse_id else company_id
                
                api_url = f"https://www.screener.in/api/company/{target_id}/quick_ratios/"
                
                # The API expects an AJAX header
                api_response = self.session.get(
                    api_url, 
                    headers={'X-Requested-With': 'XMLHttpRequest'},
                    timeout=10
                )
                
                if api_response.status_code == 200:
                    api_soup = BeautifulSoup(api_response.text, "html.parser")
                    print(f"api_soup <<<<<<<<<<<<<<<<<<<<<<{api_soup}")
                    for tr in api_soup.find_all("tr"):
                        tds = tr.find_all("td")
                        if len(tds) >= 2:
                            name = tds[0].get_text(strip=True)
                            value = tds[1].get_text(strip=True)
                            if name:
                                advanced_data[f"API Metric: {name}"] = value
            else:
                print("[WARNING] Could not locate the internal Company ID for the API call.")

            # 2. Extract Shareholding Pattern from the HTML Table
            shareholding_section = soup.find("section", id="shareholding")
            print(f"shareholding_section ====>> {shareholding_section}")
            if shareholding_section:
                table = shareholding_section.find("table")
                if table:
                    rows = table.find_all("tr")
                    if rows:
                        for row in rows[1:]: # Skip the date headers
                            cols = row.find_all(["td", "th"])
                            if len(cols) > 1:
                                investor_type = cols[0].get_text(strip=True).replace("+", "").strip()
                                latest_value = cols[-1].get_text(strip=True) # Grabs the most recent quarter
                                advanced_data[f"Shareholding ({investor_type})"] = latest_value

            # 3. Extract common historical ratios (ROCE, Working Capital Days, etc.)
            ratios_section = soup.find("section", id="ratios")
            print(f" ratios_section ====>> {ratios_section} ")
            if ratios_section:
                table = ratios_section.find("table")
                if table:
                    rows = table.find_all("tr")
                    for row in rows[1:]: # Skip the date headers
                        cols = row.find_all("td")
                        if len(cols) > 1:
                            metric_name = cols[0].get_text(strip=True)
                            latest_value = cols[-1].get_text(strip=True) # Grabs the most recent year
                            advanced_data[f"Historical: {metric_name}"] = latest_value
            
            return advanced_data
            
        except Exception as e:
            print(f"[ERROR] Exception while fetching advanced info: {e}")
            return None

    def logout(self):
        """
        Logs out of the current session safely.
        """
        if getattr(self, 'is_authenticated', False):
            print("[INFO] Logging out...")
            try:
                self.session.get("https://www.screener.in/logout/", timeout=5)
            except Exception:
                pass
            self.session.cookies.clear()
            self.is_authenticated = False
            
        if hasattr(self, 'session'):
            try:
                self.session.close()
                print(" LOGOUT from screener is successful")
            except Exception:
                pass

    def __del__(self):
        """Destructor to ensure safe logout."""
        try:
            self.logout()
        except Exception:
            pass

if __name__ == "__main__":
    # Replace with your actual credentials
    USERNAME = "ns.motog@gmail.com"
    PASSWORD = "Swamy@0684"
    
    screener = ScreenerClient(USERNAME, PASSWORD)

    if screener.login():
        print("\n--- Fetching Basic Fundamentals ---")
        hal_data = screener.get_stock_info("GVT&D")
        '''
        if hal_data:
            for key, value in hal_data.items():
                print(f"{key}: {value}")
        '''
                
        print("\n--- Fetching Advanced Quick Ratios & Shareholding ---")
        hal_advanced = screener.get_quick_ratios("GVT&D")
        
        if hal_advanced:
            for key, value in hal_advanced.items():
                print(f"{key}: {value}")