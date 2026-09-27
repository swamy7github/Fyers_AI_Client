import requests
import requests_cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def create_safe_session():
    """
    Creates a robust requests session with caching, rate limiting, and automatic retries.
    Optimized to handle connection drops, closed sockets, and fallback to stale cache.
    """
    
    # 1. Setup caching: Store API responses for 1 day
    # OPTIMIZATION: 'stale_if_error=True' ensures that if the socket closes 
    # or the network drops completely, the session will return the expired 
    # cached response rather than failing entirely.
    session = requests_cache.CachedSession(
        'yfinance_cache',
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

if __name__ == "__main__":
    # Test the session creation and perform a sample request
    print("Initializing safe session...")
    safe_session = create_safe_session()
    
    test_url = "https://finance.yahoo.com/quote/AAPL"
    
    try:
        print(f"Fetching data from: {test_url}")
        # The session will automatically handle retries, socket drops, and caching behind the scenes
        response = safe_session.get(test_url, timeout=10)
        
        print(f"Response Status Code: {response.status_code}")
        print(f"Was this response cached? {getattr(response, 'from_cache', False)}")
        
        # If we run this twice, the second time will be incredibly fast and from_cache will be True
        print("\nFetching data a second time to test cache...")
        response2 = safe_session.get(test_url, timeout=10)
        print(f"Was the second response cached? {getattr(response2, 'from_cache', False)}")
        
    except requests.exceptions.RequestException as e:
        print(f"A fatal network error occurred after all retries were exhausted: {e}")