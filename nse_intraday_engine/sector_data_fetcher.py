"""
SectorDataFetcher
=================
Retrieves intraday 5-minute data for NSE sectoral indices and computes each
index's intraday relative-strength (% change from the day's open) series,
used by SignalEngine to confirm a stock is outperforming/underperforming
its own sector.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from utils import setup_logger

SECTOR_INDEX_SYMBOLS = {
    "NIFTY BANK": "NSE:NIFTYBANK-INDEX",
    "NIFTY IT": "NSE:NIFTYIT-INDEX",
    "NIFTY AUTO": "NSE:NIFTYAUTO-INDEX",
    "NIFTY PHARMA": "NSE:NIFTYPHARMA-INDEX",
    "NIFTY FMCG": "NSE:NIFTYFMCG-INDEX",
    "NIFTY METAL": "NSE:NIFTYMETAL-INDEX",
    "NIFTY REALTY": "NSE:NIFTYREALTY-INDEX",
    "NIFTY ENERGY": "NSE:NIFTYENERGY-INDEX",
    "NIFTY FIN SERVICE": "NSE:FINNIFTY-INDEX",
    "NIFTY MEDIA": "NSE:NIFTYMEDIA-INDEX",
    "NIFTY HEALTHCARE": "NSE:NIFTYHEALTHCARE-INDEX",
    "NIFTY 500": "NSE:NIFTY500-INDEX",
}


class SectorDataFetcher:
    def __init__(self, fyers_client, config):
        self.fy = fyers_client
        self.cfg = config
        self.log = setup_logger("sector_data", config.LOG_DIR, config.LOG_LEVEL)
        self._cache: dict[tuple, pd.DataFrame] = {}

    @staticmethod
    def _fyers_symbol(sector_index: str) -> str:
        return SECTOR_INDEX_SYMBOLS.get(sector_index, SECTOR_INDEX_SYMBOLS["NIFTY 500"])

    def get_intraday_5min(self, sector_index: str, trade_date: str) -> pd.DataFrame:
        key = (sector_index, trade_date)
        if key in self._cache:
            return self._cache[key]
        symbol = self._fyers_symbol(sector_index)
        df = self.fy.get_history(symbol, resolution="5", date_from=trade_date, date_to=trade_date)
        self._cache[key] = df
        return df

    @staticmethod
    def compute_intraday_rs(df: pd.DataFrame) -> pd.Series:
        """% change of `close` from the first bar's `open`, indexed like `df`."""
        if df.empty:
            return pd.Series(dtype=float)
        day_open = df["open"].iloc[0]
        return (df["close"] - day_open) / day_open * 100

    def get_sector_rs_series(self, sector_index: str, trade_date: str) -> pd.Series:
        df = self.get_intraday_5min(sector_index, trade_date)
        rs = self.compute_intraday_rs(df)
        if not df.empty:
            rs.index = df["timestamp"]
        return rs

    def rs_at(self, sector_index: str, trade_date: str, timestamp) -> Optional[float]:
        series = self.get_sector_rs_series(sector_index, trade_date)
        if series.empty:
            return None
        matched = series[series.index <= timestamp]
        return float(matched.iloc[-1]) if not matched.empty else None
