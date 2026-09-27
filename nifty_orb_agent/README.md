# Nifty Opening-Range Breakout (ORB) Scanning Agent

A modular Python agent that, on a Fyers-connected NSE trading day:

1. **09:10** — snapshots sector strength/weakness across your mapped universe.
2. **09:20:02** — reads the 09:15 five-minute candle for your stock universe,
   filters for stocks up between 0.5% and 2.5%, and shortlists the top 5.
3. Every 5 minutes until **15:10** — checks each shortlisted stock's latest
   candle for an opening-range breakout (BUY) or a VWAP/volume breakdown (SELL),
   and logs paper trades with entry, exit, points captured and P&L%.
4. **15:10:30** — force-closes any still-open paper position and prints/saves
   a trade book.

It also includes a standalone **global market sentiment** module for the
"is the broader tape bullish or bearish" question, combining price-based
scoring with an optional Claude-powered read on qualitative news.

## ⚠️ Read this first

- **This is a scanning/paper-trading scaffold, not a finished, risk-managed
  trading system.** `config.DRY_RUN = True` by default; the code never calls
  `fyers.place_order()`. Wiring up real order placement is a deliberate,
  separate step (see "Before you go live" below) — don't skip straight to
  live orders.
- I have **not** been able to run this against a live Fyers connection while
  building it (no network/broker access in this environment), so treat it as
  a strong, carefully-reasoned starting point that still needs to be
  validated against the real API during market hours before you trust it
  with money.
- I'm not a financial advisor and this isn't trading advice — it's software
  to help you implement *your own* strategy and see it more clearly.

## What's in the box

| File | Purpose |
|---|---|
| `config.py` | All settings: credentials (via `.env`), file paths, timing, strategy thresholds |
| `fyers_service.py` | Fyers login (auto TOTP + manual fallback) and a retrying, logged data wrapper |
| `global_sentiment.py` | Global bullish/bearish scoring + optional Claude-based news sentiment |
| `sector_scanner.py` | 09:10 / 09:16 sector strength/weakness snapshot, queried directly from NSE index symbols |
| `opening_range_scanner.py` | Filters the universe by sector, then builds the top-5 opening-range gainer shortlist |
| `signal_engine.py` | VWAP + volume-MA(20) candle logic, tick-level SL/target/trailing-SL (`check_tick_for_exit`), broker position adoption (`adopt_broker_position`); `feed_candle()` is the shared replay/live entry point both `backtest.py` and `bootstrap.py` use |
| `bootstrap.py` | Market-phase detection + the late-startup catch-up pipeline: `bootstrap_and_catchup()`, historical replay, broker reconciliation — see "Late-startup catch-up" below |
| `live_tick_stream.py` | WebSocket tick feed → `SignalEngine.check_tick_for_exit()` hand-off — see "Live hand-off" below |
| `main.py` | Wires it all together with APScheduler; calls `bootstrap_and_catchup()` once at startup, then runs the ongoing 5-min candle checks and starts `LiveTickStream` |
| `backtest.py` | Replays the same live strategy against historical data over a date range; run with `python backtest.py --start ... --end ...` |
| `backtest_strategy_picks.py` | Separate tool: backtests an external daily stock-picks CSV (buy-at-open/sell-at-close, equal-weight) — see "Backtesting an external daily stock-picks strategy" below |
| `test_backtest_strategy_picks.py` | Tests for the above, including hand-verified tear-sheet math |
| `test_signal_engine.py` | Tests for tick-level SL/target/trailing-SL, broker position adoption, and the `is_backfill` gate |
| `test_bootstrap.py` | Tests for historical replay, broker reconciliation, and a full end-to-end `bootstrap_and_catchup()` run |
| `test_live_tick_stream.py` | Tests for tick routing/validation, without a real WebSocket |
| `build_sector_map.py` | Optional: builds a per-STOCK sector tag file from NSE data, if you want that for something other than the sector leaderboard (see note below) |
| `test_updates.py` | Standalone tests for sector/universe filtering and market-phase detection |
| `data/ind_nifty500list.csv` | **You provide this** — Nifty 500 constituents (see below) |
| `data/sector_index_map.csv` | Reference list of NSE index symbols (sector + broader-market + VIX + G-Sec), an editable `is_sector_index` flag, and a `sector` column used for universe filtering — ships pre-filled, verify against your Fyers account and your `Industry` text (see below) |
| `.env.example` | Template for your credentials |

