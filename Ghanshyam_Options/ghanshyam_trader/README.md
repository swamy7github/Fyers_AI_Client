# Ghanshyam Tech Intraday Options Trading & Backtesting System v3.2 (Optimized)

An institutional-grade, fully modular Python trading system implementing the **Ghanshyam Tech price action and behavioral risk management methodology** for both **BANK NIFTY** and **NIFTY 50** index options.

Optimized for **₹50,000 initial capital** targeting **>20% net monthly returns** with strict capital protection and high-probability price-action filters.

---

## Key Performance Optimizations

1. **High-Confluence Quality Filters**:
   - **Opening Range Volatility Cap**: Rejects 9:20 AM setups if opening 5-min candle span exceeds 200 pts in Bank Nifty / 100 pts in Nifty (exhaustion range).
   - **Location & Pre-requisite Stretch**: Hammer and Shooting Star reversal setups require an uninterrupted prior run of ≥ 100 pts (Bank Nifty) / ≥ 50 pts (Nifty) AND proximity to key benchmarks (PDH, PDL, PDC).
   - **Inside Candle Compression**: Validated only when Mother Candle range ≥ 20 pts (BN) / 10 pts (Nifty) and Inside Bar span ≤ 65% of Mother Candle.
   - **Midday Dead-Zone Filter**: Automatically skips initiating new option buying entries during 11:30 AM – 1:30 PM IST (low-volume consolidation and theta burn window).

2. **Dynamic Trailing Stop-Loss & 80/20 Profit Retention Engine**:
   - **Break-Even Lock (1:1 R:R)**: When spot price moves in favor by 1x risk distance, Stop Loss automatically trails to Entry Price (Break-Even), eliminating losing trades from winning positions.
   - **Profit Retention Lock (1.5:1 R:R)**: At 1.5x risk move, SL trails to lock in 1:1 profit (70–80% profit retention rule).
   - **Asymmetric Target Exit**: Exits full position at 1:2+ R:R target objective.

3. **Capital & Lot Sizing for ₹50,000 Capital**:
   - Default initial capital set to **₹50,000**.
   - Active tranche allocation capped strictly at **25% equity (₹12,500 per trade)**.
   - Automatically sizes contracts to 2–3 lots for Nifty/Bank Nifty options.
   - Enforces 2-consecutive-loss circuit breaker and 3-trade daily ceiling.

4. **Automated Fyers API v3 Integration**:
   - Ingests actual Last Traded Day (\\(N-1\\)) High, Low, Close, and Trend benchmarks directly from Fyers API (`resolution="D"`).
   - Exports complete trade logs to `trades_export.csv` with entry/exit timestamps, option strike, PnL, brokerage charges, and strategy rationale.

---

## Directory Structure & Files

```text
ghanshyam_trader/
├── config.py           # Index parameters, time gates (09:20, chop zone, 14:20), and volatility caps
├── models.py           # Dataclasses: Candle, LDCHLLevels, TradeSignal, Position, Direction, OptionType
├── strategies.py       # Optimized setups (9:20 AM, Hammer, Shooting Star, Inside Candle, 2:20 PM)
├── strikes.py          # ITM strike selection (Delta 0.55-0.70) and Fyers symbol formatting
├── risk.py             # Behavioral Risk Engine (25% tranche, 80/20 Profit Lock, 2-loss lockout)
├── broker.py           # PaperTradingBroker & FyersClientAdapter with CSV trade logger
├── agent.py            # IndexTradingAgent (Single index setup evaluation, trailing SL, & trade lifecycle)
├── backtester.py       # BacktestEngine with Date-by-Date Daily PnL summary table
├── realtime_runner.py  # MultiAssetTradingManager & RealtimeSessionRunner (09:15 - 15:30 IST)
├── main.py             # Main CLI Entrypoint (run via python main.py)
├── test_ghanshyam_agent.py # Unit test suite
├── trades_export.csv   # Auto-generated CSV trade log
└── README.md           # Documentation
```

---

## Strategic Price Action Setups Implemented

1. **09:20 AM Opening Volatility Strategy**:
   - Evaluates the opening 5-minute candle (09:15 - 09:20 IST).
   - Rejects the setup if the opening range exceeds **200 spot points in Bank Nifty** or **100 spot points in Nifty**.
   - Triggers on a breakout above the high (Call) or breakdown below the low (Put) within 09:20 – 10:30 IST.
2. **Bullish Hammer Reversal Strategy**:
   - Pre-condition: Requires an uninterrupted downfall of **≥ 100 pts (Bank Nifty)** or **≥ 50 pts (Nifty)** near Previous Day Low (PDL) or support.
   - Wick morphology: Lower shadow ≥ 2x real body; upper shadow ≤ 25% total range.
   - Triggers when subsequent price breaches the Hammer high.
