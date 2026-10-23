#!/usr/bin/env python3
"""Command-line entry point for the Ghanshyam Options Trading Framework.

Examples
--------
    python main.py --mode backtest --symbol NIFTY --date 2026-06-01
    python main.py --mode backtest --symbol BANKNIFTY --start 2026-05-01 --end 2026-06-30
    python main.py --mode backtest --symbol NIFTY --date 2026-06-01 --csv data/nifty_5min.csv
    python main.py --mode paper --symbol BANKNIFTY
    python main.py --mode eod_report --date 2026-06-01
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from config.settings import (LOG_DIR, TRADES_LOG_PATH, ExecutionConfig, PaperConfig, RiskConfig,
                             StrategyConfig, get_instrument, with_overrides)
from core.backtester import Backtester, BlackScholesPricer
from core.data_handler import CsvDataSource, FyersDataSource, LiveCandleFeed
from core.logger_handler import TradeLogger, format_metrics, generate_eod_summary, setup_logging
from core.paper_trader import PaperTrader, ist_now
from core.strategy import resolve_underlying_symbol

LOGGER = logging.getLogger("ghanshyam")


def build_fyers_source() -> FyersDataSource:
    """Connect the data layer to the already-implemented ``core/fyers_client.py``.

    The wrapper's interface is not known to this framework, so this function
    probes for common names. If yours differs, edit it here: all that is needed
    is a history callable ``(symbol, resolution, start_date, end_date)`` that
    returns candles, and an optional quote callable ``(symbol) -> last price``.
    """
    try:
        from fyersclient.fyers_client import get_fyers_client  # type: ignore
        fyers_client = get_fyers_client()
    except ImportError as exc:
        raise SystemExit(f"core/fyers_client.py could not be imported ({exc}). "
                         "Add it, or use --csv for an offline backtest.")

    def pick(*names: str):
        return next((getattr(fyers_client, n) for n in names if hasattr(fyers_client, n)), None)

    history = pick("get_candles_range", "get_history", "fetch_history", "get_historical_data")
    quote = pick("get_quote", "get_ltp", "get_quotes")
    if history is not None:
        return FyersDataSource(lambda sym, res, start, end: history(sym, res, start, end), quote)

    factory = pick("get_fyers_client", "get_client", "create_client", "get_fyers")
    if factory is not None:
        return FyersDataSource.from_fyers_model(factory())
    raise SystemExit("Could not find history hooks in core/fyers_client.py. "
                     "Edit build_fyers_source() in main.py to match your wrapper.")


def parse_date(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date {text!r}, expected YYYY-MM-DD")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py", description="Ghanshyam Options Trading Framework (5-minute options)")
    parser.add_argument("--mode", required=True, choices=["backtest", "paper", "live", "eod_report"])
    parser.add_argument("--symbol", default="NIFTY",
                        help="NIFTY, BANKNIFTY, CRUDEOIL or NATURALGAS (default NIFTY)")
    parser.add_argument("--date", type=parse_date, help="single day (backtest) or report day")
    parser.add_argument("--start", type=parse_date, help="backtest start date")
    parser.add_argument("--end", type=parse_date, help="backtest end date")
    parser.add_argument("--csv", type=Path, help="offline 5-min candle CSV for the backtest")
    parser.add_argument("--lots", type=int, help="fixed lots per trade")
    parser.add_argument("--risk-per-trade", type=float, help="INR risk budget used to size lots")
    parser.add_argument("--max-daily-loss", type=float, help="INR daily loss limit")
    parser.add_argument("--iv", type=float, help="annualised IV for the premium model (e.g. 0.15)")
    parser.add_argument("--strike-mode", choices=["ATM", "ITM", "OTM"])
    parser.add_argument("--strike-steps", type=int, help="strike steps away from ATM")
    parser.add_argument("--slippage", type=float, help="slippage as a fraction of premium (0.005)")
    parser.add_argument("--report-mode", default="paper",
                        help="trade mode to summarise in eod_report: paper (default) or all")
    parser.add_argument("--log-file", type=Path, help="also write the console log to this file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def configs_from_args(args: argparse.Namespace):
    """Build config objects, applying CLI overrides to the defaults."""
    strategy = with_overrides(StrategyConfig(), strike_mode=args.strike_mode,
                              strike_steps=args.strike_steps)
    risk = with_overrides(RiskConfig(), lots=args.lots, risk_per_trade_inr=args.risk_per_trade,
                          max_daily_loss_inr=args.max_daily_loss)
    execution = with_overrides(ExecutionConfig(), slippage_pct=args.slippage)
    return strategy, risk, execution


def run_backtest(args: argparse.Namespace) -> int:
    if args.date:
        start = end = args.date
    elif args.start and args.end:
        start, end = args.start, args.end
    else:
        print("Backtest needs --date, or both --start and --end", file=sys.stderr)
        return 2
    instrument = get_instrument(args.symbol)
    strategy_cfg, risk_cfg, exec_cfg = configs_from_args(args)
    source = CsvDataSource(args.csv) if args.csv else build_fyers_source()

    out_path = LOG_DIR / f"backtest_{instrument.name}_{start}_{end}.csv"
    out_path.unlink(missing_ok=True)
    backtester = Backtester(source, instrument, strategy_cfg, risk_cfg, exec_cfg,
                            pricer=BlackScholesPricer(instrument, iv=args.iv))
    result = backtester.run(start, end, trade_logger=TradeLogger(out_path))

    print(format_metrics(result.metrics, f"BACKTEST {instrument.name} {start}..{end}"))
    if result.daily_pnl:
        print("\nDaily net PnL")
        for day, pnl in sorted(result.daily_pnl.items()):
            print(f"  {day}  {pnl:>12,.2f}")
    if result.skipped_days:
        print(f"\n{len(result.skipped_days)} weekday(s) skipped (no data or fetch error)")
    print(f"\nTrades written to {out_path}")
    print("Premiums are Black-Scholes estimates, not historical option prices.")
    return 0


def run_paper(args: argparse.Namespace) -> int:
    instrument = get_instrument(args.symbol)
    strategy_cfg, risk_cfg, exec_cfg = configs_from_args(args)
    source = build_fyers_source()
    feed = LiveCandleFeed(source, resolve_underlying_symbol(instrument, ist_now().date()))
    trader = PaperTrader(
        instrument, feed, quote_fn=source.ltp, strategy_config=strategy_cfg,
        risk_config=risk_cfg, execution_config=exec_cfg, paper_config=PaperConfig(),
        pricer=BlackScholesPricer(instrument, iv=args.iv),
        trade_logger=TradeLogger(TRADES_LOG_PATH))
    trader.run()
    return 0


def run_eod_report(args: argparse.Namespace) -> int:
    day = args.date or ist_now().date()
    mode = None if args.report_mode == "all" else args.report_mode
    generate_eod_summary(day, mode=mode, log_path=TRADES_LOG_PATH, out_dir=LOG_DIR)
    return 0


def run_live(_: argparse.Namespace) -> int:
    print("Live order placement is intentionally NOT implemented.\n"
          "Run paper mode until the results hold up, then add an order-execution "
          "layer with its own safeguards (kill switch, max order size, duplicate "
          "order protection) before sending anything to a real account.",
          file=sys.stderr)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO, args.log_file)
    try:
        handlers = {"backtest": run_backtest, "paper": run_paper,
                    "eod_report": run_eod_report, "live": run_live}
        return handlers[args.mode](args)
    except KeyError as exc:          # unknown symbol
        print(f"Error: {exc.args[0]}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
