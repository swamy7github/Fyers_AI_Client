import unittest
import pandas as pd
from execution.paper_broker import PaperBroker
from execution.fyers_order_manager import FyersOrderManager
from execution.order_router import OrderRouter
from data.fyers_client import FyersDataClient

class TestExecutionAndOrderRouting(unittest.TestCase):

    def setUp(self):
        self.config = {
            "timeframe": {
                "primary": "DAILY",
                "intraday_execution_enabled": True,
                "intraday_resolution": "5",
                "orb_window_minutes": 15,
                "intraday_volume_surge_mult": 1.5,
                "default_product_type": "MIS"
            },
            "execution": {
                "mode": "PAPER",
                "paper": {"initial_cash": 1000000.0, "slippage_pct": 0.05, "commission_pct": 0.05},
                "fyers": {"client_id": "MOCK_CLIENT_ID", "access_token": "MOCK_TOKEN", "use_mock": True},
                "openalgo": {"openalgo_url": "http://127.0.0.1:5000/api/v1/place_order", "openalgo_enabled": True}
            },
            "portfolio_risk": {
                "capital": 1000000.0,
                "risk_per_trade_pct": 1.0,
                "max_portfolio_allocation_per_stock_pct": 20.0,
                "max_open_positions": 5
            }
        }

    def test_paper_broker_order_execution(self):
        broker = PaperBroker(initial_cash=1000000.0, slippage_pct=0.05, commission_pct=0.05)
        res = broker.place_order(
            symbol="NSE:TATASTEEL-EQ",
            side="BUY",
            quantity=100,
            order_type="LIMIT",
            price=150.0,
            stop_loss=140.0,
            target=180.0,
            tag="Mark Minervini VCP"
        )
        self.assertEqual(res["status"], "EXECUTED")
        self.assertIn("fill_price", res)
        self.assertGreater(res["fill_price"], 150.0) # Slippage added

        positions = broker.get_open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["symbol"], "NSE:TATASTEEL-EQ")

        bal = broker.get_account_balance()
        self.assertLess(bal["cash"], 1000000.0)

    def test_paper_broker_short_trade_execution(self):
        broker = PaperBroker(initial_cash=1000000.0, slippage_pct=0.05, commission_pct=0.05)
        res = broker.place_order(
            symbol="NSE:INFY-EQ",
            side="SELL",
            quantity=100,
            order_type="LIMIT",
            price=200.0,
            stop_loss=210.0,  # Stop loss higher than entry
            target=180.0,     # Target lower than entry
            product_type="MIS",
            tag="Mark Minervini VCP (SHORT)"
        )
        self.assertEqual(res["status"], "EXECUTED")
        positions = broker.get_open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["side"], "SELL")

        # Test market tick triggering stop loss on short position (price rises to 212 >= 210)
        broker.process_market_tick("NSE:INFY-EQ", current_price=211.0, high_price=212.0)
        self.assertEqual(len(broker.get_open_positions()), 0) # Position closed

    def test_paper_broker_stop_loss_trigger(self):
        broker = PaperBroker(initial_cash=1000000.0)
        broker.place_order("NSE:INFY-EQ", "BUY", 100, price=100.0, stop_loss=90.0, target=130.0)

        # Trigger market tick with price dropping below stop loss (85 < 90)
        broker.process_market_tick("NSE:INFY-EQ", current_price=88.0, low_price=85.0)

        # Position should now be closed due to stop loss auto-trigger
        positions = broker.get_open_positions()
        self.assertEqual(len(positions), 0)
        orders = broker.get_order_history()
        self.assertEqual(len(orders), 2) # Buy + Auto Stop Sell

    def test_fyers_order_manager_payload(self):
        fyers = FyersOrderManager({"client_id": "MOCK", "access_token": "MOCK", "use_mock": True})
        res = fyers.place_order(
            symbol="NSE:RELIANCE-EQ",
            side="BUY",
            quantity=50,
            order_type="LIMIT",
            price=2800.0,
            stop_loss=2700.0,
            target=3000.0,
            tag="Darvas Box Breakout"
        )
        self.assertEqual(res["status"], "SUBMITTED")
        self.assertIn("payload", res)
        self.assertEqual(res["payload"]["symbol"], "NSE:RELIANCE-EQ")
        self.assertEqual(res["payload"]["qty"], 50)

    def test_order_router_position_sizing(self):
        broker = PaperBroker(initial_cash=1000000.0)
        
        cfg = self.config.copy()
        cfg["timeframe"]["intraday_execution_enabled"] = False
        router = OrderRouter(broker, cfg)

        signals_data = [{
            "symbol": "NSE:HDFCBANK-EQ",
            "strategy": "Mark Minervini VCP",
            "entry_price": 1000.0,
            "stop_loss": 950.0,  # Risk per share = ₹50
            "target": 1150.0
        }]
        signals_df = pd.DataFrame(signals_data)

        executed = router.process_screening_signals(signals_df)
        self.assertEqual(len(executed), 1)

        pos = broker.get_open_positions()[0]
        self.assertEqual(pos["quantity"], 200)

    def test_intraday_5m_data_loader(self):
        client = FyersDataClient(use_mock=False)
        df_5m = client.get_intraday_data("NSE:RELIANCE-EQ", resolution="5")
        self.assertFalse(df_5m.empty)
        self.assertEqual(len(df_5m), 50)  # Assuming 50 bars for the test period
        self.assertIn("close", df_5m.columns)
        self.assertIn("volume", df_5m.columns)

    def test_intraday_orb_trigger_evaluation(self):
        client = FyersDataClient(use_mock=False)
        broker = PaperBroker(initial_cash=1000000.0)
        router = OrderRouter(broker, self.config)

        daily_row = pd.Series({
            "symbol": "NSE:TATASTEEL-EQ",
            "strategy": "Mark Minervini VCP",
            "composite_score": 105.0
        })

        intraday_df = client.get_intraday_data("NSE:TATASTEEL-EQ", resolution="5")
        trigger = router.evaluate_intraday_orb_trigger("NSE:TATASTEEL-EQ", daily_row, intraday_df)
        
        self.assertIsNotNone(trigger)
        self.assertIn("orb_high", trigger)
        self.assertIn("orb_low", trigger)
        self.assertGreater(trigger["entry_price"], trigger["stop_loss"])

if __name__ == "__main__":
    unittest.main()
