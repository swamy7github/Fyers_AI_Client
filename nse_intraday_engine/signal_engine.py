"""
SignalEngine
============
Adds VWAP / ATR(14) / 20-period volume SMA to 5-minute OHLCV bars and
evaluates the long/short entry trigger described in the strategy spec,
including the relative-strength check against the stock's sector index.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class Signal:
    symbol: str
    direction: str  # "LONG" | "SHORT"
    timestamp: datetime
    trigger_price: float
    atr: float
    stock_rs: float
    sector_rs: float
    reason: str


class SignalEngine:
    def __init__(self, config):
        self.cfg = config

    # ------------------------------------------------------------ indicators
    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Expects columns: timestamp, open, high, low, close, volume -- one
        trading day only (VWAP and the RS calc reset each day)."""
        df = df.copy().reset_index(drop=True)
        if df.empty:
            return df

        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        cum_vol = df["volume"].cumsum().replace(0, np.nan)
        df["vwap"] = (typical_price * df["volume"]).cumsum() / cum_vol

        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = self._wilder_atr(tr, self.cfg.ATR_PERIOD)

        df["vol_sma"] = df["volume"].rolling(self.cfg.VOLUME_SMA_PERIOD, min_periods=1).mean()
        df["intraday_rs"] = (df["close"] - df["open"].iloc[0]) / df["open"].iloc[0] * 100
        return df

    @staticmethod
    def _wilder_atr(tr: pd.Series, period: int) -> pd.Series:
        atr = pd.Series(index=tr.index, dtype=float)
        if len(tr) < period:
            return tr.expanding().mean()
        atr.iloc[:period - 1] = np.nan
        atr.iloc[period - 1] = tr.iloc[:period].mean()
        for i in range(period, len(tr)):
            atr.iloc[i] = (atr.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period
        return atr.bfill()

    # -------------------------------------------------------------- signals
    def _open_extreme_ok(self, row: pd.Series, extreme_col: str) -> bool:
        tol = self.cfg.OPEN_EXTREME_TOLERANCE_PCT / 100
        return abs(row["open"] - row[extreme_col]) <= row["open"] * tol

    def evaluate_bar(self, df: pd.DataFrame, i: int, candle_filter: dict,
                      sector_rs_at_ts) -> Optional[Signal]:
        if i < max(self.cfg.ATR_PERIOD, self.cfg.VOLUME_SMA_PERIOD):
            return None
        row = df.iloc[i]
        sector_rs = sector_rs_at_ts(row["timestamp"])
        if sector_rs is None or pd.isna(row["atr"]) or pd.isna(row["vol_sma"]):
            return None

        volume_spike = row["volume"] > self.cfg.VOLUME_SPIKE_MULTIPLE * row["vol_sma"]

        n = self.cfg.VWAP_SUSTAIN_BARS
        window = df.iloc[max(0, i - n + 1): i + 1]
        above_vwap = bool((window["close"] > window["vwap"]).all())
        below_vwap = bool((window["close"] < window["vwap"]).all())

        if (candle_filter.get("long_eligible") and self._open_extreme_ok(row, "low")
                and above_vwap and volume_spike and row["intraday_rs"] > sector_rs):
            return Signal(df.attrs.get("symbol", ""), "LONG", row["timestamp"], float(row["close"]),
                          float(row["atr"]), float(row["intraday_rs"]), float(sector_rs),
                          "open~low, VWAP sustained above, vol spike, RS>sector")

        if (candle_filter.get("short_eligible") and self._open_extreme_ok(row, "high")
                and below_vwap and volume_spike and row["intraday_rs"] < sector_rs):
            return Signal(df.attrs.get("symbol", ""), "SHORT", row["timestamp"], float(row["close"]),
                          float(row["atr"]), float(row["intraday_rs"]), float(sector_rs),
                          "open~high, VWAP sustained below, vol spike, RS<sector")
        return None

    def generate_signals(self, df: pd.DataFrame, symbol: str, candle_filter: dict,
                          sector_rs_at_ts) -> list[Signal]:
        """`df` must already carry indicators from `add_indicators()`."""
        if df.empty:
            return []
        df.attrs["symbol"] = symbol
        signals = []
        for i in range(len(df)):
            sig = self.evaluate_bar(df, i, candle_filter, sector_rs_at_ts)
            if sig:
                signals.append(sig)
        return signals
