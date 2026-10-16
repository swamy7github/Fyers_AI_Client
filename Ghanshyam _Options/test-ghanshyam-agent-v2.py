"""
================================================================================
COMPREHENSIVE UNIT TEST SUITE FOR GHANSHYAM MULTI-ASSET TRADING SYSTEM (v2.0)
================================================================================
"""

import os
import sys
import unittest
from datetime import datetime, time, timedelta

# Ensure parent directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ghanshyam_options_agent_v2 import (
    Candle, Direction, OptionType, StrategyId, TradeSignal,
    LDCHLLevels, IndexConfig, OptionStrikeSelector,
    OpeningRange920Strategy, BullishHammerReversalStrategy,
    BearishShootingStarReversalStrategy, InsideCandleCompressionStrategy,
    MomentumScalp220Strategy, StrategyRegistry,
    RiskEngine, PaperTradingBroker, IndexTradingAgent, MultiAssetTradingManager
)


class TestInstrumentConfigAndStrikeSelector(unittest.TestCase):
    def setUp(self):
        self.bn_cfg = IndexConfig.bank_nifty_default(expiry="26OCT", lot_size=15)
        self.nf_cfg = IndexConfig.nifty_50_default(expiry="26OCT", lot_size=25)

    def test_banknifty_strike_and_symbol(self):
        # Bank Nifty Spot: 51230 -> ATM is 51200
        # ITM CE: 51200 - 100 = 51100
        # ITM PE: 51200 + 100 = 51300
        ce_strike = OptionStrikeSelector.get_itm_strike(51230.0, OptionType.CE, self.bn_cfg)
        self.assertEqual(ce_strike, 51100.0)

        pe_strike = OptionStrikeSelector.get_itm_strike(51230.0, OptionType.PE, self.bn_cfg)
        self.assertEqual(pe_strike, 51300.0)

        sym = OptionStrikeSelector.format_fyers_symbol(self.bn_cfg, ce_strike, OptionType.CE)
        self.assertEqual(sym, "NSE:BANKNIFTY26OCT51100CE")

    def test_nifty_strike_and_symbol(self):
        # Nifty Spot: 24520 -> ATM is 24500
        # ITM CE: 24500 - 50 = 24450
        # ITM PE: 24500 + 50 = 24550
        ce_strike = OptionStrikeSelector.get_itm_strike(24520.0, OptionType.CE, self.nf_cfg)
        self.assertEqual(ce_strike, 24450.0)

        pe_strike = OptionStrikeSelector.get_itm_strike(24520.0, OptionType.PE, self.nf_cfg)
        self.assertEqual(pe_strike, 24550.0)

        sym = OptionStrikeSelector.format_fyers_symbol(self.nf_cfg, pe_strike, OptionType.PE)
        self.assertEqual(sym, "NSE:NIFTY26OCT24550PE")


class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        self.risk = RiskEngine(
            total_capital=1000000.0,  # 10 Lakhs
            max_capital_per_trade_pct=0.25,
            max_daily_drawdown_pct=0.03,
            max_daily_trades=3,
            max_consecutive_losses=2,
            profit_lock_pct=0.80
        )

    def test_capital_tranche_and_counter_trend_reduction(self):
        # 25% of 10L is 2.5L. If premium is 250, lot size is 15 -> cost per lot = 3,750
        # 250,000 / 3,750 = 66 lots -> 66 * 15 = 990 qty
        qty_trend = self.risk.calculate_lot_size(option_premium=250.0, lot_size=15, is_trend_aligned=True)
        self.assertEqual(qty_trend, 66 * 15)

        # Counter-trend reduces lots by 50% -> 33 lots
        qty_counter = self.risk.calculate_lot_size(option_premium=250.0, lot_size=15, is_trend_aligned=False)
        self.assertEqual(qty_counter, 33 * 15)

    def test_consecutive_losses_lockout(self):
        can_trade, _ = self.risk.can_take_trade()
        self.assertTrue(can_trade)

        # Trade 1 loss
        self.risk.record_trade_completion(-5000.0)
        self.assertTrue(self.risk.can_take_trade()[0])

        # Trade 2 loss -> Lockout
        self.risk.record_trade_completion(-4000.0)
        can_trade, reason = self.risk.can_take_trade()
        self.assertFalse(can_trade)
        self.assertIn("consecutive losses", reason)

    def test_80_20_profit_retention_rule(self):
        # Trade 1 makes 20,000 profit. Peak = 20,000.
        # 80% locked = 16,000. Allowed drawdown buffer = 4,000.
        self.risk.record_trade_completion(20000.0)
        self.assertEqual(self.risk.peak_daily_profit, 20000.0)
        self.assertEqual(self.risk.realized_pnl, 20000.0)

        # Trade 2 loses 5,000. Net PnL = 15,000 (< 16,000 locked profit).
        # This immediately breaches the profit retention rule!
        self.risk.record_trade_completion(-5000.0)
        can_trade, reason = self.risk.can_take_trade()
        self.assertFalse(can_trade)
        self.assertIn("Profit lock", reason)

    def test_max_daily_trades_limit(self):
        self.risk.record_trade_completion(1000.0)
        self.risk.record_trade_completion(1000.0)
        self.risk.record_trade_completion(1000.0)
        can_trade, reason = self.risk.can_take_trade()
        self.assertFalse(can_trade)
        self.assertIn("trade count", reason)


