import unittest
import pandas as pd
from execution.paper_broker import PaperBroker
from execution.fyers_order_manager import FyersOrderManager
from execution.order_router import OrderRouter

class TestExecutionAndOrderRouting(unittest.TestCase):

    def setUp(self):
        self.config = {
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
        router = OrderRouter(broker, self.config)

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

        # 1% of 1,000,000 = ₹10,000 risk budget. Risk/share = ₹50. Qty = 10,000 / 50 = 200 shares
        pos = broker.get_open_positions()[0]
        self.assertEqual(pos["quantity"], 200)

if __name__ == "__main__":
    unittest.main()
