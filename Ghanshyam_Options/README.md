# Ghanshyam Tech Intraday Options Trading & Backtesting System (Flat Local Imports)

An institutional-grade, fully modular Python trading system implementing the **Ghanshyam Tech price action and behavioral risk management methodology** for both **BANK NIFTY** and **NIFTY 50** index options.

All module files inside this directory use **flat direct imports** (e.g. `from config import IndexConfig`, `from models import Candle`), so you can `cd` directly into this folder and execute scripts without any module package prefix issues.

---

## Key Capabilities

- **Direct Flat Imports**: Execute scripts directly inside the folder without needing `python -m package` wrappers or setuptools installation.
- **Multi-Index Support**: Concurrently trades **BANK NIFTY** (100-pt strike step, 15 lot size) and **NIFTY 50** (50-pt strike step, 25 lot size) under a shared account-level risk engine.
- **Fyers API v3 Integration**: Live historical 5-min candle ingestion, quotes polling, and order routing via `fyers_client`.
- **High-Fidelity Paper Trading Engine**: Realistic order execution modeling bid-ask slippage, ₹20/order exchange fees, position tracking, and margin ledger.
- **Historical Backtesting Engine**: Replays multi-day 5-minute candle data day-by-day and outputs a **Date-by-Date Daily PnL Table** with win rate, profit factor, drawdown, and net returns.
- **Real-Time Intraday Session Runner**: Runs continuously from **Market Open (09:15 IST) to Market Close (15:30 IST)**, generating signals, managing paper/live positions, and enforcing a mandatory **15:15 IST EOD auto-square-off**.

---

## Directory Structure & Files

```text
ghanshyam_trader/
├── config.py           # Index parameters (Bank Nifty step 100/lot 15 vs Nifty step 50/lot 25) and time gates
├── models.py           # Dataclasses: Candle, LDCHLLevels, TradeSignal, Position, Direction, OptionType
├── strategies.py       # Price action setups (9:20 AM, Hammer, Shooting Star, Inside Candle, 2:20 PM)
├── strikes.py          # ITM strike selection (Delta 0.55-0.70) and Fyers symbol formatting
├── risk.py             # Behavioral Risk Engine (25% tranche, 80/20 Profit Lock, 2-loss lockout)
├── broker.py           # PaperTradingBroker & FyersClientAdapter
├── agent.py            # IndexTradingAgent (Single index setup evaluation & trade lifecycle)
├── backtester.py       # BacktestEngine with Date-by-Date Daily PnL summary table
├── realtime_runner.py  # MultiAssetTradingManager & RealtimeSessionRunner (09:15 - 15:30 IST)
├── main.py             # Main CLI Entrypoint (run via python main.py)
├── test_ghanshyam_agent.py # Unit test suite
└── README.md           # Documentation
```

---

## Strategic Price Action Setups Implemented

1. **09:20 AM Opening Volatility Strategy**:
   - Evaluates the opening 5-minute candle (09:15 - 09:20 IST).
   - Rejects the setup if the opening range exceeds **250 spot points in Bank Nifty** or **120 spot points in Nifty** (volatility range exhaustion).
   - Triggers on a breakout above the high (Call) or breakdown below the low (Put).
2. **Bullish Hammer Reversal Strategy**:
   - Pre-condition: Requires an uninterrupted downfall of **≥ 100 pts (Bank Nifty)** or **≥ 50 pts (Nifty)** into Previous Day Low (PDL) or support.
   - Wick morphology: Lower shadow ≥ 2x real body; upper shadow ≤ 25% total range.
   - Triggers when subsequent price breaches the Hammer high.
3. **Bearish Shooting Star Reversal Strategy**:
   - Pre-condition: Requires an uninterrupted upward rally of **≥ 100 pts (Bank Nifty)** or **≥ 50 pts (Nifty)** into Previous Day High (PDH) or resistance.
   - Wick morphology: Upper shadow ≥ 2x real body; lower shadow ≤ 25% total range.
   - Triggers when subsequent price breaches the Shooting Star low.
4. **Inside Candle Compression Strategy**:
   - Identifies volatility contraction where a 5-minute child candle range is fully enveloped inside the preceding mother candle.
   - Triggers on breakout of child candle extreme; risk is capped strictly to child candle range.
5. **02:20 PM Institutional Momentum Strategy**:
   - Captures afternoon volatility surges (14:20 IST candle breakout) driven by European market opening liquidity transitions.

---

## Behavioral Risk & Capital Preservation Rules (Ghanshyam Doctrine)

- **Tranche Capital Allocation**: Strictly caps active capital deployment to **20%–25% of total account equity** per trade tranche.
- **80/20 Profit Retention Rule ("A green session must never close red")**: Once a daily profit peak is hit, **80% of peak gains are locked**. Subsequent trades can only risk the remaining 20%.
- **Consecutive Loss Lockout**: Mandatory terminal shutdown after **2 consecutive stop-outs** to prevent emotional revenge trading.
- **Daily Trade Ceiling**: Absolute hard cap of **2 to 3 trades max** per session across all assets combined.
- **Max Account Drawdown Circuit Breaker**: Hard exit if account equity drops by **2%–3%** in a single session.
- **Asymmetric Sizing**: 100% position sizing for trend-aligned setups, **50% sizing** for counter-trend setups.

---

## Quick Execution Guide

### 1. Run Multi-Day Historical Backtest
`cd` into the folder and execute `main.py`:

```bash
cd ghanshyam_trader
python main.py --mode backtest --capital 500000 --expiry 26OCT
```

**Outputs Date-by-Date Daily PnL Table:**
```text
=========================================================================================================
DATE         | TRADES | WINS | LOSS | NET PNL (₹)    | BALANCE (₹)    | PEAK PROFIT  | STATUS              
=========================================================================================================
2026-10-12   | 3      | 1    | 2    | ₹-2,767.50     | ₹315,290.00    | ₹0.00        | LOCKED OUT          
2026-10-13   | 4      | 1    | 3    | ₹-14,665.00    | ₹300,625.00    | ₹0.00        | LOCKED OUT          
2026-10-14   | 3      | 0    | 3    | ₹-42,022.50    | ₹258,602.50    | ₹0.00        | LOCKED OUT          
2026-10-15   | 3      | 0    | 3    | ₹-42,022.50    | ₹216,580.00    | ₹0.00        | LOCKED OUT          
2026-10-16   | 3      | 1    | 2    | ₹13,507.50     | ₹230,087.50    | ₹13,507.50   | LOCKED OUT          
=========================================================================================================
```

### 2. Run Real-Time Session Simulation (Paper Trading)
```bash
python main.py --mode realtime --capital 500000 --expiry 26OCT
```

### 3. Run Live Execution via Fyers API v3
```bash
python main.py --mode live --capital 500000 --client-id YOUR_FYERS_CLIENT_ID --access-token YOUR_ACCESS_TOKEN
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
from models import Candle, Direction, LDCHLLevels
from realtime_runner import MultiAssetTradingManager

# Initialize central manager
manager = MultiAssetTradingManager(total_capital=500000.0, is_paper=True)

# Set Previous Day High, Low, Close (LDCHL) reference benchmarks
manager.set_levels("BANKNIFTY", pdh=52000, pdl=51000, pdc=51500, trend=Direction.BULLISH)
manager.set_levels("NIFTY", pdh=24600, pdl=24300, pdc=24450, trend=Direction.BULLISH)

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