class TestModularStrategies(unittest.TestCase):
    def setUp(self):
        self.bn_cfg = IndexConfig.bank_nifty_default()
        self.nf_cfg = IndexConfig.nifty_50_default()
        self.ldc_bn = LDCHLLevels(pdh=52200, pdl=51000, pdc=51500, daily_trend=Direction.BULLISH)
        self.ldc_nf = LDCHLLevels(pdh=24600, pdl=24300, pdc=24450, daily_trend=Direction.BULLISH)
        self.base_time = datetime(2026, 10, 16, 9, 15)

    def test_920_banknifty_valid_and_exhaustion(self):
        strat = OpeningRange920Strategy()
        # Valid: 51600 - 51500 = 100 pt range (<= 250 pt max)
        c1 = Candle(self.base_time, open=51520, high=51600, low=51500, close=51580)
        c2 = Candle(self.base_time + timedelta(minutes=5), open=51580, high=51650, low=51570, close=51640)

        sig = strat.evaluate(c2, [c1], self.ldc_bn, self.bn_cfg)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy, StrategyId.STRATEGY_920_AM)
        self.assertEqual(sig.option_type, OptionType.CE)

        # Exhaustion test: 51800 - 51500 = 300 pt range (> 250 pt max)
        strat.reset_daily_state()
        c_ex = Candle(self.base_time, open=51500, high=51800, low=51500, close=51780)
        sig_ex = strat.evaluate(c2, [c_ex], self.ldc_bn, self.bn_cfg)
        self.assertIsNone(sig_ex)

    def test_920_nifty_valid_and_exhaustion(self):
        strat = OpeningRange920Strategy()
        # Valid Nifty: 24520 - 24450 = 70 pt range (<= 120 pt max)
        c1 = Candle(self.base_time, open=24460, high=24520, low=24450, close=24510)
        c2 = Candle(self.base_time + timedelta(minutes=5), open=24510, high=24540, low=24500, close=24535)

        sig = strat.evaluate(c2, [c1], self.ldc_nf, self.nf_cfg)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.option_type, OptionType.CE)

        # Exhaustion Nifty: 24650 - 24500 = 150 pt range (> 120 pt max)
        strat.reset_daily_state()
        c_ex = Candle(self.base_time, open=24500, high=24650, low=24500, close=24640)
        sig_ex = strat.evaluate(c2, [c_ex], self.ldc_nf, self.nf_cfg)
        self.assertIsNone(sig_ex)

    def test_bullish_hammer_reversal(self):
        strat = BullishHammerReversalStrategy()
        candles = []
        p = 51700
        for i in range(5):
            candles.append(Candle(self.base_time + timedelta(minutes=5*i), open=p, high=p+10, low=p-30, close=p-25))
            p -= 25

        hammer = Candle(self.base_time + timedelta(minutes=25), open=51540, high=51552, low=51500, close=51550)
        candles.append(hammer)
        trigger = Candle(self.base_time + timedelta(minutes=30), open=51550, high=51570, low=51545, close=51565)
        candles.append(trigger)

        sig = strat.evaluate(trigger, candles, self.ldc_bn, self.bn_cfg)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy, StrategyId.HAMMER_REVERSAL)
        self.assertEqual(sig.option_type, OptionType.CE)

    def test_inside_candle_compression(self):
        strat = InsideCandleCompressionStrategy()
        mother = Candle(self.base_time, open=51520, high=51600, low=51500, close=51580)
        inside = Candle(self.base_time + timedelta(minutes=5), open=51530, high=51570, low=51520, close=51560)
        trigger = Candle(self.base_time + timedelta(minutes=10), open=51560, high=51590, low=51550, close=51585)

        sig = strat.evaluate(trigger, [mother, inside, trigger], self.ldc_bn, self.bn_cfg)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy, StrategyId.INSIDE_CANDLE)
        self.assertEqual(sig.option_type, OptionType.CE)


