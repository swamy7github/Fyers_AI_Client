"""Unit tests for backtest maths, execution costs, data handling, logging and the
paper-trading loop. Everything runs on synthetic data; no API credentials needed."""
import calendar
import csv
import json
import logging
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from config.settings import ExecutionConfig, RiskConfig, StrategyConfig, get_instrument
from core.backtester import (Backtester, BlackScholesPricer, ExecutionModel, SimulatedBroker,
                             month_chunks)
from core.data_handler import (CandleAggregator, CsvDataSource, DataError, FyersDataSource,
                               LiveCandleFeed, RateLimitError, find_missing_bars,
                               normalize_candles, resample_candles, with_retries)
from core.logger_handler import (ColorFormatter, TradeLogger, compute_max_drawdown,
                                 compute_metrics, format_metrics, generate_eod_summary,
                                 setup_logging)
from core.paper_trader import MarketClock, PaperTrader
from core.strategy import GhanshyamStrategy, OptionContract, Signal, SignalResult
from tests.helpers import (ConstPricer, FakeSource, breakout_day, build_day, flat_bars,
                           make_trade)

NIFTY = get_instrument("NIFTY")
DAY = date(2026, 6, 1)               # a Monday
CONTRACT = OptionContract("NIFTY", 24050.0, "CE", date(2026, 6, 2), "NSE:NIFTY2660224050CE")
ENTRY = SignalResult(Signal.BUY_CE, "test", ref_spot=24070.0, stop_spot=23980.0,
                     target_spot=24142.0)


# --------------------------------------------------------------------------- #
# Performance metrics
# --------------------------------------------------------------------------- #
def sample_trades():
    return [make_trade(net, minute, charges=10.0)
            for minute, net in enumerate([100, -50, 200, -300, 150])]


def test_metrics_known_values():
    m = compute_metrics(sample_trades())
    assert m.total_trades == 5 and m.wins == 3 and m.losses == 2
    assert m.win_rate_pct == pytest.approx(60.0)
    assert m.total_pnl == pytest.approx(100.0)
    assert m.total_charges == pytest.approx(50.0)
    assert m.gross_pnl == pytest.approx(150.0)
    assert m.profit_factor == pytest.approx(450 / 350)
    assert m.max_drawdown == pytest.approx(300.0)            # peak 250 -> trough -50
    assert m.avg_win == pytest.approx(150.0) and m.avg_loss == pytest.approx(-175.0)
    assert (m.best_trade, m.worst_trade) == (200, -300)


def test_metrics_do_not_depend_on_input_order():
    forward = compute_metrics(sample_trades())
    backward = compute_metrics(list(reversed(sample_trades())))
    assert forward.max_drawdown == backward.max_drawdown
    assert forward.total_pnl == backward.total_pnl


def test_drawdown_counts_loss_from_the_starting_balance():
    assert compute_max_drawdown([-100, 50]) == 100
    assert compute_max_drawdown([100, 100, -50, -60, 300]) == 110
    assert compute_max_drawdown([]) == 0
    assert compute_max_drawdown([10, 20, 30]) == 0


def test_profit_factor_edge_cases():
    all_wins = compute_metrics([make_trade(50, 0), make_trade(70, 1)])
    assert all_wins.profit_factor == float("inf")
    assert all_wins.to_dict()["profit_factor"] == "inf"
    all_losses = compute_metrics([make_trade(-50, 0)])
    assert all_losses.profit_factor == 0.0 and all_losses.win_rate_pct == 0.0


def test_empty_and_breakeven_trades():
    empty = compute_metrics([])
    assert empty.total_trades == 0 and empty.total_pnl == 0 and empty.profit_factor == 0
    flat = compute_metrics([make_trade(0, 0)])
    assert flat.wins == 0 and flat.losses == 0 and flat.total_trades == 1


def test_format_metrics_contains_headline_numbers():
    text = format_metrics(compute_metrics(sample_trades()), "TEST")
    assert "TEST" in text and "Net PnL" in text and "60.0%" in text and "1.29" in text


