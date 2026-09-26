# NSE Intraday Trading & Backtesting Engine

A modular Python engine for intraday NSE equity trading, built around the
`fyers-apiv3` broker API, `pandas` for data processing, and SQLite for
persistence. Two switchable modes:

- **BACKTEST** -- replays historical 5-minute bars through the full strategy
  and trade-management pipeline and prints a performance teardown.
- **PAPER_TRADING** -- polls live 5-minute bars from Fyers during market
  hours and manages simulated positions with realistic slippage and costs.
  No real orders are ever placed with the broker.

## Architecture

| Module | Responsibility |
|---|---|
| `config.py` | All strategy, cost and infra parameters, loaded from `.env` |
| `fyers_client.py` | Fyers auth, historical candles, live quotes (retry + rate limited) |
| `nifty_screener.py` | Nifty 500 universe, sector mapping, liquidity filter, 9:15 candle filter |
| `sector_data_fetcher.py` | Sector index intraday data + relative-strength series |
| `signal_engine.py` | VWAP / ATR(14) / volume SMA(20) indicators and entry-signal logic |
| `order_manager.py` | Abstract lifecycle (sizing, pyramiding, scale-out, stops, square-off) with `PaperTrader` / `BacktestTrader` |
| `database_manager.py` | SQLite schema (`schema.sql`) + all persistence |
| `performance_analytics.py` | PnL, win rate, profit factor, drawdown, initial-vs-pyramided breakdown |
| `engine.py` | `StrategyEngine` -- orchestrates the above per `MODE` |
| `main.py` | CLI entry point |

## Setup

1. `cd nse_intraday_engine && pip install -r requirements.txt`
2. `cp .env.example .env` and fill in your Fyers `FYERS_APP_ID`,
   `FYERS_APP_SECRET`, `FYERS_REDIRECT_URI`, and `PORTFOLIO_EQUITY`.
3. The first API call of any day opens the standard Fyers OAuth flow: it
   prints a login URL, you log in in the browser and paste back the
   `auth_code` from the redirected URL. The resulting access token is
   cached in `.fyers_token` for the rest of the trading day.
4. Run:
   - `python main.py --mode backtest --from 2024-01-01 --to 2024-06-30`
   - `python main.py --mode paper`

## How the strategy lifecycle maps to the code

- **Universe** (`NiftyScreener`): Nifty 500 -> sector-mapped -> ranked by
  30-day average turnover -> spread-filtered -> top `UNIVERSE_TOP_N`.
- **9:15 filter** (`NiftyScreener.passes_first_candle_filter`): the
  09:15-09:20 candle's % move gates long/short eligibility independently.
- **Signal** (`SignalEngine`): VWAP, Wilder ATR(14), 20-period volume SMA,
  open~low/high (0.1% tolerance), a `VWAP_SUSTAIN_BARS`-bar VWAP-side check,
  a 1.5x volume-SMA spike, and stock-vs-sector relative strength.
- **Lifecycle** (`OrderManager`): ATR-based 1% risk sizing -> pyramid +50%
  qty on a 1x-ATR favorable move with a VWAP retest -> book 50% at 1.5x ATR
  and move the stop to breakeven -> trail the runner by 1.5x ATR or a VWAP
  breach -> hard stop at 1x ATR -> mandatory square-off at 15:15 IST.

## Notes and caveats

- **Costs are approximate.** The brokerage/STT/exchange-charge model in
  `utils.calculate_charges` mirrors a typical discount-broker structure --
  confirm exact slabs against your own contract note before relying on
  backtest PnL.
- **NSE screener endpoints can change format** without notice; if
  `fetch_nifty500()` or a sector CSV fetch starts failing, check the current
  URL/columns on the NSE website first.
- **The backtest loop replays bar-by-bar per day**, prioritizing correctness
  and readability over raw speed. For multi-year, full-universe backtests,
  parallelize the per-symbol history fetch and cache it (e.g. to SQLite)
  rather than re-fetching on every run.
- **Algo trading via broker APIs is regulated in India.** SEBI/exchange
  rules require API-driven order flow to be registered with your broker
  (algo ID tagging) -- check current requirements with Fyers before
  connecting this to an account capable of placing live orders. This engine
  only ever simulates fills; extending it to real order placement is a
  deliberate, separate step.
- **Not financial advice.** This is a software scaffold, not a signal
  service -- backtested performance doesn't guarantee live results, and any
  strategy should be paper-traded extensively before risking capital.

## Database schema

See `schema.sql` -- tables: `universe_snapshot`, `signals`, `orders`,
`positions`, `trades`, `daily_metrics`. `DatabaseManager` executes this file
automatically on startup (`CREATE TABLE IF NOT EXISTS`, safe to re-run).
