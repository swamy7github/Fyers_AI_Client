"""
Main CLI Entrypoint for Ghanshyam Trader (Backtesting & Realtime Session Modes)
"""

import argparse
import logging
import sys
from datetime import datetime, date, time, timedelta
from typing import Dict, List, Optional, Tuple

from config import IndexConfig
from models import Candle, Direction, LDCHLLevels
from backtester import BacktestEngine
from realtime_runner import MultiAssetTradingManager, RealtimeSessionRunner
from pathlib import Path
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fyersclient.fyers_client import get_fyers_client

logger = logging.getLogger("GhanshyamTradingSystem")


def fetch_fyers_multiday_data(
    fyers_client=None,
    symbols: Optional[Dict[str, str]] = None,
    range_from: Optional[str] = None,
    range_to: Optional[str] = None,
    num_days: int = 5,
    resolution: str = "5"
) -> Tuple[Dict[date, Dict[str, List[Candle]]], Dict[date, Dict[str, LDCHLLevels]]]:
    """
    Fetches historical 5-minute candle data directly from Fyers API v3 via fyers_client.history().
    Calculates dynamic Previous Day High, Low, and Close (LDCHL) benchmarks automatically.
    """
    if symbols is None:
        symbols = {
            "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
            "NIFTY": "NSE:NIFTY50-INDEX"
        }

    if not range_to:
        range_to = datetime.now().strftime("%Y-%m-%d")

    if not range_from:
        lookback = max(num_days * 2, 10)
        range_from = (datetime.now() - timedelta(days=lookback)).strftime("%Y-%m-%d")

    multiday_candles: Dict[date, Dict[str, List[Candle]]] = {}

    if fyers_client and hasattr(fyers_client, "history"):
        print(f"\n[Fyers API] Fetching historical candles via fyers_client.history() [{range_from} to {range_to}]...")
        for code, sym in symbols.items():
            req_data = {
                "symbol": sym,
                "resolution": resolution,
                "date_format": "1",
                "range_from": range_from,
                "range_to": range_to,
                "cont_flag": "1"
            }
            res = fyers_client.history(data=req_data)
            if res and res.get("s") == "ok":
                raw_candles = res.get("candles", [])
                for c in raw_candles:
                    dt = datetime.fromtimestamp(c[0])
                    c_date = dt.date()
                    candle_obj = Candle(
                        timestamp=dt,
                        open=float(c[1]),
                        high=float(c[2]),
                        low=float(c[3]),
                        close=float(c[4]),
                        volume=float(c[5]) if len(c) > 5 else 0.0
                    )
                    if c_date not in multiday_candles:
                        multiday_candles[c_date] = {}
                    if code not in multiday_candles[c_date]:
                        multiday_candles[c_date][code] = []
                    multiday_candles[c_date][code].append(candle_obj)

    if not multiday_candles:
        print("[Info] fyers_client not provided or returned no candles. Using fallback demonstration dataset.")
        multiday_candles = generate_mock_multiday_data(num_days=num_days)

    sorted_dates = sorted(multiday_candles.keys())
    ldc_by_date: Dict[date, Dict[str, LDCHLLevels]] = {}

    for idx, current_date in enumerate(sorted_dates):
        ldc_by_date[current_date] = {}
        if idx > 0:
            prev_date = sorted_dates[idx - 1]
            for code in multiday_candles[current_date].keys():
                prev_candles = multiday_candles[prev_date].get(code, [])
                if prev_candles:
                    pdh = max(c.high for c in prev_candles)
                    pdl = min(c.low for c in prev_candles)
                    pdc = prev_candles[-1].close
                    trend = Direction.BULLISH if pdc >= prev_candles[0].open else Direction.BEARISH
                    ldc_by_date[current_date][code] = LDCHLLevels(pdh=pdh, pdl=pdl, pdc=pdc, daily_trend=trend)
        else:
            for code in multiday_candles[current_date].keys():
                day_c = multiday_candles[current_date][code]
                if day_c:
                    ldc_by_date[current_date][code] = LDCHLLevels(
                        pdh=max(c.high for c in day_c),
                        pdl=min(c.low for c in day_c),
                        pdc=day_c[-1].close,
                        daily_trend=Direction.BULLISH
                    )

    return multiday_candles, ldc_by_date


def generate_mock_multiday_data(num_days: int = 5) -> Dict[date, Dict[str, List[Candle]]]:
    """Generates synthetic multi-day 5-minute candle data for backtesting demonstration."""
    data = {}
    base_date = date(2026, 10, 12)

    for d in range(num_days):
        current_date = base_date + timedelta(days=d)
        bn_candles = []
        nf_candles = []

        bn_p = 51500.0 + (d * 100.0)
        nf_p = 24400.0 + (d * 50.0)

        start_dt = datetime.combine(current_date, time(9, 15))

        for b in range(75):
            bar_ts = start_dt + timedelta(minutes=5 * b)

            bn_high = bn_p + (20.0 if b % 2 == 0 else 80.0)
            bn_low = bn_p - (15.0 if b % 3 == 0 else 50.0)
            bn_close = bn_high - 10.0 if b % 2 == 0 else bn_low + 10.0
            bn_candles.append(Candle(bar_ts, bn_p, bn_high, bn_low, bn_close, volume=1000))
            bn_p = bn_close

            nf_high = nf_p + (10.0 if b % 2 == 0 else 35.0)
            nf_low = nf_p - (8.0 if b % 3 == 0 else 25.0)
            nf_close = nf_high - 5.0 if b % 2 == 0 else nf_low + 5.0
            nf_candles.append(Candle(bar_ts, nf_p, nf_high, nf_low, nf_close, volume=2000))
            nf_p = nf_close

        data[current_date] = {
            "BANKNIFTY": bn_candles,
            "NIFTY": nf_candles
        }

    return data


