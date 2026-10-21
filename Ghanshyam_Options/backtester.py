"""
Backtesting Engine for Historical Multi-Day Strategy Simulation
"""

import logging
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from typing import Callable, Dict, List, Optional

from broker import PaperTradingBroker, FyersClientAdapter
from config import IndexConfig, MANDATORY_SQUARE_OFF_TIME
from models import Candle, Direction, LDCHLLevels, Position, TradeSignal
from risk import RiskEngine
from agent import IndexTradingAgent

logger = logging.getLogger("GhanshyamTradingSystem")


@dataclass
class DailyBacktestRecord:
    """Stores performance metrics for a single trading session date."""
    trading_date: date
    trades_count: int
    wins_count: int
    losses_count: int
    gross_pnl: float
    brokerage_charges: float
    net_pnl: float
    ending_capital: float
    peak_daily_profit: float
    is_locked_out: bool
    lockout_reason: str


@dataclass
class BacktestResult:
    """Summary metrics of a completed historical backtest run."""
    initial_capital: float
    ending_capital: float
    total_net_pnl: float
    total_trades: int
    total_wins: int
    total_losses: int
    win_rate_pct: float
    profit_factor: float
    max_drawdown_pct: float
    daily_records: List[DailyBacktestRecord]
    broker: PaperTradingBroker

    def export_trades_to_csv(self, filepath: str = "trades_export.csv") -> str:
        """Exports all executed backtest trades to CSV."""
        return self.broker.export_trades_to_csv(filepath=filepath)

    def print_daily_pnl_table(self):
        """Prints formatted Date-by-Date Daily PnL summary table."""
        print("\n" + "=" * 105)
        print(f"{'DATE':<12} | {'TRADES':<6} | {'WINS':<4} | {'LOSS':<4} | {'NET PNL (₹)':<14} | {'BALANCE (₹)':<14} | {'PEAK PROFIT':<12} | {'STATUS':<20}")
        print("=" * 105)
        for rec in self.daily_records:
            status_str = "LOCKED OUT" if rec.is_locked_out else "OK"
            pnl_str = f"₹{rec.net_pnl:,.2f}"
            bal_str = f"₹{rec.ending_capital:,.2f}"
            peak_str = f"₹{rec.peak_daily_profit:,.2f}"
            print(f"{rec.trading_date.strftime('%Y-%m-%d'):<12} | {rec.trades_count:<6} | {rec.wins_count:<4} | {rec.losses_count:<4} | {pnl_str:<14} | {bal_str:<14} | {peak_str:<12} | {status_str:<20}")
        print("=" * 105)
        print(f"OVERALL BACKTEST SUMMARY:")
        print(f" Initial Equity Capital : ₹{self.initial_capital:,.2f}")
        print(f" Ending Balance Capital : ₹{self.ending_capital:,.2f}")
        print(f" Net Realized Return    : ₹{self.total_net_pnl:,.2f} ({((self.ending_capital - self.initial_capital) / self.initial_capital) * 100:.2f}%)")
        print(f" Total Trades Executed  : {self.total_trades} (Wins: {self.total_wins}, Losses: {self.total_losses})")
        print(f" Strategy Win Rate      : {self.win_rate_pct:.1f}%")
        print(f" Profit Factor          : {self.profit_factor:.2f}")
        print(f" Max Account Drawdown   : {self.max_drawdown_pct:.2f}%")
        print("=" * 105 + "\n")


