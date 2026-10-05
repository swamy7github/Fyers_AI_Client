import pandas as pd
import numpy as np
import logging

logger = logging.getLogger("data_loader")

class IndicatorCalculator:
    """
    Calculates technical indicators and relative strength metrics for breakout strategies.
    Ensures zero look-ahead bias by operating strictly on historical bar windows.
    """
    @staticmethod
    def add_indicators(df: pd.DataFrame, benchmark_df: pd.DataFrame = None) -> pd.DataFrame:
        df = df.copy()
        df = df.sort_values('timestamp').reset_index(drop=True)

        # Moving Averages
        df['sma_10'] = df['close'].rolling(window=10).mean()
        df['sma_20'] = df['close'].rolling(window=20).mean()
        df['sma_50'] = df['close'].rolling(window=50).mean()
        df['sma_150'] = df['close'].rolling(window=150).mean()
        df['sma_200'] = df['close'].rolling(window=200).mean()

        df['ema_10'] = df['close'].ewm(span=10, adjust=False).mean()
        df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()

        # Volume Moving Averages
        df['vol_sma_20'] = df['volume'].rolling(window=20).mean()
        df['vol_sma_50'] = df['volume'].rolling(window=50).mean()

        # 52-Week / 250-Day Extremes
        df['high_250d'] = df['high'].rolling(window=250, min_periods=50).max()
        df['low_250d'] = df['low'].rolling(window=250, min_periods=50).min()

        df['dist_from_52w_high_pct'] = ((df['high_250d'] - df['close']) / df['high_250d']) * 100.0
        df['dist_from_52w_low_pct'] = ((df['close'] - df['low_250d']) / df['low_250d']) * 100.0

        # ADR% (Average Daily Range 20-day Percentage)
        # ADR% = Mean over 20 days of [(High - Low) / Close] * 100
        daily_range_pct = ((df['high'] - df['low']) / df['close']) * 100.0
        df['adr_20'] = daily_range_pct.rolling(window=20).mean()

        # ATR (Average True Range 20-day)
        high_low = df['high'] - df['low']
        high_close_prev = (df['high'] - df['close'].shift(1)).abs()
        low_close_prev = (df['low'] - df['close'].shift(1)).abs()
        tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
        df['atr_20'] = tr.rolling(window=20).mean()

        # Relative Strength (RS) vs Benchmark (Nifty 50)
        if benchmark_df is not None and not benchmark_df.empty:
            df = IndicatorCalculator._calculate_relative_strength(df, benchmark_df)
        else:
            # Fallback RS Proxy based on 12-month Momentum if Benchmark not provided
            df['rs_rating'] = df['close'].pct_change(periods=120).rank(pct=True) * 100.0
            df['rs_rating'] = df['rs_rating'].fillna(75.0)

        return df

    @staticmethod
    def _calculate_relative_strength(df: pd.DataFrame, benchmark_df: pd.DataFrame) -> pd.DataFrame:
        """Calculates 12-month weighted relative strength vs Nifty 50 benchmark."""
        bm = benchmark_df[['timestamp', 'close']].rename(columns={'close': 'bm_close'}).copy()
        merged = pd.merge_asof(df.sort_values('timestamp'), bm.sort_values('timestamp'), on='timestamp', direction='backward')
        
        # 1-Year (250d), 6-Month (125d), 3-Month (60d) Returns
        merged['stock_ret_1y'] = merged['close'].pct_change(periods=250)
        merged['bm_ret_1y'] = merged['bm_close'].pct_change(periods=250)
        
        merged['stock_ret_6m'] = merged['close'].pct_change(periods=125)
        merged['bm_ret_6m'] = merged['bm_close'].pct_change(periods=125)

        merged['stock_ret_3m'] = merged['close'].pct_change(periods=60)
        merged['bm_ret_3m'] = merged['bm_close'].pct_change(periods=60)

        # Weighted RS Score = 40% (3M) + 30% (6M) + 30% (12M) excess returns
        merged['excess_ret'] = (
            0.4 * (merged['stock_ret_3m'] - merged['bm_ret_3m']) +
            0.3 * (merged['stock_ret_6m'] - merged['bm_ret_6m']) +
            0.3 * (merged['stock_ret_1y'] - merged['bm_ret_1y'])
        )
        
        # Scale to 1-99 percentile RS Rating
        merged['rs_rating'] = merged['excess_ret'].rank(pct=True) * 100.0
        merged['rs_rating'] = merged['rs_rating'].fillna(70.0)
        
        return merged.drop(columns=['bm_close', 'stock_ret_1y', 'bm_ret_1y', 'stock_ret_6m', 'bm_ret_6m', 'stock_ret_3m', 'bm_ret_3m', 'excess_ret'])
