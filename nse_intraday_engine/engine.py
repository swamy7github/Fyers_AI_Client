"""
StrategyEngine
==============
Top-level orchestrator that wires NiftyScreener -> SectorDataFetcher ->
SignalEngine -> OrderManager together and dispatches to either the
historical backtest loop or the live-polling paper-trading loop based on
`config.MODE`.
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from database_manager import DatabaseManager
from fyers_client import FyersClient
from nifty_screener import NiftyScreener
from order_manager import BacktestTrader, PaperTrader
from performance_analytics import PerformanceAnalyzer
from sector_data_fetcher import SectorDataFetcher
from signal_engine import SignalEngine
from utils import is_between, now_ist, setup_logger


class StrategyEngine:
    def __init__(self, config):
        self.cfg = config
        self.log = setup_logger("engine", config.LOG_DIR, config.LOG_LEVEL)
        self.db = DatabaseManager(config.DB_PATH)
        self.fy = FyersClient(config)
        self.screener = NiftyScreener(self.fy, config)
        self.sectors = SectorDataFetcher(self.fy, config)
        self.signals = SignalEngine(config)
        self.analyzer = PerformanceAnalyzer(self.db)

        if config.MODE == "PAPER_TRADING":
            self.trader = PaperTrader(self.db, config, self.fy)
        elif config.MODE == "BACKTEST":
            self.trader = BacktestTrader(self.db, config)
        else:
            raise ValueError(f"Unknown MODE: {config.MODE!r} (expected BACKTEST or PAPER_TRADING)")

    # ---------------------------------------------------------------- shared
    def _build_universe(self, as_of: datetime) -> list[dict]:
        universe = self.screener.fetch_nifty500()
        self.screener.build_sector_map(list(universe["Symbol"]))
        candidates = self.screener.compute_liquidity_universe(universe, as_of)
        self.db.insert_universe_snapshot(as_of.strftime("%Y-%m-%d"), candidates)
        self.log.info("Universe built: %d symbols", len(candidates))
        return candidates

    def _finalize_day(self, trade_date: str) -> None:
        trades_today = [t for t in self.db.get_trades(mode=self.trader.mode)
                         if t["exit_time"].startswith(trade_date)]
        gross = sum(t["gross_pnl"] for t in trades_today)
        charges = sum(t["charges"] for t in trades_today)
        net = sum(t["net_pnl"] for t in trades_today)
        wins = sum(1 for t in trades_today if t["net_pnl"] > 0)
        losses = len(trades_today) - wins
        self.db.upsert_daily_metric({
            "trade_date": trade_date, "mode": self.trader.mode, "gross_pnl": gross,
            "total_charges": charges, "net_pnl": net, "num_trades": len(trades_today),
            "wins": wins, "losses": losses, "equity_close": self.trader.equity,
        })

    # -------------------------------------------------------------- backtest
    def run_backtest(self, date_from: str, date_to: str) -> None:
        for trade_date in pd.bdate_range(date_from, date_to).strftime("%Y-%m-%d"):
            as_of = datetime.strptime(trade_date, "%Y-%m-%d")
            candidates = self._build_universe(as_of)

            min_bars = max(self.cfg.ATR_PERIOD, self.cfg.VOLUME_SMA_PERIOD) + 1
            day_bars: dict[str, pd.DataFrame] = {}
            sector_by_symbol: dict[str, str] = {}
            for c in candidates:
                fy_symbol = f"NSE:{c['symbol']}-EQ"
                df = self.fy.get_history(fy_symbol, resolution="5", date_from=trade_date, date_to=trade_date)
                if len(df) < min_bars:
                    continue
                day_bars[c["symbol"]] = df
                sector_by_symbol[c["symbol"]] = c["sector_index"]

            eligibility: dict[str, dict] = {}
            for symbol, df in day_bars.items():
                first = df[df["timestamp"].dt.strftime("%H:%M") <= self.cfg.FIRST_CANDLE_END].iloc[:1]
                if not first.empty:
                    eligibility[symbol] = self.screener.passes_first_candle_filter(first.iloc[0])

            enriched_cache: dict[str, pd.DataFrame] = {}
            signal_by_time: dict = {}
            for symbol, df in day_bars.items():
                enriched = self.signals.add_indicators(df)
                enriched.attrs["symbol"] = symbol
                enriched_cache[symbol] = enriched
                if symbol not in eligibility:
                    continue
                sector_index = sector_by_symbol[symbol]
                rs_lookup = lambda ts, si=sector_index: self.sectors.rs_at(si, trade_date, ts)
                for sig in self.signals.generate_signals(enriched, symbol, eligibility[symbol], rs_lookup):
                    signal_by_time.setdefault(sig.timestamp, []).append(sig)

            timeline = sorted({ts for df in enriched_cache.values() for ts in df["timestamp"]})

            for ts in timeline:
                if ts.strftime("%H:%M") >= self.cfg.SQUARE_OFF_TIME:
                    def price_lookup(sym, _ts=ts):
                        df = enriched_cache.get(sym)
                        row = df[df["timestamp"] == _ts] if df is not None else None
                        return float(row.iloc[0]["close"]) if row is not None and not row.empty else None
                    self.trader.square_off_all(ts, price_lookup)
                    break

                for sig in signal_by_time.get(ts, []):
                    if sig.symbol not in self.trader.open_positions:
                        bar_rows = enriched_cache[sig.symbol]
                        bar_row = bar_rows[bar_rows["timestamp"] == ts].iloc[0]
                        self.trader.open_position(sig, bar_row)

                for symbol in list(self.trader.open_positions.keys()):
                    df = enriched_cache.get(symbol)
                    if df is None:
                        continue
                    bar_rows = df[df["timestamp"] == ts]
                    if not bar_rows.empty:
                        self.trader.manage_bar(symbol, bar_rows.iloc[0])

            self._finalize_day(trade_date)

        self.analyzer.print_report("BACKTEST")

    # ---------------------------------------------------------- paper trading
    def run_paper_trading(self) -> None:
        trade_date = now_ist().strftime("%Y-%m-%d")
        candidates = self._build_universe(now_ist())
        sector_by_symbol = {c["symbol"]: c["sector_index"] for c in candidates}
        symbols = list(sector_by_symbol.keys())
        eligibility: dict[str, dict] = {}

        self.log.info("Paper trading started for %s on %d symbols", trade_date, len(symbols))
        while True:
            now = now_ist()
            if now.strftime("%H:%M") > self.cfg.MARKET_CLOSE:
                break
            if not is_between(now, "09:15", self.cfg.MARKET_CLOSE):
                time.sleep(30)
                continue

            for symbol in symbols:
                fy_symbol = f"NSE:{symbol}-EQ"
                df = self.fy.get_history(fy_symbol, resolution="5", date_from=trade_date, date_to=trade_date)
                if df.empty:
                    continue
                if symbol not in eligibility and now.strftime("%H:%M") > self.cfg.FIRST_CANDLE_END:
                    first = df[df["timestamp"].dt.strftime("%H:%M") <= self.cfg.FIRST_CANDLE_END].iloc[:1]
                    if not first.empty:
                        eligibility[symbol] = self.screener.passes_first_candle_filter(first.iloc[0])

                enriched = self.signals.add_indicators(df)
                enriched.attrs["symbol"] = symbol

                if symbol in self.trader.open_positions:
                    self.trader.manage_bar(symbol, enriched.iloc[-1])
                elif symbol in eligibility:
                    sector_index = sector_by_symbol[symbol]
                    rs_lookup = lambda ts, si=sector_index: self.sectors.rs_at(si, trade_date, ts)
                    new_signals = self.signals.generate_signals(enriched, symbol, eligibility[symbol], rs_lookup)
                    if new_signals:
                        self.trader.open_position(new_signals[-1], enriched.iloc[-1])

            if now.strftime("%H:%M") >= self.cfg.SQUARE_OFF_TIME:
                def price_lookup(sym):
                    fy_symbol = f"NSE:{sym}-EQ"
                    q = self.fy.get_quotes([fy_symbol]).get(fy_symbol, {})
                    lp = q.get("lp")
                    return float(lp) if lp else None
                self.trader.square_off_all(now, price_lookup)

            self._finalize_day(trade_date)
            time.sleep(300)  # poll every 5 minutes, aligned to the candle size

        self.analyzer.print_report("PAPER_TRADING")

    def run(self) -> None:
        if self.cfg.MODE == "BACKTEST":
            self.run_backtest(self.cfg.BACKTEST_FROM, self.cfg.BACKTEST_TO)
        else:
            self.run_paper_trading()
