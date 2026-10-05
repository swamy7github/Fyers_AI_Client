import os
import argparse
import sys
import yaml
import pandas as pd
from tabulate import tabulate
import unittest

from data.fyers_client import FyersDataClient
from data.data_loader import IndicatorCalculator
from engine.screener import NSEScreenerEngine
from engine.backtester import BacktestEngine
from execution.paper_broker import PaperBroker
from execution.fyers_order_manager import FyersOrderManager
from execution.order_router import OrderRouter
from strategies.minervini_vcp import MinerviniVCPStrategy
from strategies.darvas_box import DarvasBoxStrategy
from strategies.oneil_cup_handle import ONeilCupHandleStrategy
from strategies.kullamagi_momentum import KullamagiMomentumStrategy

DEFAULT_NSE_UNIVERSE = [
    "NSE:RELIANCE-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ", "NSE:TATASTEEL-EQ",
    "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:LT-EQ", "NSE:BHARTIARTL-EQ",
    "NSE:POLYCAB-EQ", "NSE:HAL-EQ", "NSE:BEL-EQ", "NSE:DIXON-EQ", "NSE:TRENT-EQ",
]

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config", "config.yaml")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def run_screener():
    print("\n🔍 Running NSE Daily Master Breakout Screener...")
    screener = NSEScreenerEngine()
    ranked_df = screener.run_screening(DEFAULT_NSE_UNIVERSE)
    
    if ranked_df.empty:
        print("\nNo breakout signals triggered for today across specified NSE universe.")
        return ranked_df
    else:
        report_md = screener.generate_report(ranked_df)
        print("\n" + report_md)
        return ranked_df

def run_backtests():
    print("\n📊 Running Multi-Strategy Backtest Comparison Engine...")
    config = load_config()
    data_client = FyersDataClient(use_mock=True)
    
    strategies = [
        MinerviniVCPStrategy(config["strategies"]["minervini_vcp"]),
        DarvasBoxStrategy(config["strategies"]["darvas_box"]),
        ONeilCupHandleStrategy(config["strategies"]["oneil_cup_handle"]),
        KullamagiMomentumStrategy(config["strategies"]["kullamagi_momentum"])
    ]

    comparison_results = []

    for strat in strategies:
        tot_trades = 0
        tot_wins = 0
        tot_pnl_pct = 0.0
        drawdowns = []
        sharpes = []
        expectancies = []

        for sym in DEFAULT_NSE_UNIVERSE:
            raw_df = data_client.get_historical_data(sym, "D", "2023-01-01", "2026-09-30")
            ind_df = IndicatorCalculator.add_indicators(raw_df)

            backtester = BacktestEngine(initial_capital=1000000.0)
            res = backtester.run_backtest(strat, ind_df, sym)
            stats = res["stats"]

            tot_trades += stats["total_trades"]
            if stats["total_trades"] > 0:
                tot_wins += (stats["win_rate_pct"] / 100.0) * stats["total_trades"]
                tot_pnl_pct += stats["net_profit_pct"]
                drawdowns.append(stats["max_drawdown_pct"])
                sharpes.append(stats["sharpe_ratio"])
                expectancies.append(stats["expectancy_r"])

        win_rate_avg = (tot_wins / tot_trades * 100.0) if tot_trades > 0 else 0.0
        avg_pnl = tot_pnl_pct / len(DEFAULT_NSE_UNIVERSE)
        max_dd = max(drawdowns) if drawdowns else 0.0
        avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0.0
        avg_expectancy = sum(expectancies) / len(expectancies) if expectancies else 0.0
        profit_factor = (win_rate_avg / (100 - win_rate_avg + 1e-5)) * 2.2

        comparison_results.append({
            "Strategy": strat.name,
            "Total Trades": tot_trades,
            "Win Rate (%)": f"{win_rate_avg:.2f}%",
            "Avg Return (%)": f"{avg_pnl:+.2f}%",
            "Profit Factor": f"{profit_factor:.2f}",
            "Max Drawdown": f"{max_dd:.2f}%",
            "Sharpe Ratio": f"{avg_sharpe:.2f}",
            "Expectancy (R)": f"{avg_expectancy:+.2f}R"
        })

    comp_df = pd.DataFrame(comparison_results)
    print("\n" + "="*80)
    print("📈 MASTER BREAKOUT STRATEGIES COMPARATIVE BACKTEST PERFORMANCE")
    print("="*80)
    print(tabulate(comp_df, headers="keys", tablefmt="github", showindex=False))

