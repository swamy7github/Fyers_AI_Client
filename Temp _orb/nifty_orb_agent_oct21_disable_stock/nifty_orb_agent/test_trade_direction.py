"""
test_trade_direction.py
--------------------------
Tests for trade_direction.py's pure decide_trade_direction_from_sentiment()
function. bootstrap.decide_todays_direction() (which wraps this with the
actual sentiment fetch and the config.ALLOW_*_ON_NEUTRAL fallback policy)
is tested separately in test_bootstrap.py's TestDecideTodaysDirection.

Run with:
    python test_trade_direction.py
"""

from __future__ import annotations

import unittest

from trade_direction import TradeDirection, decide_trade_direction_from_sentiment


class TestDecideTradeDirectionFromSentiment(unittest.TestCase):
    def test_bullish_returns_long(self):
        self.assertEqual(decide_trade_direction_from_sentiment("BULLISH"), TradeDirection.LONG)

    def test_bearish_returns_short(self):
        self.assertEqual(decide_trade_direction_from_sentiment("BEARISH"), TradeDirection.SHORT)

    def test_neutral_returns_none(self):
        self.assertIsNone(decide_trade_direction_from_sentiment("NEUTRAL"))

    def test_mixed_returns_none(self):
        self.assertIsNone(decide_trade_direction_from_sentiment("MIXED"))

    def test_unknown_label_returns_none(self):
        self.assertIsNone(decide_trade_direction_from_sentiment("UNKNOWN"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(decide_trade_direction_from_sentiment(""))

    def test_none_input_returns_none_not_a_crash(self):
        self.assertIsNone(decide_trade_direction_from_sentiment(None))

    def test_is_case_insensitive_and_trims_whitespace(self):
        self.assertEqual(decide_trade_direction_from_sentiment("  bullish  "), TradeDirection.LONG)
        self.assertEqual(decide_trade_direction_from_sentiment("Bearish"), TradeDirection.SHORT)

    def test_unrecognized_arbitrary_string_returns_none_not_a_crash(self):
        self.assertIsNone(decide_trade_direction_from_sentiment("SIDEWAYS"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