class BacktestEngine:
    """
    Backtesting Engine replaying historical 5-minute candles day-by-day:
    - Resets session risk state at 09:15 IST each morning.
    - Updates Previous Day Close, High, Low (LDCHL) reference benchmarks dynamically.
    - Simulates order execution, option delta pricing, SL/Target exits, and 15:15 IST auto-square-off.
    - Generates a Date-by-Date Daily PnL ledger.
    """

    def __init__(
        self,
        initial_capital: float = 500000.0,
        banknifty_expiry: str = "26OCT",
        nifty_expiry: str = "26OCT",
        banknifty_lot_size: int = 15,
        nifty_lot_size: int = 25
    ):
        self.initial_capital = initial_capital
        self.risk_engine = RiskEngine(total_capital=initial_capital)
        self.paper_broker = PaperTradingBroker(initial_capital=initial_capital)
        self.broker_adapter = FyersClientAdapter(is_paper=True, paper_broker=self.paper_broker)

        self.banknifty_config = IndexConfig.bank_nifty_default(expiry=banknifty_expiry, lot_size=banknifty_lot_size)
        self.nifty_config = IndexConfig.nifty_50_default(expiry=nifty_expiry, lot_size=nifty_lot_size)

        self.banknifty_agent = IndexTradingAgent(
            config=self.banknifty_config,
            risk_engine=self.risk_engine,
            broker_adapter=self.broker_adapter
        )
        self.nifty_agent = IndexTradingAgent(
            config=self.nifty_config,
            risk_engine=self.risk_engine,
            broker_adapter=self.broker_adapter
        )

    def run_backtest(
        self,
        historical_candles_by_date: Dict[date, Dict[str, List[Candle]]],
        daily_ldc_by_date: Optional[Dict[date, Dict[str, LDCHLLevels]]] = None
    ) -> BacktestResult:
        """
        Executes multi-day historical backtest replay.

        historical_candles_by_date: Dict[date, Dict['BANKNIFTY'|'NIFTY', List[Candle]]]
        daily_ldc_by_date: Dict[date, Dict['BANKNIFTY'|'NIFTY', LDCHLLevels]]
        """
        daily_records: List[DailyBacktestRecord] = []
        equity_curve: List[float] = [self.initial_capital]

        sorted_dates = sorted(historical_candles_by_date.keys())

        for trading_date in sorted_dates:
            day_data = historical_candles_by_date[trading_date]

            # 1. Reset daily session risk and strategy states
            self.risk_engine.reset_daily_state()
            self.banknifty_agent.strategy_registry.reset_daily_state()
            self.nifty_agent.strategy_registry.reset_daily_state()
            self.banknifty_agent.candle_history.clear()
            self.nifty_agent.candle_history.clear()

            # 2. Update dynamic LDCHL levels if supplied
            if daily_ldc_by_date and trading_date in daily_ldc_by_date:
                if "BANKNIFTY" in daily_ldc_by_date[trading_date]:
                    self.banknifty_agent.set_ldc_levels(daily_ldc_by_date[trading_date]["BANKNIFTY"])
                if "NIFTY" in daily_ldc_by_date[trading_date]:
                    self.nifty_agent.set_ldc_levels(daily_ldc_by_date[trading_date]["NIFTY"])

            day_start_closed_count = len(self.paper_broker.closed_positions)

            # 3. Time-sync replay of 5-min candles across indices
            bn_candles = day_data.get("BANKNIFTY", [])
            nifty_candles = day_data.get("NIFTY", [])

            max_len = max(len(bn_candles), len(nifty_candles))

            for i in range(max_len):
                if i < len(bn_candles):
                    c_bn = bn_candles[i]
                    self.banknifty_agent.on_candle_closed(c_bn, current_spot=c_bn.close)

                if i < len(nifty_candles):
                    c_nf = nifty_candles[i]
                    self.nifty_agent.on_candle_closed(c_nf, current_spot=c_nf.close)

            # 4. Force EOD square-off if any open positions remain
            eod_ts = datetime.combine(trading_date, time(15, 15))
            self.banknifty_agent.square_off_positions(eod_ts, reason="EOD Backtest Closeout")
            self.nifty_agent.square_off_positions(eod_ts, reason="EOD Backtest Closeout")

            # 5. Extract daily performance metrics
            day_closed_positions = self.paper_broker.closed_positions[day_start_closed_count:]

            net_pnl = sum(p.pnl for p in day_closed_positions)
            gross_pnl = sum((p.exit_price - p.entry_price) * p.quantity for p in day_closed_positions if p.exit_price)
            charges = sum(40.0 for _ in day_closed_positions)
            wins = sum(1 for p in day_closed_positions if p.pnl > 0)
            losses = sum(1 for p in day_closed_positions if p.pnl <= 0)

            rec = DailyBacktestRecord(
                trading_date=trading_date,
                trades_count=len(day_closed_positions),
                wins_count=wins,
                losses_count=losses,
                gross_pnl=gross_pnl,
                brokerage_charges=charges,
                net_pnl=net_pnl,
                ending_capital=self.paper_broker.capital,
                peak_daily_profit=self.risk_engine.peak_daily_profit,
                is_locked_out=self.risk_engine.is_locked_out,
                lockout_reason=self.risk_engine.lockout_reason
            )
            daily_records.append(rec)
            equity_curve.append(self.paper_broker.capital)

        all_closed = self.paper_broker.closed_positions
        total_wins = sum(1 for p in all_closed if p.pnl > 0)
        total_losses = sum(1 for p in all_closed if p.pnl <= 0)
        total_pnl = self.paper_broker.capital - self.initial_capital
        win_rate = (total_wins / len(all_closed) * 100.0) if all_closed else 0.0

        gross_gains = sum(p.pnl for p in all_closed if p.pnl > 0)
        gross_losses = abs(sum(p.pnl for p in all_closed if p.pnl < 0))
        profit_factor = (gross_gains / gross_losses) if gross_losses > 0 else (99.0 if gross_gains > 0 else 0.0)

        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

        return BacktestResult(
            initial_capital=self.initial_capital,
            ending_capital=self.paper_broker.capital,
            total_net_pnl=total_pnl,
            total_trades=len(all_closed),
            total_wins=total_wins,
            total_losses=total_losses,
            win_rate_pct=win_rate,
            profit_factor=profit_factor,
            max_drawdown_pct=max_dd,
            daily_records=daily_records,
            broker=self.paper_broker
        )
