import os
import unittest
import pandas as pd
import numpy as np
import yaml
from data.fyers_client import FyersDataClient
from data.data_loader import IndicatorCalculator
from strategies.base_strategy import BaseStrategy, TradeSignal
from strategies.minervini_vcp import MinerviniVCPStrategy
from strategies.darvas_box import DarvasBoxStrategy
from strategies.oneil_cup_handle import ONeilCupHandleStrategy
from strategies.kullamagi_momentum import KullamagiMomentumStrategy
from engine.backtester import BacktestEngine

class TestBreakoutStrategies(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(base_dir, "config", "config.yaml")
        with open(config_path, "r") as f:
            cls.config = yaml.safe_load(f)
        client = FyersDataClient(use_mock=False)
        cls.raw_df = client.get_historical_data("NSE:RELIANCE-EQ", "D", "2026-01-01", "2026-09-30")
        cls.ind_df = IndicatorCalculator.add_indicators(cls.raw_df)

    def test_indicator_calculator(self):
        df = self.ind_df
        self.assertIn('sma_50', df.columns)
        self.assertIn('sma_200', df.columns)
        self.assertIn('ema_10', df.columns)
        self.assertIn('adr_20', df.columns)
        self.assertIn('atr_20', df.columns)

    def test_position_sizing_formula(self):
        class DummyStrat(BaseStrategy):
            def generate_signals(self, df, symbol, index_df=None):
                return None
        strat = DummyStrat("Dummy", {})
        res = strat.calculate_position_size(account_equity=1000000.0, entry_price=100.0, stop_loss_price=90.0, risk_pct=1.0)
        self.assertEqual(res["shares"], 1000) # (1000000 * 0.01) / 10 = 1000 shares

    def test_minervini_vcp_strategy(self):
        strat = MinerviniVCPStrategy(self.config["strategies"]["minervini_vcp"])
        sig = strat.generate_signals(self.ind_df, "NSE:RELIANCE-EQ")
        # May be None or TradeSignal
        if sig is not None:
            self.assertEqual(sig.symbol, "NSE:RELIANCE-EQ")

    def test_darvas_box_strategy(self):
        strat = DarvasBoxStrategy(self.config["strategies"]["darvas_box"])
        sig = strat.generate_signals(self.ind_df, "NSE:RELIANCE-EQ")
        if sig is not None:
            self.assertEqual(sig.symbol, "NSE:RELIANCE-EQ")

    def test_oneil_cup_handle_strategy(self):
        strat = ONeilCupHandleStrategy(self.config["strategies"]["oneil_cup_handle"])
        sig = strat.generate_signals(self.ind_df, "NSE:RELIANCE-EQ")
        if sig is not None:
            self.assertEqual(sig.symbol, "NSE:RELIANCE-EQ")

    def test_kullamagi_momentum_strategy(self):
        strat = KullamagiMomentumStrategy(self.config["strategies"]["kullamagi_momentum"])
        sig = strat.generate_signals(self.ind_df, "NSE:RELIANCE-EQ")
        if sig is not None:
            self.assertEqual(sig.symbol, "NSE:RELIANCE-EQ")

    def test_backtester(self):
        strat = DarvasBoxStrategy(self.config["strategies"]["darvas_box"])
        backtester = BacktestEngine(initial_capital=1000000.0)
        results = backtester.run_backtest(strat, self.ind_df, "NSE:RELIANCE-EQ")
        self.assertIn("stats", results)
        self.assertIn("total_trades", results["stats"])

if __name__ == "__main__":
    unittest.main()
