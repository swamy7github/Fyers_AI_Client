"""
In-The-Money Strike Calculation and Symbol Formatting
"""

from typing import Optional
from config import IndexConfig
from models import OptionType


class OptionStrikeSelector:
    """
    Computes 1-strike In-The-Money (ITM) contracts targeting Delta 0.55 - 0.70.
    Formats exact broker strings for Fyers API.
    """

    @staticmethod
    def get_itm_strike(spot_price: float, option_type: OptionType, config: IndexConfig) -> float:
        """
        Calculates ITM strike level based on benchmark index step:
        - CE: ATM - Step
        - PE: ATM + Step
        """
        step = config.strike_step
        atm_strike = round(spot_price / step) * step

        if option_type == OptionType.CE:
            return atm_strike - step
        else:
            return atm_strike + step

    @staticmethod
    def format_fyers_symbol(
        config: IndexConfig,
        strike: float,
        option_type: OptionType,
        expiry_date_str: Optional[str] = None
    ) -> str:
        """
        Formats standardized Fyers API option symbol string.
        Format: NSE:{UNDERLYING_CODE}{EXPIRY}{STRIKE}{TYPE}
        Example: NSE:BANKNIFTY26OCT51100CE, NSE:NIFTY26OCT24450PE
        """
        expiry = expiry_date_str or config.default_expiry
        strike_int = int(strike)
        opt_str = option_type.value
        return f"NSE:{config.code.upper()}{expiry}{strike_int}{opt_str}"
