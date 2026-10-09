import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
from pathlib import Path

# 1. Dynamically parse the parent path ('C:\NS\Fyers_AI_Client')
# This gets the folder containing the current script and looks one level up
project_root = Path(__file__).resolve().parent.parent.parent

# 2. Add 'C:\NS\Fyers_AI_Client' to Python's search path
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))
    
logger = logging.getLogger("MDCO_fyers_client")


class FyersDataClient:
    """
    FYERS API V3 Client Wrapper for Historical Stock & Intraday 5m Data Extraction.
    Supports live FYERS API integration and deterministic mock data generation
    for backtesting, daily screening, and 5-minute ORB intraday triggers.
    """
    def __init__(self, client_id: str = "MOCK", access_token: str = "MOCK", use_mock: bool = False):
        self.client_id = client_id
        self.access_token = access_token
        self.use_mock = use_mock
        self.fyers_instance = None

        if not self.use_mock:
            try:
                from fyersclient.fyers_client import get_fyers_client, FyersDataService
                
                data_service = get_fyers_client()
                fyers = data_service
                if not fyers:
                    logger.error(f"Failed to get fyers token and authenticate for the APP {APP_ID}")
                    return
                
                self.fyers_instance = fyers
                logger.info("FYERS API client initialized successfully.")
            except Exception as e:
                logger.warning(f"Could not initialize FYERS API client ({e}). Falling back to Mock mode.")
                self.use_mock = True

    def get_historical_data(self, symbol: str, resolution: str = "D", start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        Extracts historical OHLCV data for an NSE stock.
        Returns DataFrame with columns: ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        """
        if not self.use_mock and self.fyers_instance:
            try:
                data_req = {
                    "symbol": symbol,
                    "resolution": resolution,
                    "date_format": "1",
                    "range_from": start_date,
                    "range_to": end_date,
                    "cont_flag": "1"
                }
                response = self.fyers_instance.history(data=data_req)
                if response.get("s") == "ok":
                    candles = response.get("candles", [])
                    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                    return df
                else:
                    logger.error(f"FYERS API Error for {symbol}: {response}")
            except Exception as e:
                logger.error(f"Failed to fetch data from FYERS API for {symbol}: {e}")

        # Mock Data Fallback Generator for Offline / Backtest Mode
        #return self._generate_mock_ohlcv(symbol, start_date, end_date)

    def get_intraday_data(self, symbol: str, resolution: str = "5", date_str: str = None) -> pd.DataFrame:
        """
        Extracts 5-minute intraday candle data for Opening Range Breakout (ORB) execution.
        Returns DataFrame with columns: ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        """
        if not self.use_mock and self.fyers_instance:
            try:
                today_str = date_str or datetime.now().strftime("%Y-%m-%d")
                data_req = {
                    "symbol": symbol,
                    "resolution": resolution,
                    "date_format": "1",
                    "range_from": today_str,
                    "range_to": today_str,
                    "cont_flag": "1"
                }
                response = self.fyers_instance.history(data=data_req)
                if response.get("s") == "ok":
                    candles = response.get("candles", [])
                    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                    return df
            except Exception as e:
                logger.error(f"FYERS API Intraday Error for {symbol}: {e}")

        #return self._generate_mock_intraday_5m(symbol, date_str)

    def _generate_mock_ohlcv(self, symbol: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        Generates realistic synthetic OHLCV data featuring trending, VCP coiling,
        Darvas boxes, and breakout regimes for systematic verification.
        """
        seed_val = abs(hash(symbol)) % (2**32)
        np.random.seed(seed_val)

        start_dt = datetime.strptime(start_date, "%Y-%m-%d") if start_date else datetime.now() - timedelta(days=500)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()

        dates = pd.date_range(start=start_dt, end=end_dt, freq='B') # Business days
        n = len(dates)

        # Baseline trend creation (Stage 2 Uptrend with realistic contractions)
        base_price = 100.0 + (seed_val % 200)
        trend = np.linspace(0, 0.8, n) # Strong macro trend
        noise = np.random.normal(0, 0.015, n).cumsum()
        
        # Inject cyclical consolidation / VCP / Darvas pattern in the last 60 days
        cycles = np.sin(np.linspace(0, 8 * np.pi, n)) * 0.05
        
        close_prices = base_price * (1.0 + trend + noise + cycles)
        close_prices = np.maximum(close_prices, 10.0)
        
        high_prices = close_prices * (1.0 + np.abs(np.random.normal(0.01, 0.005, n)))
        low_prices = close_prices * (1.0 - np.abs(np.random.normal(0.01, 0.005, n)))
        open_prices = low_prices + (high_prices - low_prices) * np.random.uniform(0.2, 0.8, n)
        
        base_volume = 100000 + (seed_val % 500000)
        volume = np.random.negative_binomial(5, 0.2, n) * (base_volume / 20) + base_volume
        
        if n > 10:
            volume[-5:] = volume[-5:] * 2.5
            close_prices[-5:] = close_prices[-5:] * 1.03

        df = pd.DataFrame({
            'timestamp': dates,
            'open': open_prices,
            'high': high_prices,
            'low': low_prices,
            'close': close_prices,
            'volume': volume.astype(int)
        })
        return df

    def _generate_mock_intraday_5m(self, symbol: str, date_str: str = None) -> pd.DataFrame:
        """
        Generates 75 bars of 5-minute intraday candle data (09:15 to 15:25) simulating
        an Opening Range (09:15-09:30) followed by a 5m breakout on volume expansion.
        """
        seed_val = abs(hash(symbol + "_5m")) % (2**32)
        np.random.seed(seed_val)

        base_dt = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)
        start_time = base_dt.replace(hour=9, minute=15, second=0, microsecond=0)
        timestamps = [start_time + timedelta(minutes=5 * i) for i in range(75)] # 09:15 to 15:25 (75 bars)

        base_price = 300.0 + (seed_val % 150)
        
        # 15-minute Opening Range (bars 0, 1, 2)
        # Bar 0: 09:15, Bar 1: 09:20, Bar 2: 09:25
        orb_high = base_price * 1.010
        orb_low = base_price * 0.995

        opens = []
        highs = []
        lows = []
        closes = []
        volumes = []

        curr_price = base_price
        for i in range(75):
            if i < 3: # Opening Range formation (09:15 - 09:30)
                open_p = curr_price
                close_p = open_p + np.random.uniform(-0.5, 0.5)
                high_p = max(open_p, close_p) + np.random.uniform(0.1, 0.5)
                low_p = min(open_p, close_p) - np.random.uniform(0.1, 0.5)
                vol = np.random.randint(15000, 30000)
            elif i == 4 or i == 5: # Breakout bar trigger (09:35 - 09:40)
                open_p = curr_price
                close_p = orb_high * 1.008 # Break above ORB High
                high_p = close_p * 1.003
                low_p = open_p * 0.999
                vol = np.random.randint(75000, 120000) # Volume surge (3x - 4x)
            else: # Trend continuation or consolidation
                open_p = curr_price
                drift = 0.0005 if i < 40 else -0.0002
                close_p = open_p * (1.0 + drift + np.random.normal(0, 0.002))
                high_p = max(open_p, close_p) * 1.002
                low_p = min(open_p, close_p) * 0.998
                vol = np.random.randint(10000, 25000)

            curr_price = close_p
            opens.append(round(float(open_p), 2))
            highs.append(round(float(high_p), 2))
            lows.append(round(float(low_p), 2))
            closes.append(round(float(close_p), 2))
            volumes.append(int(vol))

        df = pd.DataFrame({
            'timestamp': timestamps,
            'open': opens,
            'high': highs,
            'low': lows,
            'close': closes,
            'volume': volumes
        })
        return df


def get_historical_data(symbol: str, resolution: str = "D", start_date: str = "2025-09-01", end_date: str = "2026-09-30") -> pd.DataFrame:
    """Standalone wrapper function matching FYERS module convention."""
    client = FyersDataClient(use_mock=False)
    return client.get_historical_data(symbol, resolution, start_date, end_date)
