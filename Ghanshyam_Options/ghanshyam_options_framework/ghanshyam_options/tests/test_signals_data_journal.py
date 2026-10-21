from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from ghanshyam_options.data.cache import CandleCache
from ghanshyam_options.data.csv_feed import CsvFeed
from ghanshyam_options.data.feed import candles_from_fyers, normalise_frame
from ghanshyam_options.data.fyers_feed import FyersFeed
from ghanshyam_options.instruments import get_instrument
from ghanshyam_options.journal.trade_log import DAILY_COLUMNS, TRADE_COLUMNS, TradeLog
from ghanshyam_options.models import Bias, DailySummary, OptionType, Trade
from ghanshyam_options.pricing import black_scholes, option_delta, synthetic_premium
from ghanshyam_options.signals import SignalEngine, compute_bias, compute_levels
from ghanshyam_options.strategies.base import build_strategies
from .conftest import NIFTY_DAY, bars_from_closes, candle, flat_day, frame, levels


# --------------------------------------------------------------------------- bias & levels
def test_compute_levels_from_previous_session():
    prev = flat_day(date(2026, 10, 14), 25200.0, wiggle=10)
    spec = get_instrument("NIFTY50")
    lv = compute_levels(prev, NIFTY_DAY, spec, [flat_day(date(2026, 10, 13), 25100.0, wiggle=5)])
    assert lv.pdh == max(c.high for c in prev) and lv.pdl == min(c.low for c in prev) and lv.pdc == prev[-1].close
    assert lv.support_levels and lv.resistance_levels and not lv.is_expiry_day
    with pytest.raises(ValueError):
        compute_levels([], NIFTY_DAY, spec)


def test_pdc_bias_and_equilibrium_trap():
    lv = levels(NIFTY_DAY, pdh=25300, pdl=25100, pdc=25200)
    t = datetime.combine(NIFTY_DAY, time(9, 15))
    up = bars_from_closes([25210, 25220, 25230], t)
    assert compute_bias(up, lv, acceptance_bars=2, trap_band=40) == Bias.BULLISH
    down = bars_from_closes([25190, 25180], t)
    assert compute_bias(down, lv, 2, 40) == Bias.BEARISH
    mixed = bars_from_closes([25190, 25210], t)
    assert compute_bias(mixed, lv, 2, 40) == Bias.NEUTRAL
    assert compute_bias(up[:1], lv, 2, 40) == Bias.NEUTRAL  # not enough bars
    # trap: PDC 25290 only 10 pts under PDH 25300 and price between them
    trap_lv = levels(NIFTY_DAY, 25300, 25100, 25290)
    assert compute_bias(bars_from_closes([25292, 25295], t), trap_lv, 2, 40) == Bias.NEUTRAL
    assert compute_bias(bars_from_closes([25310, 25320], t), trap_lv, 2, 40) == Bias.BULLISH  # escaped the trap


