# ghanshyam_options — Ghanshyam Tech intraday option-buying framework

A production-style Python package that implements the **Ghanshyam Tech / Ghanshyam
Yadav** intraday index-option-buying methodology (as documented in
`Ghanshyam Tech Options Trading Guide.pdf`) for **NIFTY50**, **BANKNIFTY** and
**MCX commodity options** (CRUDEOIL, NATURALGAS, GOLD, SILVER, COPPER), on top of the
repository's existing `fyersclient` integration.

* Rules with page references: [`docs/GHANSHYAM_RULES.md`](../docs/GHANSHYAM_RULES.md)
* Supersedes the v1 attempt in `Ghanshyam _Options/` (left untouched).
* **No live trading by default.** Backtest and paper modes never call the order API.

---

## Architecture

```
                    ┌──────────────────────────────────────────────────────────────┐
                    │                      config/settings.yaml                    │
                    │  instruments · sessions · strategy params · risk · charges    │
                    └───────────────┬──────────────────────────────────────────────┘
                                    │ Settings
   fyersclient/fyers_client.py      ▼
   ┌───────────────────────┐   ┌──────────┐   ┌───────────────┐   ┌────────────┐
   │ get_fyers_client()    │──▶│ data/    │──▶│ signals.py    │──▶│ risk.py    │
   │ FyersDataService      │   │ FyersFeed│   │ SignalEngine  │   │ RiskEngine │
   │  .history / .quotes   │   │ CsvFeed  │   │  LDCHL levels │   │  R1-R7     │
   │  .get_candles_range   │   │ cache    │   │  PDC bias     │   │  sizing    │
   │  .get_quotes_batched  │   └──────────┘   │  windows      │   │  SL/TGT    │
   │  .place_order (LIVE)  │                  │  confidence   │   │  trailing  │
   └───────────────────────┘                  └──────┬────────┘   └─────┬──────┘
                                                     │ Signal              │
                                      strategies/    │                     ▼
                                      ┌──────────────┴──────────┐   ┌────────────────┐
                                      │ morning_0920  inside    │   │ engine.py      │
                                      │ hammer   shooting_star  │   │ TradingEngine  │
                                      │ ldchl_levels  base      │   │ strike select  │
                                      │ trendline  1420  1500   │   │ premium model  │
                                      └─────────────────────────┘   └───────┬────────┘
                                                                            │ OrderRequest
                        ┌────────────────┐    ┌─────────────────┐   ┌───────▼─────────┐
                        │ backtest/      │    │ paper/runner.py │   │ execution/      │
                        │ Backtester     │    │ PaperRunner     │   │ PaperBroker     │
                        │ metrics        │    │ (polls feed)    │   │ FyersBroker (!) │
                        └───────┬────────┘    └────────┬────────┘   └─────────────────┘
                                └──────────┬───────────┘
                                           ▼
                        journal/trade_log.py  →  logs/trades_YYYYMMDD.csv, daily_summary.csv
                        reports.py            →  reports/*.md, *_equity.png
```

Data flow per closed underlying bar: `feed → SignalEngine.on_bar → [Signal] →
RiskEngine.can_trade/size → StrikeSelector → premium (market|synthetic) →
Broker.buy → Position → RiskEngine.manage (SL / target / trail / time / square-off)
→ Broker.sell → Trade → CSV`.

## How it uses the existing `fyersclient`

`data/fyers_feed.py` is a thin adapter over the user's existing
`fyersclient/fyers_client.py`; nothing is re-implemented:

| Existing API (fyersclient) | Used for |
|---|---|
| `get_fyers_client() -> FyersDataService` | authentication, token cache, TOTP auto-login, rate limiting, retries |
| `FyersDataService.get_candles_range(symbol, resolution, from, to) -> [[epoch,o,h,l,c,v],…]` | intraday / daily history (chunked 10 days like the client) |
| `FyersDataService.history(data) -> {"s","candles"}` | fallback when `get_candles_range` is absent |
| `FyersDataService.get_quotes_batched(symbols) -> {symbol: {"lp",…}}` / `get_quotes` | underlying + option LTPs for paper trading |
| `FyersDataService.place_order(symbol, qty, side, product_type, order_tag)` | **only** by `execution/fyers_broker.py`, which is disabled by default |

The adapter is imported lazily, so the package and its tests run without
`fyers_apiv3`, `pyotp` or any credentials. Symbol formats follow the repo's symbol
master: `NSE:NIFTY50-INDEX`, `NSE:NIFTYBANK-INDEX`, weekly `NSE:NIFTY26O1322000CE`,
monthly `NSE:BANKNIFTY26OCT51100CE`, MCX `MCX:CRUDEOIL26NOV5500CE`.

## Setup

```bash
cd /path/to/Fyers_AI_Client
pip install -r ghanshyam_options/requirements.txt      # pandas, numpy, PyYAML, pytest (+ optional matplotlib/pyarrow)
python -m pytest ghanshyam_options/tests -q            # 61 tests, no network
```

