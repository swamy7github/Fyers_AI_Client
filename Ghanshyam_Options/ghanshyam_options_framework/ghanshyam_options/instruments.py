"""Instrument registry, expiry resolution and Fyers option-symbol builder.

NSE index options
-----------------
* NIFTY50  — Fyers underlying ``NSE:NIFTY50-INDEX``, option root ``NIFTY``,
  strike step 50, weekly expiry **Tuesday**, monthly expiry last Tuesday
  (2026 rules, taken from ``fyersclient/nifty_fyers_symbol_generator.py``).
* BANKNIFTY — underlying ``NSE:NIFTYBANK-INDEX``, root ``BANKNIFTY``, strike
  step 100, **monthly only** (weekly contracts discontinued), last Tuesday.

Fyers symbol formats (verified against the repo's symbol master):
* weekly:  ``NSE:NIFTY26O1322000CE``  -> root + YY + month-code + DD + strike + CE/PE
  month-code: 1..9 for Jan..Sep, O = Oct, N = Nov, D = Dec
* monthly: ``NSE:BANKNIFTY26OCT51100CE`` -> root + YY + MMM + strike + CE/PE

MCX commodity options (options on futures)
------------------------------------------
Fyers uses the monthly format ``MCX:CRUDEOIL26OCT5500CE``. MCX option expiries
are contract-specific (typically a few days before the futures expiry) so they
are supplied as explicit dates in the registry; the symbol master should be
consulted for anything beyond the configured list.

Lot sizes / tick sizes / strike steps below are the commonly listed values as
of late 2026 and MUST be verified against the broker's symbol master before
real-money use (``InstrumentSpec.verify_note``).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence

from .models import Exchange, OptionContract, OptionType

# NSE holiday fallback (same list the user's generator uses). Extend as needed.
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),
    date(2026, 3, 21),
    date(2026, 10, 2),
    date(2026, 10, 20),
    date(2026, 11, 24),
    date(2026, 12, 25),
}

MCX_HOLIDAYS_2026 = {
    date(2026, 1, 26),
    date(2026, 10, 2),
    date(2026, 12, 25),
}


@dataclass(frozen=True)
class InstrumentSpec:
    name: str                      # registry key: NIFTY50, BANKNIFTY, CRUDEOIL ...
    exchange: Exchange
    underlying_symbol: str         # Fyers symbol whose candles drive the strategies
    option_root: str               # text before the expiry code in the option symbol
    lot_size: int
    strike_step: float
    tick_size: float
    weekly_expiry_weekday: Optional[int]   # 0=Mon ... 6=Sun; None => monthly only
    monthly_expiry_weekday: Optional[int]  # last <weekday> of month; None => explicit list
    explicit_expiries: tuple = ()          # for MCX: sorted ISO dates
    point_value: float = 1.0               # INR per 1.0 move in the underlying per unit (MCX uses lot multipliers)
    constituents: tuple = ()
    verify_note: str = ""
    holidays: frozenset = field(default_factory=frozenset)

    @property
    def is_index(self) -> bool:
        return self.exchange == Exchange.NSE

    def atm_strike(self, spot: float) -> float:
        return round(spot / self.strike_step) * self.strike_step

    def itm_strike(self, spot: float, option_type: OptionType, steps: int = 1) -> float:
        atm = self.atm_strike(spot)
        return atm - steps * self.strike_step if option_type == OptionType.CE else atm + steps * self.strike_step

    def round_strike(self, strike: float) -> float:
        return round(strike / self.strike_step) * self.strike_step


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_REGISTRY: Dict[str, InstrumentSpec] = {}


def register(spec: InstrumentSpec) -> InstrumentSpec:
    _REGISTRY[spec.name] = spec
    return spec


def get_instrument(name: str) -> InstrumentSpec:
    key = name.upper().replace("-", "").replace(" ", "")
    aliases = {"NIFTY": "NIFTY50", "BANKNIFTY": "BANKNIFTY", "NIFTYBANK": "BANKNIFTY",
               "CRUDE": "CRUDEOIL", "NATGAS": "NATURALGAS"}
    key = aliases.get(key, key)
    if key not in _REGISTRY:
        raise KeyError(f"Unknown instrument '{name}'. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[key]


def all_instruments() -> List[InstrumentSpec]:
    return list(_REGISTRY.values())


def _mcx_expiries(*dates: str) -> tuple:
    return tuple(sorted(dates))


register(InstrumentSpec(
    name="NIFTY50", exchange=Exchange.NSE, underlying_symbol="NSE:NIFTY50-INDEX", option_root="NIFTY",
    lot_size=75, strike_step=50.0, tick_size=0.05, weekly_expiry_weekday=1, monthly_expiry_weekday=1,
    verify_note="NIFTY lot size 75 (post Nov-2024 revision); weekly expiry Tuesday from Sep-2025. Verify.",
    holidays=frozenset(NSE_HOLIDAYS_2026),
))
register(InstrumentSpec(
    name="BANKNIFTY", exchange=Exchange.NSE, underlying_symbol="NSE:NIFTYBANK-INDEX", option_root="BANKNIFTY",
    lot_size=35, strike_step=100.0, tick_size=0.05, weekly_expiry_weekday=None, monthly_expiry_weekday=1,
    constituents=("NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:SBIN-EQ", "NSE:AXISBANK-EQ", "NSE:KOTAKBANK-EQ"),
    verify_note="BANKNIFTY lot size 35 (2025 revision), monthly-only expiry on last Tuesday. Verify.",
    holidays=frozenset(NSE_HOLIDAYS_2026),
))
# MCX — options on futures. Expiry dates are placeholders aligned with typical
# MCX option expiry (a few sessions before the futures expiry). Replace with the
# broker symbol master dates before trading.
register(InstrumentSpec(
    name="CRUDEOIL", exchange=Exchange.MCX, underlying_symbol="MCX:CRUDEOIL26OCTFUT", option_root="CRUDEOIL",
    lot_size=100, strike_step=50.0, tick_size=0.05, weekly_expiry_weekday=None, monthly_expiry_weekday=None,
    explicit_expiries=_mcx_expiries("2026-10-15", "2026-11-17", "2026-12-15"),
    verify_note="CRUDEOIL option lot 100 bbl, strike step 50. Verify expiries from the symbol master.",
    holidays=frozenset(MCX_HOLIDAYS_2026),
))
register(InstrumentSpec(
    name="NATURALGAS", exchange=Exchange.MCX, underlying_symbol="MCX:NATURALGAS26OCTFUT", option_root="NATURALGAS",
    lot_size=1250, strike_step=5.0, tick_size=0.05, weekly_expiry_weekday=None, monthly_expiry_weekday=None,
    explicit_expiries=_mcx_expiries("2026-10-26", "2026-11-24", "2026-12-24"),
    verify_note="NATURALGAS option lot 1250 mmBtu, strike step 5. Verify.",
    holidays=frozenset(MCX_HOLIDAYS_2026),
))
register(InstrumentSpec(
    name="GOLD", exchange=Exchange.MCX, underlying_symbol="MCX:GOLD26DECFUT", option_root="GOLD",
    lot_size=100, strike_step=100.0, tick_size=1.0, weekly_expiry_weekday=None, monthly_expiry_weekday=None,
    explicit_expiries=_mcx_expiries("2026-10-28", "2026-11-26", "2026-12-28"),
    verify_note="GOLD (big) option lot 100 g (price per 10 g -> point value 10), strike step 100. Verify.",
    point_value=10.0,
    holidays=frozenset(MCX_HOLIDAYS_2026),
))
register(InstrumentSpec(
    name="SILVER", exchange=Exchange.MCX, underlying_symbol="MCX:SILVER26DECFUT", option_root="SILVER",
    lot_size=30, strike_step=250.0, tick_size=1.0, weekly_expiry_weekday=None, monthly_expiry_weekday=None,
    explicit_expiries=_mcx_expiries("2026-10-27", "2026-11-25", "2026-12-24"),
    verify_note="SILVER option lot 30 kg (price per kg), strike step 250. Verify.",
    holidays=frozenset(MCX_HOLIDAYS_2026),
))
register(InstrumentSpec(
    name="COPPER", exchange=Exchange.MCX, underlying_symbol="MCX:COPPER26OCTFUT", option_root="COPPER",
    lot_size=2500, strike_step=5.0, tick_size=0.05, weekly_expiry_weekday=None, monthly_expiry_weekday=None,
    explicit_expiries=_mcx_expiries("2026-10-28", "2026-11-26", "2026-12-28"),
    verify_note="COPPER option lot 2500 kg, strike step 5. Verify.",
    holidays=frozenset(MCX_HOLIDAYS_2026),
))


# ---------------------------------------------------------------------------
# Trading-day helpers
# ---------------------------------------------------------------------------
def is_trading_day(d: date, spec: InstrumentSpec) -> bool:
    return d.weekday() < 5 and d not in spec.holidays


def previous_trading_day(d: date, spec: InstrumentSpec) -> date:
    cur = d - timedelta(days=1)
    while not is_trading_day(cur, spec):
        cur -= timedelta(days=1)
    return cur


def _shift_back_to_trading_day(d: date, spec: InstrumentSpec) -> date:
    while not is_trading_day(d, spec):
        d -= timedelta(days=1)
    return d


def last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def monthly_expiry(on: date, spec: InstrumentSpec) -> date:
    """Nearest monthly expiry on/after ``on`` (last <weekday>, holiday-adjusted backwards)."""
    if spec.monthly_expiry_weekday is None:
        return nearest_explicit_expiry(on, spec)
    y, m = on.year, on.month
    for _ in range(3):
        exp = _shift_back_to_trading_day(last_weekday_of_month(y, m, spec.monthly_expiry_weekday), spec)
        if exp >= on:
            return exp
        m += 1
        if m > 12:
            m, y = 1, y + 1
    raise RuntimeError("could not resolve monthly expiry")


def weekly_expiry(on: date, spec: InstrumentSpec) -> date:
    """Nearest weekly expiry on/after ``on`` (holiday-adjusted backwards, as NSE does)."""
    if spec.weekly_expiry_weekday is None:
        return monthly_expiry(on, spec)
    days_ahead = (spec.weekly_expiry_weekday - on.weekday()) % 7
    candidate = on + timedelta(days=days_ahead)
    exp = _shift_back_to_trading_day(candidate, spec)
    if exp < on:
        candidate += timedelta(days=7)
        exp = _shift_back_to_trading_day(candidate, spec)
    return exp


def nearest_explicit_expiry(on: date, spec: InstrumentSpec) -> date:
    for iso in spec.explicit_expiries:
        exp = date.fromisoformat(iso)
        if exp >= on:
            return exp
    raise ValueError(f"No explicit expiry on/after {on} configured for {spec.name}; extend InstrumentSpec.explicit_expiries")


def nearest_expiry(on: date, spec: InstrumentSpec, min_days_to_expiry: int = 0) -> date:
    """Nearest tradeable expiry for ``spec`` on date ``on``.

    ``min_days_to_expiry`` > 0 rolls to the next expiry when the nearest one is
    too close (some traders avoid 0-DTE for non-expiry setups).
    """
    probe = on + timedelta(days=min_days_to_expiry)
    if spec.weekly_expiry_weekday is not None:
        return weekly_expiry(probe, spec)
    return monthly_expiry(probe, spec)


def is_expiry_day(on: date, spec: InstrumentSpec) -> bool:
    try:
        return nearest_expiry(on, spec) == on
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Symbol builder
# ---------------------------------------------------------------------------
_MONTH_CODE = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "O", 11: "N", 12: "D"}


def _strike_text(strike: float) -> str:
    return str(int(strike)) if float(strike).is_integer() else f"{strike:g}"


def build_option_symbol(spec: InstrumentSpec, expiry: date, strike: float, option_type: OptionType) -> str:
    """Fyers option symbol for ``spec``.

    Weekly NSE contracts use ``YY<M><DD>``; monthly NSE and all MCX contracts
    use ``YY<MMM>``. A weekly NSE expiry that coincides with the monthly expiry
    is quoted with the monthly code, which is how Fyers lists it.
    """
    yy = expiry.strftime("%y")
    mmm = expiry.strftime("%b").upper()
    is_monthly = (
        spec.exchange == Exchange.MCX
        or spec.weekly_expiry_weekday is None
        or (spec.monthly_expiry_weekday is not None and expiry == monthly_expiry(expiry, spec))
    )
    if is_monthly:
        code = f"{yy}{mmm}"
    else:
        code = f"{yy}{_MONTH_CODE[expiry.month]}{expiry.strftime('%d')}"
    return f"{spec.exchange.value}:{spec.option_root}{code}{_strike_text(strike)}{option_type.value}"


def make_contract(spec: InstrumentSpec, expiry: date, strike: float, option_type: OptionType) -> OptionContract:
    return OptionContract(
        symbol=build_option_symbol(spec, expiry, strike, option_type),
        underlying=spec.name,
        exchange=spec.exchange,
        option_type=option_type,
        strike=float(strike),
        expiry=expiry,
        lot_size=spec.lot_size,
        tick_size=spec.tick_size,
    )


def with_overrides(spec: InstrumentSpec, **kwargs) -> InstrumentSpec:
    """Return a copy of ``spec`` with fields replaced (used by tests / YAML overrides)."""
    return replace(spec, **kwargs)


def apply_yaml_instrument_overrides(instruments_block: Optional[Dict[str, Dict]]) -> None:
    """Allow ``instruments:`` in settings.yaml to override lot sizes, expiries, symbols."""
    if not instruments_block:
        return
    for name, fields in instruments_block.items():
        if name not in _REGISTRY or not isinstance(fields, dict):
            continue
        allowed = {k: v for k, v in fields.items() if k in InstrumentSpec.__dataclass_fields__}
        if "explicit_expiries" in allowed:
            allowed["explicit_expiries"] = tuple(sorted(str(x) for x in allowed["explicit_expiries"]))
        if "exchange" in allowed:
            allowed["exchange"] = Exchange(allowed["exchange"])
        _REGISTRY[name] = replace(_REGISTRY[name], **allowed)


def trading_days(start: date, end: date, spec: InstrumentSpec) -> Iterable[date]:
    cur = start
    while cur <= end:
        if is_trading_day(cur, spec):
            yield cur
        cur += timedelta(days=1)


def parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def describe(spec: InstrumentSpec) -> Dict[str, object]:
    return {
        "name": spec.name, "exchange": spec.exchange.value, "underlying": spec.underlying_symbol,
        "lot_size": spec.lot_size, "strike_step": spec.strike_step, "tick_size": spec.tick_size,
        "weekly_expiry_weekday": spec.weekly_expiry_weekday, "monthly_expiry_weekday": spec.monthly_expiry_weekday,
        "explicit_expiries": list(spec.explicit_expiries), "verify": spec.verify_note,
    }


def expiries_between(start: date, end: date, spec: InstrumentSpec) -> Sequence[date]:
    out: List[date] = []
    cur = start
    while cur <= end:
        try:
            exp = nearest_expiry(cur, spec)
        except ValueError:
            break
        if exp > end:
            break
        if not out or out[-1] != exp:
            out.append(exp)
        cur = exp + timedelta(days=1)
    return out