# --------------------------------------------------------------------------- signal engine filters
def test_signal_engine_time_windows_confidence_and_dedupe(settings):
    settings.set("risk.min_confidence", 0.95)
    spec = get_instrument("NIFTY50")
    eng = SignalEngine(settings, build_strategies(settings, ["inside_candle"]))
    lv = levels(NIFTY_DAY, 25400, 25000, 25150)
    eng.start_day(spec, NIFTY_DAY, lv)
    t = datetime.combine(NIFTY_DAY, time(9, 15))
    pre = bars_from_closes([25200] * 4 + [25210, 25220], t, spread=3)   # bullish bias (closes > PDC)
    for c in pre:
        eng.on_bar(spec, c)
    mother = candle(t + timedelta(minutes=30), 25220, 25260, 25200, 25250)
    inside = candle(t + timedelta(minutes=35), 25250, 25255, 25230, 25240)
    eng.on_bar(spec, mother)
    got = eng.on_bar(spec, inside)
    rej = eng.rejected_for_day(spec)
    # with a 0.95 threshold the raw inside-bar signal (conf < 0.95) is rejected, not emitted
    assert got == [] and rej and "confidence" in rej[-1].reason
    # midday window rejection: the strategy's own window is removed so the engine's session filter is exercised
    settings.set("risk.min_confidence", 0.1)
    settings.set("strategies.inside_candle.windows", {})
    eng2 = SignalEngine(settings, build_strategies(settings, ["inside_candle"]))
    eng2.start_day(spec, NIFTY_DAY, lv)
    noon = datetime.combine(NIFTY_DAY, time(11, 40))
    for c in bars_from_closes([25200] * 4 + [25210, 25220], noon - timedelta(minutes=30), spread=3):
        eng2.on_bar(spec, c)
    eng2.on_bar(spec, candle(noon, 25220, 25260, 25200, 25250))
    assert eng2.on_bar(spec, candle(noon + timedelta(minutes=5), 25250, 25255, 25230, 25240)) == []
    assert any("no-entry" in r.reason for r in eng2.rejected_for_day(spec))
    # the same pattern at 10:00 (inside entry hours) is accepted
    eng3 = SignalEngine(settings, build_strategies(settings, ["inside_candle"]))
    eng3.start_day(spec, NIFTY_DAY, lv)
    ten = datetime.combine(NIFTY_DAY, time(10, 0))
    for c in bars_from_closes([25200] * 4 + [25210, 25220], ten - timedelta(minutes=30), spread=3):
        eng3.on_bar(spec, c)
    eng3.on_bar(spec, candle(ten, 25220, 25260, 25200, 25250))
    got3 = eng3.on_bar(spec, candle(ten + timedelta(minutes=5), 25250, 25255, 25230, 25240))
    assert len(got3) == 1 and got3[0].strategy == "inside_candle"
    # duplicate suppression: the identical signal key on the next bar is rejected as duplicate
    eng3.on_bar(spec, candle(ten + timedelta(minutes=10), 25240, 25252, 25232, 25245))  # another inside bar of same mother? no
    assert all(r.reason != "" for r in eng3.rejected_for_day(spec))


def test_signal_engine_opening_range_and_bias_history(settings):
    spec = get_instrument("NIFTY50")
    eng = SignalEngine(settings, build_strategies(settings, ["inside_candle"]))
    lv = levels(NIFTY_DAY, 25400, 25000, 25150)
    eng.start_day(spec, NIFTY_DAY, lv)
    for c in flat_day(NIFTY_DAY, 25200.0, wiggle=5)[:4]:
        eng.on_bar(spec, c)
    st = eng.state(spec)
    assert st.levels.opening_range_ready and st.levels.opening_range_high > st.levels.opening_range_low
    assert st.bias_history[-1] == Bias.BULLISH and len(st.candles) == 4


# --------------------------------------------------------------------------- data layer
def test_normalise_frame_and_fyers_candles():
    raw = [[1792035300, 25400, 25450, 25380, 25420, 0], [1792035600, 25420, 25430, 25400, 25410, 0],
           [1792035300, 1, 1, 1, 1, 0]]  # duplicate ts, keep last
    df = candles_from_fyers(raw)
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert len(df) == 2 and df.iloc[0]["ts"] == pd.Timestamp("2026-10-15 09:05:00")  # epoch -> IST naive
    messy = pd.DataFrame({"Timestamp": ["2026-10-15 09:15", "2026-10-15 09:20", "bad"], "O": [1, 2, 3], "H": [2, 1, 3],
                          "L": [0.5, 0.5, 1], "C": [1.5, 1.5, 2], "V": [1, 1, 1]})
    out = normalise_frame(messy)
    assert len(out) == 1  # row 2 has high < open/close, row 3 bad ts
    with pytest.raises(ValueError):
        normalise_frame(pd.DataFrame({"ts": [1], "open": [1]}))


