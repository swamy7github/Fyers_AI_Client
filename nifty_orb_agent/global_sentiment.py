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


# ----------------------------------------------------------------------------
# NEW API: combined news + EMA bias for a single stock or index.
# ----------------------------------------------------------------------------
def fyers_symbol_to_yahoo(fyers_symbol: str) -> str:
    """
    Converts a Fyers equity symbol ('NSE:TCS-EQ') to yfinance's convention
    ('TCS.NS'), so get_stock_news_ema_bias() below can be called directly
    with symbols from elsewhere in this codebase (e.g. a future per-stock
    use against the opening-range shortlist). Only handles the common
    NSE:<SYMBOL>-EQ pattern - anything else (an index symbol, a BSE symbol,
    ...) is returned unchanged, so check the result if you pass something
    unusual.
    """
    if fyers_symbol.startswith("NSE:") and fyers_symbol.endswith("-EQ"):
        core = fyers_symbol[len("NSE:"):-len("-EQ")]
        return f"{core}.NS"
    return fyers_symbol


def _compute_ema_bias(yahoo_symbol: str, ema_fast_period: int, ema_slow_period: int) -> tuple[str, dict]:
    """
    Classic EMA-crossover technical read: BULLISH if the fast EMA is above
    the slow EMA AND price is above the fast EMA (confirmed uptrend);
    BEARISH for the mirror image; NEUTRAL otherwise (e.g. mixed signals
    around a crossover); UNKNOWN if there isn't enough price history.
    """
    try:
        hist = yf.Ticker(yahoo_symbol).history(period="6mo", interval="1d")
        if len(hist) < ema_slow_period + 1:
            logger.warning("Not enough price history for %s (%d bars) to compute "
                            "EMA(%d) reliably.", yahoo_symbol, len(hist), ema_slow_period)
            return "UNKNOWN", {}

        close = hist["Close"]
        ema_fast = close.ewm(span=ema_fast_period, adjust=False).mean()
        ema_slow = close.ewm(span=ema_slow_period, adjust=False).mean()
        latest_close = float(close.iloc[-1])
        latest_fast = float(ema_fast.iloc[-1])
        latest_slow = float(ema_slow.iloc[-1])

        if latest_fast > latest_slow and latest_close > latest_fast:
            bias = "BULLISH"
        elif latest_fast < latest_slow and latest_close < latest_fast:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        detail = {
            "latest_close": round(latest_close, 2),
            f"ema_{ema_fast_period}": round(latest_fast, 2),
            f"ema_{ema_slow_period}": round(latest_slow, 2),
        }
        return bias, detail
    except Exception as exc:
        logger.warning("Could not compute EMA bias for %s: %s", yahoo_symbol, exc)
        return "UNKNOWN", {}


def _fetch_recent_headlines(yahoo_symbol: str, count: int) -> list[str]:
    """
    yfinance's `.news` schema has shifted between versions (sometimes a
    flat 'title' key, sometimes nested under 'content') and can return an
    empty list even when there genuinely is recent news - this checks both
    shapes and never raises, since a missing headline shouldn't take down
    the EMA half of the read.
    """
    try:
        news_items = yf.Ticker(yahoo_symbol).news or []
        headlines = []
        for item in news_items[:count]:
            title = item.get("title") or item.get("content", {}).get("title")
            if title:
                headlines.append(title)
        return headlines
    except Exception as exc:
        logger.warning("Could not fetch news for %s: %s", yahoo_symbol, exc)
        return []


def get_stock_news_ema_bias(
    yahoo_symbol: str,
    anthropic_api_key: str = "",
    ema_fast_period: int = 12,
    ema_slow_period: int = 26,
    news_headline_count: int = 5,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """
    Combines a price-based EMA(fast) vs EMA(slow) technical read with an
    LLM-scored read of that symbol's own recent news headlines (fetched via
    yfinance) into one bullish/bearish/neutral verdict.

    yahoo_symbol: a Yahoo Finance ticker - "^NSEI" for Nifty 50, or
        "TCS.NS" for an NSE-listed stock. NOT the same as a Fyers symbol
        ("NSE:TCS-EQ") used elsewhere in this codebase - convert with
        fyers_symbol_to_yahoo() first if that's what you have.

    Without anthropic_api_key, the news half is skipped (news_bias stays
    None) and combined_bias falls back to the EMA-only read - this function
    still works, just without the qualitative layer.

    Returns:
        {
            "yahoo_symbol": str,
            "ema_bias": "BULLISH" | "BEARISH" | "NEUTRAL" | "UNKNOWN",
            "ema_detail": {"latest_close": ..., "ema_<fast>": ..., "ema_<slow>": ...},
            "news_bias": "BULLISH" | "BEARISH" | "NEUTRAL" | None,
            "news_reason": str | None,
            "headlines_used": [str, ...],
            "combined_bias": "BULLISH" | "BEARISH" | "MIXED" | "NEUTRAL" | "UNKNOWN",
        }
    "MIXED" means the two components actively disagreed (one bullish, one
    bearish) - worth a second look rather than trading through it blindly.
    """
    ema_bias, ema_detail = _compute_ema_bias(yahoo_symbol, ema_fast_period, ema_slow_period)
    headlines = _fetch_recent_headlines(yahoo_symbol, news_headline_count)

    news_bias, news_reason = None, None
    if headlines and anthropic_api_key:
        try:
            raw = get_llm_news_sentiment(headlines, anthropic_api_key, model=model)
            if "BIAS:" in raw:
                head, _, tail = raw.partition("|")
                news_bias = head.split("BIAS:", 1)[1].strip().upper()
                if "REASON:" in tail:
                    news_reason = tail.split("REASON:", 1)[1].strip()
        except Exception as exc:
            logger.warning("Could not get LLM news sentiment for %s: %s", yahoo_symbol, exc)

    if ema_bias in ("BULLISH", "BEARISH") and news_bias in ("BULLISH", "BEARISH"):
        combined_bias = ema_bias if ema_bias == news_bias else "MIXED"
    elif news_bias in ("BULLISH", "BEARISH", "NEUTRAL"):
        combined_bias = news_bias
    else:
        combined_bias = ema_bias  # EMA-only fallback (itself possibly UNKNOWN/NEUTRAL)

    return {
        "yahoo_symbol": yahoo_symbol,
        "ema_bias": ema_bias, "ema_detail": ema_detail,
        "news_bias": news_bias, "news_reason": news_reason,
        "headlines_used": headlines,
        "combined_bias": combined_bias,
    }
