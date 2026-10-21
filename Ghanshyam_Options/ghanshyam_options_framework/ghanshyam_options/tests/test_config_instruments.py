from datetime import date, datetime, time

import pytest

from ghanshyam_options.config.config import load_settings
from ghanshyam_options.instruments import (
    build_option_symbol, get_instrument, is_expiry_day, is_trading_day, make_contract, monthly_expiry, nearest_expiry,
    weekly_expiry,
)
from ghanshyam_options.models import Exchange, OptionType
from ghanshyam_options.strikes import StrikeSelector


# --------------------------------------------------------------------------- config
def test_settings_load_and_sections(settings):
    assert settings.total_capital == 500000
    assert set(settings.enabled_strategies()) >= {"inside_candle", "hammer", "shooting_star", "morning_0920",
                                                  "afternoon_1420", "squeeze_1500", "trendline"}
    nse = settings.session("NSE")
    assert nse.open == time(9, 15) and nse.square_off == time(15, 15) and nse.close == time(15, 30)
    assert nse.in_no_entry_window(time(12, 0)) and not nse.in_no_entry_window(time(10, 0))
    assert not nse.can_enter(time(15, 10))  # after no_new_entries_after
    mcx = settings.session("MCX")
    assert mcx.open == time(9, 0) and mcx.close == time(23, 30) and mcx.square_off == time(23, 15)
    assert mcx.can_enter(time(21, 0))


def test_settings_overrides_and_per_instrument_lookup():
    s = load_settings(overrides={"capital.total": 250000, "risk.max_trades_per_day": 2})
    assert s.total_capital == 250000 and s.get("risk.max_trades_per_day") == 2
    assert s.instrument_value("strategies.hammer.min_run_points", "BANKNIFTY") == 150
    assert s.instrument_value("strategies.hammer.min_run_points", "NIFTY50") == 70
    assert s.instrument_value("strategies.hammer.min_run_points", "UNKNOWN", 5.0) == 5.0
    assert s.strategy_windows("inside_candle", "NSE")[0] == (time(9, 25), time(11, 30))


def test_settings_validation_rejects_bad_values():
    with pytest.raises(ValueError):
        load_settings(overrides={"capital.total": 0})
    with pytest.raises(ValueError):
        load_settings(overrides={"risk.max_daily_loss_pct": 5})
    with pytest.raises(ValueError):
        load_settings(overrides={"sessions.NSE.square_off": "15:45"})


# --------------------------------------------------------------------------- instruments
def test_registry_and_aliases():
    assert get_instrument("nifty").name == "NIFTY50"
    assert get_instrument("NIFTYBANK").name == "BANKNIFTY"
    assert get_instrument("crude").exchange == Exchange.MCX
    with pytest.raises(KeyError):
        get_instrument("DOGECOIN")
    n = get_instrument("NIFTY50")
    assert n.strike_step == 50 and n.underlying_symbol == "NSE:NIFTY50-INDEX"
    b = get_instrument("BANKNIFTY")
    assert b.strike_step == 100 and b.weekly_expiry_weekday is None


def test_atm_itm_strikes():
    n = get_instrument("NIFTY50")
    assert n.atm_strike(25432.0) == 25450.0
    assert n.itm_strike(25432.0, OptionType.CE) == 25400.0
    assert n.itm_strike(25432.0, OptionType.PE) == 25500.0
    b = get_instrument("BANKNIFTY")
    assert b.atm_strike(56549.0) == 56500.0 and b.itm_strike(56549.0, OptionType.PE) == 56600.0
    c = get_instrument("CRUDEOIL")
    assert c.atm_strike(5476.0) == 5500.0 and c.itm_strike(5476.0, OptionType.CE) == 5450.0