# --------------------------------------------------------------------------- #
# Execution model
# --------------------------------------------------------------------------- #
def test_slippage_is_adverse_and_tick_rounded():
    model = ExecutionModel(NIFTY, ExecutionConfig(slippage_pct=0.005))
    assert model.fill_price(100.0, "BUY") == pytest.approx(100.5)
    assert model.fill_price(100.0, "SELL") == pytest.approx(99.5)
    assert model.fill_price(0.05, "SELL") == pytest.approx(0.05)     # never below one tick


def test_charges_match_manual_calculation():
    cfg = ExecutionConfig()
    model = ExecutionModel(NIFTY, cfg)
    turnover = 100.0 * 65
    brokerage = min(cfg.brokerage_flat_inr, cfg.brokerage_max_pct * turnover)
    exchange = turnover * NIFTY.exchange_txn_pct
    sebi = turnover * cfg.sebi_pct
    gst = cfg.gst_pct * (brokerage + exchange + sebi)
    buy = brokerage + exchange + sebi + gst + turnover * cfg.stamp_buy_pct
    sell = brokerage + exchange + sebi + gst + turnover * NIFTY.stt_sell_pct
    assert model.charges(100.0, 65, "BUY") == pytest.approx(buy)
    assert model.charges(100.0, 65, "SELL") == pytest.approx(sell)
    assert sell > buy                    # sell carries STT, buy only stamp duty


def test_brokerage_is_capped_at_flat_fee_for_large_orders():
    cfg = ExecutionConfig(sebi_pct=0, stamp_buy_pct=0, gst_pct=0)
    model = ExecutionModel(NIFTY, cfg)
    big = model.charges(200.0, 6500, "BUY")
    assert big == pytest.approx(20.0 + 200.0 * 6500 * NIFTY.exchange_txn_pct)


# --------------------------------------------------------------------------- #
# Black-Scholes pricer
# --------------------------------------------------------------------------- #
def test_black_scholes_basic_properties():
    pricer = BlackScholesPricer(NIFTY, iv=0.14)
    ts = datetime(2026, 6, 1, 9, 30)
    ce = OptionContract("NIFTY", 24050.0, "CE", date(2026, 6, 2), "x")
    pe = OptionContract("NIFTY", 24050.0, "PE", date(2026, 6, 2), "y")
    atm_ce = pricer.price(ce, ts, 24050.0)
    assert 0 < atm_ce < 24050.0 * 0.01
    assert pricer.price(ce, ts, 24150.0) > atm_ce
    assert pricer.price(pe, ts, 24150.0) < pricer.price(pe, ts, 24050.0)
    assert BlackScholesPricer(NIFTY, iv=0.30).price(ce, ts, 24050.0) > atm_ce


def test_put_call_parity_holds_within_tick_rounding():
    pricer = BlackScholesPricer(NIFTY, iv=0.15, rate=0.065)
    ts, expiry = datetime(2026, 6, 1, 9, 30), date(2026, 6, 2)
    ce = OptionContract("NIFTY", 24000.0, "CE", expiry, "x")
    pe = OptionContract("NIFTY", 24000.0, "PE", expiry, "y")
    t_years = (datetime.combine(expiry, NIFTY.expiry_time) - ts).total_seconds() / (365 * 86400)
    forward_gap = 24100.0 - 24000.0 * pow(2.718281828459045, -0.065 * t_years)
    assert pricer.price(ce, ts, 24100.0) - pricer.price(pe, ts, 24100.0) == \
        pytest.approx(forward_gap, abs=0.11)


def test_price_near_expiry_stays_positive_and_close_to_intrinsic():
    pricer = BlackScholesPricer(NIFTY, iv=0.14)
    ce = OptionContract("NIFTY", 24000.0, "CE", date(2026, 6, 2), "x")
    late = datetime(2026, 6, 2, 15, 29, 30)
    assert pricer.price(ce, late, 24100.0) == pytest.approx(100.0, abs=1.0)
    assert pricer.price(ce, late, 23000.0) == NIFTY.tick_size        # worthless -> one tick
    assert pricer.price(ce, datetime(2026, 6, 3, 10, 0), 24100.0) > 0   # past expiry: no crash


def test_invalid_iv_is_rejected():
    with pytest.raises(ValueError):
        BlackScholesPricer(NIFTY, iv=0)


