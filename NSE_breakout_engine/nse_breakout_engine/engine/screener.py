import os
import pandas as pd
import yaml
import logging
from typing import List, Dict, Any
from data.fyers_client import FyersDataClient
from data.data_loader import IndicatorCalculator
from strategies.minervini_vcp import MinerviniVCPStrategy
from strategies.darvas_box import DarvasBoxStrategy
from strategies.oneil_cup_handle import ONeilCupHandleStrategy
from strategies.kullamagi_momentum import KullamagiMomentumStrategy
from engine.ranker import StockRanker

logger = logging.getLogger("screener")

class NSEScreenerEngine:
    """
    NSE Stock Screener Pipeline.
    Loads NSE universe data, evaluates 4 breakout strategies (Long & Short Intraday MIS),
    ranks signals, and produces daily actionable reports.
    """
    def __init__(self, config_path: str = None):
        if config_path is None or not os.path.exists(config_path):
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(base_dir, "config", "config.yaml")

        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        fyers_cfg = self.config.get("fyers", {})
        self.data_client = FyersDataClient(
            client_id=fyers_cfg.get("client_id", "MOCK"),
            access_token=fyers_cfg.get("access_token", "MOCK"),
            use_mock=fyers_cfg.get("use_mock", True)
        )

        strat_cfg = self.config.get("strategies", {})
        self.strategies = [
            MinerviniVCPStrategy(strat_cfg.get("minervini_vcp", {})),
            DarvasBoxStrategy(strat_cfg.get("darvas_box", {})),
            ONeilCupHandleStrategy(strat_cfg.get("oneil_cup_handle", {})),
            KullamagiMomentumStrategy(strat_cfg.get("kullamagi_momentum", {}))
        ]

        self.ranker = StockRanker(self.config.get("strategies", {}))

    def run_screening(self, symbol_list: List[str]) -> pd.DataFrame:
        raw_signals = []

        for symbol in symbol_list:
            try:
                raw_df = self.data_client.get_historical_data(symbol, "D", "2025-10-01", "2026-09-30")
                if raw_df.empty or len(raw_df) < 200:
                    continue

                ind_df = IndicatorCalculator.add_indicators(raw_df)

                for strategy in self.strategies:
                    if not strategy.enabled:
                        continue

                    sig = strategy.generate_signals(ind_df, symbol)
                    if sig is not None and getattr(sig, "signal_type", None) in ["BUY", "SELL"]:
                        raw_signals.append(sig)

            except Exception as e:
                logger.error(f"Error screening {symbol}: {str(e)}")

        ranked_df = self.ranker.rank_signals(raw_signals)
        return ranked_df

    def generate_report(self, ranked_df: pd.DataFrame) -> str:
        if ranked_df.empty:
            return "# 🚀 NSE Master Breakout Screener Report\n\nNo qualified breakout signals found."

        report = "# 🚀 NSE Master Breakout Screener Report (Long & Short Intraday)\n\n"
        report += f"**Total Qualified Signals:** {len(ranked_df)}\n\n"

        display_cols = ["composite_score", "symbol", "signal", "strategy", "entry_price", "stop_loss", "target_price", "risk_reward", "strategy_confluence", "rationale"]
        df_show = ranked_df[display_cols].copy()
        
        df_show.rename(columns={
            "composite_score": "Score",
            "symbol": "Symbol",
            "signal": "Direction",
            "strategy": "Strategy",
            "entry_price": "Entry (₹)",
            "stop_loss": "Stop Loss (₹)",
            "target_price": "Target (₹)",
            "risk_reward": "Risk:Reward",
            "strategy_confluence": "Confluence",
            "rationale": "Rationale"
        }, inplace=True)

        df_show["Direction"] = df_show["Direction"].apply(lambda s: "🟢 BUY (Long)" if s == "BUY" else "🔴 SELL (Short MIS)")
        df_show["Entry (₹)"] = df_show["Entry (₹)"].apply(lambda x: f"₹{x:.2f}")
        df_show["Stop Loss (₹)"] = df_show["Stop Loss (₹)"].apply(lambda x: f"₹{x:.2f}")
        df_show["Target (₹)"] = df_show["Target (₹)"].apply(lambda x: f"₹{x:.2f}")
        df_show["Risk:Reward"] = df_show["Risk:Reward"].apply(lambda x: f"**{x:.1f}R**")
        df_show["Confluence"] = df_show["Confluence"].apply(lambda x: f"{x}x")
        df_show["Symbol"] = df_show["Symbol"].apply(lambda x: f"`{x}`")
        df_show["Score"] = df_show["Score"].apply(lambda x: f"**{x:.1f}**")

        report += df_show.to_markdown(index=False)
        return report
