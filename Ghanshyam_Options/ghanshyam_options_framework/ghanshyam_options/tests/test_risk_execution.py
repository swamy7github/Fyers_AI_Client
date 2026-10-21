from datetime import datetime, time

import pytest

from ghanshyam_options.execution.broker import ChargeModel, OrderRequest
from ghanshyam_options.execution.fyers_broker import FyersBroker, LiveTradingDisabled, make_broker
from ghanshyam_options.execution.paper_broker import PaperBroker, PaperConfig
from ghanshyam_options.instruments import get_instrument, make_contract
from ghanshyam_options.models import Exchange, ExitReason, OptionType, Position, Signal
from ghanshyam_options.risk import RiskEngine
from .conftest import NIFTY_DAY

NOW = datetime.combine(NIFTY_DAY, time(10, 0))


def _signal(level=25200.0, sl=25180.0, target=25240.0, counter=False, strategy="inside_candle"):
    return Signal(instrument="NIFTY50", strategy=strategy, option_type=OptionType.CE, ts=NOW, spot_level=level,
                  stop_loss=sl, target=target, reason="t", confidence=0.9, counter_trend=counter)


def _position(risk: RiskEngine, pid="P1", entry_price=150.0, lots=2, sl=25180.0, target=25240.0, sig=None):
    spec = get_instrument("NIFTY50")
    sig = sig or _signal(sl=sl, target=target)
    contract = make_contract(spec, NIFTY_DAY, 25200.0, OptionType.CE)
    pos = Position(position_id=pid, contract=contract, signal=sig, lots=lots, qty=lots * spec.lot_size, entry_ts=NOW,
                   entry_price=entry_price, entry_spot=25200.0, spot_sl=sl, spot_target=target, premium_sl=entry_price - 11,
                   premium_target=None, premium_model="synthetic", initial_risk_points=25200.0 - sl, best_spot=25200.0,
                   worst_spot=25200.0, risk_budget=5000.0)
    risk.register_open(pos, spec)
    return pos


# --------------------------------------------------------------------------- risk: circuit breakers
def test_sizing_respects_risk_budget_tranche_and_counter_trend(settings):
    r = RiskEngine(settings)
    spec = get_instrument("NIFTY50")
    r.start_day(NIFTY_DAY)
    sig = _signal()  # 20 pt risk
    # premium 150, premium SL 139 -> 11/unit risk x 75 = 825 per lot; budget 1 % of 5L = 5000 -> 6 lots
    res = r.size(sig, spec, entry_premium=150.0, premium_sl=139.0, delta=0.55)
    assert res.lots == 6 and res.qty == 450 and res.per_lot_risk == pytest.approx(825.0)
    res_ct = r.size(_signal(counter=True), spec, 150.0, 139.0, 0.55)
    assert res_ct.lots == 3  # 50 % size for counter-trend
    # tranche cap: premium outlay 150 x 75 = 11,250 per lot; tranche 125,000 -> at most 11 lots; max_lots 10
    settings.set("capital.risk_per_trade_pct", 0.5)
    r2 = RiskEngine(settings)
    r2.start_day(NIFTY_DAY)
    assert r2.size(sig, spec, 150.0, 139.0, 0.55).lots == 10
    settings.set("capital.max_lots", 100)
    r3 = RiskEngine(settings)
    r3.start_day(NIFTY_DAY)
    assert r3.size(sig, spec, 150.0, 139.0, 0.55).lots == 11
    assert r3.size(sig, spec, 0.0, None, 0.55).lots == 0


def test_max_trades_and_consecutive_loss_lockout(settings):
    r = RiskEngine(settings)
    r.start_day(NIFTY_DAY)
    spec = get_instrument("NIFTY50")
    session = settings.session("NSE")
    assert r.can_trade(_signal(), NOW, session)[0]
    p1 = _position(r, "A")
    r.register_close(p1, -3000.0, ExitReason.STOP_LOSS, spec)
    p2 = _position(r, "B")
    r.register_close(p2, -2500.0, ExitReason.STOP_LOSS, spec)
    ok, why = r.can_trade(_signal(), NOW, session)
    assert not ok and "consecutive" in why and r.day.locked_out
    # fresh day: 3 trades (2 wins + 1 loss) hits the max-trades cap without a consecutive-loss lockout
    r.start_day(NIFTY_DAY)
    for pid, pnl in (("C", 2000.0), ("D", 1500.0), ("E", -500.0)):
        r.register_close(_position(r, pid), pnl, ExitReason.TARGET if pnl > 0 else ExitReason.TIME_STOP, spec)
    ok, why = r.can_trade(_signal(), NOW, session)
    assert not ok and "max trades" in why


