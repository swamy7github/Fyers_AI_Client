# Ghanshyam Options Trading Framework

Opening-range breakout options framework (Nifty, BankNifty, MCX) on 5-minute candles,
built on top of your existing `core/fyers_client.py`.

## Setup
    pip install -r requirements.txt
    pytest -q                     # no credentials needed

## Connect Fyers
`main.py:build_fyers_source()` looks in `core/fyers_client.py` for a history function
(`get_history` / `fetch_history` / `get_historical_data`, called as
`(symbol, resolution, start_date, end_date)`) and a quote function (`get_quote` / `get_ltp`),
or a client factory returning a raw `FyersModel`. If your names differ, edit that one function.

## Commands
    python main.py --mode backtest --symbol NIFTY --date 2026-06-01
    python main.py --mode backtest --symbol BANKNIFTY --start 2026-05-01 --end 2026-06-30
    python main.py --mode backtest --symbol NIFTY --date 2026-06-01 --csv bars.csv   # offline
    python main.py --mode paper --symbol BANKNIFTY
    python main.py --mode eod_report --date 2026-06-01

`--mode live` is intentionally not implemented (it exits with an explanation).

## Read before trusting any result
* Strategy thresholds are percentages derived from a secondhand description of the
  09:20 setup (see `StrategyConfig`). They are hypotheses to backtest, not verified rules.
* Backtest premiums are Black-Scholes estimates (`--iv` to change), not historical option prices.
* Lot sizes, expiry rules and statutory charges in `config/settings.py` must be verified
  (NSE values were checked Oct 2026; MCX values were not).
* Paper trading never sends orders to a broker.
