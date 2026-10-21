"""End-to-end: engine, backtester on synthetic data, paper runner replay, CLI."""

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import pytest

from ghanshyam_options.backtest.backtester import Backtester
from ghanshyam_options.backtest.metrics import compute_metrics
from ghanshyam_options.cli import main as cli_main
from ghanshyam_options.data.csv_feed import CsvFeed
from ghanshyam_options.engine import TradingEngine
from ghanshyam_options.execution.paper_broker import PaperBroker
from ghanshyam_options.instruments import get_instrument
from ghanshyam_options.journal.trade_log import TRADE_COLUMNS, TradeLog
from ghanshyam_options.models import Candle, ExitReason
from ghanshyam_options.paper.runner import PaperRunner
from ghanshyam_options.reports import metrics_markdown, write_report
from ghanshyam_options.risk import RiskEngine
from ghanshyam_options.sample_data.generator import generate
from ghanshyam_options.signals import SignalEngine
from ghanshyam_options.strategies.base import build_strategies
from .conftest import NIFTY_DAY, candle, flat_day, frame, levels


def _trend_day(day: date, start: float = 25200.0) -> list[Candle]:
    """Bullish day: opening range, then a clean inside-bar continuation that runs to target."""
    t = datetime.combine(day, time(9, 15))
    cs = [candle(t, start, start + 30, start - 10, start + 20)]                           # 09:15 opening bar 40 pts
    px = start + 20
    for i in range(1, 75):
        ts = t + timedelta(minutes=5 * i)
        if i == 10:   # mother: wide bar
            cs.append(candle(ts, px, px + 40, px - 5, px + 30)); px += 30
        elif i == 11:  # inside: fully contained in the mother (high px+5 < mother high px+10, low px-20 > mother low)
            cs.append(candle(ts, px, px + 5, px - 20, px - 10)); px -= 10
        elif 12 <= i <= 16:  # breakout and run to target (+2x inside range = 50)
            cs.append(candle(ts, px, px + 18, px - 2, px + 15)); px += 15
        else:
            # drift bars: each one strictly exceeds the previous bar's high -> never an inside bar
            prev = cs[-1]
            hi = prev.high + 2
            lo = max(prev.low - 1, hi - 12)
            close = px + 1
            cs.append(candle(ts, px, max(hi, px, close), min(lo, px, close), close)); px = close
    return cs


def test_engine_full_lifecycle_inside_candle_to_target(settings):
    spec = get_instrument("NIFTY50")
    jl = TradeLog(settings.path("log_dir"), mode="backtest")
    eng = TradingEngine(settings, spec, SignalEngine(settings, build_strategies(settings, ["inside_candle"])),
                        RiskEngine(settings), PaperBroker.from_settings(settings), jl, mode="backtest")
    lv = levels(NIFTY_DAY, pdh=25400, pdl=25000, pdc=25150)
    eng.start_day(NIFTY_DAY, lv)
    bars = _trend_day(NIFTY_DAY)
    for i, b in enumerate(bars):
        eng.on_bar(b, next_bar_open=bars[i + 1].open if i + 1 < len(bars) else None)
    summary = eng.end_day(bars[-1])
    assert summary.trades >= 1 and eng.trades
    t = eng.trades[0]
    assert t.strategy == "inside_candle" and t.direction == "CE" and t.exit_reason in ("TARGET", "TRAILING_STOP")
    assert t.net_pnl > 0 and t.premium_model == "synthetic" and t.option_symbol.startswith("NSE:NIFTY")
    assert t.charges > 0 and t.gross_pnl - t.charges == pytest.approx(t.net_pnl, abs=0.01)
    assert t.entry_ts > t.signal_ts  # stop entry fills on a later bar, never on the signal bar
    assert not eng.open_positions()
    assert (settings.path("log_dir") / f"trades_{NIFTY_DAY:%Y%m%d}.csv").exists()