def run_backtest_mode(
    capital: float,
    expiry: str,
    fyers_client=None,
    range_from: Optional[str] = None,
    range_to: Optional[str] = None,
    export_csv_path: str = "trades_export.csv"
):
    """Executes multi-day historical backtest and prints date-by-date PnL table."""
    print("\n" + "=" * 80)
    print("        RUNNING HISTORICAL MULTI-DAY BACKTESTING ENGINE")
    print("=" * 80)

    engine = BacktestEngine(initial_capital=capital, banknifty_expiry=expiry, nifty_expiry=expiry)

    multiday_data, ldc_by_date = fetch_fyers_multiday_data(
        fyers_client=fyers_client,
        range_from=range_from,
        range_to=range_to,
        num_days=5
    )

    result = engine.run_backtest(multiday_data, daily_ldc_by_date=ldc_by_date)
    result.print_daily_pnl_table()

    if export_csv_path:
        csv_file = result.export_trades_to_csv(filepath=export_csv_path)
        print(f"[CSV Export] All {result.total_trades} trades successfully exported with entry/exit times & strategies to: {csv_file}\n")


def run_realtime_mode(
    capital: float,
    expiry: str,
    is_live: bool = False,
    fyers_client=None,
    export_csv_path: str = "trades_export.csv"
):
    """Executes live/paper trading session from 09:15 to 15:30 IST."""
    manager = MultiAssetTradingManager(
        total_capital=capital,
        fyers_client=fyers_client,
        is_paper=not is_live,
        banknifty_expiry=expiry,
        nifty_expiry=expiry
    )

    # 1. Automatically fetch LDCHL levels from Fyers API if client is available
    if fyers_client:
        manager.auto_fetch_levels_from_fyers(fyers_client=fyers_client)
    else:
        # Fallback: compute dynamic LDCHL levels from previous day's candles
        mock_data = generate_mock_multiday_data(num_days=2)
        sorted_dates = sorted(mock_data.keys())
        if len(sorted_dates) >= 2:
            prev_day = sorted_dates[-2]
            for code in ["BANKNIFTY", "NIFTY"]:
                c_list = mock_data[prev_day].get(code, [])
                if c_list:
                    pdh = max(c.high for c in c_list)
                    pdl = min(c.low for c in c_list)
                    pdc = c_list[-1].close
                    trend = Direction.BULLISH if pdc >= c_list[0].open else Direction.BEARISH
                    manager.set_levels(code, pdh=pdh, pdl=pdl, pdc=pdc, trend=trend)

    runner = RealtimeSessionRunner(manager)

    # Ingest today's candles
    mock_data = generate_mock_multiday_data(num_days=1)
    today_dt = list(mock_data.keys())[0]

    bn_candles = mock_data[today_dt]["BANKNIFTY"]
    nf_candles = mock_data[today_dt]["NIFTY"]

    runner.run_simulated_session(bn_candles, nf_candles, export_csv_path=export_csv_path)


def main():
    parser = argparse.ArgumentParser(description="Ghanshyam Tech Multi-Asset Options Trading Agent v3.1")
    parser.add_argument("--mode", type=str, choices=["backtest", "realtime", "live"], default="backtest",
                        help="Execution mode: 'backtest' (multi-day historical test), 'realtime' (paper simulation), or 'live' (Fyers API)")
    parser.add_argument("--capital", type=float, default=50000.0, help="Initial trading capital in INR (default: 500000)")
    parser.add_argument("--expiry", type=str, default="26OCT", help="Option expiry string e.g. '26OCT'")
    parser.add_argument("--range-from", type=str, default=None, help="Backtest start date 'YYYY-MM-DD'")
    parser.add_argument("--range-to", type=str, default=None, help="Backtest end date 'YYYY-MM-DD'")
    parser.add_argument("--export-csv", type=str, default="trades_export.csv", help="CSV filename to export all trade records (default: 'trades_export.csv')")
    parser.add_argument("--client-id", type=str, default=None, help="Fyers API Client ID (required for live mode)")
    parser.add_argument("--access-token", type=str, default=None, help="Fyers API Access Token (required for live mode)")

    args = parser.parse_args()

    fyers_client = None
    
    try:
        #from fyers_apiv3 import fyersModel
        fyers_client = get_fyers_client()
        #fyers_client = fyersModel.FyersModel(client_id=args.client_id, token=args.access_token, is_async=False, log_path="")
        print("Successfully authenticated Fyers API client.")
    except ImportError:
        print("fyers_apiv3 library not installed. Continuing without live Fyers API connection.")

    if args.mode == "backtest":
        run_backtest_mode(
            capital=args.capital,
            expiry=args.expiry,
            fyers_client=fyers_client,
            range_from=args.range_from,
            range_to=args.range_to,
            export_csv_path=args.export_csv
        )
    else:
        run_realtime_mode(
            capital=args.capital,
            expiry=args.expiry,
            is_live=(args.mode == "live"),
            fyers_client=fyers_client,
            export_csv_path=args.export_csv
        )


if __name__ == "__main__":
    main()