# --------------------------------------------------------------------------- #
# Simulated broker
# --------------------------------------------------------------------------- #
def make_broker(*premiums, slippage=0.0):
    model = ExecutionModel(NIFTY, ExecutionConfig(slippage_pct=slippage))
    return SimulatedBroker(NIFTY, model, ConstPricer(*premiums), "backtest"), model


def test_round_trip_pnl_arithmetic():
    broker, model = make_broker(100.0, 110.0)
    strategy = GhanshyamStrategy(NIFTY)
    opened = broker.enter(strategy, ENTRY, CONTRACT, datetime(2026, 6, 1, 9, 30), lots=2)
    assert opened.quantity == 130 and opened.entry_price == 100.0
    trade = broker.exit(24142.0, datetime(2026, 6, 1, 9, 40), "target")
    expected_charges = model.charges(100.0, 130, "BUY") + model.charges(110.0, 130, "SELL")
    assert trade.gross_pnl == pytest.approx(1300.0)
    assert trade.charges == pytest.approx(expected_charges)
    assert trade.net_pnl == pytest.approx(1300.0 - expected_charges)
    assert trade.side == "BUY_CE" and trade.exit_reason == "target"
    assert broker.open_trade is None


def test_losing_round_trip_and_slippage_cost():
    broker, _ = make_broker(100.0, 100.0, slippage=0.01)
    strategy = GhanshyamStrategy(NIFTY)
    broker.enter(strategy, ENTRY, CONTRACT, datetime(2026, 6, 1, 9, 30), lots=1)
    trade = broker.exit(24070.0, datetime(2026, 6, 1, 9, 35), "stalled")
    assert trade.entry_price == pytest.approx(101.0) and trade.exit_price == pytest.approx(99.0)
    assert trade.gross_pnl == pytest.approx(-2.0 * 65)
    assert trade.net_pnl < trade.gross_pnl


def test_broker_guards():
    broker, _ = make_broker(100.0)
    strategy = GhanshyamStrategy(NIFTY)
    with pytest.raises(RuntimeError):
        broker.exit(24000.0, datetime(2026, 6, 1, 9, 40), "x")
    with pytest.raises(ValueError):
        broker.enter(strategy, ENTRY, CONTRACT, datetime(2026, 6, 1, 9, 30), lots=0)
    broker.enter(strategy, ENTRY, CONTRACT, datetime(2026, 6, 1, 9, 30), lots=1)
    with pytest.raises(RuntimeError):
        broker.enter(strategy, ENTRY, CONTRACT, datetime(2026, 6, 1, 9, 31), lots=1)


def test_live_premium_override_is_used():
    broker, _ = make_broker(100.0)
    strategy = GhanshyamStrategy(NIFTY)
    opened = broker.enter(strategy, ENTRY, CONTRACT, datetime(2026, 6, 1, 9, 30), 1, premium=55.0)
    assert opened.entry_price == 55.0


# --------------------------------------------------------------------------- #
# Backtester end to end
# --------------------------------------------------------------------------- #
def run_backtest(*frames, start=DAY, end=DAY, **kwargs):
    risk = kwargs.pop("risk", RiskConfig())
    strategy_cfg = kwargs.pop("strategy_cfg", StrategyConfig())
    bt = Backtester(FakeSource(*frames), NIFTY, strategy_cfg, risk, **kwargs)
    return bt.run(start, end)


def test_winning_breakout_hits_target():
    frame = breakout_day(DAY, "up", follow=[(24070.0, 24150.0, 24065.0, 24140.0)])
    result = run_backtest(frame)
    assert result.metrics.total_trades == 1
    trade = result.trades[0]
    assert trade.side == "BUY_CE" and trade.exit_reason == "target"
    assert trade.entry_time == datetime(2026, 6, 1, 9, 30)
    assert trade.exit_spot == pytest.approx(24070.0 * 1.003)
    assert trade.quantity == 65 and trade.strike == 24050.0
    assert trade.gross_pnl > 0 and trade.net_pnl == pytest.approx(trade.gross_pnl - trade.charges)
    assert result.daily_pnl[DAY] == pytest.approx(result.metrics.total_pnl)


def test_put_breakout_is_traded_as_pe():
    frame = breakout_day(DAY, "down", follow=[(23960.0, 23965.0, 23880.0, 23890.0)])
    trade = run_backtest(frame).trades[0]
    assert trade.side == "BUY_PE" and trade.exit_reason == "target" and trade.net_pnl > 0