def test_engine_squares_off_open_position_at_session_end(settings):
    spec = get_instrument("NIFTY50")
    settings.set("risk.trail_to_breakeven_after_r", 0)   # keep the trade open
    eng = TradingEngine(settings, spec, SignalEngine(settings, build_strategies(settings, ["inside_candle"])),
                        RiskEngine(settings), PaperBroker.from_settings(settings),
                        TradeLog(settings.path("log_dir")), mode="backtest")
    lv = levels(NIFTY_DAY, 25400, 25000, 25150)
    eng.start_day(NIFTY_DAY, lv)
    t0 = datetime.combine(NIFTY_DAY, time(9, 15))
    bars = [candle(t0, 25200, 25215, 25190, 25205)]
    px = 25205.0
    for i in range(1, 9):  # rising staircase, never an inside bar
        prev = bars[-1]
        close = px + 1
        bars.append(candle(t0 + timedelta(minutes=5 * i), px, max(prev.high + 2, close), min(prev.low - 1, px), close))
        px = close
    mother = candle(t0 + timedelta(minutes=45), px, px + 40, px - 5, px + 30); px += 30
    inside = candle(t0 + timedelta(minutes=50), px, px + 5, px - 20, px - 10); px -= 10
    bars += [mother, inside]
    bars.append(candle(t0 + timedelta(minutes=55), px, inside.high + 6, px - 2, inside.high + 4)); px = inside.high + 4  # fill at inside.high
    # drift sideways between SL (inside.low) and target (inside.high + 50) until the close
    i = 12
    while t0 + timedelta(minutes=5 * i) < datetime.combine(NIFTY_DAY, time(15, 30)):
        d = 1 if i % 2 else -1
        bars.append(candle(t0 + timedelta(minutes=5 * i), px, px + 2, px - 2, px + d))
        px += d
        i += 1
    settings.set("strategies.inside_candle.max_hold_minutes", 1000)
    for b in bars:
        eng.on_bar(b)
    eng.end_day(bars[-1])
    assert len(eng.trades) == 1 and eng.trades[-1].exit_reason == ExitReason.SQUARE_OFF.value
    assert eng.trades[-1].exit_ts.time() <= time(15, 20) and eng.trades[-1].entry_price > 0


def _sample_feed(tmp_path: Path, instrument: str, start: date, end: date, seed: int = 7) -> CsvFeed:
    spec = get_instrument(instrument)
    df = generate(instrument, start, end, seed=seed)
    feed = CsvFeed(tmp_path)
    feed.add_frame(spec.underlying_symbol, df, 5)
    return feed


def test_backtester_end_to_end_nifty_and_crude(settings, tmp_path):
    for inst in ("NIFTY50", "CRUDEOIL"):
        feed = _sample_feed(tmp_path, inst, date(2026, 9, 1), date(2026, 9, 18))
        jl = TradeLog(tmp_path / f"logs_{inst}", mode="backtest")
        res = Backtester(settings, feed, jl).run(inst, "2026-09-02", "2026-09-18", 5)
        assert res.sessions_run >= 10 and res.daily and res.metrics.trading_days == len({d.trade_date for d in res.daily})
        assert res.trades, f"expected trades for {inst}"
        assert all(t.premium_model == "synthetic" for t in res.trades)
        assert res.premium_models == {"synthetic": len(res.trades)}
        assert all(t.exit_ts.time() <= settings.session(get_instrument(inst).exchange.value).square_off or
                   t.exit_reason != "SQUARE_OFF" for t in res.trades)
        # one position per instrument at a time
        for a, b in zip(res.trades, res.trades[1:]):
            assert b.entry_ts >= a.exit_ts
        # daily caps: never more than max_trades_per_day
        for d in res.daily:
            assert d.trades <= settings.get("risk.max_trades_per_day")
        df = pd.read_csv(res.trade_files[0])
        assert list(df.columns) == TRADE_COLUMNS
        assert Path(res.daily_file).exists() and len(pd.read_csv(res.daily_file)) == len(res.daily)
        assert res.metrics.trades == len(res.trades) and abs(res.metrics.net_pnl - sum(t.net_pnl for t in res.trades)) < 0.01
        assert res.equity.iloc[-1]["equity"] == pytest.approx(settings.total_capital + res.metrics.net_pnl)


def test_backtester_metrics_and_report(settings, tmp_path):
    feed = _sample_feed(tmp_path, "NIFTY50", date(2026, 9, 1), date(2026, 9, 12), seed=3)
    res = Backtester(settings, feed, TradeLog(tmp_path / "l", mode="backtest")).run("NIFTY50", "2026-09-02", "2026-09-12")
    m = compute_metrics(res.trades, res.daily, settings.total_capital)
    assert m.trades == len(res.trades) and 0 <= m.win_rate <= 1 and m.max_drawdown >= 0
    assert m.best_day >= m.worst_day and m.by_strategy and sum(m.by_exit_reason.values()) == m.trades
    md = metrics_markdown(m, "t")
    assert "Profit factor" in md and "Synthetic-premium trades" in md
    paths = write_report(res.trades, res.daily, settings.total_capital, tmp_path / "rep", name="r", png=True)
    assert Path(paths["markdown"]).exists()
    if "png" in paths:
        assert Path(paths["png"]).stat().st_size > 0


def test_backtester_requires_previous_session_and_date_order(settings, tmp_path):
    feed = _sample_feed(tmp_path, "NIFTY50", date(2026, 9, 1), date(2026, 9, 3))
    bt = Backtester(settings, feed, TradeLog(tmp_path / "l"))
    with pytest.raises(ValueError):
        bt.run("NIFTY50", "2026-09-03", "2026-09-02")
    res = bt.run("NIFTY50", "2026-09-01", "2026-09-03")
    assert any("no previous session" in s for s in res.sessions_skipped)
    with pytest.raises(ValueError):
        bt.run("BANKNIFTY", "2026-09-01", "2026-09-03")  # not in the feed


