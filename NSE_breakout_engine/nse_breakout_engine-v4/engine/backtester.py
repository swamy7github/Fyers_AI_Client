import pandas as pd
import numpy as np
from typing import List, Dict, Any
from strategies.base_strategy import BaseStrategy, TradeSignal

class BacktestEngine:
    """
    Production-grade event-driven Backtesting Engine for NSE stocks.
    Models entry execution, partial profit booking, dynamic trailing stops,
    NSE statutory transaction costs (Brokerage, STT, STT delivery), and slippage.
    Guarantees zero look-ahead bias.
    """
    def __init__(self, initial_capital: float = 1000000.0, brokerage_pct: float = 0.05, stt_pct: float = 0.1):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.brokerage_pct = brokerage_pct / 100.0
        self.stt_pct = stt_pct / 100.0

    def run_backtest(self, strategy: BaseStrategy, df: pd.DataFrame, symbol: str) -> Dict[str, Any]:
        df = df.copy().sort_values('timestamp').reset_index(drop=True)
        n = len(df)

        equity_curve = []
        trades = []
        active_position = None

        for t in range(200, n):
            current_bar = df.iloc[t]
            current_date = current_bar['timestamp']
            c = current_bar['close']
            h = current_bar['high']
            l = current_bar['low']

            # Historical slice up to bar t (prevent look-ahead bias)
            hist_df = df.iloc[:t+1].copy()

            # 1. Manage Active Position (Exits & Trailing Stops)
            if active_position is not None:
                entry_p = active_position['entry_price']
                stop_p = active_position['stop_loss']
                shares = active_position['shares']
                partial_sold = active_position['partial_sold']

                # Check Stop-Loss Breach
                if l <= stop_p:
                    exit_p = stop_p * (1.0 - 0.0005) # Slippage
                    gross_pnl = shares * (exit_p - entry_p)
                    costs = shares * exit_p * (self.brokerage_pct + self.stt_pct)
                    net_pnl = gross_pnl - costs

                    self.capital += net_pnl
                    trades.append({
                        "symbol": symbol,
                        "entry_date": active_position['entry_date'],
                        "exit_date": current_date,
                        "strategy": strategy.name,
                        "entry_price": entry_p,
                        "exit_price": exit_p,
                        "shares": shares,
                        "pnl": net_pnl,
                        "return_pct": ((exit_p - entry_p) / entry_p) * 100.0,
                        "exit_reason": "Stop Loss Hit"
                    })
                    active_position = None

                # Check Partial Profit Target (50% sell at 20% gain or 2.5R)
                elif not partial_sold and h >= active_position['target_price']:
                    sell_shares = int(shares * 0.5)
                    exit_p = active_position['target_price']
                    gross_pnl = sell_shares * (exit_p - entry_p)
                    costs = sell_shares * exit_p * (self.brokerage_pct + self.stt_pct)
                    net_pnl = gross_pnl - costs

                    self.capital += net_pnl
                    active_position['shares'] -= sell_shares
                    active_position['partial_sold'] = True
                    active_position['stop_loss'] = entry_p # Move stop to breakeven!

                    trades.append({
                        "symbol": symbol,
                        "entry_date": active_position['entry_date'],
                        "exit_date": current_date,
                        "strategy": strategy.name,
                        "entry_price": entry_p,
                        "exit_price": exit_p,
                        "shares": sell_shares,
                        "pnl": net_pnl,
                        "return_pct": ((exit_p - entry_p) / entry_p) * 100.0,
                        "exit_reason": "Partial Target (2.5R) Booked"
                    })

                # Trailing Stop Management along Moving Averages
                else:
                    if strategy.name == "Christian Kullamagi Momentum":
                        trailing_stop = current_bar['ema_10']
                    else:
                        trailing_stop = current_bar['sma_50']
                    
                    if c < trailing_stop and c > stop_p:
                        active_position['stop_loss'] = max(active_position['stop_loss'], trailing_stop)

            # 2. Evaluate Strategy Signals if no active position
            if active_position is None:
                signal = strategy.generate_signals(hist_df, symbol)
                if signal and signal.signal_type == "BUY" and signal.position_shares > 0:
                    entry_p = current_bar['close'] * 1.0005 # Slippage
                    shares = signal.position_shares
                    costs = shares * entry_p * (self.brokerage_pct + self.stt_pct)
                    
                    active_position = {
                        "entry_date": current_date,
                        "entry_price": entry_p,
                        "stop_loss": signal.stop_loss_price,
                        "target_price": signal.target_price,
                        "shares": shares,
                        "partial_sold": False
                    }
                    self.capital -= costs

            equity_curve.append({"timestamp": current_date, "capital": self.capital})

        # Final Summary Statistics
        eq_df = pd.DataFrame(equity_curve)
        stats = self._calculate_performance_metrics(eq_df, trades)
        return {"stats": stats, "trades": pd.DataFrame(trades), "equity_curve": eq_df}

    def _calculate_performance_metrics(self, eq_df: pd.DataFrame, trades: List[Dict]) -> Dict[str, Any]:
        if not trades:
            return {
                "total_trades": 0, "win_rate_pct": 0.0, "net_profit_pct": 0.0,
                "profit_factor": 0.0, "max_drawdown_pct": 0.0, "sharpe_ratio": 0.0, "expectancy_r": 0.0
            }

        tdf = pd.DataFrame(trades)
        total_trades = len(tdf)
        wins = tdf[tdf['pnl'] > 0]
        losses = tdf[tdf['pnl'] <= 0]

        win_rate = (len(wins) / total_trades) * 100.0
        gross_profit = wins['pnl'].sum() if not wins.empty else 0.0
        gross_loss = abs(losses['pnl'].sum()) if not losses.empty else 1.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit

        final_capital = eq_df['capital'].iloc[-1] if not eq_df.empty else self.initial_capital
        net_profit_pct = ((final_capital - self.initial_capital) / self.initial_capital) * 100.0

        # Maximum Drawdown calculation
        eq_df['peak'] = eq_df['capital'].cummax()
        eq_df['dd'] = (eq_df['capital'] - eq_df['peak']) / eq_df['peak']
        max_dd = abs(eq_df['dd'].min()) * 100.0

        # Sharpe Ratio (daily returns annualized)
        eq_df['ret'] = eq_df['capital'].pct_change()
        mean_ret = eq_df['ret'].mean()
        std_ret = eq_df['ret'].std()
        sharpe = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0

        # R-Expectancy: Avg Win / Avg Loss ratio
        avg_win = wins['return_pct'].mean() if not wins.empty else 0.0
        avg_loss = abs(losses['return_pct'].mean()) if not losses.empty else 1.0
        expectancy_r = (win_rate/100.0 * (avg_win / avg_loss)) - ((1 - win_rate/100.0))

        return {
            "total_trades": total_trades,
            "win_rate_pct": round(win_rate, 2),
            "net_profit_pct": round(net_profit_pct, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe_ratio": round(sharpe, 2),
            "expectancy_r": round(expectancy_r, 2)
        }
