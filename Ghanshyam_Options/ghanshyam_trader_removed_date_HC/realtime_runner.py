"""
Real-time Intraday Market Session Runner (09:15 to 15:30 IST)
"""

import logging
import time as pytime
from datetime import datetime, date, time, timedelta
from typing import Callable, Dict, List, Optional

from broker import PaperTradingBroker, FyersClientAdapter
from config import IndexConfig, MARKET_OPEN_TIME, MARKET_CLOSE_TIME, MANDATORY_SQUARE_OFF_TIME, get_dynamic_default_expiry
from models import Candle, Direction, LDCHLLevels, TradeSignal
from risk import RiskEngine
from agent import IndexTradingAgent

logger = logging.getLogger("GhanshyamTradingSystem")


def fetch_fyers_ldchl_for_date(fyers_client, target_date: date, symbol: str) -> Optional[LDCHLLevels]:
    """
    Queries Fyers API (resolution='D') to fetch the last traded session OHLC
    strictly prior to target_date (e.g. for target 2026-10-12, returns 2026-10-09 values).
    """
    if not fyers_client or not hasattr(fyers_client, "history"):
        return None
    try:
        range_to = target_date.strftime("%Y-%m-%d")
        range_from = (target_date - timedelta(days=15)).strftime("%Y-%m-%d")
        req_data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
            "cont_flag": "1"
        }
        res = fyers_client.history(data=req_data)
        if res and res.get("s") == "ok":
            candles = res.get("candles", [])
            prior_candles = []
            for c in candles:
                c_dt = datetime.fromtimestamp(c[0]).date()
                if c_dt < target_date:
                    prior_candles.append((c_dt, c))

            if prior_candles:
                last_traded_date, last_bar = prior_candles[-1]
                pdo = float(last_bar[1])
                pdh = float(last_bar[2])
                pdl = float(last_bar[3])
                pdc = float(last_bar[4])
                trend = Direction.BULLISH if pdc >= pdo else Direction.BEARISH

                print(
                    f"[Fyers LDCHL API] Target Date: {target_date} | "
                    f"Last Traded Date: {last_traded_date} ({symbol}) | "
                    f"PDH={pdh:.1f}, PDL={pdl:.1f}, PDC={pdc:.1f}, Trend={trend.value}"
                )
                return LDCHLLevels(pdh=pdh, pdl=pdl, pdc=pdc, daily_trend=trend)
    except Exception as e:
        logger.error(f"Error fetching LDCHL from Fyers API: {e}")
    return None


class MultiAssetTradingManager:
    """
    Central Orchestrator managing BANK NIFTY and NIFTY 50 under a unified Risk Engine.
    """

    def __init__(
        self,
        total_capital: float = 500000.0,
        fyers_client=None,
        is_paper: bool = True,
        banknifty_expiry: Optional[str] = None,
        nifty_expiry: Optional[str] = None,
        banknifty_lot_size: int = 15,
        nifty_lot_size: int = 25,
    ):
        self.total_capital = total_capital
        self.is_paper = is_paper
        self.fyers_client = fyers_client

        bn_exp = banknifty_expiry or get_dynamic_default_expiry()
        nf_exp = nifty_expiry or get_dynamic_default_expiry()

        # Shared Global Risk Engine & Paper Broker
        self.risk_engine = RiskEngine(total_capital=total_capital)
        self.paper_broker = PaperTradingBroker(initial_capital=total_capital)
        self.broker_adapter = FyersClientAdapter(
            fyers_client=fyers_client,
            is_paper=is_paper,
            paper_broker=self.paper_broker
        )

        self.banknifty_config = IndexConfig.bank_nifty_default(expiry=bn_exp, lot_size=banknifty_lot_size)
        self.nifty_config = IndexConfig.nifty_50_default(expiry=nf_exp, lot_size=nifty_lot_size)

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

        self.agents: Dict[str, IndexTradingAgent] = {
            self.banknifty_config.code: self.banknifty_agent,
            self.nifty_config.code: self.nifty_agent,
        }

    def set_levels(self, underlying_code: str, pdh: float, pdl: float, pdc: float, trend: Direction = Direction.BULLISH):
        """Sets LDCHL reference benchmarks for a specific index."""
        code = underlying_code.upper()
        if code in self.agents:
            self.agents[code].set_ldc_levels(LDCHLLevels(pdh=pdh, pdl=pdl, pdc=pdc, daily_trend=trend))

    def auto_fetch_levels_from_fyers(self, target_date: Optional[date] = None, fyers_client=None):
        """
        Queries Fyers API to dynamically fetch Previous Day High (PDH), Low (PDL), Close (PDC),
        and Daily Trend for the last traded session prior to target_date.
        """
        client = fyers_client or self.fyers_client
        if not target_date:
            target_date = datetime.now().date()

        symbols = {
            "BANKNIFTY": self.banknifty_config.symbol,
            "NIFTY": self.nifty_config.symbol
        }

        for code, sym in symbols.items():
            ldc = fetch_fyers_ldchl_for_date(client, target_date=target_date, symbol=sym)
            if ldc:
                self.set_levels(code, pdh=ldc.pdh, pdl=ldc.pdl, pdc=ldc.pdc, trend=ldc.daily_trend)

    def on_candle_event(
        self,
        symbol: str,
        candle: Candle,
        spot_ltp: float,
        option_ltp_lookup_fn: Optional[Callable[[str], float]] = None
    ) -> Optional[TradeSignal]:
        """Ingests 5-minute candle and routes to target index agent."""
        sym_upper = symbol.upper()
        if "BANK" in sym_upper:
            agent = self.banknifty_agent
        elif "NIFTY" in sym_upper:
            agent = self.nifty_agent
        else:
            logger.warning(f"Unrecognized symbol '{symbol}' passed to MultiAssetTradingManager.")
            return None

        return agent.on_candle_closed(candle, current_spot=spot_ltp, option_ltp_lookup_fn=option_ltp_lookup_fn)

    def square_off_all(self, timestamp: datetime, reason: str = "Global Square-Off"):
        """Square off all active positions across all index agents."""
        for agent in self.agents.values():
            agent.square_off_positions(timestamp, reason=reason)

    def export_trades_to_csv(self, filepath: str = "trades_export.csv") -> str:
        """Exports all recorded trades to CSV."""
        return self.broker_adapter.export_trades_to_csv(filepath=filepath)

    def get_consolidated_report(self) -> Dict:
        """Returns consolidated session execution performance summary."""
        summary = self.broker_adapter.get_portfolio_summary()
        closed_trades = self.paper_broker.closed_positions

        bn_trades = [p for p in closed_trades if p.underlying_code == "BANKNIFTY"]
        nifty_trades = [p for p in closed_trades if p.underlying_code == "NIFTY"]

        bn_pnl = sum(p.pnl for p in bn_trades)
        nifty_pnl = sum(p.pnl for p in nifty_trades)

        summary.update({
            "banknifty_trades": len(bn_trades),
            "banknifty_pnl": bn_pnl,
            "nifty_trades": len(nifty_trades),
            "nifty_pnl": nifty_pnl,
            "daily_peak_profit": self.risk_engine.peak_daily_profit,
            "is_risk_locked": self.risk_engine.is_locked_out,
            "risk_lock_reason": self.risk_engine.lockout_reason
        })
        return summary


