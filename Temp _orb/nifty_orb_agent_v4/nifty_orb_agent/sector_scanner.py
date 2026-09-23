"""
sector_scanner.py
------------------
Snapshot of sector strength/weakness, queried DIRECTLY from NSE's own
sectoral index values via Fyers (symbols like NSE:NIFTYIT-INDEX,
NSE:NIFTYBANK-INDEX, ...) rather than reconstructed from individual stock
constituents. This is both simpler and more accurate than the constituent-
averaging approach this module used to use: one batched /quotes call for
the whole reference file (~25 symbols = 1 call) instead of hundreds of
per-stock calls.

data/sector_index_map.csv columns:
    symbol           Fyers index symbol, e.g. NSE:NIFTYIT-INDEX
    index_name       Human-readable name, e.g. "Nifty IT"
    category         Free-text description - for your reference only, not
                      used in any ranking logic
    is_sector_index  True/False - explicit, editable flag for whether this
                      row counts toward the "top gaining/losing sector"
                      leaderboard. Broad market-cap benchmarks (Nifty 50/
                      100/200/500/Midcap 50/Next 50), India VIX (a
                      volatility gauge, not a sector), G-Sec bond indices
                      (fixed income), and multi-sector strategy indices are
                      flagged False by default - they're informational
                      context, not sectors, and averaging their % change
                      alongside real sector indices would be comparing
                      different things. Flip any row yourself if you
                      disagree with a default.

CAVEAT (please read): NSE only (re)computes an index's value once the
*normal* session begins at 09:15 - during the pre-open window (09:00-09:15)
constituent stocks get an indicative price, but derived indices (sectoral or
otherwise) are not recalculated. Querying NSE:NIFTYIT-INDEX at 09:10 will
most likely just return the previous day's closing value (i.e. ~0% change),
regardless of whether you fetch it directly (as here) or reconstruct it from
constituents (as this module used to). This limitation is about the
exchange's own calculation schedule, not about which method you query it
with - it doesn't go away. Re-run `scan_sectors()` again shortly after 09:15
(main.py can schedule a second call) to get the real, meaningful numbers.
"""

from __future__ import annotations

import logging

import pandas as pd

import config
from fyers_service import FyersDataService

logger = logging.getLogger("orb_agent.sector")


def load_index_reference() -> pd.DataFrame:
    df = pd.read_csv(config.SECTOR_INDEX_MAP_CSV)
    df["is_sector_index"] = df["is_sector_index"].astype(bool)
    return df


def get_index_snapshot(data_service: FyersDataService) -> pd.DataFrame:
    """
    Returns EVERY row in the reference file annotated with prev_close, ltp
    and pct_change - both the true sector/thematic rows and the contextual
    ones (Nifty 50, India VIX, G-Secs, ...). Callers filter as needed;
    get_sector_leaderboard() below is the filtered, ranked view.
    """
    reference = load_index_reference()
    quote_lookup = data_service.get_quotes_batched(reference["symbol"].tolist())

    rows = []
    for _, row in reference.iterrows():
        quote = quote_lookup.get(row["symbol"])
        if not quote:
            logger.warning("No quote returned for %s (%s) - skipping. If this persists, "
                            "confirm the symbol exists in your Fyers symbol master.",
                            row["symbol"], row["index_name"])
            continue
        prev_close = quote.get("prev_close_price")
        ltp = quote.get("lp")
        if not prev_close or not ltp:
            continue
        pct_change = (ltp - prev_close) / prev_close * 100
        rows.append({
            "symbol": row["symbol"], "index_name": row["index_name"],
            "sector": row["sector"], "is_sector_index": row["is_sector_index"],
            "prev_close": prev_close, "ltp": ltp, "pct_change": round(pct_change, 2),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        logger.warning("Index snapshot returned no data - check sector_index_map.csv, "
                        "your Fyers symbol access, and market hours.")
    return df


def get_sector_leaderboard(snapshot_df: pd.DataFrame) -> pd.DataFrame:
    """The is_sector_index=True subset, sorted best-to-worst by pct_change."""
    if snapshot_df.empty:
        return snapshot_df
    sectors = snapshot_df[snapshot_df["is_sector_index"]].copy()
    return sectors.sort_values("pct_change", ascending=False).reset_index(drop=True)


def scan_sectors(data_service: FyersDataService) -> pd.DataFrame:
    """Convenience wrapper: fetch the snapshot and return just the ranked sector leaderboard."""
    snapshot = get_index_snapshot(data_service)
    leaderboard = get_sector_leaderboard(snapshot)
    if not leaderboard.empty:
        logger.info("Top gaining sectors:\n%s", leaderboard.head(5).to_string(index=False))
        logger.info("Top losing sectors:\n%s", leaderboard.tail(5).to_string(index=False))
    return leaderboard
