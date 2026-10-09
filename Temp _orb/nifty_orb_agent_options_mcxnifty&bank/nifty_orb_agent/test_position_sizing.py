"""
test_position_sizing.py
--------------------------
Tests for position_sizing.py's capital-based sizing rules: calculate_quantity()
(the floor-to-whole-lot rule shared by equities, options, and commodities)
and capital_per_slot() (the equal-split capital allocator).

Run with:
    python test_position_sizing.py
"""

from __future__ import annotations

import unittest

from position_sizing import calculate_quantity, capital_per_slot


class TestCalculateQuantityEquities(unittest.TestCase):
    """lot_size=1 (the equity default) - plain floor(capital / price)."""

    def test_exact_division(self):
        self.assertEqual(calculate_quantity(capital=40_000, price=100.0), 400)

    def test_rounds_down_not_up(self):
        self.assertEqual(calculate_quantity(capital=1_000, price=99.0), 10)  # 10.1010... -> 10

    def test_capital_smaller_than_one_share_returns_zero(self):
        self.assertEqual(calculate_quantity(capital=50, price=100.0), 0)

    def test_zero_capital_returns_zero(self):
        self.assertEqual(calculate_quantity(capital=0, price=100.0), 0)

    def test_negative_capital_returns_zero(self):
        self.assertEqual(calculate_quantity(capital=-500, price=100.0), 0)

    def test_zero_price_returns_zero_without_dividing_by_zero(self):
        self.assertEqual(calculate_quantity(capital=40_000, price=0.0), 0)

    def test_negative_price_returns_zero(self):
        self.assertEqual(calculate_quantity(capital=40_000, price=-10.0), 0)


class TestCalculateQuantityWithLotSize(unittest.TestCase):
    """lot_size > 1 (options/commodity futures) - rounds DOWN to a whole number of lots."""

    def test_whole_lots_only(self):
        # One lot = 75 units @ 200/unit = 15,000/lot. 40,000 capital affords 2 lots (30,000),
        # NOT 2.66 lots - the leftover 10,000 must NOT buy a partial lot.
        self.assertEqual(calculate_quantity(capital=40_000, price=200.0, lot_size=75), 150)  # 2 lots x 75

    def test_capital_smaller_than_one_lot_returns_zero_not_a_partial_lot(self):
        # One lot costs 15,000; 10,000 can't afford it - must be 0, not a fractional lot.
        self.assertEqual(calculate_quantity(capital=10_000, price=200.0, lot_size=75), 0)

    def test_exact_multiple_of_lot_value(self):
        self.assertEqual(calculate_quantity(capital=30_000, price=200.0, lot_size=75), 150)

    def test_zero_lot_size_returns_zero(self):
        self.assertEqual(calculate_quantity(capital=40_000, price=200.0, lot_size=0), 0)

    def test_negative_lot_size_returns_zero(self):
        self.assertEqual(calculate_quantity(capital=40_000, price=200.0, lot_size=-5), 0)

    def test_returned_quantity_is_always_a_whole_multiple_of_lot_size(self):
        for capital in (1_000, 12_345, 50_000, 199_999):
            qty = calculate_quantity(capital=capital, price=37.0, lot_size=10)
            self.assertEqual(qty % 10, 0, f"qty={qty} for capital={capital} is not a whole number of lots")


class TestCapitalPerSlot(unittest.TestCase):
    def test_equal_split_across_several_slots(self):
        self.assertEqual(capital_per_slot(200_000, 5), 40_000)

    def test_single_slot_returns_full_capital(self):
        self.assertEqual(capital_per_slot(200_000, 1), 200_000)

    def test_zero_slots_returns_full_capital_rather_than_dividing_by_zero(self):
        self.assertEqual(capital_per_slot(200_000, 0), 200_000)

    def test_zero_total_capital_returns_zero(self):
        self.assertEqual(capital_per_slot(0, 5), 0.0)

    def test_negative_total_capital_returns_zero(self):
        self.assertEqual(capital_per_slot(-1000, 5), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