## Setup

1. **Get Fyers API credentials.** Create an app at
   [myapi.fyers.in](https://myapi.fyers.in) to get an App ID, Secret Key, and
   set a Redirect URI. Enable TOTP-based 2FA on your Fyers account
   (Profile → Security) and note the TOTP secret if you want unattended
   automated logins — otherwise the script falls back to a manual,
   once-a-day browser login.

2. **Install dependencies** (Python 3.10+ recommended):
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure credentials:**
   ```bash
   cp .env.example .env
   # then edit .env with your real values
   ```

4. **Download the Nifty 500 constituent list** from the NSE indices archive
   (search "ind_nifty500list.csv" on nseindia.com/niftyindices.com — the
   exact URL changes periodically) and save it as
   `data/ind_nifty500list.csv`. It needs a `Symbol` column, and — for the
   sector-based universe filtering described in step 5 — an `Industry`
   column too; if that column is missing, filtering is skipped automatically
   (with a logged warning) and the full universe is scanned instead, so
   nothing breaks if your download doesn't have it.

5. **Verify `data/sector_index_map.csv` against your own Fyers symbol
   access, and against your actual `Industry` column text.** This file does
   two separate jobs, via two separate columns — don't conflate them:
   - `is_sector_index` (True/False) controls which rows count toward the
     "top gaining/losing sector" **leaderboard**. `sector_scanner.py` quotes
     NSE's index symbols directly (e.g. `NSE:NIFTYIT-INDEX`), so there's no
     per-stock mapping to build for this part.
   - `sector` (a short label like `IT`, `Banks`, `Automobiles`) controls
     which stocks are even **eligible for the opening-range scan** in the
     first place: `opening_range_scanner.py` keeps only universe rows whose
     `Industry` matches one of these labels (case-insensitive, trimmed).
     Rows with a blank `sector` (broad-market indices, VIX, G-Secs,
     cross-sector baskets like Nifty PSE/Commodities/Consumption) are
     intentionally excluded from filtering — not a mistake, they were never
     meant to correspond to one single `Industry` value.

   Two things to check before relying on either: (a) confirm each `symbol`
   actually resolves in your Fyers account — not every data vendor exposes
   every index, especially the G-Sec ones, and the code logs a warning and
   skips gracefully for any that don't; (b) **the `sector` values shipped in
   this file (IT, Banks, Automobiles, FMCG, Energy, Infrastructure,
   Financial Services) are my best guess at plausible NSE `Industry` text —
   I have not verified them against an actual downloaded
   `ind_nifty500list.csv`.** `opening_range_scanner.py` logs how many stocks
   matched each sector and warns loudly about any sector matching zero
   stocks — check that log the first time you run this (or run
   `python test_updates.py`, which exercises the same matching logic
   against sample data) and fix any row that comes back at zero by editing
   the `sector` column to match your file's actual text. Feel free to also
   flip any `is_sector_index` value if you disagree with a default.
   `build_sector_map.py` still exists if you separately want a per-STOCK
   sector tag for something other than either of the above.

6. **Run it** on a trading day, ideally started a few minutes before 09:00 IST:
   ```bash
   python main.py
   ```
   Logs stream to the console and to `logs/orb_agent.log`. The day's trade
   book is written to `logs/trade_book_<YYYYMMDD>.csv`. If you start the
   process late (anywhere from just after market open to shortly before
   close), it detects this and catches up automatically — see "Late-startup
   catch-up" below for exactly what it does and doesn't do in that case.

   To check the filtering and late-startup logic without touching your
   Fyers account at all, run the test script:
   ```bash
   python test_updates.py
   ```

## Late-startup catch-up (bootstrap.py)

If the process starts mid-session instead of before market open,
`bootstrap.bootstrap_and_catchup()` — called once at the top of `main.main()`,
before the scheduler starts — handles it as a single, ordered pipeline
rather than the previous per-job `next_run_time` patching this project used
in an earlier iteration:

1. **Market phase detection** (`detect_market_phase`): `PRE_OPEN` (before
   09:15), `POST_OPEN` (09:15–15:30), or `POST_CLOSE` (after 15:30 or a
   weekend) — weekday + time-of-day only, no NSE holiday calendar.
   - `PRE_OPEN` → logs and returns immediately; the normal cron schedule in
     `main.py` handles everything from here.
   - `POST_CLOSE` → logs and exits; nothing useful left to do today.
   - `POST_OPEN` **but past the 15:10 signal-checking cutoff**
     (`_within_signal_window` is `False`) → also returns immediately,
     deliberately more conservative than a plain "is the market open" check:
     starting a brand-new tracked position minutes before close, after
     today's square-off trigger has already fired uneventfully, would leave
     that position with nothing to close it out.
2. **Sector/breadth snapshot + gap-up screen**: `sector_scanner.get_index_snapshot()` /
   `compute_market_breadth()`, then `opening_range_scanner.scan_opening_range_accurate()`
   — always the historical-candle path here, never the quotes shortcut
   (which is only valid within ~3 minutes of 09:20:02 and would silently
   return wrong numbers at any other time).
3. **Historical replay** (`_replay_intraday_history`): every fully-closed
   5-minute candle from 09:15 up to now, for every shortlisted symbol, fed
   through the *exact same* `SignalEngine.feed_candle()` used for live
   candles — same VWAP accumulation, same volume-MA warm-up, same
   2-consecutive-candle BUY/weakness-event logic — with `is_backfill=True`.
   That flag does exactly one thing: it stops `_maybe_place_live_order()`
   from ever placing a real order for a signal reconstructed from history
   (the entry price has likely moved on by now) — it does *not* change
   whether the signal fires, so a reconstructed BUY still opens
   `position_open=True` and is still fully risk-managed (stop-loss/target/
   trailing-SL) going forward from this point.
4. **Broker reconciliation** (`_reconcile_broker_state`): fetches the
   broker's actual open positions and adopts/clears local state to match —
   see "Broker position sync" below.
5. Returns a `BootstrapResult` (phase, populated `SignalEngine` or `None`,
   sector leaderboard, market breadth) that `main.py` hands off to the
   ongoing 5-minute candle-check scheduler and to `LiveTickStream` for
   tick-level risk management from this point forward.

`coalesce=True` + `max_instances=1` on every recurring APScheduler job
remain as a *separate*, ongoing safety net for transient delays while the
process is already running (a slow API call briefly blocking the loop) —
not the mechanism for the initial late start, which bootstrap.py now
handles explicitly and in the correct order.

## Broker position sync

`_reconcile_broker_state()` (in `bootstrap.py`, using
`FyersDataService.get_positions()`) is what closes the single biggest gap
in a **history-replay-only** catch-up: replay reconstructs what *should*
have happened from candles, but it can't know about a position that
already exists at the broker for another reason (the process crashed and
restarted mid-day with a real position still open, or was started against
an account with a manual trade already in it). Reconciliation runs after
replay and always lets broker truth win:
- Broker shows a position replay didn't reconstruct → **adopt** it
  (`SignalEngine.adopt_broker_position()`), using the broker's real average
  price and quantity, immediately under full stop-loss/target/trailing-SL
  management.
- Replay reconstructed a position the broker doesn't confirm → **clear**
  it locally rather than manage a position that doesn't actually exist.
- Both agree → left untouched (replay's own entry price is kept, not
  second-guessed).
- Broker API call fails → logged, replay-only state is kept as a fallback,
  and it says so explicitly — verify manually against your broker terminal
  before trusting that session with live orders.
- A broker position in a symbol outside today's shortlist is logged, not
  adopted (out of scope for this engine — you still hold it for real;
  manage it separately).

## Live hand-off (live_tick_stream.py)

Once `bootstrap_and_catchup()` returns a populated engine, `main.py` starts
`LiveTickStream`, a thin wrapper around Fyers' WebSocket tick feed
(`fyers_apiv3.FyersWebsocket.data_ws`) that routes every tick for a tracked
symbol straight into `SignalEngine.check_tick_for_exit()` — fast,
tick-level stop-loss/target/trailing-SL, quicker to react than the 5-minute
candle-close weakness-event checks. This is a genuinely new capability, not
a rename of the existing 5-minute REST polling: the two run *alongside*
each other — 5-minute candles still drive BUY/weakness-event detection,
ticks drive the tighter risk exits once a position is open. Testable
without a real socket via `on_tick_override` (see `test_live_tick_stream.py`);
verify the tick message field names (`symbol`, `ltp`) against your installed
`fyers_apiv3` version before relying on this — broker WebSocket message
shapes are exactly the kind of thing that changes between SDK versions.

## Stop-loss, target, and trailing-SL (tick-level)

`SignalEngine.check_tick_for_exit()` runs on every tick for an open
position: `config.STOP_LOSS_PCT` / `TARGET_PCT` / `TRAILING_SL_PCT` (all %
from entry, trailing measured from the highest price seen since entry, not
from entry). One property worth knowing, confirmed by
`test_signal_engine.py` rather than assumed: on the tick that first sets a
new high, the trailing-stop level is always strictly below that new high
(`new_high × (1 - trail%) < new_high`), so the stop check can never
preempt the target check on that same tick — meaning **target is
effectively a hard ceiling**, however large a single move is; the trailing
stop can never "overtake" an unreached target on the way up. An initial
hypothesis that it could was tested directly and found to be wrong before
this was documented as fact — see that test's docstring for the full
reasoning. `config.DRY_RUN=True` (default) logs every exit instead of
placing it; flipping it to `False` reaches a clearly-marked
`[LIVE ORDER PLACEHOLDER]` log line, not a real order — `fyers.place_order()`
itself is intentionally not wired in yet (see `_maybe_place_live_order`'s
TODO in `signal_engine.py`).

## Backtesting

`backtest.py` replays the exact same strategy — unmodified — against
historical Fyers data. It doesn't reimplement the rules: `SignalEngine` gets
a new public `feed_candle()` method that lets historical candles be pushed
through the identical VWAP/volume/weakness-event logic `main.py` uses live,
so a backtest can't silently drift out of sync with what live trading
actually does. If you change the strategy in `signal_engine.py`, both live
trading and the backtest pick it up automatically.

Run it directly:
```bash
python backtest.py --start 2026-06-01 --end 2026-08-31 --out backtest_trades.csv
```
It prints a per-day summary and an overall total (trades, points, win
rate), and optionally saves the combined trade log as CSV.

**How it stays fast**: rather than one API call per stock per day (500
stocks × N days), it fetches each universe symbol's full intraday and daily
price history for the whole requested range **once**, chunked to respect
Fyers' actual documented per-request limits (confirmed via their docs and
community forum — 100 days per request for intraday resolutions, ~365 days
for daily), then slices out each day's opening-range candle and previous
close from that in memory. Total API calls end up roughly proportional to
universe size × chunks, not universe size × trading days — backtesting 5
days costs about the same as backtesting 60. It logs an estimated call
count and time before starting the (still slow, it's still rate-limited)
pre-fetch.

**Read this before trusting the numbers**:
- **No slippage, brokerage, STT, or spread** — real fills will be worse.
- **No NSE holiday calendar** — only weekends are skipped; a holiday just
  comes back with no shortlist for that day (logged, not a crash).
- **Survivorship bias** — it filters using *today's*
  `ind_nifty500list.csv` and `sector_index_map.csv`, so stocks since
  removed from the index, or reclassified, are backtested as if today's
  universe always applied. This tends to inflate apparent historical
  performance.
- A subtle bug I caught while building this, worth knowing about even
  though it's fixed: `SignalEngine.square_off_all()` originally always
  stamped end-of-day exits with the real current wall-clock time — fine
  live, wrong for a backtest (it would've shown the time you happened to
  run the backtest, not the historical trading day). It now takes an
  optional `at_time_str`, which `backtest.py` sets to the replayed day's
  own last-seen candle time; `main.py`'s live call site is unchanged
  (defaults to real "now" as before).

Tested end-to-end with synthetic multi-day candle data covering: sector
filtering feeding into the shortlist, the 20-candle volume-MA warm-up, a
BUY trigger, riding to EOD square-off with a correctly-timestamped exit, a
day with no qualifying stocks (shortlist empty, day skipped), and a symbol
with no returned data (skipped, not a crash). This isn't yet folded into
`test_updates.py` as a standalone test — worth adding there if you extend
this further.

## Backtesting an external daily stock-picks strategy

`backtest_strategy_picks.py` is a **different, separate tool** from
`backtest.py` above — don't confuse the two. `backtest.py` replays *this
project's own* opening-range/VWAP signal strategy. `backtest_strategy_picks.py`
instead backtests *any* daily stock-picking strategy you already have picks
for: give it a `strategy_picks.csv` with `date` and `stocks` (comma-separated
Fyers symbols) columns, and it simulates buying each day's picks at the open
and selling at the close, equal-weighted across capital, with configurable
slippage/brokerage — it doesn't know or care how the picks were chosen.

```bash
python backtest_strategy_picks.py   # edit the history_provider stub near the bottom first
```

It appends three columns back into the **same** CSV
(`daily_net_profit`, `daily_return_pct`, `stock_performance_details` as a
JSON string per stock) and prints the tear-sheet shown at the top of this
section. Plug in your active Fyers session via the stub at the bottom of
the file — either this project's own `FyersAuthManager`/`FyersDataService`
(recommended: already rate-limited and retrying), or a ~5-line adapter
around a raw `fyersModel.FyersModel()` instance you already have running
elsewhere; both are spelled out in the stub's comments.

**How the numbers are computed** (worth knowing before trusting them):
- **Equal weight is across that day's *tradable* picks, not the full list**
  — a pick with no candle for that date (holiday, data gap, delisting) or
  an illiquid/invalid one (zero volume, non-positive price, NaN) is
  dropped from that day's basket and logged with a reason, and the
  remaining picks split the *full* day's capital rather than leaving some
  sitting idle. If a day has zero tradable picks, it contributes exactly
  zero P&L rather than crashing.
- **Capital compounds day over day**: each day's allocation is based on
  that day's *starting* capital (yesterday's close), not the original
  initial capital every time.
