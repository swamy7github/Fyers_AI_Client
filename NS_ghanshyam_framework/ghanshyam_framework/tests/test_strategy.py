"""Unit tests for signal generation, contract selection and risk management."""
from datetime import date, datetime, time

import pandas as pd
import pytest

from config.settings import RiskConfig, StrategyConfig, get_instrument, with_overrides
from core.strategy import (DayRiskState, ExpiryResolver, GhanshyamStrategy, Position,
                           RiskManager, Signal, StrikeSelector, build_option_symbol, ema, rsi,
                           vwap)
from tests.helpers import breakout_day, build_day, flat_bars

DAY = date(2026, 6, 1)
NIFTY = get_instrument("NIFTY")
BANKNIFTY = get_instrument("BANKNIFTY")


def strategy(**overrides) -> GhanshyamStrategy:
    return GhanshyamStrategy(NIFTY, with_overrides(StrategyConfig(), **overrides))


def evaluate_last(frame, prev_close=24000.0, position=None, **overrides):
    return strategy(**overrides).evaluate(frame, prev_close, position)


# ---- opening range ---------------------------------------------------------
def test_opening_range_uses_first_candle():
    rng = strategy().opening_range(breakout_day(DAY))
    assert (rng.high, rng.low) == (24050.0, 23980.0)
    assert rng.end_time == datetime(2026, 6, 1, 9, 20)
    assert rng.width == 70.0


def test_opening_range_missing_when_first_candle_not_session_open():
    frame = build_day(DAY, flat_bars(24000, 5), start=time(9, 30))
    assert strategy().opening_range(frame) is None
    assert evaluate_last(frame).signal == Signal.HOLD


def test_multi_candle_opening_range():
    frame = breakout_day(DAY).iloc[:3]
    rng = strategy(opening_range_candles=2).opening_range(frame)
    assert rng.end_time == datetime(2026, 6, 1, 9, 25)
    assert rng.high == 24050.0


# ---- entries ---------------------------------------------------------------
def test_buy_ce_on_close_above_range_high():
    result = evaluate_last(breakout_day(DAY, "up"))
    assert result.signal == Signal.BUY_CE
    assert result.ref_spot == 24070.0
    assert result.stop_spot == 23980.0                       # opposite side of range
    assert result.target_spot == pytest.approx(24070.0 * 1.003)


def test_buy_pe_on_close_below_range_low():
    result = evaluate_last(breakout_day(DAY, "down"))
    assert result.signal == Signal.BUY_PE
    assert result.stop_spot == 24050.0
    assert result.target_spot == pytest.approx(23960.0 * 0.997)


def test_no_signal_while_building_opening_range():
    assert evaluate_last(breakout_day(DAY).iloc[:1]).reason == "building opening range"


def test_wick_above_range_without_close_is_ignored():
    wick = [(24020.0, 24100.0, 24010.0, 24040.0)]
    frame = build_day(DAY, [(24000.0, 24050.0, 23980.0, 24020.0), *wick])
    assert evaluate_last(frame).signal == Signal.HOLD
    assert evaluate_last(frame, confirm_on_close=False).signal == Signal.BUY_CE


def test_gap_filter_skips_the_day():
    result = evaluate_last(breakout_day(DAY, "up"), prev_close=23000.0)
    assert result.signal == Signal.HOLD
    assert "gap" in result.reason


def test_small_gap_is_allowed():
    assert evaluate_last(breakout_day(DAY, "up"), prev_close=23950.0).signal == Signal.BUY_CE


def test_breakout_after_entry_window_is_ignored():
    inside = [(24020.0, 24040.0, 24000.0, 24030.0)] * 7        # 09:20 .. 09:50
    trigger = [(24030.0, 24075.0, 24025.0, 24070.0)]           # candle starting 09:55
    frame = build_day(DAY, [(24000.0, 24050.0, 23980.0, 24020.0), *inside, *trigger])
    assert frame.index[-1].time() == time(9, 55)
    assert evaluate_last(frame).reason == "entry window closed"