For `--feed fyers` the existing `fyersclient/.envfyers` and token file must be in
place exactly as the rest of the repo expects; this package never reads or prints them.

## Running

Generate the synthetic sample data (already shipped under `sample_data/`):

```bash
python -m ghanshyam_options.sample_data.generator --out ghanshyam_options/sample_data \
    --from 2026-09-01 --to 2026-10-17 --instruments NIFTY50 CRUDEOIL BANKNIFTY
```

Demo backtests on the sample data (CSV feed is the default):

```bash
python -m ghanshyam_options backtest --instrument NIFTY50  --from 2026-09-01 --to 2026-10-17 --resolution 5
python -m ghanshyam_options backtest --instrument CRUDEOIL --from 2026-09-01 --to 2026-10-17 --resolution 5
```

Outputs: `ghanshyam_options/logs/trades_YYYYMMDD.csv` (one per session),
`logs/daily_summary.csv`, `logs/signals_YYYYMMDD.csv`, and
`reports/backtest_<inst>_<from>_<to>.md` + `_equity.png`.

Backtest against real Fyers history (cached under `ghanshyam_options/cache/`):

```bash
python -m ghanshyam_options backtest --instrument BANKNIFTY --from 2026-09-01 --to 2026-10-17 --feed fyers
python -m ghanshyam_options backtest --instrument NIFTY50 --from 2026-10-13 --to 2026-10-17 --feed fyers --option-candles
```

`--option-candles` asks the feed for the chosen option contract's candles first and
falls back to the synthetic model when Fyers has none (normal for expired contracts).

Paper trading (safe with the real feed — never places orders):

```bash
python -m ghanshyam_options paper --instrument BANKNIFTY --feed fyers                 # live session, polls every 15 s
python -m ghanshyam_options paper --instrument NIFTY50 --date 2026-10-16              # replay a day from CSV
```

Signals for a date (dry scan, no trades):

```bash
python -m ghanshyam_options signals --instrument CRUDEOIL --date 2026-10-17 --show-rejected
```

Journal report, instrument specs, config overrides:

```bash
python -m ghanshyam_options report --from 2026-09-01 --to 2026-10-17
python -m ghanshyam_options instruments
python -m ghanshyam_options backtest --instrument NIFTY50 --from 2026-09-01 --to 2026-10-17 --set risk.min_confidence=0.75 --set strategies.inside_candle.target_range_multiple=1.5 --strategies inside_candle hammer
```

### Live trading (off by default)

Three independent gates must all be open: `live.enabled: true` in `settings.yaml`,
the CLI flag `--mode live --i-understand-live-risk`, and `GHANSHYAM_LIVE=1` in the
environment. Otherwise `FyersBroker` raises `LiveTradingDisabled` before touching the
client. Live fills are market orders through the existing `place_order`; the fill
price is approximated by the last quote.

## Configuration reference (`config/settings.yaml`)

| Section | Keys | Guide rule |
|---|---|---|
| `capital` | `total`, `active_tranche_pct` (0.25), `risk_per_trade_pct` (0.01), `max_lots` | R1, R2 |
| `risk` | `max_trades_per_day` (3), `max_consecutive_losses` (2), `max_daily_loss_pct` (0.02), `profit_retention_pct` (0.8), `secondary_risk_pct_of_banked` (0.25), `one_position_per_instrument`, `counter_trend_size_factor`, `trail_to_breakeven_after_r`, `trail_atr_fraction`, `min_confidence` | R3–R7 |
| `sessions.NSE / MCX` | `open`, `close`, `no_new_entries_after`, `square_off`, `no_entry_windows` | p9–p12 matrix |
| `bias` | `acceptance_bars`, `trap_band_points{inst}`, `level_tolerance_points{inst}` | S4, S8 |
| `strike` | `mode` (ATM_ITM/ATM/ITM1), `min_delta` (0.5), `iv_assumption{inst}`, `risk_free_rate` | F4 |
| `execution` | `slippage_pct`, `slippage_ticks`, `fill_on`, `charges.*` (brokerage, STT, exchange, SEBI, stamp, GST, CTT) | — |
| `strategies.<name>` | `enabled`, `timeframe`, `windows{exchange}`, setup-specific thresholds (per instrument where the guide gives points) | §3 |
| `confluence.weights` | pattern 0.40, bias 0.25, structural 0.15, time 0.10, volume 0.10 | "high-confluence" |
| `instruments` (optional) | override `lot_size`, `explicit_expiries`, `underlying_symbol` … per instrument | — |

Confidence = weighted sum of the five components; the signal engine drops anything
below `risk.min_confidence`. Every strategy exposes its components in `Signal.components`.

## Strategies (one module per setup)