class RealtimeSessionRunner:
    """
    Executes an active market session (09:15 to 15:30 IST):
    - Ingests 5-minute candle feeds continuously.
    - Emits signals in real-time.
    - Enforces mandatory 15:15 IST auto-closeout.
    """

    def __init__(self, manager: MultiAssetTradingManager):
        self.manager = manager

    def run_simulated_session(
        self,
        banknifty_candles: List[Candle],
        nifty_candles: List[Candle],
        export_csv_path: Optional[str] = "trades_export.csv"
    ) -> Dict:
        """
        Replays a complete intraday session from 09:15 to 15:30 IST step-by-step.
        """
        print("\n" + "=" * 80)
        print("      GHANSHYAM TECH INTRADAY TRADING SESSION SIMULATION RUNNER")
        print("=" * 80 + "\n")

        max_len = max(len(banknifty_candles), len(nifty_candles))

        for i in range(max_len):
            if i < len(banknifty_candles):
                c_bn = banknifty_candles[i]
                self.manager.on_candle_event("NSE:NIFTYBANK-INDEX", c_bn, spot_ltp=c_bn.close)

            if i < len(nifty_candles):
                c_nf = nifty_candles[i]
                self.manager.on_candle_event("NSE:NIFTY50-INDEX", c_nf, spot_ltp=c_nf.close)

        # Force 15:15 IST EOD Square-Off
        last_ts = datetime.now()
        if banknifty_candles:
            last_ts = banknifty_candles[-1].timestamp
        elif nifty_candles:
            last_ts = nifty_candles[-1].timestamp

        eod_ts = datetime.combine(last_ts.date(), time(15, 15))
        self.manager.square_off_all(eod_ts, reason="EOD Market Close Square-Off")

        if export_csv_path:
            self.manager.export_trades_to_csv(filepath=export_csv_path)

        report = self.manager.get_consolidated_report()

        print("\n" + "=" * 80)
        print("                    CONSOLIDATED SESSION PERFORMANCE REPORT")
        print("=" * 80)
        print(f" Initial Equity Capital   : ₹{report['initial_capital']:,.2f}")
        print(f" Final Account Balance   : ₹{report['current_capital']:,.2f}")
        print(f" Net Realized PnL       : ₹{report['net_realized_pnl']:,.2f}")
        print(f" Peak Accrued Profit     : ₹{report['daily_peak_profit']:,.2f}")
        print(f" Win Rate                : {report['win_rate_pct']:.1f}% ({report['wins']} Wins / {report['losses']} Losses)")
        print(f" Total Trades Executed   : {report['total_trades']}")
        print(f"   • Bank Nifty Trades   : {report['banknifty_trades']} (PnL: ₹{report['banknifty_pnl']:,.2f})")
        print(f"   • Nifty 50 Trades     : {report['nifty_trades']} (PnL: ₹{report['nifty_pnl']:,.2f})")
        print(f" Risk Lock Status        : {'LOCKED OUT' if report['is_risk_locked'] else 'ACTIVE'}")
        if report['is_risk_locked']:
            print(f" Lockout Reason          : {report['risk_lock_reason']}")
        if export_csv_path:
            print(f" Trades Export File      : {export_csv_path}")
        print("=" * 80 + "\n")

        return report