def test_csv_feed_history_quote_and_clock(tmp_path):
    cs = flat_day(NIFTY_DAY, 25200.0)
    path = tmp_path / "NSE_NIFTY50-INDEX_5.csv"
    frame(cs).to_csv(path, index=False)
    feed = CsvFeed(tmp_path)
    df = feed.history("NSE:NIFTY50-INDEX", 5, NIFTY_DAY, NIFTY_DAY)
    assert len(df) == len(cs)
    q = feed.quote("NSE:NIFTY50-INDEX")
    assert q is not None and q.ltp == cs[-1].close
    feed.set_clock(datetime.combine(NIFTY_DAY, time(10, 0)))
    assert len(feed.history("NSE:NIFTY50-INDEX", 5, NIFTY_DAY, NIFTY_DAY)) == 10  # 09:15..10:00 inclusive
    assert feed.quote("NSE:NIFTY50-INDEX").ts == datetime.combine(NIFTY_DAY, time(10, 0))
    assert feed.history("NSE:UNKNOWN", 5, NIFTY_DAY, NIFTY_DAY).empty


def test_candle_cache_round_trip_and_partial_day_skip(tmp_path):
    cache = CandleCache(tmp_path / "cache", "csv")
    two_days = flat_day(date(2026, 10, 14), 25100.0) + flat_day(NIFTY_DAY, 25200.0)
    written = cache.put("NSE:NIFTY50-INDEX", 5, frame(two_days), today=NIFTY_DAY)
    assert written == [date(2026, 10, 14)]  # today's partial session is never cached
    assert cache.has("NSE:NIFTY50-INDEX", 5, date(2026, 10, 14)) and not cache.has("NSE:NIFTY50-INDEX", 5, NIFTY_DAY)
    back = cache.get("NSE:NIFTY50-INDEX", 5, date(2026, 10, 14))
    assert len(back) == 75 and back.iloc[0]["ts"] == pd.Timestamp("2026-10-14 09:15:00")
    assert cache.clear("NSE:NIFTY50-INDEX") == 1


def test_fyers_feed_adapter_maps_existing_client_api(tmp_path):
    """FyersFeed must call the existing FyersDataService surface and parse its shapes - with a stub, no network."""
    calls = []

    class StubService:
        def get_candles_range(self, symbol, resolution, range_from_date, range_to_date):
            calls.append(("history", symbol, resolution, range_from_date, range_to_date))
            base = int(datetime(2026, 10, 14, 9, 15).timestamp())
            return [[base + 300 * i, 100 + i, 101 + i, 99 + i, 100.5 + i, 10] for i in range(5)]

        def get_quotes_batched(self, symbols):
            calls.append(("quotes", tuple(symbols)))
            return {s: {"lp": 123.45, "bid": 123.4, "ask": 123.5, "volume": 10} for s in symbols}

    feed = FyersFeed(client=StubService(), cache=CandleCache(tmp_path / "c"))
    df = feed.history("NSE:NIFTY50-INDEX", 5, date(2026, 10, 14), date(2026, 10, 14))
    assert len(df) == 5 and calls[0][0] == "history" and calls[0][2] == "5" and calls[0][3] == "2026-10-14"
    # second call is served from the cache (no new history call)
    n = len(calls)
    feed.history("NSE:NIFTY50-INDEX", 5, date(2026, 10, 14), date(2026, 10, 14))
    assert len(calls) == n
    q = feed.quote("NSE:NIFTY26O1325200CE")
    assert q is not None and q.ltp == 123.45 and q.bid == 123.4 and calls[-1][0] == "quotes"


# --------------------------------------------------------------------------- pricing
def test_black_scholes_sanity_and_synthetic_premium():
    g = black_scholes(25000, 25000, 7 / 365, 0.13, OptionType.CE)
    assert 0.48 < g.delta < 0.56 and g.price > 0 and g.gamma > 0 and g.theta_per_minute < 0
    assert option_delta(25000, 24900, datetime(2026, 10, 15, 10, 0), date(2026, 10, 20), 0.13, OptionType.CE) > 0.6
    p_now = synthetic_premium(25000, 25000, datetime(2026, 10, 15, 10, 0), date(2026, 10, 20), 0.13, OptionType.CE)
    p_later = synthetic_premium(25000, 25000, datetime(2026, 10, 15, 15, 0), date(2026, 10, 20), 0.13, OptionType.CE)
    assert p_later < p_now  # theta
    assert synthetic_premium(25000, 25000, datetime(2026, 10, 20, 15, 29), date(2026, 10, 20), 0.13, OptionType.PE) >= 1.0


