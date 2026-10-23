"""Command-line interface.

    python -m ghanshyam_options backtest --instrument NIFTY50 --from 2026-09-01 --to 2026-10-17 --resolution 5
    python -m ghanshyam_options paper    --instrument BANKNIFTY
    python -m ghanshyam_options signals  --instrument CRUDEOIL --date 2026-10-17
    python -m ghanshyam_options report
    python -m ghanshyam_options instruments

``--feed csv`` (default) reads ``paths.sample_data_dir`` (or ``--data-dir``);
``--feed fyers`` uses the existing ``fyersclient`` through ``FyersFeed`` and
the on-disk cache. Live order routing requires ``--mode live``,
``live.enabled: true`` in YAML, ``--i-understand-live-risk`` **and**
``GHANSHYAM_LIVE=1`` in the environment; otherwise everything is paper.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from .config.config import Settings, load_settings
from .instruments import all_instruments, apply_yaml_instrument_overrides, describe, get_instrument
from .logging_setup import setup_logging

logger = logging.getLogger("ghanshyam.cli")


def _settings(args: argparse.Namespace) -> Settings:
    overrides = {}
    for item in args.set or []:
        if "=" not in item:
            raise SystemExit(f"--set expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        try:
            v_parsed = json.loads(v)
        except json.JSONDecodeError:
            v_parsed = v
        overrides[k] = v_parsed
    if getattr(args, "capital", None):
        overrides["capital.total"] = float(args.capital)
    s = load_settings(args.config, overrides)
    apply_yaml_instrument_overrides(s.get("instruments"))
    setup_logging(s.path("log_dir"), s.get("logging.level", "INFO"), s.get("logging.file", "app.log"),
                  bool(s.get("logging.json", False)))
    return s


def _feed(args: argparse.Namespace, s: Settings):
    if args.feed == "fyers":
        from .data.cache import CandleCache
        from .data.fyers_feed import FyersFeed

        return FyersFeed(cache=CandleCache(s.path("cache_dir"), str(s.get("data.cache_format", "csv"))))
    from .data.csv_feed import CsvFeed

    return CsvFeed(Path(args.data_dir) if args.data_dir else s.path("sample_data_dir"))


def cmd_backtest(args: argparse.Namespace) -> int:
    s = _settings(args)
    feed = _feed(args, s)
    from .backtest.backtester import Backtester, option_lookup_from_feed
    from .journal.trade_log import TradeLog
    from .reports import daily_table, metrics_markdown, write_report

    log_dir = Path(args.log_dir) if args.log_dir else s.path("log_dir")
    journal = TradeLog(log_dir, mode="backtest")
    lookup = option_lookup_from_feed(feed, int(args.resolution)) if args.feed == "fyers" and args.option_candles else None
    bt = Backtester(s, feed, journal, option_lookup=lookup, strategies=args.strategies)
    res = bt.run(args.instrument, args.start, args.end, int(args.resolution))
    extra = {"Premium models": ", ".join(f"{k}: {v}" for k, v in res.premium_models.items()) or "n/a",
             "Sessions run / skipped": f"{res.sessions_run} / {len(res.sessions_skipped)}"}
    print(metrics_markdown(res.metrics, f"{res.instrument} backtest {res.start} -> {res.end} ({res.resolution}m)", extra))
    print("\n" + daily_table(res.daily))
    paths = write_report(res.trades, res.daily, s.total_capital, Path(args.report_dir) if args.report_dir else s.path("report_dir"),
                         name=f"backtest_{res.instrument}_{res.start}_{res.end}",
                         title=f"{res.instrument} backtest {res.start} -> {res.end}", extra=extra, png=not args.no_png)
    print("\nTrade logs:", *res.trade_files, sep="\n  ")
    print("Daily summary:", res.daily_file)
    print("Report:", paths)
    if res.sessions_skipped:
        print("Skipped sessions:", *res.sessions_skipped, sep="\n  ")
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    s = _settings(args)
    feed = _feed(args, s)
    from .execution.fyers_broker import make_broker
    from .journal.trade_log import TradeLog
    from .paper.runner import PaperRunner

    broker = make_broker(s, args.mode, armed=bool(args.i_understand_live_risk))
    if broker.is_live:
        print("*** LIVE MODE ARMED: real orders will be sent to Fyers ***", file=sys.stderr)
    print(f"===========args.poll_seconds = {args.poll_seconds} ======")
    journal = TradeLog(Path(args.log_dir) if args.log_dir else s.path("log_dir"), mode=args.mode)
    runner = PaperRunner(s, feed, args.instrument, int(args.resolution), broker=broker, journal=journal,
                         poll_seconds=int(args.poll_seconds), strategies=args.strategies)
    today = date.fromisoformat(args.date) if args.date else None
    if args.feed == "csv" and today is not None:
        # replay mode: drive the runner from history with a synthetic clock
        from datetime import timedelta

        session = s.session(get_instrument(args.instrument).exchange.value)
        clock = {"now": datetime.combine(today, session.open)}

        def _clock() -> datetime:
            clock["now"] = clock["now"] + timedelta(minutes=int(args.resolution))
            return clock["now"]

        runner.clock = _clock
        if hasattr(feed, "set_clock"):
            pass  # CsvFeed.history is bounded by the date; closed-bar filtering is done by the runner
    summary = runner.run_day(today)
    print(json.dumps(summary.__dict__, default=str, indent=2))
    return 0


def cmd_signals(args: argparse.Namespace) -> int:
    s = _settings(args)
    feed = _feed(args, s)
    from .signals import SignalEngine, compute_levels, split_sessions
    from .strategies.base import build_strategies
    from datetime import timedelta

    spec = get_instrument(args.instrument)
    day = date.fromisoformat(args.date) if args.date else date.today()
    df = feed.history(spec.underlying_symbol, int(args.resolution), day - timedelta(days=12), day)
    sessions = split_sessions(df)
    days = sorted(sessions)
    if day not in sessions or days.index(day) == 0:
        print(f"no session data for {spec.name} on {day} (have {days[-3:] if days else 'nothing'})")
        return 1
    i = days.index(day)
    levels = compute_levels(sessions[days[i - 1]], day, spec, [sessions[d] for d in days[max(0, i - 4):i - 1]])
    engine = SignalEngine(s, build_strategies(s, args.strategies))
    engine.start_day(spec, day, levels)
    print(f"{spec.name} {day}  PDH={levels.pdh:.1f} PDL={levels.pdl:.1f} PDC={levels.pdc:.1f} expiry_day={levels.is_expiry_day}")
    print(f"{'bar':<6}{'bias':<9}{'strategy':<17}{'dir':<4}{'level':>10}{'sl':>10}{'target':>10}{'conf':>6}  reason")
    for bar in sessions[day]:
        for sig in engine.on_bar(spec, bar):
            tgt = f"{sig.target:.1f}" if sig.target is not None else f"+{sig.target_premium_pct:.0%}p"
            print(f"{bar.ts:%H:%M} {engine.state(spec).bias.value:<9}{sig.strategy:<17}{sig.option_type.value:<4}"
                  f"{sig.spot_level:>10.1f}{sig.stop_loss:>10.1f}{tgt:>10}{sig.confidence:>6.2f}  {sig.reason}")
    rej = engine.rejected_for_day(spec)
    if args.show_rejected and rej:
        print("\nrejected:")
        for r in rej:
            print(f"  {r.signal.ts:%H:%M} {r.signal.strategy:<17}{r.signal.option_type.value} conf={r.signal.confidence:.2f}: {r.reason}")
    print(f"\n{len(engine.signals_for_day(spec))} accepted, {len(rej)} rejected")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    s = _settings(args)
    from .reports import report_from_logs

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    print(report_from_logs(Path(args.log_dir) if args.log_dir else s.path("log_dir"), start, end, s.total_capital))
    return 0


def cmd_instruments(args: argparse.Namespace) -> int:
    _settings(args)
    for spec in all_instruments():
        print(json.dumps(describe(spec), default=str))
    return 0


def _common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--config", default=None, help="settings.yaml path (default: package settings)")
    sp.add_argument("--set", action="append", help="override a dotted key, e.g. --set risk.min_confidence=0.7")
    sp.add_argument("--feed", choices=["csv", "fyers"], default="csv")
    sp.add_argument("--data-dir", default=None, help="CSV directory for --feed csv")
    sp.add_argument("--log-dir", default=None)
    sp.add_argument("--capital", default=None, help="override capital.total")
    sp.add_argument("--strategies", nargs="*", default=None, help="subset of strategy names to enable")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ghanshyam_options", description="Ghanshyam Tech intraday option-buying framework")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest")
    _common(b)
    b.add_argument("--instrument", required=True)
    b.add_argument("--from", dest="start", required=True)
    b.add_argument("--to", dest="end", required=True)
    b.add_argument("--resolution", default=5)
    b.add_argument("--report-dir", default=None)
    b.add_argument("--no-png", action="store_true")
    b.add_argument("--option-candles", action="store_true", help="try real option candles from the feed before synthetic")
    b.set_defaults(func=cmd_backtest)

    pp = sub.add_parser("paper")
    _common(pp)
    pp.add_argument("--instrument", required=True)
    pp.add_argument("--resolution", default=5)
    pp.add_argument("--date", default=None, help="replay a past date from the CSV feed")
    pp.add_argument("--poll-seconds", default=15)
    pp.add_argument("--mode", choices=["paper", "live"], default="paper")
    pp.add_argument("--i-understand-live-risk", action="store_true")
    pp.set_defaults(func=cmd_paper)

    sg = sub.add_parser("signals")
    _common(sg)
    sg.add_argument("--instrument", required=True)
    sg.add_argument("--date", default=None)
    sg.add_argument("--resolution", default=5)
    sg.add_argument("--show-rejected", action="store_true")
    sg.set_defaults(func=cmd_signals)

    r = sub.add_parser("report")
    _common(r)
    r.add_argument("--from", dest="start", default=None)
    r.add_argument("--to", dest="end", default=None)
    r.set_defaults(func=cmd_report)

    ins = sub.add_parser("instruments")
    _common(ins)
    ins.set_defaults(func=cmd_instruments)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