def test_max_daily_loss_stops_trading(settings):
    r = RiskEngine(settings)   # 2 % of 5,00,000 = 10,000
    r.start_day(NIFTY_DAY)
    spec = get_instrument("NIFTY50")
    r.register_close(_position(r, "A"), -6000.0, ExitReason.TIME_STOP, spec)
    assert not r.day.locked_out
    r.register_close(_position(r, "B"), 1000.0, ExitReason.TARGET, spec)
    r.register_close(_position(r, "C"), -5500.0, ExitReason.TIME_STOP, spec)
    assert r.day.locked_out and "daily loss" in r.day.lockout_reason
    assert not r.can_trade(_signal(), NOW, settings.session("NSE"))[0]


def test_profit_retention_rule_80_20(settings):
    r = RiskEngine(settings)
    r.start_day(NIFTY_DAY)
    spec = get_instrument("NIFTY50")
    r.register_close(_position(r, "A"), 10000.0, ExitReason.TARGET, spec)
    # secondary risk capped at 25 % of banked 10,000 = 2,500 (< 1 % capital 5,000)
    assert r.risk_budget() == pytest.approx(2000.0)  # min(5000, 2500, 10000 - 8000 = 2000)
    sig = _signal()
    res = r.size(sig, spec, 150.0, 139.0, 0.55)
    assert res.lots == 2  # 2000 // 825
    r.register_close(_position(r, "B"), -1500.0, ExitReason.STOP_LOSS, spec)
    assert r.day.locked_out and "R6" in r.day.lockout_reason
    assert r.day.realised_pnl == pytest.approx(8500.0)  # green session stays green


def test_entry_window_and_square_off_time(settings):
    r = RiskEngine(settings)
    r.start_day(NIFTY_DAY)
    session = settings.session("NSE")
    ok, why = r.can_trade(_signal(), datetime.combine(NIFTY_DAY, time(12, 30)), session)
    assert not ok and "window" in why
    assert r.can_trade(_signal(), datetime.combine(NIFTY_DAY, time(13, 45)), session)[0]
    assert not r.can_trade(_signal(), datetime.combine(NIFTY_DAY, time(15, 6)), session)[0]
    assert r.must_square_off(datetime.combine(NIFTY_DAY, time(15, 15)), session)
    mcx = settings.session("MCX")
    assert r.can_trade(_signal(), datetime.combine(NIFTY_DAY, time(22, 0)), mcx)[0]
    assert not r.must_square_off(datetime.combine(NIFTY_DAY, time(22, 0)), mcx)


def test_one_position_per_instrument(settings):
    r = RiskEngine(settings)
    r.start_day(NIFTY_DAY)
    _position(r, "A")
    ok, why = r.can_trade(_signal(), NOW, settings.session("NSE"))
    assert not ok and "already open" in why


def test_manage_stop_target_trailing_and_square_off(settings):
    r = RiskEngine(settings)
    r.start_day(NIFTY_DAY)
    session = settings.session("NSE")
    pos = _position(r, "A", sl=25180.0, target=25240.0)
    t = datetime.combine(NIFTY_DAY, time(10, 5))
    assert r.manage(pos, 25210, 155, t, session, 10.0, bar_high=25215, bar_low=25195) is None
    # +1R reached -> breakeven + trail
    assert r.manage(pos, 25222, 165, t, session, 10.0, bar_high=25225, bar_low=25205) is None
    assert pos.trailing_active and pos.spot_sl >= 25200.0
    # pullback through the trailed stop -> TRAILING_STOP
    assert r.manage(pos, 25199, 150, t, session, 10.0, bar_high=25210, bar_low=25190) == ExitReason.TRAILING_STOP
    pos2 = _position(r, "B", sl=25180.0, target=25240.0)
    assert r.manage(pos2, 25245, 190, t, session, 10.0, bar_high=25250, bar_low=25230) == ExitReason.TARGET
    pos3 = _position(r, "C", sl=25180.0, target=25240.0)
    assert r.manage(pos3, 25185, 120, t, session, 10.0, bar_high=25205, bar_low=25178) == ExitReason.STOP_LOSS
    pos4 = _position(r, "D")
    late = datetime.combine(NIFTY_DAY, time(15, 15))
    assert r.manage(pos4, 25205, 152, late, session, 10.0, 25210, 25200) == ExitReason.SQUARE_OFF
    pos5 = _position(r, "E", sig=_signal(strategy="squeeze_1500"))
    pos5.signal.max_hold_minutes = 10
    assert r.manage(pos5, 25205, 152, datetime.combine(NIFTY_DAY, time(10, 10)), session, 10.0, 25210, 25200) == ExitReason.TIME_STOP