- **Costs are applied to the execution price on each side**, not
  subtracted as a flat fee afterward — 0.05% per side means you effectively
  buy at `open × 1.0005` and sell at `close × 0.9995`. The `entry`/`exit`
  values written to `stock_performance_details` are still the *raw* open/close
  though, so you can see the real market prices separately from the
  cost-adjusted `return_pct`/`net_pnl`.
- **No whole-share lot rounding** — allocation is treated as continuous
  capital, not `floor(capital / price)` shares. Standard simplifying
  assumption for a return-based backtest at this level; flag it if you need
  lot-accurate P&L.
- **Sharpe Ratio** is annualized from the *daily* return series
  (`mean / sample_std × √252`, risk-free rate 0 by default — both the
  annualization factor and risk-free rate are function parameters if you
  want different ones), and reports `0.0` rather than crashing on fewer
  than 2 days of data or exactly zero volatility.
- **Max drawdown** and the equity curve itself are just `initial_capital`
  plus a running sum of `daily_net_profit` — valid specifically *because*
  this is intraday-only with no overnight exposure, so there's no
  mark-to-market step needed between days.

**Caching**: the whole point of pre-fetching per symbol rather than
per-symbol-per-day is API-call efficiency — `DailyPriceCache` fetches each
unique symbol's daily OHLC **once** for the full needed date range (chunked
via `config.DAILY_HISTORY_MAX_DAYS_PER_CHUNK`, same Fyers-documented limits
`backtest.py` uses) and writes it to a small CSV under
`cache/strategy_picks_daily_ohlc/` per symbol. A second run covering the
same symbols/date range makes **zero** new API calls — verified directly in
`test_backtest_strategy_picks.py`.