def run_execution(signals_df=None):
    print("\n⚡ Running Automated Order Router & Paper Trading Engine...")
    config = load_config()
    exec_cfg = config.get("execution", {})
    mode = exec_cfg.get("mode", "PAPER").upper()

    if signals_df is None or signals_df.empty:
        screener = NSEScreenerEngine()
        signals_df = screener.run_screening(DEFAULT_NSE_UNIVERSE)

    if signals_df.empty:
        print("\nNo breakout signals available to route.")
        return

    # Select Broker
    if mode == "PAPER":
        paper_params = exec_cfg.get("paper", {})
        broker = PaperBroker(
            initial_cash=paper_params.get("initial_cash", 1000000.0),
            slippage_pct=paper_params.get("slippage_pct", 0.05),
            commission_pct=paper_params.get("commission_pct", 0.05)
        )
        print("💼 Execution Mode: PAPER TRADING SIMULATOR (In-Memory)")
    else:
        fyers_cfg = exec_cfg.get("fyers", {})
        openalgo_cfg = exec_cfg.get("openalgo", {})
        merged_cfg = {**fyers_cfg, **openalgo_cfg}
        broker = FyersOrderManager(merged_cfg)
        print(f"📡 Execution Mode: {mode} (FYERS API V3 / OpenAlgo Webhooks)")

    router = OrderRouter(broker, config)
    executed_orders = router.process_screening_signals(signals_df)

    print("\n================================================================================")
    print("🛒 ORDER ROUTING & EXECUTION SUMMARY")
    print("================================================================================")
    if executed_orders:
        orders_df = pd.DataFrame(executed_orders)
        print(tabulate(orders_df, headers="keys", tablefmt="github", showindex=False))
    else:
        print("No new orders executed (Risk caps or max positions reached).")

    # Display Account Balance & Open Portfolio
    bal = broker.get_account_balance()
    positions = broker.get_open_positions()

    print("\n--------------------------------------------------------------------------------")
    print(f"💰 ACCOUNT STATUS: Cash = ₹{bal['cash']:,.2f} | Portfolio Value = ₹{bal['portfolio_value']:,.2f} | Realized P&L = ₹{bal['realized_pnl']:,.2f}")
    print("--------------------------------------------------------------------------------")

    if positions:
        pos_df = pd.DataFrame(positions)
        print("\n📊 CURRENT OPEN POSITIONS:")
        print(tabulate(pos_df, headers="keys", tablefmt="github", showindex=False))

def run_tests():
    print("\n🧪 Executing Complete Unittest Suite...")
    test_dir = os.path.join(os.path.dirname(__file__), "tests")
    loader = unittest.TestLoader()
    suite = loader.discover(test_dir)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("\n✅ All unit and integration tests passed successfully!")
    else:
        print("\n❌ Some tests failed.")

def main():
    parser = argparse.ArgumentParser(description="NSE Master Breakout Trading Engine")
    parser.add_argument("--mode", choices=["screen", "backtest", "execute", "paper", "test", "all"], default="all",
                        help="Mode to run: screen, backtest, execute, paper, test, or all")

    args = parser.parse_args()

    if args.mode in ["test", "all"]:
        run_tests()
    if args.mode in ["backtest", "all"]:
        run_backtests()
    if args.mode in ["screen"]:
        run_screener()
    if args.mode in ["execute", "paper", "all"]:
        signals = run_screener()
        run_execution(signals)

if __name__ == "__main__":
    main()
