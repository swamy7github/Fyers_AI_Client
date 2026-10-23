"""
build_sector_map.py
---------------------
One-time (or whenever index composition changes / gets rebalanced) helper
to build data/sector_index_map.csv from NSE's OWN official sectoral index
constituent downloads - so the sector labels and stock coverage come from
NSE directly, not from anyone's memory (the sample file this repo ships
with was hand-typed from general knowledge and was only ever meant as a
placeholder - see the README).

How to use
-----------
1. For each sector you want to track, open its NSE page and click the
   "Download (.csv)" button above the constituents table, e.g.:
     https://www.nseindia.com/market-data/live-equity-market?symbol=NIFTY%20AUTO
     https://www.nseindia.com/market-data/live-equity-market?symbol=NIFTY%20BANK
     https://www.nseindia.com/market-data/live-equity-market?symbol=NIFTY%20IT
     https://www.nseindia.com/market-data/live-equity-market?symbol=NIFTY%20FMCG
   ...swap the `symbol=` value for whichever sectoral index you want
   (NIFTY METAL, NIFTY PHARMA, NIFTY REALTY, NIFTY ENERGY, NIFTY MEDIA,
   NIFTY PSU BANK, NIFTY PRIVATE BANK, NIFTY FINANCIAL SERVICES, NIFTY
   HEALTHCARE INDEX, NIFTY CONSUMER DURABLES, NIFTY OIL & GAS, ...).
   Alternatively, https://nseindia.com/products/content/equities/indices/
   sectoral_indices.htm lists most sectoral indices with a direct
   "Download List of ... Index Stocks (.csv)" link per index - same data,
   one click closer.
2. Save each downloaded file into one folder (e.g. data/raw_sector_csvs/),
   named after its sector with underscores for spaces, e.g.
   NIFTY_AUTO.csv, NIFTY_BANK.csv, NIFTY_IT.csv - the filename becomes the
   `sector` label in the output (underscores are turned back into spaces).
3. Run:
     python build_sector_map.py data/raw_sector_csvs
   This (re)writes data/sector_index_map.csv in the symbol,sector format
   sector_scanner.py expects, converting each NSE symbol into Fyers'
   'NSE:<SYMBOL>-EQ' format along the way.

NSE's own constituent CSVs have used slightly different column
names/casing over time ("SYMBOL" vs "Symbol"); this script checks a few
known variants and fails loudly with the actual column names it found if
none match, rather than silently skipping a file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

import config

SYMBOL_COLUMN_CANDIDATES = ["Symbol", "SYMBOL", "symbol"]


def _find_symbol_column(df: pd.DataFrame, source_file: Path) -> str:
    for candidate in SYMBOL_COLUMN_CANDIDATES:
        if candidate in df.columns:
            return candidate
    raise ValueError(
        f"Could not find a symbol column in {source_file.name} "
        f"(looked for {SYMBOL_COLUMN_CANDIDATES}, found {list(df.columns)}). "
        f"Open the file and check its header row."
    )


def build_sector_map(raw_csv_dir: str, output_path: str = config.SECTOR_INDEX_MAP_CSV) -> pd.DataFrame:
    raw_dir = Path(raw_csv_dir)
    csv_files = sorted(raw_dir.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No .csv files found in {raw_dir}")

    rows = []
    for csv_file in csv_files:
        sector_label = csv_file.stem.replace("_", " ").strip()
        df = pd.read_csv(csv_file)
        symbol_col = _find_symbol_column(df, csv_file)
        for raw_symbol in df[symbol_col].dropna().tolist():
            fyers_symbol = f"NSE:{str(raw_symbol).strip()}-EQ"
            rows.append({"symbol": fyers_symbol, "sector": sector_label})
        print(f"{csv_file.name}: {len(df)} constituent(s) -> sector '{sector_label}'")

    combined = pd.DataFrame(rows).drop_duplicates(subset="symbol")
    combined.to_csv(output_path, index=False)
    print(f"\nWrote {len(combined)} symbol -> sector row(s) to {output_path}")
    return combined


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python build_sector_map.py <folder_of_downloaded_sector_csvs>")
        sys.exit(1)
    build_sector_map(sys.argv[1])