# --------------------------------------------------------------------------- charges / paper broker
def test_charge_model_itemisation():
    cm = ChargeModel(brokerage_per_order=20, stt_sell_pct=0.001, exchange_txn_pct_nse=0.00035, sebi_fee_pct=0.000001,
                     stamp_duty_buy_pct=0.00003, gst_pct=0.18, exchange_txn_pct_mcx=0.0005, ctt_sell_pct_mcx=0.0005)
    buy = cm.charges(Exchange.NSE, "BUY", 10000.0)
    assert buy["stt"] == 0 and buy["stamp"] == pytest.approx(0.3) and buy["exchange"] == pytest.approx(3.5)
    assert buy["gst"] == pytest.approx((20 + 3.5 + 0.01) * 0.18)
    sell = cm.charges(Exchange.NSE, "SELL", 10000.0)
    assert sell["stt"] == pytest.approx(10.0) and sell["stamp"] == 0 and sell["total"] > buy["total"]
    mcx_sell = cm.charges(Exchange.MCX, "SELL", 10000.0)
    assert mcx_sell["ctt"] == pytest.approx(5.0) and mcx_sell["stt"] == 0 and mcx_sell["exchange"] == pytest.approx(5.0)


def test_paper_broker_fills_with_slippage_and_ledger():
    pb = PaperBroker(ChargeModel(), PaperConfig(slippage_pct=0.01, slippage_ticks=1, fill_on="next_bar_open"), starting_cash=100000)
    req = OrderRequest(symbol="NSE:NIFTY26O1325200CE", side="BUY", qty=75, exchange=Exchange.NSE, ts=NOW,
                       reference_price=100.0, tick_size=0.05, bar_open=102.0)
    res = pb.buy(req)
    assert res.ok and res.fill.price == pytest.approx(103.05)   # 102 + 1 % + 1 tick
    assert pb.positions() == {"NSE:NIFTY26O1325200CE": 75}
    assert pb.cash == pytest.approx(100000 - 103.05 * 75 - res.fill.charges)
    bad = pb.sell(OrderRequest(symbol="NSE:X", side="SELL", qty=75, exchange=Exchange.NSE, ts=NOW, reference_price=1.0))
    assert not bad.ok
    out = pb.sell(OrderRequest(symbol="NSE:NIFTY26O1325200CE", side="SELL", qty=75, exchange=Exchange.NSE, ts=NOW,
                               reference_price=110.0, tick_size=0.05, bar_open=110.0))
    assert out.ok and out.fill.price == pytest.approx(108.85) and pb.positions() == {}
    assert not pb.buy(OrderRequest(symbol="NSE:Y", side="BUY", qty=0, exchange=Exchange.NSE, ts=NOW, reference_price=1.0)).ok


def test_live_broker_is_disabled_by_default(settings, monkeypatch):
    monkeypatch.delenv("GHANSHYAM_LIVE", raising=False)
    with pytest.raises(LiveTradingDisabled):
        make_broker(settings, "live", armed=True)
    settings.set("live.enabled", True)
    with pytest.raises(LiveTradingDisabled):
        make_broker(settings, "live", armed=False)          # needs --i-understand-live-risk
    with pytest.raises(LiveTradingDisabled):
        make_broker(settings, "live", armed=True)           # needs GHANSHYAM_LIVE=1
    broker = make_broker(settings, "paper")
    assert isinstance(broker, PaperBroker) and not broker.is_live

    class FakeClient:
        calls = []

        def place_order(self, **kw):
            self.calls.append(kw)
            return {"s": "ok", "id": "ORD1"}

    monkeypatch.setenv("GHANSHYAM_LIVE", "1")
    fb = FyersBroker(settings, armed=True, client=FakeClient())
    res = fb.buy(OrderRequest(symbol="NSE:NIFTY26O1325200CE", side="BUY", qty=75, exchange=Exchange.NSE, ts=NOW, reference_price=100.0))
    assert res.ok and res.fill.order_id == "ORD1" and FakeClient.calls[0]["side"] == "BUY" and FakeClient.calls[0]["qty"] == 75
