"""Black-Scholes helpers for strike selection and the *synthetic premium* fallback.

Why this exists
---------------
Fyers history does not reliably serve candles for *expired* option contracts,
so a backtest over past dates usually cannot observe the real premium path.
When no option candles are available the backtester prices the chosen
contract from the underlying path with Black-Scholes using a per-instrument
IV assumption (``strike.iv_assumption``), decaying time-to-expiry minute by
minute. Every trade priced this way is tagged ``premium_model = synthetic`` in
the trade log; trades priced from real option candles are tagged ``market``.

This is a *model*, not data: it ignores the volatility smile, intraday IV
changes, bid-ask spread (handled separately as slippage) and liquidity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional

from .models import OptionType

TRADING_MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0  # calendar-time convention for intraday decay


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta_per_minute: float
    vega: float


def time_to_expiry_years(now: datetime, expiry: date, expiry_time: time = time(15, 30)) -> float:
    exp_dt = datetime.combine(expiry, expiry_time)
    minutes = (exp_dt - now).total_seconds() / 60.0
    minutes = max(minutes, 1.0)  # never zero: keeps the model finite on expiry day
    return minutes / TRADING_MINUTES_PER_YEAR


def black_scholes(spot: float, strike: float, t_years: float, iv: float, option_type: OptionType,
                  r: float = 0.065) -> Greeks:
    """European option price and greeks; ``iv`` annualised, ``t_years`` > 0."""
    t = max(t_years, 1e-9)
    sigma = max(iv, 1e-6)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc = math.exp(-r * t)
    if option_type == OptionType.CE:
        price = spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        theta = (-(spot * _norm_pdf(d1) * sigma) / (2 * sqrt_t) - r * strike * disc * _norm_cdf(d2))
    else:
        price = strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
        theta = (-(spot * _norm_pdf(d1) * sigma) / (2 * sqrt_t) + r * strike * disc * _norm_cdf(-d2))
    gamma = _norm_pdf(d1) / (spot * sigma * sqrt_t)
    vega = spot * _norm_pdf(d1) * sqrt_t
    return Greeks(price=max(price, 0.0), delta=delta, gamma=gamma,
                  theta_per_minute=theta / TRADING_MINUTES_PER_YEAR, vega=vega)


def synthetic_premium(spot: float, strike: float, now: datetime, expiry: date, iv: float,
                      option_type: OptionType, r: float = 0.065, min_premium: float = 1.0,
                      expiry_time: time = time(15, 30)) -> float:
    g = black_scholes(spot, strike, time_to_expiry_years(now, expiry, expiry_time), iv, option_type, r)
    return max(g.price, min_premium)


def option_delta(spot: float, strike: float, now: datetime, expiry: date, iv: float,
                 option_type: OptionType, r: float = 0.065, expiry_time: time = time(15, 30)) -> float:
    g = black_scholes(spot, strike, time_to_expiry_years(now, expiry, expiry_time), iv, option_type, r)
    return abs(g.delta)


def intrinsic(spot: float, strike: float, option_type: OptionType) -> float:
    return max(spot - strike, 0.0) if option_type == OptionType.CE else max(strike - spot, 0.0)


def implied_vol_from_price(price: float, spot: float, strike: float, t_years: float, option_type: OptionType,
                           r: float = 0.065, lo: float = 0.01, hi: float = 3.0, tol: float = 1e-4) -> Optional[float]:
    """Bisection IV solve (used to calibrate the synthetic model from a live quote)."""
    if price <= intrinsic(spot, strike, option_type):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        p = black_scholes(spot, strike, t_years, mid, option_type, r).price
        if abs(p - price) < tol:
            return mid
        if p > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)