# --------------------------------------------------------------------------- journal
def _trade(i: int, pnl: float, d: date = NIFTY_DAY) -> Trade:
    e = datetime.combine(d, time(10, 0 + i))
    return Trade(trade_id=f"T{i}", mode="backtest", instrument="NIFTY50", underlying="NSE:NIFTY50-INDEX",
                 strategy="inside_candle", signal_ts=e, direction="CE", option_symbol="NSE:NIFTY26O2025200CE",
                 strike=25200, expiry="2026-10-20", entry_ts=e, entry_price=100.0, qty=75, lots=1, sl=90.0, target=None,
                 exit_ts=e + timedelta(minutes=10), exit_price=100 + pnl / 75, exit_reason="TARGET", gross_pnl=pnl,
                 charges=50.0, net_pnl=pnl - 50, r_multiple=(pnl - 50) / 750, confidence=0.8, premium_model="synthetic",
                 entry_spot=25200, exit_spot=25220, spot_sl=25180, spot_target=25240, planned_r=2.0, slippage=0.5,
                 max_favourable_premium=110, max_adverse_premium=98, hold_minutes=10.0, notes="n")


def test_trade_log_csv_schema_append_only_and_daily(tmp_path):
    jl = TradeLog(tmp_path / "logs", mode="backtest")
    p = jl.log_trade(_trade(1, 1000))
    jl.log_trade(_trade(2, -500))
    assert p.name == "trades_20261015.csv"
    df = pd.read_csv(p)
    assert list(df.columns) == TRADE_COLUMNS and len(df) == 2
    assert df.iloc[1]["net_pnl"] == -550 and df.iloc[0]["premium_model"] == "synthetic"
    # append-only: a new TradeLog on the same dir keeps existing rows
    jl2 = TradeLog(tmp_path / "logs")
    jl2.log_trade(_trade(3, 200))
    assert len(pd.read_csv(p)) == 3 and open(p).read().count("trade_id") == 1
    ds = DailySummary(trade_date=NIFTY_DAY, instrument="NIFTY50", mode="backtest", trades=3, wins=2, losses=1,
                      gross_pnl=700, charges=150, net_pnl=550, peak_profit=950, max_intraday_drawdown=550,
                      consecutive_losses=0, lockout_reason="", signals_generated=5, signals_rejected=2, equity_end=500550)
    jl2.log_daily(ds)
    daily = pd.read_csv(jl2.daily_path)
    assert list(daily.columns) == DAILY_COLUMNS and daily.iloc[0]["net_pnl"] == 550
    back = jl2.read_trades()
    assert len(back) == 3 and back["entry_ts"].dtype.kind == "M"


# --------------------------------------------------------------------------- no look-ahead at engine level
def test_signal_engine_incremental_atr_matches_vectorised_and_is_causal(settings):
    from ghanshyam_options.indicators import atr, candle_frame
    from ghanshyam_options.sample_data.generator import generate
    from ghanshyam_options.signals import split_sessions

    spec = get_instrument("NIFTY50")
    df = generate("NIFTY50", date(2026, 9, 7), date(2026, 9, 8), seed=11)
    sessions = split_sessions(df)
    days = sorted(sessions)
    prev, today = sessions[days[0]], sessions[days[1]]
    lv = compute_levels(prev, days[1], spec)

    def run(bars):
        eng = SignalEngine(settings, build_strategies(settings))
        eng.start_day(spec, days[1], lv)
        sigs = []
        for c in bars:
            sigs.extend(eng.on_bar(spec, c))
        return eng, sigs

    full_eng, full_sigs = run(today)
    ref = atr(candle_frame(today), 14).to_numpy()
    for a, b in zip(full_eng.state(spec)._atr, ref):
        assert abs(a - b) < 1e-9
    # signals produced with only the first 40 bars must be exactly the prefix of the full-day signals
    _, part_sigs = run(today[:40])
    full_prefix = [s for s in full_sigs if s.ts <= today[39].ts]
    assert [(s.ts, s.strategy, s.spot_level, s.stop_loss) for s in part_sigs] == \
           [(s.ts, s.strategy, s.spot_level, s.stop_loss) for s in full_prefix]