def test_stop_loss_creates_a_losing_trade():
    frame = breakout_day(DAY, "up", follow=[(24070.0, 24075.0, 23970.0, 23990.0)])
    result = run_backtest(frame)
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss" and trade.exit_spot == 23980.0
    assert trade.net_pnl < 0 and result.metrics.win_rate_pct == 0.0
    assert result.metrics.max_drawdown == pytest.approx(-trade.net_pnl)


def test_no_breakout_means_no_trades():
    frame = build_day(DAY, [(24000.0, 24050.0, 23980.0, 24020.0)] + flat_bars(24020, 20, 10))
    result = run_backtest(frame)
    assert result.trades == [] and result.metrics.total_trades == 0


def test_stalled_trade_is_closed_at_the_candle_close():
    frame = breakout_day(DAY, "up", follow=flat_bars(24072, 5))
    trade = run_backtest(frame).trades[0]
    assert trade.exit_reason == "stalled"
    assert trade.exit_time == datetime(2026, 6, 1, 9, 50)           # 4th post-entry bar ends
    assert trade.exit_spot == 24072.0


def test_open_position_is_squared_off_at_1515():
    drift = [(24070.0, 24090.0, 24065.0, 24085.0)] + [(24085.0, 24095.0, 24075.0, 24085.0)] * 68
    frame = breakout_day(DAY, "up", follow=drift)
    assert frame.index[-1].time() == time(15, 10)
    trade = run_backtest(frame).trades[0]
    assert trade.exit_reason == "square_off"
    assert trade.exit_time == datetime(2026, 6, 1, 15, 15)


def test_trade_limit_and_reentry_on_the_opposite_side():
    frame = breakout_day(DAY, "up", follow=[(24070.0, 24075.0, 23970.0, 23990.0),   # stop out
                                            (23990.0, 23995.0, 23940.0, 23950.0)])  # fresh breakdown
    two = run_backtest(frame)
    assert [t.side for t in two.trades] == ["BUY_CE", "BUY_PE"]
    assert two.trades[1].exit_reason == "end_of_data"
    one = run_backtest(frame, risk=RiskConfig(max_trades_per_day=1))
    assert len(one.trades) == 1


def test_daily_loss_limit_stops_trading_after_a_loss():
    frame = breakout_day(DAY, "up", follow=[(24070.0, 24075.0, 23970.0, 23990.0),
                                            (23990.0, 23995.0, 23940.0, 23950.0)])
    result = run_backtest(frame, risk=RiskConfig(max_daily_loss_inr=1.0))
    assert len(result.trades) == 1


def test_gap_day_is_skipped_but_same_move_without_gap_trades():
    day2 = DAY + timedelta(days=1)
    quiet = build_day(DAY, flat_bars(24000, 10))
    normal = breakout_day(day2, "up")
    gapped = normal.copy()
    gapped[["open", "high", "low", "close"]] += 1000.0              # opens ~4% above prior close
    skipped = run_backtest(quiet, gapped, start=DAY, end=day2)
    assert skipped.trades == []
    traded = run_backtest(quiet, normal, start=DAY, end=day2)
    assert len(traded.trades) == 1 and traded.trades[0].entry_time.date() == day2


def test_missing_days_are_reported_as_skipped():
    frame = breakout_day(DAY, "up", follow=[(24070.0, 24150.0, 24065.0, 24140.0)])
    result = run_backtest(frame, start=DAY, end=date(2026, 6, 5))
    assert sorted(result.skipped_days) == [date(2026, 6, d) for d in (2, 3, 4, 5)]
    assert result.metrics.total_trades == 1


def test_fetch_errors_are_recorded_not_raised():
    class Broken:
        def fetch_history(self, symbol, start, end):
            raise DataError("boom")
    result = Backtester(Broken(), NIFTY).run(DAY, date(2026, 6, 2))
    assert len(result.skipped_days) == 2 and result.trades == []


def test_backtest_rejects_reversed_dates():
    with pytest.raises(ValueError):
        Backtester(FakeSource(), NIFTY).run(date(2026, 6, 2), DAY)