def test_backtester_honours_strategy_subset_and_live_never_touched(settings, tmp_path):
    feed = _sample_feed(tmp_path, "NIFTY50", date(2026, 9, 1), date(2026, 9, 12))
    res = Backtester(settings, feed, TradeLog(tmp_path / "l"), strategies=["morning_0920"]).run("NIFTY50", "2026-09-02", "2026-09-12")
    assert all(t.strategy == "morning_0920" for t in res.trades)
    assert all(t.mode == "backtest" for t in res.trades)


def test_paper_runner_replays_a_day_without_orders(settings, tmp_path):
    spec = get_instrument("NIFTY50")
    prev = flat_day(date(2026, 10, 14), 25150.0, wiggle=20)
    today = _trend_day(NIFTY_DAY)
    feed = CsvFeed(tmp_path)
    feed.add_frame(spec.underlying_symbol, frame(prev + today), 5)
    clock = {"now": datetime.combine(NIFTY_DAY, time(9, 14))}

    def tick() -> datetime:
        clock["now"] += timedelta(minutes=5)
        return clock["now"]

    runner = PaperRunner(settings, feed, "NIFTY50", 5, journal=TradeLog(tmp_path / "paper", mode="paper"),
                         clock=tick, strategies=["inside_candle"])
    summary = runner.run_day(NIFTY_DAY)
    assert summary.mode == "paper" and summary.instrument == "NIFTY50"
    assert summary.trades >= 1 and runner.engine.trades[0].mode == "paper"
    assert runner.engine.trades[0].premium_model == "synthetic"  # CsvFeed has no option quotes -> synthetic
    assert not runner.engine.open_positions()
    assert (tmp_path / "paper" / "daily_summary.csv").exists()


def test_cli_backtest_signals_report_instruments(settings, tmp_path, capsys):
    spec = get_instrument("NIFTY50")
    df = generate("NIFTY50", date(2026, 9, 1), date(2026, 9, 10), seed=7)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    df.to_csv(data_dir / "NSE_NIFTY50-INDEX_5.csv", index=False)
    log_dir = tmp_path / "logs"
    rc = cli_main(["backtest", "--instrument", "NIFTY50", "--from", "2026-09-02", "--to", "2026-09-10",
                   "--data-dir", str(data_dir), "--log-dir", str(log_dir), "--report-dir", str(tmp_path / "rep"),
                   "--no-png", "--set", "risk.min_confidence=0.5"])
    out = capsys.readouterr().out
    assert rc == 0 and "backtest" in out and "Trade logs" in out
    assert (log_dir / "daily_summary.csv").exists()
    rc = cli_main(["signals", "--instrument", "NIFTY50", "--date", "2026-09-03", "--data-dir", str(data_dir),
                   "--log-dir", str(log_dir), "--show-rejected"])
    out = capsys.readouterr().out
    assert rc == 0 and "PDH=" in out and "accepted" in out
    rc = cli_main(["report", "--log-dir", str(log_dir)])
    out = capsys.readouterr().out
    assert rc == 0 and ("Journal summary" in out or "No trades" in out)
    rc = cli_main(["instruments", "--log-dir", str(log_dir)])
    out = capsys.readouterr().out
    assert rc == 0 and spec.underlying_symbol in out and "MCX:CRUDEOIL" in out


def test_paper_runner_uses_market_option_quotes_when_feed_has_them(settings, tmp_path):
    """With a feed that serves option quotes, trades are tagged premium_model=market and no order API is touched."""
    from ghanshyam_options.models import Quote

    spec = get_instrument("NIFTY50")
    prev = flat_day(date(2026, 10, 14), 25150.0, wiggle=20)
    today = _trend_day(NIFTY_DAY)

    class QuoteFeed(CsvFeed):
        quote_calls: list = []

        def quote(self, symbol):
            if symbol.endswith("CE") or symbol.endswith("PE"):
                self.quote_calls.append(symbol)
                return Quote(symbol=symbol, ltp=150.0 + len(self.quote_calls) * 0.5)
            return super().quote(symbol)

    feed = QuoteFeed(tmp_path)
    feed.add_frame(spec.underlying_symbol, frame(prev + today), 5)
    clock = {"now": datetime.combine(NIFTY_DAY, time(9, 14))}

    def tick() -> datetime:
        clock["now"] += timedelta(minutes=5)
        return clock["now"]

    runner = PaperRunner(settings, feed, "NIFTY50", 5, journal=TradeLog(tmp_path / "paper", mode="paper"),
                         clock=tick, strategies=["inside_candle"])
    runner.run_day(NIFTY_DAY)
    assert runner.engine.trades and runner.engine.trades[0].premium_model == "market"
    assert feed.quote_calls and all(s.startswith("NSE:NIFTY") for s in feed.quote_calls)
    assert not runner.broker.is_live
