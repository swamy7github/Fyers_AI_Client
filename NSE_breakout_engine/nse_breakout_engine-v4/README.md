# 🚀 NSE Master Breakout Trading Engine & Paper Trading Execution Framework

A modular, production-ready Python framework designed for screening, backtesting, ranking, paper trading, and live order routing on the **Indian National Stock Exchange (NSE)** using four master breakout trading methodologies:

1. **Mark Minervini Volatility Contraction Pattern (VCP) & SEPA Framework**
2. **Nicolas Darvas Box Theory System**
3. **William O'Neil CAN SLIM Cup with Handle System**
4. **Christian Kullamagi Stairstep Momentum & Power Play System**

---

## 🏗️ Architecture & Project Structure

```
nse_breakout_engine/
├── config/
│   └── config.yaml             # Configurable strategy parameters, risk limits, & execution modes
├── data/
│   ├── fyers_client.py         # FYERS API V3 historical data client & mock bar generator
│   └── data_loader.py          # Vectorized technical indicator & RS Rating engine
├── strategies/
│   ├── base_strategy.py        # Abstract Strategy class & position sizing logic
│   ├── minervini_vcp.py        # Minervini Stage 2 VCP breakout strategy
│   ├── darvas_box.py           # Darvas Box 3-Day Rule breakout strategy
│   ├── oneil_cup_handle.py     # O'Neil Cup with Handle breakout strategy
│   └── kullamagi_momentum.py   # Kullamagi Stairstep Momentum strategy
├── execution/
│   ├── base_broker.py          # Abstract Broker interface for paper & live trading
│   ├── paper_broker.py         # Paper Trading engine with slippage & P&L tracking
│   ├── fyers_order_manager.py  # FYERS API V3 & OpenAlgo Webhook REST integration
│   └── order_router.py         # Risk-managed position sizer & order dispatcher
├── engine/
│   ├── backtester.py           # Event-driven backtesting engine with brokerage & STT
│   ├── ranker.py               # Signal consensus ranker & sizer
│   └── screener.py             # Daily NSE stock screener & report generator
├── tests/
│   ├── test_strategies.py      # Strategy & indicator unit tests
│   └── test_execution.py       # Paper broker & FYERS order router unit tests
├── main.py                     # CLI entry point (--mode screen/backtest/execute/test/all)
├── requirements.txt            # Package dependencies
└── README.md                   # Documentation
```

---

## ⚡ Quick Start & CLI Usage

### 1. Run Complete Pipeline (Tests, Backtests, Screener, and Paper Trading Execution)
```bash
python3 main.py --mode all
```

### 2. Run Paper Trading & Order Router Execution
```bash
python3 main.py --mode execute
```

### 3. Run Strategy Backtests
```bash
python3 main.py --mode backtest
```

### 4. Run Daily Screener
```bash
python3 main.py --mode screen
```

### 5. Run Unit Test Suite
```bash
python3 main.py --mode test
```

---

## ⚙️ Configuration & Execution Modes (`config/config.yaml`)

You can switch between **PAPER TRADING**, **LIVE FYERS API**, or **OPENALGO WEBHOOKS** without editing source code:

```yaml
execution:
  mode: "PAPER"  # Options: "PAPER", "LIVE_FYERS", "OPENALGO"
  paper:
    initial_cash: 1000000.0   # ₹10 Lakhs paper capital
    slippage_pct: 0.05        # 0.05% realistic slippage
    commission_pct: 0.05      # 0.05% brokerage fee
  fyers:
    client_id: "YOUR_FYERS_CLIENT_ID"
    access_token: "YOUR_FYERS_ACCESS_TOKEN"
    product_type: "CNC"       # CNC delivery or INTRADAY
  openalgo:
    openalgo_url: "http://127.0.0.1:5000/api/v1/place_order"
    openalgo_enabled: true
```

---

## 🛡️ Risk Management & Position Sizing Rules

The Order Router automatically calculates exact share quantity based on 3 constraining rules:

$$\text{Quantity} = \min \left( \frac{\text{Portfolio Equity} \times \text{Risk \%}}{\text{Entry Price} - \text{Stop Loss}}, \frac{\text{Portfolio Equity} \times \text{Max Stock Allocation \%}}{\text{Entry Price}}, \frac{\text{Available Cash}}{\text{Entry Price}} \right)$$

* **Max Equity Risk per Trade:** 1.0%
* **Max Allocation per Stock:** 20.0% of portfolio value
* **Max Open Portfolio Positions:** 8 stocks