class TestMultiAssetTradingManager(unittest.TestCase):
    def setUp(self):
        self.manager = MultiAssetTradingManager(
            total_capital=1000000.0,
            is_paper=True,
            banknifty_expiry="26OCT",
            nifty_expiry="26OCT",
            banknifty_lot_size=15,
            nifty_lot_size=25
        )
        self.manager.set_levels("BANKNIFTY", pdh=52000, pdl=51000, pdc=51500, trend=Direction.BULLISH)
        self.manager.set_levels("NIFTY", pdh=24600, pdl=24300, pdc=24450, trend=Direction.BULLISH)

    def test_multi_index_event_handling_and_shared_risk(self):
        t0 = datetime(2026, 10, 16, 9, 15)
        t1 = datetime(2026, 10, 16, 9, 20)

        # 1. Feed Bank Nifty 9:15 and 9:20 candles (triggers CE trade)
        bn_c0 = Candle(t0, open=51500, high=51580, low=51480, close=51560)
        self.manager.on_candle_event("NSE:NIFTYBANK-INDEX", bn_c0, spot_ltp=51560)

        bn_c1 = Candle(t1, open=51560, high=51620, low=51550, close=51610)
        bn_sig = self.manager.on_candle_event("NSE:NIFTYBANK-INDEX", bn_c1, spot_ltp=51610)
        self.assertIsNotNone(bn_sig)
        self.assertEqual(bn_sig.underlying_code, "BANKNIFTY")

        # 2. Feed Nifty 9:15 and 9:20 candles (triggers CE trade)
        nf_c0 = Candle(t0, open=24460, high=24520, low=24450, close=24510)
        self.manager.on_candle_event("NSE:NIFTY50-INDEX", nf_c0, spot_ltp=24510)

        nf_c1 = Candle(t1, open=24510, high=24540, low=24500, close=24535)
        nf_sig = self.manager.on_candle_event("NSE:NIFTY50-INDEX", nf_c1, spot_ltp=24535)
        self.assertIsNotNone(nf_sig)
        self.assertEqual(nf_sig.underlying_code, "NIFTY")

        # Verify both positions exist simultaneously in shared paper broker
        active_pos = self.manager.paper_broker.positions
        self.assertEqual(len(active_pos), 2)
        symbols = list(active_pos.keys())
        self.assertTrue(any("BANKNIFTY" in s for s in symbols))
        self.assertTrue(any("NIFTY" in s for s in symbols))

        # 3. Simulate target hit on Bank Nifty
        t2 = datetime(2026, 10, 16, 9, 30)
        bn_c2 = Candle(t2, open=51610, high=51800, low=51600, close=51790)
        self.manager.on_candle_event("NSE:NIFTYBANK-INDEX", bn_c2, spot_ltp=51790)

        # Bank Nifty position closed with profit, Nifty remains active
        self.assertEqual(len(self.manager.paper_broker.positions), 1)
        self.assertEqual(len(self.manager.paper_broker.closed_positions), 1)

        # 4. Consolidated reporting
        rep = self.manager.get_consolidated_report()
        self.assertEqual(rep["banknifty_trades"], 1)
        self.assertGreater(rep["banknifty_pnl"], 0)
        self.assertEqual(rep["active_positions"], 1)


if __name__ == "__main__":
    unittest.main()