def test_only_first_close_beyond_the_range_triggers():
    frame = breakout_day(DAY, "up", follow=[(24070.0, 24090.0, 24060.0, 24085.0)])
    assert evaluate_last(frame.iloc[:3]).signal == Signal.BUY_CE
    assert evaluate_last(frame).reason == "stale breakout (up)"


def test_opposite_side_breakout_still_valid_after_failed_breakout():
    follow = [(24070.0, 24072.0, 23950.0, 23960.0)]            # reversal closes below range
    frame = breakout_day(DAY, "up", follow=follow)
    assert evaluate_last(frame).signal == Signal.BUY_PE


def test_narrow_range_uses_minimum_stop_distance():
    opening = (24000.0, 24020.0, 24005.0, 24015.0)             # width 15 pts
    frame = build_day(DAY, [opening, (24015.0, 24030.0, 24010.0, 24025.0)])
    result = evaluate_last(frame)
    assert result.signal == Signal.BUY_CE
    assert result.stop_spot == pytest.approx(24025.0 * (1 - 0.0018))


def test_very_wide_range_falls_back_to_minimum_stop():
    opening = (24000.0, 24200.0, 24000.0, 24150.0)             # 200 pts = 0.83% of spot
    frame = build_day(DAY, [opening, (24150.0, 24260.0, 24140.0, 24250.0)])
    result = evaluate_last(frame)
    assert result.signal == Signal.BUY_CE
    assert result.stop_spot == pytest.approx(24250.0 * (1 - 0.0018))


def test_vwap_filter_can_block_entry():
    # The breakout candle has a long upper wick, so VWAP sits above its close.
    frame = build_day(DAY, [(24000.0, 24050.0, 23980.0, 24020.0),
                            (24020.0, 24040.0, 24000.0, 24030.0),
                            (24040.0, 24400.0, 24035.0, 24055.0)])
    assert evaluate_last(frame).signal == Signal.BUY_CE
    blocked = evaluate_last(frame, use_vwap_filter=True)
    assert blocked.signal == Signal.HOLD and "VWAP" in blocked.reason


def test_rsi_filter_blocks_when_momentum_disagrees():
    frame = breakout_day(DAY, "up")
    result = evaluate_last(frame, use_rsi_filter=True, rsi_bull=101.0)
    assert result.signal == Signal.HOLD
    assert "RSI" in result.reason


# ---- exits and position management -----------------------------------------
def make_position(call=True, entry=24070.0, stop=23980.0, target=24142.0) -> Position:
    side = Signal.BUY_CE if call else Signal.BUY_PE
    if not call:
        stop, target = 24160.0, 23998.0
    return Position(side, datetime(2026, 6, 1, 9, 30), entry, stop, target)


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_intrabar_stop_hit_for_call():
    event = strategy().check_intrabar(make_position(), bar(24060, 24065, 23970, 23990))
    assert (event.reason, event.spot) == ("stop_loss", 23980.0)


def test_intrabar_target_hit_for_call():
    event = strategy().check_intrabar(make_position(), bar(24080, 24150, 24075, 24140))
    assert (event.reason, event.spot) == ("target", 24142.0)


def test_bar_touching_both_levels_counts_as_stop_first():
    event = strategy().check_intrabar(make_position(), bar(24070, 24150, 23970, 24100))
    assert event.reason == "stop_loss"


def test_gap_through_stop_fills_at_open():
    event = strategy().check_intrabar(make_position(), bar(23950, 23990, 23940, 23960))
    assert event.spot == 23950.0


def test_put_stop_and_target():
    s, pos = strategy(), make_position(call=False)
    assert s.check_intrabar(pos, bar(24100, 24170, 24090, 24150)).reason == "stop_loss"
    assert s.check_intrabar(pos, bar(24050, 24060, 23990, 24000)).spot == 23998.0
    assert s.check_intrabar(pos, bar(24060, 24070, 24040, 24050)) is None


def test_no_event_inside_levels():
    assert strategy().check_intrabar(make_position(), bar(24070, 24100, 24020, 24090)) is None


def test_stop_moves_to_cost_after_favourable_move():
    s, pos = strategy(), make_position()
    s.update_position(pos, bar(24070, 24110, 24060, 24100))   # +0.166% >= 0.15%
    assert pos.stop_at_cost and pos.stop_spot == 24070.0
    assert pos.bars_held == 1 and pos.mfe_pct == pytest.approx(40 / 24070 * 100)
    event = s.check_intrabar(pos, bar(24100, 24105, 24060, 24065))
    assert event.reason == "breakeven_stop"


