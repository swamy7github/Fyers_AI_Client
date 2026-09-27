import pandas as pd
import numpy as np
import datetime
from typing import Dict, List

# =====================================================================
# 1. CAPITAL-AWARE QUANTITATIVE BACKTESTER
# =====================================================================
class FiveMinBreakoutBacktester:
    def __init__(
        self,
        initial_capital: float = 100000.0,
        allocation_pct: float = 1.0,      # 1.0 = 100% of equity deployed per trade
        slippage_per_trade: float = 0.05   # Points deducted per side (entry + exit)
    ):
        self.initial_capital = float(initial_capital)
        self.current_capital = float(initial_capital)
        self.allocation_pct = allocation_pct
        self.slippage = slippage_per_trade
        self.trades: List[Dict] = []

    def _prepare_indicators(self, day_df: pd.DataFrame) -> pd.DataFrame:
        """Computes daily resetting Intraday VWAP and rolling 20-period Volume SMA."""
        df = day_df.copy().sort_values("datetime").reset_index(drop=True)
        
        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        cum_vol_price = (typical_price * df["volume"]).cumsum()
        cum_vol = df["volume"].cumsum()
        
        df["vwap"] = np.where(cum_vol > 0, cum_vol_price / cum_vol, df["close"])
        df["vol_ma20"] = df["volume"].rolling(window=20, min_periods=1).mean()
        return df

    def backtest_day(self, date_val, day_df: pd.DataFrame, symbol: str):
        """Executes trading strategy for a single market session with capital tracking."""
        df = self._prepare_indicators(day_df)
        if len(df) < 3:
            return

        # Benchmark: High of first 5-minute candle (09:15 to 09:20)
        c1 = df.iloc[0]
        benchmark_high = c1["high"]
        
        in_position = False
        entry_price = 0.0
        entry_time = None
        quantity = 0
        entry_idx = 0

        # Iterate from candle index 2 (09:25 AM) to evaluate two completed prior candles
        for i in range(2, len(df)):
            curr_c = df.iloc[i]
            prev_c = df.iloc[i - 1]
            c_time = curr_c["datetime"].time()

            # ---------------- 15:10 PM EOD MANDATORY SQUARE OFF ----------------
            if c_time >= datetime.time(15, 10):
                if in_position:
                    exit_price = curr_c["close"] - self.slippage
                    
                    pnl_points = exit_price - entry_price
                    realized_pnl = round(pnl_points * quantity, 2)
                    
                    capital_before = self.current_capital
                    self.current_capital += realized_pnl

                    self.trades.append({
                        "symbol": symbol,
                        "date": date_val,
                        "entry_time": entry_time,
                        "entry_price": round(entry_price, 2),
                        "exit_time": curr_c["datetime"].time(),
                        "exit_price": round(exit_price, 2),
                        "quantity": quantity,
                        "pnl_points": round(pnl_points, 2),
                        "realized_pnl": realized_pnl,
                        "capital_after_trade": round(self.current_capital, 2),
                        "exit_reason": "EOD_SQUAREOFF",
                        "bars_held": i - entry_idx
                    })
                    in_position = False
                break

            # ---------------- EVALUATE BUY ENTRY ----------------
            if not in_position:
                cond_two_above_high = (curr_c["close"] > benchmark_high) and (prev_c["close"] > benchmark_high)
                cond_above_vwap = curr_c["close"] > curr_c["vwap"]
                cond_vol_breakout = curr_c["volume"] > curr_c["vol_ma20"]

                if cond_two_above_high and cond_above_vwap and cond_vol_breakout:
                    entry_price = curr_c["close"] + self.slippage
                    
                    # Calculate position size based on current capital
                    deployable_capital = self.current_capital * self.allocation_pct
                    quantity = int(deployable_capital // entry_price)

                    if quantity > 0:
                        in_position = True
                        entry_time = curr_c["datetime"].time()
                        entry_idx = i

            # ---------------- EVALUATE SELL / EXIT ----------------
            else:
                cond_two_below_vwap = (curr_c["close"] < curr_c["vwap"]) and (prev_c["close"] < prev_c["vwap"])
                cond_exit_vol = curr_c["volume"] > curr_c["vol_ma20"]

                if cond_two_below_vwap and cond_exit_vol:
                    exit_price = curr_c["close"] - self.slippage
                    pnl_points = exit_price - entry_price
                    realized_pnl = round(pnl_points * quantity, 2)
                    
                    self.current_capital += realized_pnl

                    self.trades.append({
                        "symbol": symbol,
                        "date": date_val,
                        "entry_time": entry_time,
                        "entry_price": round(entry_price, 2),
                        "exit_time": curr_c["datetime"].time(),
                        "exit_price": round(exit_price, 2),
                        "quantity": quantity,
                        "pnl_points": round(pnl_points, 2),
                        "realized_pnl": realized_pnl,
                        "capital_after_trade": round(self.current_capital, 2),
                        "exit_reason": "TWO_CLOSES_BELOW_VWAP",
                        "bars_held": i - entry_idx
                    })
                    in_position = False

    def run(self, df: pd.DataFrame, symbol: str = "ASSET") -> pd.DataFrame:
        """Executes simulation across all sessions sorted chronologically."""
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["datetime"])
        df["date"] = df["datetime"].dt.date
        
        grouped = df.groupby("date")
        for date_val, day_data in grouped:
            self.backtest_day(date_val, day_data, symbol)

        return pd.DataFrame(self.trades)

    def generate_performance_metrics(self) -> Dict:
        """Generates portfolio-level capital and risk statistics."""
        final_capital = self.current_capital
        capital_gain = final_capital - self.initial_capital
        total_return_pct = (capital_gain / self.initial_capital) * 100.0

        if not self.trades:
            return {
                "Initial Capital": f"₹{self.initial_capital:,.2f}",
                "Final Capital": f"₹{final_capital:,.2f}",
                "Net Capital Gain": f"₹{capital_gain:,.2f}",
                "Total Return (%)": f"{total_return_pct:.2f}%",
                "Status": "No trades triggered"
            }

        tdf = pd.DataFrame(self.trades)
        wins = tdf[tdf["realized_pnl"] > 0]
        losses = tdf[tdf["realized_pnl"] <= 0]

        total_trades = len(tdf)
        win_rate = (len(wins) / total_trades) * 100.0
        gross_profit = wins["realized_pnl"].sum()
        gross_loss = abs(losses["realized_pnl"].sum())
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.nan

        # Equity Curve Drawdown calculation
        equity_series = pd.concat([pd.Series([self.initial_capital]), tdf["capital_after_trade"]]).reset_index(drop=True)
        peak_series = equity_series.cummax()
        drawdown_series = (peak_series - equity_series) / peak_series * 100.0
        max_drawdown_pct = drawdown_series.max()

        metrics = {
            "Initial Capital": f"₹{self.initial_capital:,.2f}",
            "Final Capital": f"₹{final_capital:,.2f}",
            "Net Capital Gain (P&L)": f"₹{capital_gain:+,.2f}",
            "Total Return (%)": f"{total_return_pct:+.2f}%",
            "Total Executed Trades": total_trades,
            "Win Rate (%)": f"{win_rate:.2f}%",
            "Gross Profit": f"₹{gross_profit:,.2f}",
            "Gross Loss": f"₹{gross_loss:,.2f}",
            "Profit Factor": round(profit_factor, 2) if not np.isnan(profit_factor) else "Inf",
            "Max Portfolio Drawdown (%)": f"{max_drawdown_pct:.2f}%",
            "Avg Points Captured / Trade": round(tdf["pnl_points"].mean(), 2),
            "Avg Realized Cash / Trade": f"₹{tdf['realized_pnl'].mean():+,.2f}"
        }
        return metrics