def test_month_chunks():
    assert month_chunks(date(2026, 5, 28), date(2026, 6, 3)) == [
        (date(2026, 5, 28), date(2026, 5, 31)), (date(2026, 6, 1), date(2026, 6, 3))]
    assert month_chunks(DAY, DAY) == [(DAY, DAY)]


def test_backtest_writes_trades_to_csv(tmp_path):
    frame = breakout_day(DAY, "up", follow=[(24070.0, 24150.0, 24065.0, 24140.0)])
    bt = Backtester(FakeSource(frame), NIFTY)
    log = TradeLogger(tmp_path / "bt.csv")
    result = bt.run(DAY, DAY, trade_logger=log)
    rows = list(csv.DictReader((tmp_path / "bt.csv").open()))
    assert [r["event"] for r in rows] == ["ENTRY", "EXIT"]
    assert float(rows[1]["net_pnl"]) == pytest.approx(result.trades[0].net_pnl, abs=0.01)


# --------------------------------------------------------------------------- #
# Data handling
# --------------------------------------------------------------------------- #
def epoch_ist(y, mo, d, h, mi):
    """Epoch seconds for an IST wall-clock time."""
    return calendar.timegm((y, mo, d, h, mi, 0)) - 19800


def test_normalize_fyers_response_converts_epoch_to_ist():
    raw = {"s": "ok", "candles": [[epoch_ist(2026, 6, 1, 9, 15), 100, 110, 95, 105, 1000],
                                  [epoch_ist(2026, 6, 1, 9, 20), 105, 112, 101, 111, 800]]}
    frame = normalize_candles(raw)
    assert list(frame.index) == [pd.Timestamp("2026-06-01 09:15"), pd.Timestamp("2026-06-01 09:20")]
    assert frame["close"].tolist() == [105.0, 111.0]


def test_normalize_drops_duplicates_and_malformed_rows():
    t = epoch_ist(2026, 6, 1, 9, 15)
    raw = [[t, 1, 2, 0.5, 1.5, 0], [t, 1, 3, 0.5, 2.0, 0],            # duplicate timestamp
           [t + 300, 5, 4, 6, 5, 0]]                                    # high < low
    frame = normalize_candles(raw)
    assert len(frame) == 1 and frame["close"].iloc[0] == 2.0


def test_normalize_error_responses():
    assert normalize_candles({"s": "no_data", "candles": []}).empty
    assert normalize_candles([]).empty
    with pytest.raises(RateLimitError):
        normalize_candles({"s": "error", "code": 429, "message": "request limit reached"})
    with pytest.raises(DataError):
        normalize_candles({"s": "error", "code": 401, "message": "token expired"})
    with pytest.raises(DataError):
        normalize_candles(pd.DataFrame({"open": [1.0]}, index=pd.to_datetime(["2026-06-01"])))


def test_resample_one_minute_to_five_minute():
    idx = pd.date_range("2026-06-01 09:15", periods=10, freq="1min")
    one = pd.DataFrame({"open": range(10), "high": [x + 2 for x in range(10)],
                        "low": [x - 1 for x in range(10)], "close": [x + 1 for x in range(10)],
                        "volume": [10] * 10}, index=idx).astype(float)
    five = resample_candles(one)
    assert len(five) == 2
    first = five.iloc[0]
    assert (first["open"], first["high"], first["low"], first["close"], first["volume"]) == \
        (0.0, 6.0, -1.0, 5.0, 50.0)


def test_candle_aggregator_builds_five_minute_bars_from_ticks():
    agg = CandleAggregator()
    t0 = datetime(2026, 6, 1, 9, 15, 2)
    assert agg.add_tick(t0, 100.0, 1) is None
    assert agg.add_tick(t0 + timedelta(minutes=2), 104.0, 2) is None
    assert agg.add_tick(t0 + timedelta(minutes=3), 98.0, 3) is None
    assert agg.add_tick(datetime(2026, 6, 1, 9, 14, 0), 1.0) is None      # stale tick ignored
    done = agg.add_tick(datetime(2026, 6, 1, 9, 20, 1), 101.0, 4)
    assert done == {"timestamp": datetime(2026, 6, 1, 9, 15), "open": 100.0, "high": 104.0,
                    "low": 98.0, "close": 98.0, "volume": 6}
    assert agg.flush()["open"] == 101.0


