"""
position_sizing.py
---------------------
Capital-based position sizing, shared by every market this agent trades
(NSE equities, NIFTY INDEX options via options_engine.py, MCX commodity
futures via commodity_engine.py) through signal_engine.StockTracker's
`capital_allocated` / `lot_size` fields and SignalEngine._maybe_place_live_order().

There is exactly one rule: spend at most `capital` rupees on this one
trade, buy as many WHOLE lots as that capital affords at `price`, and never
round UP into a partial-lot overspend. For equities lot_size is always 1
(any whole share is its own "lot"), so this reduces to plain
floor(capital / price). For options and commodity futures, lot_size > 1 -
the exchange only lets you transact in whole lot multiples - so this rounds
DOWN to the nearest whole lot, which can legitimately leave some of the
allocated capital unused (e.g. if a single NIFTY option lot alone costs
more than the whole per-trade allocation, see calculate_quantity's "0
quantity" case below).

This module replaces config.DEFAULT_ORDER_QTY (the old flat "always 1
share" placeholder) as the real sizing logic config.py's own comment said
was still needed "before DRY_RUN = False means anything". DEFAULT_ORDER_QTY
itself is kept only as a last-resort fallback for the (should-never-happen)
case where a caller has no capital/price/lot_size to work with at all.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("orb_agent.sizing")


def calculate_quantity(capital: float, price: float, lot_size: int = 1) -> int:
    """
    Returns the number of shares/units to transact for up to `capital`
    rupees at `price` per share/unit, rounded DOWN to the nearest whole
    `lot_size` (1 for equities; >1 for options/futures, which only trade in
    fixed lot multiples).

    Returns 0 - never negative, never a fractional lot - if `capital`,
    `price`, or `lot_size` is non-positive, or if `capital` can't afford
    even a single lot at `price`. Callers MUST treat a 0 return as "skip
    this trade - the capital allocation doesn't cover one whole lot", never
    round up or retry with more capital than configured: overspending the
    configured allocation defeats the entire point of capital-based sizing.
    """
    if capital <= 0 or price <= 0 or lot_size <= 0:
        return 0
    lots = int(capital // (price * lot_size))
    qty = lots * lot_size
    if qty == 0:
        logger.warning("Capital %.2f can't afford even one lot of size %d at price %.2f "
                        "(would need %.2f) - sizing to 0, caller should skip this trade.",
                        capital, lot_size, price, price * lot_size)
    return qty


def capital_per_slot(total_capital: float, num_slots: int) -> float:
    """
    Equal-split capital allocator: divides `total_capital` evenly across
    `num_slots` concurrent trade slots (e.g. config.TOP_N_STOCKS shortlisted
    stocks for equities, or a fixed MCX commodity universe's length).

    Returns `total_capital` unchanged if `num_slots` <= 1 (nothing to
    split), and 0.0 if `total_capital` is itself non-positive. Never
    raises on a bad input - both feed straight into calculate_quantity(),
    which already treats a non-positive capital as "skip the trade"
    cleanly, so there's nothing useful an exception would add here.
    """
    if total_capital <= 0:
        return 0.0
    if num_slots <= 1:
        return total_capital
    return total_capital / num_slots
