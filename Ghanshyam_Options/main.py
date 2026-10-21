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


def fetch_fyers_ldchl_for_date(fyers_client, target_date: date, symbol: str) -> Optional[LDCHLLevels]:
    """
    Queries Fyers API (resolution='D') to fetch the last traded session OHLC
    strictly prior to target_date (e.g., target 2026-10-12 returns 2026-10-09 values).
    """
    if not fyers_client or not hasattr(fyers_client, "history"):
        return None
    try:
        range_to = target_date.strftime("%Y-%m-%d")
        range_from = (target_date - timedelta(days=15)).strftime("%Y-%m-%d")
        req_data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
            "cont_flag": "1"
        }
        res = fyers_client.history(data=req_data)
        if res and res.get("s") == "ok":
            candles = res.get("candles", [])
            prior_candles = []
            for c in candles:
                c_dt = datetime.fromtimestamp(c[0]).date()
                if c_dt < target_date:
                    prior_candles.append((c_dt, c))
            if prior_candles:
                last_traded_date, last_bar = prior_candles[-1]
                pdo = float(last_bar[1])
                pdh = float(last_bar[2])
                pdl = float(last_bar[3])
                pdc = float(last_bar[4])
                trend = Direction.BULLISH if pdc >= pdo else Direction.BEARISH
                
                print(
                    f"[Fyers LDCHL API] Target Date: {target_date} | "
                    f"Last Traded Date: {last_traded_date} | "
                    f"PDH={pdh:.1f}, PDL={pdl:.1f}, PDC={pdc:.1f}, Trend={trend.value}"
                )
                logger.info(
                    f"Fyers LDCHL for target {target_date}: Last traded date was {last_traded_date} | "
                    f"PDH={pdh}, PDL={pdl}, PDC={pdc}, Trend={trend.value}"
                )
                return LDCHLLevels(pdh=pdh, pdl=pdl, pdc=pdc, daily_trend=trend)
    except Exception as e:
        logger.error(f"Error fetching LDCHL from Fyers API: {e}")
    return None


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
    Automatically looks back to include the last traded session prior to range_from (e.g. 2026-10-09 before 2026-10-12).
    Calculates dynamic Previous Day High, Low, and Close (LDCHL) benchmarks automatically.
    """
    if symbols is None:
        symbols = {
            "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
            "NIFTY": "NSE:NIFTY50-INDEX"
        }

    if not range_to:
        range_to = datetime.now().strftime("%Y-%m-%d")

    range_from_dt = datetime.strptime(range_from, "%Y-%m-%d").date() if range_from else (datetime.now() - timedelta(days=num_days)).date()
    extended_from = (range_from_dt - timedelta(days=15)).strftime("%Y-%m-%d")

    multiday_candles: Dict[date, Dict[str, List[Candle]]] = {}

    if fyers_client and hasattr(fyers_client, "history"):
        print(f"\n[Fyers API] Fetching historical candles via fyers_client.history() [{extended_from} to {range_to}]...")
        for code, sym in symbols.items():
            req_data = {
                "symbol": sym,
                "resolution": resolution,
                "date_format": "1",
                "range_from": extended_from,
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
            prev_date = sorted_dates[idx - 1]  # Exact last traded session before current_date!
            for code in multiday_candles[current_date].keys():
                prev_candles = multiday_candles[prev_date].get(code, [])
                if prev_candles:
                    pdh = max(c.high for c in prev_candles)
                    pdl = min(c.low for c in prev_candles)
                    pdc = prev_candles[-1].close
                    trend = Direction.BULLISH if pdc >= prev_candles[0].open else Direction.BEARISH
                    ldc_by_date[current_date][code] = LDCHLLevels(pdh=pdh, pdl=pdl, pdc=pdc, daily_trend=trend)
                    if current_date >= range_from_dt:
                        print(f"[LDCHL Ingestion] Target Date: {current_date} | Last Traded Date Used: {prev_date} | PDH={pdh:.1f}, PDL={pdl:.1f}, PDC={pdc:.1f}")

    # Retain candles starting from range_from_dt for the backtest execution
    backtest_candles = {dt: v for dt, v in multiday_candles.items() if dt >= range_from_dt}

    return backtest_candles, ldc_by_date


def generate_mock_multiday_data(num_days: int = 5) -> Dict[date, Dict[str, List[Candle]]]:
    """Generates synthetic multi-day 5-minute candle data including the prior traded session (2026-10-09)."""
    data = {}
    trading_dates = [date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]

    for d, current_date in enumerate(trading_dates):
        bn_candles = []
        nf_candles = []

        bn_p = 51400.0 + (d * 100.0)
        nf_p = 24350.0 + (d * 50.0)

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
    print("      RUNNING HISTORICAL MULTI-DAY BACKTESTING ENGINE (SPOT INDEX)")
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

    if fyers_client:
        target_dt = date(2026, 10, 12) if not is_live else datetime.now().date()
        manager.auto_fetch_levels_from_fyers(target_date=target_dt)
    else:
        manager.set_levels("BANKNIFTY", pdh=52000.0, pdl=51000.0, pdc=51500.0, trend=Direction.BULLISH)
        manager.set_levels("NIFTY", pdh=24600.0, pdl=24300.0, pdc=24450.0, trend=Direction.BULLISH)

    runner = RealtimeSessionRunner(manager)

    mock_data = generate_mock_multiday_data(num_days=1)
    today_dt = list(mock_data.keys())[1]  # Target date 2026-10-12

    bn_candles = mock_data[today_dt]["BANKNIFTY"]
    nf_candles = mock_data[today_dt]["NIFTY"]

    runner.run_simulated_session(bn_candles, nf_candles, export_csv_path=export_csv_path)


def main():
    parser = argparse.ArgumentParser(description="Ghanshyam Tech Multi-Asset Options Trading Agent v3.1")
    parser.add_argument("--mode", type=str, choices=["backtest", "realtime", "live"], default="backtest",
                        help="Execution mode: 'backtest' (multi-day historical test), 'realtime' (paper simulation), or 'live' (Fyers API)")
    parser.add_argument("--capital", type=float, default=500000.0, help="Initial trading capital in INR (default: 500000)")
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