def test_nse_weekly_and_monthly_expiry_rules_2026():
    n = get_instrument("NIFTY50")
    # Thursday 2026-10-15 -> next Tuesday 2026-10-20 is a holiday (Dussehra) -> shifts back to Monday 19th
    assert weekly_expiry(date(2026, 10, 15), n) == date(2026, 10, 19)
    # Monday 2026-10-12 -> Tuesday 13th
    assert weekly_expiry(date(2026, 10, 12), n) == date(2026, 10, 13)
    # on the expiry day itself the nearest expiry is today
    assert nearest_expiry(date(2026, 10, 13), n) == date(2026, 10, 13)
    assert is_expiry_day(date(2026, 10, 13), n) and not is_expiry_day(date(2026, 10, 14), n)
    # monthly = last Tuesday: Oct 2026 -> 27th; Nov 2026 -> 24th is a holiday -> 23rd
    assert monthly_expiry(date(2026, 10, 1), n) == date(2026, 10, 27)
    assert monthly_expiry(date(2026, 11, 1), n) == date(2026, 11, 23)
    b = get_instrument("BANKNIFTY")
    assert nearest_expiry(date(2026, 10, 15), b) == date(2026, 10, 27)  # monthly only
    assert not is_trading_day(date(2026, 10, 20), n) and not is_trading_day(date(2026, 10, 17), n)


def test_symbol_builder_formats():
    n = get_instrument("NIFTY50")
    # weekly: YY + month code + DD (Oct = O) -> matches the repo's symbol master (NSE:NIFTY26O1321850CE)
    assert build_option_symbol(n, date(2026, 10, 13), 21850, OptionType.CE) == "NSE:NIFTY26O1321850CE"
    assert build_option_symbol(n, date(2026, 9, 8), 25400, OptionType.PE) == "NSE:NIFTY2690825400PE"
    # weekly expiry coinciding with the monthly expiry uses the monthly code
    assert build_option_symbol(n, date(2026, 10, 27), 25000, OptionType.CE) == "NSE:NIFTY26OCT25000CE"
    b = get_instrument("BANKNIFTY")
    assert build_option_symbol(b, date(2026, 10, 27), 51100, OptionType.CE) == "NSE:BANKNIFTY26OCT51100CE"
    c = get_instrument("CRUDEOIL")
    assert build_option_symbol(c, date(2026, 11, 17), 5500, OptionType.PE) == "MCX:CRUDEOIL26NOV5500PE"
    contract = make_contract(c, nearest_expiry(date(2026, 10, 16), c), 5450, OptionType.CE)
    assert contract.symbol == "MCX:CRUDEOIL26NOV5450CE" and contract.lot_size == 100


def test_mcx_explicit_expiry_resolution():
    c = get_instrument("CRUDEOIL")
    assert nearest_expiry(date(2026, 10, 1), c) == date(2026, 10, 15)
    assert nearest_expiry(date(2026, 10, 16), c) == date(2026, 11, 17)
    with pytest.raises(ValueError):
        nearest_expiry(date(2027, 6, 1), c)


def test_strike_selector_never_otm():
    n = get_instrument("NIFTY50")
    now = datetime(2026, 10, 15, 10, 0)
    sel = StrikeSelector(mode="ATM_ITM", min_delta=0.5, iv=0.13)
    for spot in (25401.0, 25424.0, 25426.0, 25449.0):
        for ot in (OptionType.CE, OptionType.PE):
            ch = sel.select(n, spot, ot, now)
            assert not StrikeSelector.is_otm(n, spot, ch.contract.strike, ot), (spot, ot, ch)
            assert ch.model_delta >= 0.45
    itm = StrikeSelector(mode="ITM1").select(n, 25432.0, OptionType.CE, now)
    assert itm.contract.strike == 25400.0 and itm.moneyness == "ITM1"
    # very late on expiry day the ATM delta is ~0.5 either side; selector must still return a non-OTM strike
    exp_now = datetime(2026, 10, 13, 15, 0)
    ch = sel.select(n, 25440.0, OptionType.PE, exp_now)
    assert ch.contract.strike >= 25450.0