def test_find_missing_bars():
    frame = build_day(DAY, flat_bars(24000, 6)).drop(pd.Timestamp("2026-06-01 09:25"))
    missing = find_missing_bars(frame, datetime(2026, 6, 1, 9, 15), datetime(2026, 6, 1, 9, 50))
    assert missing == [datetime(2026, 6, 1, 9, 25), datetime(2026, 6, 1, 9, 45)]


def test_retry_backs_off_longer_for_rate_limits():
    waits, attempts = [], []

    @with_retries(max_attempts=4, base_delay=1.0, sleep_fn=waits.append)
    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RateLimitError("slow down")
        return "ok"

    assert flaky() == "ok" and waits == [4.0, 8.0]


def test_retry_gives_up_after_max_attempts():
    waits = []

    @with_retries(max_attempts=2, sleep_fn=waits.append)
    def always_fails():
        raise DataError("down")

    with pytest.raises(DataError):
        always_fails()
    assert len(waits) == 1


def test_fyers_source_chunks_long_ranges_and_retries():
    calls, state = [], {"failed": False}

    def history(symbol, resolution, start, end):
        calls.append((start, end))
        if not state["failed"]:
            state["failed"] = True
            return {"s": "error", "code": 429, "message": "rate limit"}
        stamp = epoch_ist(start.year, start.month, start.day, 9, 15)
        return {"s": "ok", "candles": [[stamp, 1, 2, 0.5, 1.5, 0]]}

    source = FyersDataSource(history, chunk_days=2, sleep_fn=lambda s: None)
    frame = source.fetch_history("X", date(2026, 6, 1), date(2026, 6, 5))
    assert [c[0] for c in calls] == [date(2026, 6, 1), date(2026, 6, 1), date(2026, 6, 3),
                                     date(2026, 6, 5)]        # first chunk retried once
    assert len(frame) == 3


def test_fyers_source_wraps_unexpected_sdk_errors():
    def history(*args):
        raise ValueError("sdk exploded")
    source = FyersDataSource(history, sleep_fn=lambda s: None)
    with pytest.raises(DataError):
        source.fetch_history("X", DAY, DAY)
    with pytest.raises(DataError):
        source.ltp("X")


def test_from_fyers_model_uses_sdk_shapes():
    class FakeFyers:
        def history(self, data):
            self.history_args = data
            return {"s": "ok", "candles": [[epoch_ist(2026, 6, 1, 9, 15), 1, 2, 0.5, 1.5, 0]]}

        def quotes(self, data):
            return {"s": "ok", "d": [{"n": data["symbols"], "v": {"lp": 24123.5}}]}

    fyers = FakeFyers()
    source = FyersDataSource.from_fyers_model(fyers)
    assert len(source.fetch_history("NSE:NIFTY50-INDEX", DAY, DAY)) == 1
    assert fyers.history_args["resolution"] == "5" and fyers.history_args["range_from"] == "2026-06-01"
    assert source.ltp("NSE:NIFTY50-INDEX") == 24123.5


def test_live_feed_never_returns_the_in_progress_candle():
    feed = LiveCandleFeed(FakeSource(build_day(DAY, flat_bars(24000, 3))), "X")
    assert len(feed.completed_candles(datetime(2026, 6, 1, 9, 24, 59))) == 1
    assert len(feed.completed_candles(datetime(2026, 6, 1, 9, 25))) == 2
    assert feed.previous_close(DAY) is None
    assert feed.previous_close(DAY + timedelta(days=1)) == 24000.0


def test_csv_data_source(tmp_path):
    frame = breakout_day(DAY).reset_index()
    path = tmp_path / "bars.csv"
    frame.to_csv(path, index=False)
    source = CsvDataSource(path)
    assert len(source.fetch_history("any", DAY, DAY)) == 3
    assert source.fetch_history("any", DAY + timedelta(days=1), DAY + timedelta(days=2)).empty
    with pytest.raises(FileNotFoundError):
        CsvDataSource(tmp_path / "missing.csv")