def test_small_move_does_not_trail():
    s, pos = strategy(), make_position()
    s.update_position(pos, bar(24070, 24080, 24060, 24075))
    assert not pos.stop_at_cost and pos.stop_spot == 23980.0


def test_stalled_trade_exits_after_configured_candles():
    frame = breakout_day(DAY, "up", follow=flat_bars(24072, 5))
    pos = make_position()
    pos.bars_held, pos.mfe_pct = 4, 0.02
    assert evaluate_last(frame, position=pos).reason == "stalled"
    pos.mfe_pct = 0.10
    assert evaluate_last(frame, position=pos).signal == Signal.HOLD


def test_square_off_exit():
    bars = flat_bars(24030, 74)                                # 09:15 .. 15:20 start
    frame = build_day(DAY, bars)
    pos = make_position()
    last_start = frame.index[-1].time()
    assert last_start >= time(15, 10)
    assert evaluate_last(frame, position=pos).reason == "square_off"


def test_close_beyond_stop_is_a_backstop_exit():
    frame = breakout_day(DAY, "up", follow=[(24000.0, 24010.0, 23950.0, 23960.0)])
    assert evaluate_last(frame, position=make_position()).reason == "stop_loss"


def test_empty_frame_holds():
    assert evaluate_last(pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
                         ).signal == Signal.HOLD


def test_open_position_requires_entry_levels():
    s = strategy()
    result = s.evaluate(breakout_day(DAY, "up"), 24000.0)
    pos = s.open_position(result, datetime(2026, 6, 1, 9, 30))
    assert pos.side == Signal.BUY_CE and pos.entry_spot == 24070.0
    with pytest.raises(ValueError):
        s.open_position(s.evaluate(breakout_day(DAY).iloc[:1]), datetime(2026, 6, 1, 9, 30))


# ---- strikes, expiries, symbols ----------------------------------------------
def test_atm_rounding():
    sel = StrikeSelector(NIFTY)
    assert sel.atm(24024.9) == 24000.0 and sel.atm(24025.0) == 24050.0


def test_itm_and_otm_strikes_by_side():
    sel = StrikeSelector(BANKNIFTY, "ITM", 1)
    assert sel.select(55040, Signal.BUY_CE) == 54900.0
    assert sel.select(55040, Signal.BUY_PE) == 55100.0
    otm = StrikeSelector(BANKNIFTY, "OTM", 2)
    assert otm.select(55040, Signal.BUY_CE) == 55200.0
    assert otm.select(55040, Signal.BUY_PE) == 54800.0
    with pytest.raises(ValueError):
        sel.select(55040, Signal.HOLD)


def test_weekly_expiry_is_next_tuesday():
    resolver = ExpiryResolver(NIFTY, overrides={}, holidays=[])
    assert resolver.next_expiry(date(2026, 10, 21)) == date(2026, 10, 27)
    assert resolver.next_expiry(date(2026, 10, 27)) == date(2026, 10, 27)   # expiry day counts
    assert resolver.next_expiry(date(2026, 10, 28)) == date(2026, 11, 3)


def test_expiry_shifts_back_for_holiday():
    resolver = ExpiryResolver(NIFTY, overrides={}, holidays=[date(2026, 10, 27)])
    assert resolver.next_expiry(date(2026, 10, 21)) == date(2026, 10, 26)


def test_monthly_expiry_is_last_tuesday():
    resolver = ExpiryResolver(BANKNIFTY, overrides={}, holidays=[])
    assert resolver.next_expiry(date(2026, 10, 1)) == date(2026, 10, 27)
    assert resolver.next_expiry(date(2026, 10, 28)) == date(2026, 11, 24)
    assert resolver.next_expiry(date(2026, 12, 29)) == date(2026, 12, 29)
    assert resolver.next_expiry(date(2026, 12, 30)) == date(2027, 1, 26)