# =====================================================================
# 2. RUN TEST WITH CAPITAL SIZING (₹2,00,000 Initial Capital)
# =====================================================================
if __name__ == "__main__":
    np.random.seed(42)
    dates = pd.date_range("2026-03-01", periods=10, freq="B")
    dfs = []
    
    # Generate 10 days of synthetic 5m candles
    for d in dates:
        times = pd.date_range(f"{d.strftime('%Y-%m-%d')} 09:15:00", f"{d.strftime('%Y-%m-%d')} 15:25:00", freq="5min")
        base_price = 2400.0 + np.random.uniform(-40, 40)
        returns = np.random.normal(0.0004, 0.0022, len(times))
        price_curve = base_price * np.exp(np.cumsum(returns))
        
        open_p = price_curve
        high_p = price_curve * (1 + np.abs(np.random.normal(0, 0.0016, len(times))))
        low_p = price_curve * (1 - np.abs(np.random.normal(0, 0.0016, len(times))))
        close_p = (open_p + high_p + low_p) / 3.0 + np.random.normal(0, 0.25, len(times))
        volume = np.random.randint(20000, 200000, len(times))
        
        day_df = pd.DataFrame({
            "datetime": times,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": volume
        })
        dfs.append(day_df)

    synthetic_data = pd.concat(dfs, ignore_index=True)

    # Initialize Backtester with 2,00,000 INR
    INITIAL_CAPITAL = 200000.0
    backtester = FiveMinBreakoutBacktester(
        initial_capital=INITIAL_CAPITAL,
        allocation_pct=1.0,       # Deploy 100% of balance per trade
        slippage_per_trade=0.05   # 0.05 pts slippage
    )

    trade_log = backtester.run(synthetic_data, symbol="NSE:RELIANCE-EQ")
    summary = backtester.generate_performance_metrics()

    print("\n========================= TRADE EXECUTION LOG =========================")
    if not trade_log.empty:
        display_cols = [
            "date", "entry_time", "entry_price", "exit_time", 
            "exit_price", "quantity", "pnl_points", "realized_pnl", "capital_after_trade"
        ]
        print(trade_log[display_cols].to_string(index=False))
    else:
        print("No trade setups triggered with current parameters.")

    print("\n====================== FINAL CAPITAL & PERFORMANCE ======================")
    for key, value in summary.items():
        print(f"{key:<28}: {value}")