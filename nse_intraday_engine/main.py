"""
CLI entry point.

    python main.py --mode backtest --from 2024-01-01 --to 2024-06-30
    python main.py --mode paper
"""
from __future__ import annotations

import argparse

from config import config
from engine import StrategyEngine


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NSE Intraday Trading & Backtesting Engine")
    p.add_argument("--mode", choices=["backtest", "paper"], help="Overrides MODE from .env")
    p.add_argument("--from", dest="date_from", help="Backtest start date YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="Backtest end date YYYY-MM-DD")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode:
        config.MODE = "BACKTEST" if args.mode == "backtest" else "PAPER_TRADING"
    if args.date_from:
        config.BACKTEST_FROM = args.date_from
    if args.date_to:
        config.BACKTEST_TO = args.date_to

    print(f"Starting engine in {config.MODE} mode...")
    engine = StrategyEngine(config)
    engine.run()


if __name__ == "__main__":
    main()
