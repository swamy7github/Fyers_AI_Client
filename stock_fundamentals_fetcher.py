import json
try:
    import yfinance as yf
except ImportError:
    print("Please install yfinance using: pip install yfinance")
    exit()

def get_indian_stock_fundamentals(symbol):
    """
    Fetches fundamental data for an Indian stock using Yahoo Finance.
    Symbol should be the NSE ticker (e.g., 'RELIANCE').
    """
    # Append .NS to the symbol to fetch from the National Stock Exchange of India
    yahoo_symbol = f"{symbol}.NS"
    print(f"Fetching fundamental data for {yahoo_symbol}...")
    
    ticker = yf.Ticker(yahoo_symbol)
    
    try:
        # info dictionary contains most of the fundamental ratios and metrics
        info = ticker.info
        
        fundamentals = {
            "Company Name": info.get("longName", "N/A"),
            "Sector": info.get("sector", "N/A"),
            "Industry": info.get("industry", "N/A"),
            
            # Valuation Metrics
            "Market Cap (₹)": info.get("marketCap", "N/A"),
            "Trailing P/E": info.get("trailingPE", "N/A"),
            "Forward P/E": info.get("forwardPE", "N/A"),
            "Price to Book (P/B)": info.get("priceToBook", "N/A"),
            
            # Profitability & Financials
            "EPS (Trailing 12M)": info.get("trailingEps", "N/A"),
            "Dividend Yield (%)": info.get("dividendYield", 0) * 100 if info.get("dividendYield") else "N/A",
            "Profit Margin (%)": info.get("profitMargins", 0) * 100 if info.get("profitMargins") else "N/A",
            "Operating Margin (%)": info.get("operatingMargins", 0) * 100 if info.get("operatingMargins") else "N/A",
            "Return on Equity (ROE) (%)": info.get("returnOnEquity", 0) * 100 if info.get("returnOnEquity") else "N/A",
            
            # Debt & Liquidity
            "Debt to Equity": info.get("debtToEquity", "N/A"),
            "Current Ratio": info.get("currentRatio", "N/A"),
            
            # 52 Week High/Low
            "52 Week High (₹)": info.get("fiftyTwoWeekHigh", "N/A"),
            "52 Week Low (₹)": info.get("fiftyTwoWeekLow", "N/A"),
        }
        
        return fundamentals
        
    except Exception as e:
        print(f"Error fetching data for {symbol}: {e}")
        return None

if __name__ == "__main__":
    # Example: Fetching fundamentals for Reliance Industries
    stock_symbol = "RELIANCE" 
    with  open("", "r") as f:
    data = get_indian_stock_fundamentals(stock_symbol)
    
    if data:
        print("\n--- FUNDAMENTAL DATA ---")
        for key, value in data.items():
            # Formatting large numbers (like Market Cap) for better readability
            if isinstance(value, (int, float)) and value > 1000000:
                print(f"{key:25}: {value:,.2f}")
            elif isinstance(value, float):
                print(f"{key:25}: {value:.2f}")
            else:
                print(f"{key:25}: {value}")
        
        print("\n--- RECENT QUARTERLY EARNINGS ---")
        ticker = yf.Ticker(f"{stock_symbol}.NS")
        try:
            # You can also access income statements, balance sheets, and cash flows
            income_stmt = ticker.quarterly_income_stmt
            if not income_stmt.empty:
                # Print the last 2 quarters of Total Revenue as a quick example
                revenues = income_stmt.loc['Total Revenue']
                print(revenues.head(2))
            else:
                print("Quarterly earnings data not available.")
        except Exception:
            print("Could not fetch financial statements.")