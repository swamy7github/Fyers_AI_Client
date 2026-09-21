"""
global_sentiment.py
--------------------
A lightweight, *directional* read on whether global markets look bullish or
bearish ahead of the Indian session, plus an optional LLM-assisted layer for
qualitative macro / geopolitical sentiment (the "customised agent" piece).

This module is informational only - it does NOT gate or trigger anything in
signal_engine.py. Print it, log it, glance at it over coffee; treat it as a
dashboard, not a green/red trading light.
"""

from __future__ import annotations

import logging

import yfinance as yf

logger = logging.getLogger("orb_agent.sentiment")

# Tickers used as global-cue proxies. GIFT Nifty itself isn't reliably
# available on yfinance, so this uses the standard proxy set traders watch
# before the Indian open: US overnight action, Asian majors, crude, dollar
# index and the VIX.
GLOBAL_TICKERS = {
    "S&P 500": "^GSPC",
    "Nasdaq": "^IXIC",
    "Dow Jones": "^DJI",
    "Nikkei 225": "^N225",
    "Hang Seng": "^HSI",
    "US VIX": "^VIX",
    "Crude Oil (WTI)": "CL=F",
    "Dollar Index": "DX-Y.NYB",
}

# Rule-of-thumb weights: positive equity moves lean bullish for India
# (risk-on), while VIX / crude / dollar strength lean bearish (fear,
# import-cost pressure, and FII outflow pressure respectively).
SENTIMENT_WEIGHTS = {
    "S&P 500": 1.0, "Nasdaq": 0.5, "Dow Jones": 0.5,
    "Nikkei 225": 0.5, "Hang Seng": 0.5,
    "US VIX": -1.0, "Crude Oil (WTI)": -0.5, "Dollar Index": -0.5,
}


def fetch_global_snapshot() -> dict[str, float]:
    """Returns {name: % change over the last completed session} for each ticker."""
    snapshot = {}
    for name, ticker in GLOBAL_TICKERS.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d", interval="1d")
            if len(hist) < 2:
                continue
            prev_close, last_close = hist["Close"].iloc[-2], hist["Close"].iloc[-1]
            pct_change = (last_close - prev_close) / prev_close * 100
            snapshot[name] = round(float(pct_change), 2)
        except Exception as exc:
            logger.warning("Could not fetch global sentiment ticker %s (%s): %s", name, ticker, exc)
    return snapshot


def score_global_bias(snapshot: dict[str, float]) -> tuple[str, float]:
    """
    Combines the snapshot into a single heuristic score and label.
    This is a rule-of-thumb, not a fitted model - treat the output as a
    talking point / conversation starter, not a signal to act on.
    """
    score = sum(snapshot.get(name, 0.0) * weight for name, weight in SENTIMENT_WEIGHTS.items())
    if score > 0.5:
        label = "BULLISH"
    elif score < -0.5:
        label = "BEARISH"
    else:
        label = "NEUTRAL"
    return label, round(score, 2)


def get_llm_news_sentiment(headlines: list[str], api_key: str,
                            model: str = "claude-sonnet-4-6") -> str:
    """
    Optional: send a handful of the morning's top macro/geopolitical
    headlines to Claude and ask for a short, structured bullish/bearish/
    neutral read for Indian equities.

    Requires: pip install anthropic, and an Anthropic API key in
    config.ANTHROPIC_API_KEY (or passed in directly).

    Why bother with this alongside score_global_bias()? Price-based scoring
    can tell you markets moved; it can't tell you *why*, or whether a
    geopolitical headline is likely to matter tomorrow. An LLM call is the
    piece that reasons over qualitative news the way a human trader would -
    it's the natural place to plug in "geopolitical sentiment" from the
    original brief, which isn't really a numeric time series at all.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "You are helping a discretionary Indian equity trader form a pre-market view. "
        "Given these overnight headlines, respond with exactly one line in the format "
        "'BIAS: <BULLISH|BEARISH|NEUTRAL> | REASON: <one sentence>'.\n\nHeadlines:\n"
        + "\n".join(f"- {h}" for h in headlines)
    )
    response = client.messages.create(
        model=model,
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