Tested with 12 cases: the full tear-sheet's numbers checked against an
independently hand-calculated (and numpy-cross-checked) 3-day scenario to
several decimal places — not just "doesn't crash" — plus the cost model,
equal-weight allocation, missing-candle reallocation, illiquid-candle
exclusion, a day with zero tradable picks, the CSV round-trip (including a
`stocks` cell containing a comma and a JSON field containing quotes,
re-parsed with the plain `csv` module, not just pandas, to confirm it's
genuinely valid CSV), duplicate-date rows not cross-contaminating each
other's results, and the caching behavior itself.


## Nifty news + EMA bias (in job_global_sentiment)

`global_sentiment.get_stock_news_ema_bias(yahoo_symbol, anthropic_api_key=...)`
is a new, general-purpose function: it combines a classic EMA(12) vs EMA(26)
crossover technical read with an LLM-scored read of that symbol's own recent
news headlines (fetched via `yfinance`'s `.news`), into one combined verdict
(`BULLISH` / `BEARISH` / `MIXED` / `NEUTRAL` / `UNKNOWN`). `job_global_sentiment()`
now calls it once for Nifty 50 (`^NSEI`) alongside the existing global-cues
score, printing both. Without `ANTHROPIC_API_KEY` set, the news half is
skipped automatically and it falls back to the EMA-only read - it still
runs either way.

It takes a Yahoo Finance symbol, not a Fyers one - use the included
`fyers_symbol_to_yahoo()` helper to convert (`NSE:TCS-EQ` -> `TCS.NS`) if
you want to point it at an individual stock later (e.g. the opening-range
shortlist) rather than just Nifty 50; that wiring isn't done here, only the
function itself and its use in `job_global_sentiment()`. Two things to
know: `yfinance`'s `.news` schema has shifted between versions and can
return an empty list even when there's real recent news - the function
handles this without raising, but check what you're actually getting for
your `yfinance` version before relying on it; and this is a second
heuristic layered on top of `score_global_bias()`'s global-cues score, not
a replacement for it - both are informational, neither gates a trade.

## Candle-level position sizing: the "weakness event" add/square-off rule

On top of the original single-candle exit, the engine uses one shared
detector for both adding to a position and stopping it out:

- **Weakness event** = two *consecutive* candles that each close below VWAP
  **and** each have volume above a fast 2-period volume average
  (`VOLUME_SMA_STOPLOSS_PERIOD` in `config.py`) — deliberately quicker to
  react than the 20-period average used for entries.
- **1st weakness event** on an open position → **add** to the position
  (`entry_type="ADD"` in the trade book) instead of exiting, up to
  `config.MAX_POSITION_ADDS` times (default 1).
- **Any weakness event after that** → **full square-off**: every open row
  for that symbol (the initial buy plus every add) closes together at the
  same price/time.

This was my best-effort read of a somewhat ambiguous instruction — if you
actually meant something different (e.g. add on a plain single close below
VWAP, and reserve the two-candle+volume rule purely for the exit), it's a
small, localized change in `signal_engine.SignalEngine._check_weakness_and_act`.
Set `MAX_POSITION_ADDS = 0` to disable adding entirely and stop out on the
very first weakness event instead.

## API rate limits

Every call `FyersDataService` makes goes through a shared `RateLimiter`
(`config.FYERS_MAX_CALLS_PER_SECOND`, default 8 — set a little under
whatever your account's actual documented ceiling is, not exactly at it).

More importantly, the opening-range scan no longer loops per-stock calls at
all: at 09:20:02 the exchange's cumulative "day so far" OHLC for each stock
*is* the 09:15 candle (only 5 minutes have traded), so
`opening_range_scanner.py` fetches it via `FyersDataService.get_quotes_batched()`
— one `/quotes` call per ~50 symbols (`config.QUOTES_BATCH_SIZE`), i.e.
roughly 10 calls for a 500-stock universe instead of ~1,000. `sector_scanner.py`
uses the same batched path. Double-check `QUOTES_BATCH_SIZE` and the quote
response field names (`open_price`, `high_price`, `low_price`, `lp`,
`prev_close_price`, `volume`) against your account before relying on this.

This fast path only holds true right around 09:20:02, though — if the scan
runs significantly later (a late-startup catch-up, see below),
`job_opening_range_scan()` in `main.py` detects this and automatically
switches to `scan_opening_range_accurate()`, which fetches the real
historical 09:15-09:20 candle per stock instead. That's back to one-ish
call per stock, so it's slower (bounded by the rate limiter — a minute or
two for a full universe), but it's the only way to get correct numbers once
"day so far" has drifted away from "the actual 09:15 candle."

## Why the timing lines up the way it does

- The 09:15 candle *is* the opening range (09:15–09:20). The scan for it
  fires at **09:20:02**, two seconds after it closes, to avoid racing the
  candle-close.
- The buy rule needs **two consecutive** post-opening-range candles to
  qualify. The first two candidate candles are 09:20–09:25 and 09:25–09:30 —
  so the earliest a BUY can trigger is right at the **09:30** candle close,
  which is exactly the "wait till 9:30 candle closed" checkpoint in the
  original brief.
- The volume moving average needs 20 five-minute candles of history before
  it means anything. Early in the day it will legitimately return `None`
  and the engine skips (not fakes) a signal until it has real history —
  this is a deliberate design choice, not a bug.

## Known limitations (please read before trusting the numbers)

- **Sector snapshot at 09:10 will likely show ~0% change.** NSE doesn't
  recompute any index's value (sectoral or otherwise) during the 09:00–09:15
  pre-open window — this is an exchange-side limitation, true whether you
  query the index symbol directly (as this module now does) or reconstruct
  it from constituents (as it used to). The scheduled 09:16 re-run (see
  `config.SECTOR_SCAN_TIME_POSTOPEN`) is where the real numbers show up.
  Full details are in `sector_scanner.py`'s docstring.
- **Not every symbol in `sector_index_map.csv` is guaranteed to resolve on
  your Fyers account** — particularly the seven G-Sec bond indices, which
  aren't always exposed by equity-focused data feeds. The code logs a
  warning and skips any symbol with no quote rather than failing the whole
  scan; check the logs once to see what's actually available to you.
- **REST polling, not tick-by-tick streaming.** The signal engine re-fetches
  the latest closed candle every 5 minutes via the historical-data endpoint
  rather than aggregating live ticks from the Fyers WebSocket feed. This is
  simpler and plenty for a 5-minute strategy, but it means the agent reacts
  right after each candle closes, not intra-candle.
- **Gain% is computed vs. previous close**, not open-to-close of the 09:15
  candle itself — i.e. it captures the whole move so far today (gap +
  intraday), which is the more common reading of "up by X%" in a pre-market
  scanner. Flip this in `opening_range_scanner.py` if you intended the
  latter.
- **The automated TOTP login is an unofficial, community-style flow** (see
  the big comment at the top of `fyers_service.py`). It can break if Fyers
  changes their login page; the manual fallback always works.
- No slippage, brokerage, STT, or spread is modeled in the paper P&L —
  real fills will be worse than the numbers in the trade book.

## Before you go live

1. **Paper-trade first**, for at least a few weeks across different market
   regimes, and sanity-check every logged BUY/EXIT against a chart.
2. **Add risk controls** the current scaffold intentionally leaves out:
   a hard stop-loss per trade, a max position size, a max number of
   concurrent positions, and a daily loss limit that halts new entries.
3. **Check the current SEBI/exchange rules for API-based retail algo
   trading** before placing live orders through the API — brokers,
   including Fyers, have been rolling out requirements (such as
   registering a unique algo/strategy ID) for API-driven order placement.
   Confirm the current requirements directly with Fyers before flipping
   `config.DRY_RUN = False`.
4. Only then wire `signal_engine.py`'s BUY/EXIT branches to
   `fyers.place_order(...)`, starting with tiny size.

## "Which AI should I use to build/customize this agent?"

- For **iterating on this codebase itself** (adding order placement,
  backtesting it against historical data, debugging a scheduling edge
  case, adding new filters) — an agentic coding tool such as Claude Code
  is the natural fit: it can run the script, read the Fyers docs alongside
  you, and make multi-file edits directly.
- For the **qualitative layer** (macro/geopolitical news sentiment, which
  isn't really a clean numeric time series) — that's what
  `global_sentiment.get_llm_news_sentiment()` demonstrates: pull a handful
  of overnight headlines and have Claude's API return a structured
  bullish/bearish read with a reason, which you can log alongside the
  price-based score from `score_global_bias()`.
- You don't need a different AI for each piece — the same model family can
  reasonably do both jobs; the split above is about *how* you use it
  (coding assistant vs. an API call embedded in the running agent), not
  about needing multiple vendors.
