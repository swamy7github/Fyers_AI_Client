"""
NiftyScreener
=============
Builds the tradeable intraday universe:
  1. Downloads the live Nifty 500 constituent list from NSE.
  2. Maps every stock to its NSE sectoral index.
  3. Filters down to the most liquid `UNIVERSE_TOP_N` names by 30-day average
     turnover and a tight bid-ask spread.
  4. Applies the 09:15-09:20 "first candle" filter per direction.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO

import pandas as pd
import requests

from utils import retry_with_backoff, setup_logger

_NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,*/*",
}

# NSE sectoral-index constituent lists we map the universe against.
_SECTOR_CSVS = {
    "NIFTY BANK": "ind_niftybanklist.csv",
    "NIFTY IT": "ind_niftyitlist.csv",
    "NIFTY AUTO": "ind_niftyautolist.csv",
    "NIFTY PHARMA": "ind_niftypharmalist.csv",
    "NIFTY FMCG": "ind_niftyfmcglist.csv",
    "NIFTY METAL": "ind_niftymetallist.csv",
    "NIFTY REALTY": "ind_niftyrealtylist.csv",
    "NIFTY ENERGY": "ind_niftyenergylist.csv",
    "NIFTY FIN SERVICE": "ind_niftyfinservicelist.csv",
    "NIFTY MEDIA": "ind_niftymedialist.csv",
    "NIFTY HEALTHCARE": "ind_niftyhealthcarelist.csv",
}
_DEFAULT_SECTOR = "NIFTY 500"


class NiftyScreener:
    def __init__(self, fyers_client, config):
        self.fy = fyers_client
        self.cfg = config
        self.log = setup_logger("nifty_screener", config.LOG_DIR, config.LOG_LEVEL)
        self._sector_map: dict[str, str] = {}

    # --------------------------------------------------------- universe list
    @retry_with_backoff(exceptions=(requests.RequestException,))
    def fetch_nifty500(self) -> pd.DataFrame:
        resp = requests.get(self.cfg.NIFTY500_URL, headers=_NSE_HEADERS, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
        df.columns = [c.strip() for c in df.columns]
        df["Symbol"] = df["Symbol"].str.strip()
        return df[["Company Name", "Industry", "Symbol", "Series", "ISIN Code"]]

    @retry_with_backoff(exceptions=(requests.RequestException,))
    def _fetch_sector_csv(self, filename: str) -> set[str]:
        url = self.cfg.SECTOR_INDEX_CSV_BASE + filename
        resp = requests.get(url, headers=_NSE_HEADERS, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
        df.columns = [c.strip() for c in df.columns]
        return set(df["Symbol"].str.strip())

    def build_sector_map(self, universe_symbols: list[str]) -> dict[str, str]:
        """symbol -> sector index name, falling back to NIFTY 500."""
        mapping = {s: _DEFAULT_SECTOR for s in universe_symbols}
        for sector_name, csv_file in _SECTOR_CSVS.items():
            try:
                members = self._fetch_sector_csv(csv_file)
            except requests.RequestException as exc:
                self.log.warning("Could not fetch sector list %s: %s", csv_file, exc)
                continue
            for sym in members:
                if sym in mapping:
                    mapping[sym] = sector_name
        self._sector_map = mapping
        return mapping

    # ------------------------------------------------------------ liquidity
    def compute_liquidity_universe(self, universe: pd.DataFrame, as_of: datetime) -> list[dict]:
        """Ranks `universe` by 30-day average turnover, applies a spread
        filter via live quotes, and returns the top `UNIVERSE_TOP_N` rows."""
        date_to = as_of.strftime("%Y-%m-%d")
        date_from = (as_of - timedelta(days=self.cfg.TURNOVER_LOOKBACK_DAYS * 2)).strftime("%Y-%m-%d")

        turnovers: dict[str, float] = {}
        for symbol in universe["Symbol"]:
            fy_symbol = f"NSE:{symbol}-EQ"
            try:
                df = self.fy.get_history(fy_symbol, resolution="D", date_from=date_from, date_to=date_to)
            except Exception as exc:  # noqa: BLE001 -- one bad symbol shouldn't kill the screen
                self.log.warning("history failed for %s: %s", fy_symbol, exc)
                continue
            if df.empty:
                continue
            df = df.tail(self.cfg.TURNOVER_LOOKBACK_DAYS)
            turnovers[symbol] = float((df["close"] * df["volume"]).mean())

        ranked = sorted(turnovers.items(), key=lambda kv: kv[1], reverse=True)
        candidates = [sym for sym, _ in ranked[: self.cfg.UNIVERSE_TOP_N * 2]]  # pad before spread filter

        quotes = self.fy.get_quotes([f"NSE:{s}-EQ" for s in candidates])
        results = []
        for rank, symbol in enumerate(candidates, start=1):
            fy_symbol = f"NSE:{symbol}-EQ"
            quote = quotes.get(fy_symbol, {})
            spread = self.fy.get_bid_ask_spread_pct(fy_symbol, quote)
            if spread is not None and spread > self.cfg.MAX_SPREAD_PCT:
                continue
            results.append({
                "symbol": symbol,
                "sector_index": self._sector_map.get(symbol, _DEFAULT_SECTOR),
                "avg_turnover": turnovers[symbol],
                "spread_pct": spread,
                "rank": rank,
            })
            if len(results) >= self.cfg.UNIVERSE_TOP_N:
                break
        return results

    # -------------------------------------------------------- 9:15 filter
    @staticmethod
    def first_candle_pct_change(candle: pd.Series) -> float:
        return (candle["close"] - candle["open"]) / candle["open"] * 100

    def passes_first_candle_filter(self, first_candle: pd.Series) -> dict:
        """Eligibility for LONG and SHORT setups based on the 09:15-09:20
        candle's % move: reject spikes/dumps beyond +/-CANDLE_FILTER_PCT."""
        pct = self.first_candle_pct_change(first_candle)
        limit = self.cfg.CANDLE_FILTER_PCT
        return {
            "pct_change": pct,
            "long_eligible": pct <= limit,
            "short_eligible": pct >= -limit,
        }
