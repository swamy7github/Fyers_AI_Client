"""Strike selection per guide p2: ATM or slightly ITM, initial delta >= 0.50, never OTM.

Modes (``strike.mode`` in settings.yaml):

* ``ATM_ITM`` (default) — start at ATM; if the model delta at ATM is below
  ``strike.min_delta`` (typical late on expiry day or far from the strike
  grid) step one strike ITM. Never steps OTM.
* ``ATM`` — always the nearest strike.
* ``ITM1`` — always one strike in the money (the v1 attempt's choice; higher
  delta, higher premium outlay).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from .instruments import InstrumentSpec, make_contract, nearest_expiry
from .models import OptionContract, OptionType
from .pricing import option_delta


@dataclass(frozen=True)
class StrikeChoice:
    contract: OptionContract
    model_delta: float
    moneyness: str  # ATM | ITM1 | ITM2


class StrikeSelector:
    def __init__(self, mode: str = "ATM_ITM", min_delta: float = 0.50, iv: float = 0.15,
                 risk_free_rate: float = 0.065, min_days_to_expiry: int = 0):
        self.mode = mode.upper()
        self.min_delta = float(min_delta)
        self.iv = float(iv)
        self.r = float(risk_free_rate)
        self.min_days_to_expiry = int(min_days_to_expiry)

    def expiry_for(self, spec: InstrumentSpec, on: date) -> date:
        return nearest_expiry(on, spec, self.min_days_to_expiry)

    def select(self, spec: InstrumentSpec, spot: float, option_type: OptionType, now: datetime,
               expiry: Optional[date] = None) -> StrikeChoice:
        expiry = expiry or self.expiry_for(spec, now.date())
        atm = spec.atm_strike(spot)
        if self.mode == "ITM1":
            strike, label = spec.itm_strike(spot, option_type, 1), "ITM1"
        elif self.mode == "ATM":
            strike, label = atm, "ATM"
        else:  # ATM_ITM
            strike, label = atm, "ATM"
            delta = option_delta(spot, strike, now, expiry, self.iv, option_type, self.r)
            steps = 0
            while delta < self.min_delta and steps < 2:
                steps += 1
                strike = spec.itm_strike(spot, option_type, steps)
                label = f"ITM{steps}"
                delta = option_delta(spot, strike, now, expiry, self.iv, option_type, self.r)
        delta = option_delta(spot, strike, now, expiry, self.iv, option_type, self.r)
        return StrikeChoice(contract=make_contract(spec, expiry, strike, option_type), model_delta=delta, moneyness=label)

    @staticmethod
    def is_otm(spec: InstrumentSpec, spot: float, strike: float, option_type: OptionType) -> bool:
        """Guard used by tests and the broker layer: the framework must never buy OTM."""
        atm = spec.atm_strike(spot)
        return strike > atm if option_type == OptionType.CE else strike < atm
