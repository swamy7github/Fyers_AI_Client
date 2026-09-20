"""
sector_scanner.py
------------------
Snapshot of sector-level strength/weakness ahead of the open.

CAVEAT - please read before trusting this at face value
-----------------------------------------------------------
NSE sector indices (NIFTY IT, NIFTY AUTO, ...) are only computed once the
*normal* market session begins at 09:15. During the pre-open session
(09:00-09:15), only the individual stocks in the pre-open call-auction get an
indicative equilibrium price - the sector indices themselves are not
recalculated. So a literal "09:10 sector index change" doesn't exist on the
exchange side.

What this module does instead, to stay useful at 09:10: it pulls the
pre-open indicative price for every stock in `sector_index_map.csv`,
compares each to the previous close, and averages the % change per sector as
a proxy for sector strength. Treat it as an approximation and re-run the
same computation again after 09:15 for the real, exchange-computed picture
(same function, just called again from a later scheduled job if you want
that).
"""

from __future__ import annotations

import logging

import pandas as pd

import config
from fyers_service import FyersDataService

logger = logging.getLogger("orb_agent.sector")


def load_sector_map() -> pd.DataFrame:
    """CSV with columns: symbol (Fyers format, e.g. NSE:TCS-EQ), sector"""
    return pd.read_csv(config.SECTOR_INDEX_MAP_CSV)


def scan_sectors(data_service: FyersDataService) -> pd.DataFrame:
    """Returns a DataFrame of sectors sorted by average % change, best first."""
    sector_map = load_sector_map()
    symbols = sector_map["symbol"].tolist()

    quotes_response = data_service.get_quotes(symbols)
    quote_lookup = {q["n"]: q["v"] for q in quotes_response.get("d", [])}

    rows = []
    for _, row in sector_map.iterrows():
        quote = quote_lookup.get(row["symbol"])
        if not quote:
            continue
        prev_close = quote.get("prev_close_price")
        ltp = quote.get("lp")  # last traded / pre-open indicative price
        if not prev_close:
            continue
        pct_change = round(((ltp - prev_close) / prev_close * 100), 2)
        rows.append({"symbol": row["symbol"], "sector": row["sector"], "pct_change": pct_change})

    df = pd.DataFrame(rows)
    if df.empty:
        logger.warning("Sector scan returned no data - check sector_index_map.csv and market hours.")
        return df

    sector_avg = df.groupby("sector")["pct_change"].mean().reset_index()
    sector_avg = sector_avg.sort_values("pct_change", ascending=False).reset_index(drop=True)

    logger.info("Top gaining sectors:\n%s", sector_avg.head(5).to_string(index=False))
    logger.info("Top losing sectors:\n%s", sector_avg.tail(5).to_string(index=False))
    return sector_avg
