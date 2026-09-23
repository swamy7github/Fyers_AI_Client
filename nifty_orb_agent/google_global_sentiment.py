"""
global_sentiment.py
--------------------
A lightweight, *directional* read on whether global markets look bullish or
bearish ahead of the Indian session, sourced directly from Google Finance,
plus an optional LLM-assisted layer for qualitative macro sentiment.
"""

from __future__ import annotations

import logging
import re
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("orb_agent.sentiment")

# Google Finance format: "SYMBOL:EXCHANGE"
# You can verify any symbol at: https://www.google.com/finance/quote/{ticker}
GOOGLE_FINANCE_TICKERS = {
    "S&P 500": ".INX:INDEXSP",
    "Nasdaq": ".IXIC:INDEXNASDAQ",
    "Dow Jones": ".DJI:INDEXDJX",
    "Nikkei 225": "NI225:INDEXNIKKEI",
    "Hang Seng": "HSI:INDEXHANGSENG",
    "US VIX": "VIX:INDEXCBOE",
    "Brent Crude": "BZW00:NYMEX",           # Front-month Brent futures on NYMEX
    "Dollar Index": "NYICDX:INDEXNYSEGIS",  # ICE US Dollar Index
}

SENTIMENT_WEIGHTS = {
    "S&P 500": 1.0,
    "Nasdaq": 0.5,
    "Dow Jones": 0.5,
    "Nikkei 225": 0.5,
    "Hang Seng": 0.5,
    "US VIX": -1.0,
    "Brent Crude": -0.5,
    "Dollar Index": -0.5,
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _scrape_google_finance_change(ticker_code: str, session: requests.Session) -> float | None:
    """
    Scrapes the current/session % change from Google Finance for a given SYMBOL:EXCHANGE.
    """
    url = f"https://www.google.com/finance/quote/{ticker_code}?hl=en"
    try:
        resp = session.get(url, headers=_HEADERS, timeout=6)
        if resp.status_code != 200:
            logger.warning("HTTP %d when fetching %s", resp.status_code, ticker_code)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # 1. Primary extraction: Google Finance displays percentage change 
        # in elements with aria-label containing '%'. Example: aria-label="Up 0.85% today" or "Down 1.12%"
        pct_elem = soup.find(attrs={"aria-label": re.compile(r"(\+|\-)?\d+\.?\d*%", re.IGNORECASE)})
        if pct_elem:
            match = re.search(r"([-+]?\d*\.?\d+)\s*%", pct_elem.get("aria-label", ""))
            if match:
                sign = -1.0 if "down" in pct_elem.get("aria-label", "").lower() else 1.0
                return abs(float(match.group(1))) * sign

        # 2. Fallback: Search visible text matching percentage formats like "+0.85%" or "-1.24%"
        text_matches = soup.find_all(string=re.compile(r"^[+-]?\d+\.\d+%$"))
        if text_matches:
            val_str = text_matches[0].strip().replace("%", "").replace("+", "")
            return float(val_str)

    except Exception as exc:
        logger.warning("Failed parsing Google Finance quote for %s: %s", ticker_code, exc)

    return None


def fetch_global_snapshot() -> dict[str, float]:
    """
    Returns {name: % change} for all tracked tickers using Google Finance.
    """
    snapshot = {}
    with requests.Session() as session:
        for name, ticker_code in GOOGLE_FINANCE_TICKERS.items():
            pct_change = _scrape_google_finance_change(ticker_code, session)
            if pct_change is not None:
                snapshot[name] = round(pct_change, 2)
            else:
                logger.warning("Could not extract % change for %s (%s)", name, ticker_code)

    return snapshot


def score_global_bias(snapshot: dict[str, float]) -> tuple[str, float]:
    """
    Calculates weighted directional bias score normalized by available indicators.
    """
    if not snapshot:
        return "NEUTRAL", 0.0

    raw_score = sum(snapshot[k] * SENTIMENT_WEIGHTS[k] for k in snapshot if k in SENTIMENT_WEIGHTS)
    active_weight = sum(abs(SENTIMENT_WEIGHTS[k]) for k in snapshot if k in SENTIMENT_WEIGHTS)
    nominal_weight = sum(abs(w) for w in SENTIMENT_WEIGHTS.values())

    # Scale score so missing/blocked tickers don't artificially dampen variance
    normalized_score = (raw_score / active_weight) * (nominal_weight / len(SENTIMENT_WEIGHTS))

    if normalized_score > 0.5:
        label = "BULLISH"
    elif normalized_score < -0.5:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    return label, round(normalized_score, 2)


def get_llm_news_sentiment(
    headlines: list[str],
    api_key: str,
    model: str = "claude-3-5-sonnet-latest",
) -> str:
    """
    Sends morning macro headlines to Claude for an Indian equity bias assessment.
    """
    if not headlines:
        return "BIAS: NEUTRAL | REASON: No overnight headlines provided."

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "You are assisting an Indian equity index / F&O trader forming a pre-market view.\n"
        "Analyze these overnight macro/geopolitical headlines specifically through the lens of Indian markets "
        "(FII flows, crude oil sensitivity, INR movement, global risk appetite).\n\n"
        "Format: exactly one line:\n"
        "BIAS: <BULLISH|BEARISH|NEUTRAL> | REASON: <one concise sentence>\n\n"
        "Headlines:\n" + "\n".join(f"- {h}" for h in headlines)
    )

    response = client.messages.create(
        model=model,
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()