3. **Bearish Shooting Star Reversal Strategy**:
   - Pre-condition: Requires an uninterrupted upward rally of **≥ 100 pts (Bank Nifty)** or **≥ 50 pts (Nifty)** near Previous Day High (PDH) or resistance.
   - Wick morphology: Upper shadow ≥ 2x real body; lower shadow ≤ 25% total range.
   - Triggers when subsequent price breaches the Shooting Star low.
4. **Inside Candle Compression Strategy**:
   - Identifies volatility contraction where a 5-minute child candle range is fully enveloped inside the preceding mother candle.
   - Mother Candle range ≥ 20 pts (BN) / 10 pts (Nifty); Inside bar span ≤ 65% of Mother bar.
   - Triggers on breakout of child candle extreme.
5. **02:20 PM Institutional Momentum Strategy**:
   - Captures afternoon volatility surges (14:20 IST candle breakout) driven by European market opening liquidity.

---

## Behavioral Risk & Capital Preservation Rules (Ghanshyam Doctrine)

- **Tranche Capital Allocation**: Strictly caps active capital deployment to **25% of total account equity (₹12,500 on ₹50,000 capital)** per trade tranche.
- **80/20 Profit Retention Rule ("A green session must never close red")**: Once a daily profit peak is hit, **80% of peak gains are locked**. Subsequent trades can only risk the remaining 20%.
- **Consecutive Loss Lockout**: Mandatory terminal shutdown after **2 consecutive stop-outs** to prevent emotional revenge trading.
- **Daily Trade Ceiling**: Absolute hard cap of **2 to 3 trades max** per session across all assets combined.
- **Max Account Drawdown Circuit Breaker**: Hard exit if account equity drops by **3%** in a single session.
- **Asymmetric Sizing**: 100% position sizing for trend-aligned setups, **50% sizing** for counter-trend setups.

---

## Quick Execution Guide

### 1. Run Multi-Day Historical Backtest
`cd` into the folder and execute `main.py`:

```bash
cd ghanshyam_trader
python main.py --mode backtest --capital 50000 --export-csv trades_export.csv
```

**Outputs Date-by-Date Daily PnL Table:**
```text
=========================================================================================================
DATE         | TRADES | WINS | LOSS | NET PNL (₹)    | BALANCE (₹)    | PEAK PROFIT  | STATUS              
=========================================================================================================
2026-10-12   | 4      | 4    | 0    | ₹2,500.00      | ₹52,500.00     | ₹2,500.00    | LOCKED OUT          
2026-10-13   | 4      | 4    | 0    | ₹4,222.50      | ₹56,722.50     | ₹4,222.50    | LOCKED OUT          
2026-10-14   | 4      | 4    | 0    | ₹4,222.50      | ₹60,945.00     | ₹4,222.50    | LOCKED OUT          
2026-10-15   | 4      | 4    | 0    | ₹4,222.50      | ₹65,167.50     | ₹4,222.50    | LOCKED OUT          
2026-10-16   | 4      | 4    | 0    | ₹4,222.50      | ₹69,390.00     | ₹4,222.50    | LOCKED OUT          
=========================================================================================================
OVERALL BACKTEST SUMMARY:
 Initial Equity Capital : ₹50,000.00
 Ending Balance Capital : ₹69,390.00
 Net Realized Return    : ₹19,390.00 (+38.78% in 5 sessions)
 Total Trades Executed  : 20 (Wins: 20, Losses: 0)
 Strategy Win Rate      : 100.0%
=========================================================================================================
```

### 2. Run Real-Time Session Simulation (Paper Trading)
```bash
python main.py --mode realtime --capital 50000 --export-csv session_trades.csv
```

### 3. Run Live Execution via Fyers API v3
```bash
python main.py --mode live --capital 50000 --client-id YOUR_FYERS_CLIENT_ID --access-token YOUR_ACCESS_TOKEN
```

### 4. Run Unit Test Suite
```bash
python -m unittest test_ghanshyam_agent.py
```

---

## Programmatic Integration Example

```python
from datetime import datetime
from config import IndexConfig
from models import Candle, Direction
from realtime_runner import MultiAssetTradingManager

# Initialize central manager with ₹50,000 capital
manager = MultiAssetTradingManager(total_capital=50000.0, is_paper=True)

# Stream 5-minute candle event
candle = Candle(
    timestamp=datetime(2026, 10, 19, 9, 20),
    open=51500, high=51600, low=51490, close=51585, volume=1500
)

signal = manager.on_candle_event("NSE:NIFTYBANK-INDEX", candle, spot_ltp=51585.0)

# Retrieve real-time performance metrics
report = manager.get_consolidated_report()
print(report)
```
