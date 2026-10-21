"""
Unit Test Suite for Ghanshyam Trader Engine
"""

import os
import unittest
from datetime import datetime, time, timedelta

from config import IndexConfig
from models import Candle, Direction, LDCHLLevels, OptionType, StrategyId
from risk import RiskEngine
from broker import PaperTradingBroker
from strikes import OptionStrikeSelector
from strategies import StrategyRegistry, OpeningRange920Strategy


class TestTradingEngine(unittest.TestCase):

    def setUp(self):
        self.risk = RiskEngine(total_capital=500000.0)
        self.broker = PaperTradingBroker(initial_capital=500000.0)
        self.config = IndexConfig.bank_nifty_default()

    def test_paper_broker_order_lifecycle_and_csv_export(self):
        t_entry = datetime(2026, 10, 19, 9, 20)
        pos = self.broker.place_option_order(
            symbol="NSE:BANKNIFTY26OCT51500CE",
            underlying_code="BANKNIFTY",
            option_type=OptionType.CE,
            strike=51500.0,
            quantity=15,
            option_ltp=300.0,
            timestamp=t_entry,
            spot_entry=51600.0,
            spot_sl=51530.0,
            spot_target=51800.0,
            strategy=StrategyId.STRATEGY_920_AM,
            option_sl=240.0,
            option_target=400.0,
            entry_reason="9:20 AM Candle High Breach"
        )
        self.assertIsNotNone(pos)
        self.assertEqual(pos.entry_reason, "9:20 AM Candle High Breach")

        t_exit = datetime(2026, 10, 19, 9, 35)
        closed = self.broker.close_position(
            identifier=pos.position_id,
            current_ltp=400.0,
            timestamp=t_exit,
            exit_reason="Target Achieved"
        )
        self.assertIsNotNone(closed)
        self.assertEqual(closed.exit_reason, "Target Achieved")

        csv_file = "/workspace/scratch/test_trades.csv"
        res_file = self.broker.export_trades_to_csv(filepath=csv_file)
        self.assertTrue(os.path.exists(res_file))

        with open(res_file, "r") as f:
            content = f.read()
            self.assertIn("9:20 AM Candle High Breach", content)
            self.assertIn("Target Achieved", content)
            self.assertIn("NSE:BANKNIFTY26OCT51500CE", content)

        if os.path.exists(csv_file):
            os.remove(csv_file)

    def test_strike_selection(self):
        strike_ce = OptionStrikeSelector.get_itm_strike(51585.0, OptionType.CE, self.config)
        self.assertEqual(strike_ce, 51500.0)

        strike_pe = OptionStrikeSelector.get_itm_strike(51585.0, OptionType.PE, self.config)
        self.assertEqual(strike_pe, 51700.0)

    def test_consecutive_losses_lockout(self):
        self.risk.record_trade_completion(-5000.0)
        self.risk.record_trade_completion(-4000.0)
        can_trade, reason = self.risk.can_take_trade()
        self.assertFalse(can_trade)
        self.assertIn("consecutive losses", reason)


if __name__ == "__main__":
    unittest.main()