| Module | Setup | Entry | SL | Target |
|---|---|---|---|---|
| `ghanshyam_morning_0920` | 09:20 range breakout | stop at opening-bar high/low, within 3 bars | opposite extreme or 50 % midpoint (wide) | +40–80 pts (per instrument) |
| `ghanshyam_inside_candle` | inside-bar compression | stop at inside (or mother) high/low in bias direction | opposite inside extreme | 1.5–2.5× pattern range |
| `ghanshyam_hammer` | bullish hammer after 7–8 reds / 70–200 pt slide at a level | stop above hammer high | 1–2 pts below low | ≥ 1:2 |
| `ghanshyam_shooting_star` | mirror of hammer at resistance / PDH | stop below star low | 1–2 pts above high | ≥ 1:2 |
| `ghanshyam_ldchl_levels` | PDH/PDL acceptance & wick rejection | market after N-bar acceptance / rejection bar extreme | beyond the level / wick | 1:2 |
| `ghanshyam_congestion_base` | 20–60 min base or opening 15-min range breakout with volume | market on breakout close | opposite base boundary | 1:2 |
| `ghanshyam_trendline` | ≥ 3-touch trendline break + low-volume retest | stop at break-bar extreme | behind retest bar | 1:2 |
| `ghanshyam_afternoon_1420` | 14:15 candle breaks the 13:30–14:15 base | market at 14:20 open | base swing | 1:2 |
| `ghanshyam_squeeze_1500` | expiry-day 14:30–14:58 range break at 15:00 | market at 15:00 open | base midpoint | +15–30 % premium, ≤ 10 min |

All entries are **long options only** (CE for bullish, PE for bearish); strikes are
ATM or one step ITM with model delta ≥ 0.50 — never OTM.

## Trade log CSV schema (`logs/trades_YYYYMMDD.csv`)

`trade_id, mode, instrument, underlying, strategy, signal_ts, direction, option_symbol,
strike, expiry, entry_ts, entry_price, qty, lots, sl, target, exit_ts, exit_price,
exit_reason, gross_pnl, charges, net_pnl, r_multiple, confidence, premium_model,
entry_spot, exit_spot, spot_sl, spot_target, planned_r, slippage,
max_favourable_premium, max_adverse_premium, hold_minutes, notes`

* `sl` / `target` are premium levels; `spot_sl` / `spot_target` are underlying levels.
* `r_multiple` = net P&L / INR risked at entry (premium risk per lot × lots).
* `premium_model` is `market` (real option candle/quote) or `synthetic` (Black-Scholes).
* `daily_summary.csv`: `trade_date, instrument, mode, trades, wins, losses, gross_pnl,
  charges, net_pnl, peak_profit, max_intraday_drawdown, consecutive_losses,
  lockout_reason, signals_generated, signals_rejected, equity_end`.

## Guide rules summary

* Index option **buying only**, ATM/slightly ITM, delta ≥ 0.50, holding 3–15 min (p1–p2).
* **LDCHL** map (PDH/PDL/PDC): PDC is the trend filter (CE-only above, PE-only below);
  trap between PDC and PDH/PDL ⇒ no trade (p2–p3).
* Setups: inside candle, hammer, shooting star, 09:20, 14:20, 15:00, trendline
  break-&-retest, congestion base (p3–p7).
* Capital: 20–25 % active tranche; **max 2–3 trades/day**; **stop after 2 consecutive
  stop-outs**; **max daily loss 2–3 %**; **80/20 profit retention** (p7–p9).
* Time matrix: no orders 09:15–09:20; no new entries 11:30–13:30; square off **before
  15:15**; never carry overnight (p9–p12).

## Limitations and caveats

* **Synthetic premiums.** Fyers does not serve expired option candles, so historical
  backtests price options with Black-Scholes from the underlying path and a fixed
  per-instrument IV (`strike.iv_assumption`). This ignores the smile, IV changes, spreads
  and liquidity. Every such trade is tagged `premium_model=synthetic`; treat results as a
  test of the *rules*, not of achievable P&L. Use `--feed fyers --option-candles` on
  recent dates or paper-trade with live quotes for `market` premiums.
* **Index volume.** Fyers index symbols have no volume, so volume-confirmation rules
  score neutral on NIFTY/BANKNIFTY; use the futures symbol as `underlying_symbol` if you
  want real volume.
* **Bank Nifty constituent confirmation** (HDFCBANK/ICICIBANK) is modelled as a
  confidence penalty and disabled by default (needs constituent bars in the feed).
* **MCX** is outside the guide's scope; the same setups are applied with MCX session
  timings and scaled point thresholds that must be tuned. MCX option expiries are explicit
  dates in `instruments.py` — verify against the symbol master. Lot sizes (NIFTY 75,
  BANKNIFTY 35, CRUDEOIL 100, …) are the commonly listed 2026 values — verify.
* **Expiry days**: NIFTY weekly Tuesday, monthly last Tuesday, BANKNIFTY monthly only —
  taken from the repo's `fyersclient/nifty_fyers_symbol_generator.py`; holidays are a
  short fallback list.
* **Charges** are parameterised approximations of Indian F&O costs; check your contract note.
* **Paper runner** polls candles (no websocket); fills happen at the next bar open plus
  slippage, so live results will differ.
* **No live trading by default**; even when armed, fills are market orders with the
  quote as the assumed price.
* Past performance, simulated or real, is no guarantee of future results. This is not
  investment advice.