# --------------------------------------------------------------------------- #
# CSV log and EOD summary
# --------------------------------------------------------------------------- #
def test_trade_log_round_trip(tmp_path):
    log = TradeLogger(tmp_path / "trades.csv")
    trades = [make_trade(120.0, 0, 15.0), make_trade(-80.0, 10, 15.0, day=date(2026, 6, 2),
                                                     mode="paper")]
    log.log_trades(trades)
    rows = list(csv.DictReader((tmp_path / "trades.csv").open()))
    assert [r["event"] for r in rows] == ["ENTRY", "EXIT", "ENTRY", "EXIT"]
    assert set(rows[0]) >= {"trade_id", "timestamp", "price", "quantity", "option_symbol"}
    loaded = log.read_trades()
    assert [round(t.net_pnl, 2) for t in loaded] == [120.0, -80.0]
    assert len(log.read_trades(trade_date=date(2026, 6, 2))) == 1
    assert len(log.read_trades(mode="paper")) == 1
    assert TradeLogger(tmp_path / "none.csv").read_trades() == []


def test_eod_summary_files_and_numbers(tmp_path, capsys):
    log = TradeLogger(tmp_path / "trades.csv")
    log.log_trades([make_trade(300.0, 0, 10.0, mode="paper"),
                    make_trade(-100.0, 10, 10.0, mode="paper"),
                    make_trade(50.0, 20, 10.0, mode="backtest")])
    summary = generate_eod_summary(DAY, mode="paper", log_path=tmp_path / "trades.csv",
                                   out_dir=tmp_path)
    assert summary["metrics"]["total_trades"] == 2
    assert summary["metrics"]["total_pnl"] == 200.0
    assert summary["win_loss_ratio"] == 1.0 and summary["peak_drawdown"] == 100.0
    saved = json.loads((tmp_path / "eod_summary_2026-06-01.json").read_text())
    assert saved["metrics"]["win_rate_pct"] == 50.0
    assert "Net PnL" in (tmp_path / "eod_summary_2026-06-01.txt").read_text()
    assert "EOD SUMMARY" in capsys.readouterr().out


def test_eod_summary_with_no_trades(tmp_path):
    summary = generate_eod_summary(DAY, log_path=tmp_path / "x.csv", out_dir=tmp_path, echo=False)
    assert summary["metrics"]["total_trades"] == 0 and summary["trades"] == []


def test_logging_setup_and_formatter():
    setup_logging(logging.WARNING)
    assert logging.getLogger().level == logging.WARNING and len(logging.getLogger().handlers) == 1
    record = logging.LogRecord("n", logging.ERROR, "f", 1, "bad", None, None)
    assert "\033[31m" in ColorFormatter(use_color=True).format(record)
    assert "\033[" not in ColorFormatter(use_color=False).format(record)


# --------------------------------------------------------------------------- #
# Paper trading loop (fake feed and quotes; no broker involved)
# --------------------------------------------------------------------------- #
class QuoteBook:
    """Underlying LTP is scripted; option quotes are unavailable (model fallback)."""

    def __init__(self, spot=24060.0):
        self.spot = spot

    def __call__(self, symbol):
        if symbol.endswith(("CE", "PE")):
            raise DataError("no option quote")
        return self.spot


def make_trader(tmp_path, frame, book, clock):
    feed = LiveCandleFeed(FakeSource(frame), "NSE:NIFTY50-INDEX")
    return PaperTrader(NIFTY, feed, quote_fn=book, trade_logger=TradeLogger(tmp_path / "t.csv"),
                       now_fn=lambda: clock["now"], sleep_fn=lambda s: None)


def at(h, m, s=0, day=DAY):
    return datetime.combine(day, time(h, m, s))


def test_paper_trader_enters_then_exits_on_tick_target(tmp_path):
    book, clock = QuoteBook(), {"now": at(9, 31)}
    frame = breakout_day(DAY, "up", follow=[(24070.0, 24150.0, 24065.0, 24140.0)])
    trader = make_trader(tmp_path, frame, book, clock)

    assert trader.step(at(9, 21)) == "candle"                      # opening range built
    assert trader.step(at(9, 26)) == "candle"                      # inside the range
    assert trader.broker.open_trade is None
    assert trader.step(at(9, 30, 5)) == "candle"                   # breakout candle closed
    opened = trader.broker.open_trade
    assert opened is not None and opened.contract.option_type == "CE"
    assert opened.contract.symbol == "NSE:NIFTY2660224050CE"
    assert trader.step(at(9, 31)) == "idle"                        # no new candle, no stop hit

    book.spot = 24150.0                                            # LTP jumps through target
    assert trader.step(at(9, 32)) == "tick_exit"
    assert trader.broker.open_trade is None
    assert len(trader.trades) == 1 and trader.trades[0].exit_reason == "target"
    rows = list(csv.DictReader((tmp_path / "t.csv").open()))
    assert [r["event"] for r in rows] == ["ENTRY", "EXIT"] and rows[0]["mode"] == "paper"


