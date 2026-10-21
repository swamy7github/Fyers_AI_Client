"""
trade_direction.py
---------------------
A tiny, dependency-free module shared by signal_engine.py,
opening_range_scanner.py, sector_scanner.py-adjacent callers, and
bootstrap.py, so all of them agree on what "LONG" and "SHORT" mean without
importing each other (avoiding an import cycle: signal_engine and
opening_range_scanner don't otherwise need to know about each other at all).
"""

from __future__ import annotations

from enum import Enum


class TradeDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


def decide_trade_direction_from_sentiment(bias_label: str) -> TradeDirection | None:
    """
    Turns a sentiment label - "BULLISH" / "BEARISH" / "NEUTRAL" / "MIXED" /
    "UNKNOWN", from global_sentiment.score_global_bias() or
    get_stock_news_ema_bias()'s combined_bias - into a trading direction for
    the day.

    Returns None for anything that isn't a clear BULLISH or BEARISH read
    (NEUTRAL, MIXED, UNKNOWN, or an unrecognized string). What "no clear
    direction" means for whether the agent trades at all that day is a
    caller decision, not this function's - see
    config.ALLOW_LONG_ON_NEUTRAL / ALLOW_SHORT_ON_NEUTRAL and how
    bootstrap.py and main.py use them. This function only answers "given
    this label, which single direction would sentiment argue for" - it
    doesn't decide the neutral-case policy itself, so that policy lives in
    exactly one place (config.py) rather than being duplicated in every
    caller.
    """
    label = (bias_label or "").strip().upper()
    if label == "BULLISH":
        return TradeDirection.LONG
    if label == "BEARISH":
        return TradeDirection.SHORT
    return None