def test_explicit_expiry_calendar_wins():
    resolver = ExpiryResolver(NIFTY, overrides={"NIFTY": [date(2026, 11, 5)]}, holidays=[])
    assert resolver.next_expiry(date(2026, 10, 21)) == date(2026, 11, 5)


def test_symbol_formats():
    resolver = ExpiryResolver(NIFTY, overrides={}, holidays=[])
    assert build_option_symbol(BANKNIFTY, date(2026, 10, 27), 55000, "CE", True) == \
        "NSE:BANKNIFTY26OCT55000CE"
    assert build_option_symbol(NIFTY, date(2026, 10, 13), 24000, "PE", False) == \
        "NSE:NIFTY26O1324000PE"
    assert build_option_symbol(NIFTY, date(2026, 6, 9), 24050, "CE", False) == \
        "NSE:NIFTY2660924050CE"
    assert resolver.is_monthly(date(2026, 10, 27)) and not resolver.is_monthly(date(2026, 10, 13))


def test_contract_for_signal():
    resolver = ExpiryResolver(NIFTY, overrides={}, holidays=[])
    contract = StrikeSelector(NIFTY).contract(24070.0, Signal.BUY_CE, date(2026, 10, 21), resolver)
    assert (contract.strike, contract.option_type, contract.expiry) == \
        (24050.0, "CE", date(2026, 10, 27))
    assert contract.symbol == "NSE:NIFTY26OCT24050CE"


# ---- risk management ---------------------------------------------------------
def test_trade_count_limit():
    risk, state = RiskManager(RiskConfig(max_trades_per_day=2)), DayRiskState()
    assert risk.can_enter(state)[0]
    risk.register_entry(state)
    risk.register_entry(state)
    allowed, why = risk.can_enter(state)
    assert not allowed and "max trades" in why


def test_daily_loss_limit_blocks_entries():
    risk, state = RiskManager(RiskConfig(max_daily_loss_inr=1000)), DayRiskState()
    risk.register_exit(state, -1200)
    allowed, why = risk.can_enter(state)
    assert not allowed and "daily loss" in why


def test_consecutive_losses_block_and_win_resets():
    risk, state = RiskManager(RiskConfig(max_consecutive_losses=2, max_daily_loss_inr=1e9)), DayRiskState()
    risk.register_exit(state, -100)
    risk.register_exit(state, 50)
    assert state.consecutive_losses == 0 and risk.can_enter(state)[0]
    risk.register_exit(state, -100)
    risk.register_exit(state, -100)
    assert not risk.can_enter(state)[0]


def test_fixed_lot_sizing_is_clamped():
    assert RiskManager(RiskConfig(lots=3)).lots_for(24000, 23950, 65) == 3
    assert RiskManager(RiskConfig(lots=50, max_lots=10)).lots_for(24000, 23950, 65) == 10
    assert RiskManager(RiskConfig(lots=0)).lots_for(24000, 23950, 65) == 1


def test_risk_based_sizing():
    risk = RiskManager(RiskConfig(risk_per_trade_inr=5000, assumed_delta=0.5, max_lots=10))
    # 100 pt stop * 0.5 delta * 65 = 3250 per lot -> 1 lot fits a 5000 budget
    assert risk.lots_for(24000, 23900, 65) == 1
    # 20 pt stop -> 650 per lot -> 7 lots
    assert risk.lots_for(24000, 23980, 65) == 7
    # budget too small for even one lot
    assert RiskManager(RiskConfig(risk_per_trade_inr=1000)).lots_for(24000, 23900, 65) == 0


# ---- indicators --------------------------------------------------------------
def test_indicators():
    rising = pd.Series([float(x) for x in range(1, 40)])
    assert rsi(rising).iloc[-1] == 100.0
    assert rsi(pd.Series([5.0] * 20)).iloc[-1] == 50.0
    assert ema(pd.Series([10.0, 10.0, 10.0]), 3).iloc[-1] == 10.0
    frame = build_day(DAY, [(10, 12, 8, 11), (11, 13, 9, 12)])
    assert vwap(frame).iloc[0] == pytest.approx((12 + 8 + 11) / 3)


def test_signal_values():
    assert {s.value for s in Signal} == {"BUY_CE", "BUY_PE", "EXIT", "HOLD"}