def test_paper_trader_exits_on_completed_candle_when_tick_missed_it(tmp_path):
    book, clock = QuoteBook(), {"now": at(9, 31)}
    frame = breakout_day(DAY, "up", follow=[(24070.0, 24150.0, 24065.0, 24140.0)])
    trader = make_trader(tmp_path, frame, book, clock)
    trader.step(at(9, 30, 5))                                      # enters (acts on latest candle)
    assert trader.broker.open_trade is not None
    assert trader.step(at(9, 35, 5)) == "candle"                   # candle high touched target
    assert trader.trades[0].exit_reason == "target"


def test_paper_trader_stops_out_on_adverse_tick(tmp_path):
    book, clock = QuoteBook(), {"now": at(9, 31)}
    trader = make_trader(tmp_path, breakout_day(DAY, "up"), book, clock)
    trader.step(at(9, 30, 5))
    book.spot = 23950.0
    assert trader.step(at(9, 31)) == "tick_exit"
    assert trader.trades[0].exit_reason == "stop_loss" and trader.trades[0].net_pnl < 0


def test_paper_trader_squares_off_at_1515_and_writes_eod(tmp_path):
    book, clock = QuoteBook(), {"now": at(15, 16)}
    trader = make_trader(tmp_path, breakout_day(DAY, "up"), book, clock)
    trader.step(at(9, 30, 5))
    book.spot = 24080.0
    assert trader.step(at(15, 16)) == "squared_off"
    assert trader.trades[0].exit_reason == "square_off"
    summary = trader.finish()
    assert summary["metrics"]["total_trades"] == 1
    assert (tmp_path / "eod_summary_2026-06-01.json").exists()


def test_finish_closes_any_open_position(tmp_path):
    clock = {"now": at(10, 0)}
    trader = make_trader(tmp_path, breakout_day(DAY, "up"), QuoteBook(), clock)
    trader.step(at(9, 30, 5))
    assert trader.finish()["metrics"]["total_trades"] == 1
    assert trader.trades[0].exit_reason == "session_end"


def test_paper_trader_is_idle_when_market_is_closed(tmp_path):
    trader = make_trader(tmp_path, breakout_day(DAY, "up"), QuoteBook(), {"now": at(9, 0)})
    assert trader.step(at(9, 0)) == "closed"
    assert trader.step(at(10, 0, day=date(2026, 6, 6))) == "closed"   # Saturday
    assert trader.step(at(15, 30)) == "closed"


def test_paper_trader_run_loop_terminates(tmp_path):
    ticks = iter(at(9, 20) + timedelta(seconds=5 * i) for i in range(10_000))
    clock = {"now": at(9, 20)}

    def now_fn():
        clock["now"] = next(ticks)
        return clock["now"]

    feed = LiveCandleFeed(FakeSource(breakout_day(DAY, "up")), "NSE:NIFTY50-INDEX")
    trader = PaperTrader(NIFTY, feed, quote_fn=QuoteBook(), now_fn=now_fn,
                         trade_logger=TradeLogger(tmp_path / "t.csv"), sleep_fn=lambda s: None)
    summary = trader.run(max_steps=200)
    assert "metrics" in summary


def test_market_clock():
    clock = MarketClock(NIFTY, holidays=[date(2026, 6, 2)])
    assert clock.is_open(at(9, 15)) and not clock.is_open(at(9, 14, 59))
    assert not clock.is_open(at(15, 30))
    assert not clock.is_trading_day(date(2026, 6, 2)) and not clock.is_trading_day(date(2026, 6, 6))
    assert clock.last_boundary(at(9, 29, 59)) == at(9, 25)
