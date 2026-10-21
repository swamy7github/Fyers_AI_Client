# Nifty Opening-Range Breakout (ORB) Scanning Agent

A modular Python agent that, on a Fyers-connected NSE trading day, runs its
original/core equity strategy whenever `config.ENABLE_STOCK_TRADING = True`
(off by default — see "⚠️ Read this first" below):

1. **09:10** — snapshots sector strength/weakness across your mapped universe.
2. **09:20:02** — reads the 09:15 five-minute candle for your stock universe,
   filters for stocks up between 0.5% and 2.5%, and shortlists the top 5.
3. Every 5 minutes until **15:10** — checks each shortlisted stock's latest
   candle for an opening-range breakout (BUY) or a VWAP/volume breakdown (SELL),
   and logs paper trades with entry, exit, points captured and P&L%.
4. **15:10:30** — force-closes any still-open paper position and prints/saves
   a trade book.

Alongside (or instead of) equities, the same agent can run NIFTY/BANK NIFTY
index options and/or MCX commodities — each its own opt-in strategy, all
**off by default** (see "What's in the box" and the strategy-specific
sections below).

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
| `config.py` | All settings: credentials (via `.env`), file paths, timing, strategy thresholds. Four independent master switches, **all `False` by default**: `ENABLE_STOCK_TRADING` (the original/core equity strategy), `ENABLE_NIFTY_OPTIONS_TRADING`, `ENABLE_BANKNIFTY_OPTIONS_TRADING`, `ENABLE_MCX_COMMODITY_TRADING` — the agent trades nothing until at least one is turned on |
| `fyers_service.py` | Fyers login (auto TOTP + manual fallback) and a retrying, logged data wrapper |
| `global_sentiment.py` | Global bullish/bearish scoring + optional Claude-based news sentiment — TWO separate ticker/weight sets: `GLOBAL_TICKERS`/`SENTIMENT_WEIGHTS` (equity cues, drives equities/NIFTY/BANKNIFTY options) and `COMMODITY_GLOBAL_TICKERS`/`COMMODITY_SENTIMENT_WEIGHTS` (gold/silver/crude/copper/natural-gas/dollar-index, drives MCX commodities only) |
| `sector_scanner.py` | 09:10 / 09:16 sector strength/weakness snapshot, queried directly from NSE index symbols |
| `opening_range_scanner.py` | Filters the NSE equity universe by sector, then builds the top-5 opening-range gainer shortlist. `scan_symbols_accurate()` is the market-agnostic, explicit-symbol-list-in/shortlist-out core equities use (`commodity_engine.py` no longer shares this — MCX trades its full configured universe unfiltered, see "MCX commodities trading" below) |
| `signal_engine.py` | Direction-aware (LONG or SHORT) VWAP + volume-MA(20) candle logic — entry, weakness-event add/stop-loss, and broker position adoption (`adopt_broker_position`) are ALL candle-driven, no tick-level exit; `feed_candle()` is the shared replay/live entry point `backtest.py`, `bootstrap.py`, and `commodity_engine.py` all use (`options_engine.py` reuses this module's `StockTracker`/`TradeBook` building blocks directly instead, since its dual-instrument signal doesn't fit the one-symbol-drives-itself shape `feed_candle()` assumes). Also owns capital-based order sizing (`_size_order`, via `position_sizing.py`) and the single shared real-order gate (`place_or_log_order()` → `fyers_service.FyersDataService.place_order`, used by both this module's `_maybe_place_live_order` and `options_engine.NiftyOptionsEngine`) |
| `position_sizing.py` | Capital-based quantity sizing (`calculate_quantity`, `capital_per_slot`) shared by equities, options, and commodities — see "Capital-based position sizing" below |
| `options_engine.py` | Single-source, BOTH-SIDES index options engine: PRICE/VWAP/VOLUME signal all from the index future, watched for a breakout in EITHER direction every candle; the index itself is read only — fresh, at the moment an entry signal actually fires, never upfront — for its spot price to pick a strike (ITM/ATM/OTM, `config.OPTION_STRIKE_MODE`). BUY-ONLY execution on a single option: CE on an upside breakout, PE on a downside one, decided by the strategy's own entry criteria, not a pre-computed daily bias — same `NiftyOptionsEngine` class drives both NIFTY (`run_nifty_options_engine()`) and BANK NIFTY (`run_banknifty_options_engine()`) independently, see "NIFTY INDEX options trading" / "BANK NIFTY options" below. Off by default |
| `commodity_engine.py` | Applies the SAME signal_engine strategy, long or short, to the FULL `config.MCX_COMMODITY_UNIVERSE` directly — no scan, no gain/loss-band shortlist, direction from a commodity-specific global sentiment read — see "MCX commodities trading" below. Off by default |
| `trade_direction.py` | Tiny shared `TradeDirection` enum (LONG/SHORT) + `decide_trade_direction_from_sentiment()` — kept dependency-free so `signal_engine`, `opening_range_scanner`, and `bootstrap` can all import it without an import cycle |
| `bootstrap.py` | Market-phase detection + the late-startup catch-up pipeline: `bootstrap_and_catchup()`, historical replay, broker reconciliation — see "Late-startup catch-up" below |
| `live_tick_stream.py` | WebSocket tick feed wrapper — NOT currently used by `main.py` (no tick-level exit exists to feed); kept for a possible future use, see the module docstring |
| `main.py` | Wires it all together with APScheduler; calls `bootstrap_and_catchup()` once at startup, then runs the ongoing 5-min candle checks |
| `backtest.py` | Replays the same live strategy against historical data over a date range; run with `python backtest.py --start ... --end ...` |
| `backtest_strategy_picks.py` | Separate tool: backtests an external daily stock-picks CSV (buy-at-open/sell-at-close, equal-weight) — see "Backtesting an external daily stock-picks strategy" below |
| `test_backtest_strategy_picks.py` | Tests for the above, including hand-verified tear-sheet math |
| `test_signal_engine.py` | Tests for broker position adoption, the `is_backfill` gate, and capital-based sizing/qty/pnl_amount (`TestCapitalBasedPositionSizing`) (the candle-level entry/weakness logic they sit on top of is covered by `TestFeedCandle*` elsewhere in the same file) |
| `test_position_sizing.py` | Tests for `calculate_quantity`'s whole-lot rounding and `capital_per_slot`'s equal-split allocator |
| `test_options_engine.py` | Tests for the F&O symbol-master loader/column validation, ITM/ATM/OTM strike/expiry resolution (including the fewer-strikes-than-requested fallback), `NiftyOptionsEngine`'s single-source entry/exit arithmetic (hand-verified futures candle sequences, both directions), proof that the strike is resolved at signal time rather than engine construction, a same-day CE-then-PE "second leg" scenario, and the BANK NIFTY wiring (underlying-filtered symbol resolution from a mixed master, `run_banknifty_options_engine()`, and both engines running independently off one shared master) |
| `test_commodity_engine.py` | Tests for `fetch_mcx_universe_opening_ranges()` (every universe symbol tracked regardless of its price move; only a genuine data gap excludes one) and `run_mcx_commodity_engine()`'s per-tracker lot-size/capital wiring |
| `test_bootstrap.py` | Tests for historical replay, broker reconciliation, and a full end-to-end `bootstrap_and_catchup()` run |
| `test_live_tick_stream.py` | Tests for `live_tick_stream.py`'s message validation (now a no-op placeholder), without a real WebSocket |
| `test_trade_direction.py` | Tests for the sentiment-label → direction mapping |
| `test_opening_range_scanner.py` | Tests for the LONG/SHORT gap-band filter and sort order |
| `test_historical_sentiment.py` | Tests for the as-of-date sentiment fetch backtest.py uses for per-day direction |
| `test_backtest.py` | Tests for backtest.py's per-day direction switch, historical sector leaderboard, and prior-session volume seeding |
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

0. **`config.ENABLE_STOCK_TRADING` gate** (checked first, right after market
   phase is computed): if `False` (the default), all of the catch-up work
   below is skipped entirely and this returns immediately with no engine —
   but it still reports the REAL market phase computed in step 1, not a
   faked one, since NIFTY/BANKNIFTY options share this same NSE session and
   `main()`'s whole-process exit decision depends on an accurate phase
   regardless of whether equities themselves are enabled.
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
2. **Sector/breadth snapshot, direction decision, gap screen**:
   `sector_scanner.get_index_snapshot()` / `compute_market_breadth()`
   (informational, always run), then `decide_todays_direction()` — global
   sentiment picks LONG or SHORT for the day (see "Long and short" below);
   an unclear read exits here with no engine, before spending API calls on
   a universe scan. Otherwise `opening_range_scanner.scan_opening_range_accurate(direction=...)`
   builds the shortlist (gap-up for LONG, gap-down for SHORT) — always the
   historical-candle path here, never the quotes shortcut (which is only
   valid within ~3 minutes of 09:20:02 and would silently return wrong
   numbers at any other time).
3. **Volume-MA seeding** (`fetch_and_seed_volume_windows`, BEFORE replay):
   pre-fills the 20-period volume MA — shared by both entries and the
   weakness/exit detector — from the prior trading session's real candles
   — without this, the volume MA needed for every entry (and exit) stays
   `None` for the first 20 candles fed to a fresh
   tracker, silently pushing the earliest possible entry to ~11:00 AM
   instead of the documented ~09:30. See "Volume-MA warm-up" below for the
   full story — this was a genuine, previously-unnoticed bug.
4. **Historical replay** (`_replay_intraday_history`): every fully-closed
   5-minute candle from 09:15 up to now, for every shortlisted symbol, fed
   through the *exact same* `SignalEngine.feed_candle()` used for live
   candles — same VWAP accumulation, same (now pre-warmed) volume-MA, same
   2-consecutive-candle BUY/weakness-event logic — with `is_backfill=True`.
   That flag does exactly one thing: it stops `_maybe_place_live_order()`
   from ever placing a real order for a signal reconstructed from history
   (the entry price has likely moved on by now) — it does *not* change
   whether the signal fires, so a reconstructed BUY still opens
   `position_open=True` and is still managed by the same candle-level
   weakness detector going forward from this point (there is no tick-level
   exit any more — see "Stop-loss, target, and trailing-SL" below).
5. **Broker reconciliation** (`_reconcile_broker_state`): fetches the
   broker's actual open positions and adopts/clears local state to match —
   see "Broker position sync" below.
6. Returns a `BootstrapResult` (phase, populated `SignalEngine` or `None`,
   sector leaderboard, market breadth) that `main.py` hands off to the
   ongoing 5-minute candle-check scheduler, which is now the ONLY thing
   managing open positions.

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
  price and quantity. It's managed by the same candle-level weakness
  detector as any other open position from the next candle check onward —
  there is no tick-level protection, so know that the first check can be up
  to `CANDLE_INTERVAL_MINUTES` (5) away, not immediate.
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

## Live hand-off (live_tick_stream.py) — not currently used

Earlier revisions started `LiveTickStream` here, a thin wrapper around
Fyers' WebSocket tick feed (`fyers_apiv3.FyersWebsocket.data_ws`) that
routed every tick for a tracked symbol into a tick-level stop-loss/target/
trailing-SL check, reacting faster than a 5-minute candle close.

**That tick-level exit has been removed.** Stop-loss is now purely
candle/VWAP-driven: a position only closes once 2 consecutive 5-minute
candles confirm the move against it (see "Stop-loss, target, and
trailing-SL" below), the same conviction bar entry requires in its favor.
`main.py` no longer constructs or starts a `LiveTickStream` — there is no
consumer left for tick data. The class itself is still there, fully
working and fully testable via `on_tick_override` (see
`test_live_tick_stream.py`), in case a future need for live tick data comes
up (a live LTP/unrealized-P&L display, say); `_handle_tick()` is currently
a no-op placeholder for that reason. Verify the tick message field names
(`symbol`, `ltp`) against your installed `fyers_apiv3` version before
reviving this — broker WebSocket message shapes are exactly the kind of
thing that changes between SDK versions.

## Long and short: sentiment-gated direction

The engine trades **one direction per day**, chosen from global sentiment
(`global_sentiment.score_global_bias()`, via `bootstrap.decide_todays_direction()`):

| Global sentiment | Direction | Shortlist screen |
|---|---|---|
| `BULLISH` | LONG | gap-**up** in `(MIN_GAIN_PCT, MAX_GAIN_PCT]` (0.5%–2.5%), biggest gainers first |
| `BEARISH` | SHORT | gap-**down** in `[-MAX_LOSS_PCT, -MIN_LOSS_PCT)` (−2.5% to −0.5%), biggest losers first |
| `NEUTRAL` / `MIXED` / `UNKNOWN` | none | **no new positions that day** by default |

The neutral row is a deliberate, conservative default: unclear sentiment means
sitting out rather than guessing a side. Set `config.ALLOW_LONG_ON_NEUTRAL` or
`ALLOW_SHORT_ON_NEUTRAL` to `True` to fall back to one direction instead (LONG
wins if both are set). The decision is made in two places that share the same
function: `bootstrap_and_catchup()` for a mid-session start, and
`main.job_opening_range_scan()` for a normal 09:20:02 start (which re-reads
sentiment fresh rather than reusing the pre-market print, since it can move in
between). `job_global_sentiment()` also prints today's direction for visibility.

**The short side is an exact mirror of the long side**, not new strategy logic:

| | LONG | SHORT |
|---|---|---|
| Breakout level | above opening-range **high** | below opening-range **low** |
| Entry (2 consecutive candles) | close > OR high, > VWAP, volume > volMA(20) | close < OR low, < VWAP, volume > volMA(20) |
| Weakness event (against you) | close **below** VWAP, volume > volMA(20) | close **above** VWAP, volume > volMA(20) |
| Entry / add / exit orders | BUY / BUY / SELL | SELL / SELL / BUY |
| P&L per trade | exit − entry | **entry − exit** |

It runs through the same single code path (`SignalEngine`, with a `direction`
field on each `StockTracker` and each trade-book row), so live trading,
`backtest.py` (`--direction SHORT`), and the catch-up replay all pick up both
sides automatically. The trade-book sign convention is covered by direct tests
in `test_signal_engine.py` (a short that falls is a *profit*, one that rises is
a *loss*) since inverting it would silently flip every short trade's reported P&L.

Broker sync is direction-aware too: `adopt_broker_position()` takes direction
from the sign of the broker's `netQty` (negative = short), and **broker truth
wins** — if the broker says you're short a stock the engine was building long
today (a manual trade, or a leftover position), the tracker is corrected to
match rather than managing a real short under long-side stop logic.

**Things to know before trusting the short side:**
- **Adding to a losing position is riskier short than long.** The weakness-event
  rule adds to the position on the first adverse event (`MAX_POSITION_ADDS=1`).
  For a long, a loser is bounded at zero; a short's loss is theoretically
  unbounded, so averaging *up* into a rising stock deserves more caution.
  Setting `MAX_POSITION_ADDS = 0` makes shorts (and longs) stop out on the first
  adverse event instead of adding.
- **Not every stock can be shorted intraday.** Some Nifty 500 names sit in
  segments or bans where your broker won't allow a same-day sell-first order
  (e.g. trade-for-trade series, F&O-ban stocks). The screen doesn't know this —
  check your broker's rules; a shortlisted stock that can't be shorted will fail
  at order time when `place_order()` actually reaches the broker.
- **Sentiment is a blunt gate.** It's built from the *last completed session's*
  change in S&P/Nasdaq/Dow/Asian indices, VIX, crude and the dollar index via
  `yfinance` — a rule-of-thumb weighting, not a fitted model, and Asian markets
  may be mid-session when it's read. It says nothing about India-specific news.
- **Tested on synthetic data only.** The short logic (and the options/MCX
  engines, which reuse it) is verified against hand-computed scenarios and
  faked data services, not against real Fyers order/options-chain/MCX
  behaviour. Paper-trade (`DRY_RUN=True`) every market you enable before
  anything else.

## Stop-loss, target, and trailing-SL: none — exit is purely candle-driven

Earlier revisions had a separate, tick-level stop-loss/target/trailing-SL
(`config.STOP_LOSS_PCT` / `TARGET_PCT` / `TRAILING_SL_PCT`, checked on every
tick via `SignalEngine.check_tick_for_exit()`). **That has been removed.**
There is no fixed-percentage exit and no tick-level check of any kind any
more. The ONLY way a position closes before EOD is the candle-level
weakness-event detector described above: 2 consecutive 5-minute candles
confirming the move against the position, with volume above the same
20-period volume MA entry requires — i.e. stop-loss is dynamic and
VWAP/volume-driven, not a static % away from entry. See the "Candle-level
position sizing" section below for the add/square-off rule itself, and
`signal_engine.py`'s module docstring for the full picture.

`config.DRY_RUN=True` (default) logs every entry/add/exit instead of placing
it. Flipping it to `False` now reaches a REAL broker call —
`fyers_service.FyersDataService.place_order()` (a market order, `productType`
`"INTRADAY"`, DAY validity) — via `SignalEngine._maybe_place_live_order()`,
which is still the single gate every order of any kind routes through
(entries, adds, weakness-event exits, and EOD square-offs, for every market
this agent trades). A failed broker call is logged loudly and does NOT crash
the candle-check loop (other symbols' signals still need evaluating that
tick) — see that method's docstring. Paper-trade (`DRY_RUN=True`) for a good
stretch before ever setting this to `False` — see "Before you go live" below.

## Capital-based position sizing

Every order this agent places — equities, options, or commodities — is sized
from a configured capital allocation, via `position_sizing.calculate_quantity()`:
buy as many WHOLE shares/lots as the allocated capital affords at the entry
price, rounding DOWN, never up into a partial-lot overspend. This replaced
the old flat `config.DEFAULT_ORDER_QTY = 1` placeholder (kept only as a
last-resort fallback for a mis-configured `capital_allocated <= 0`, not the
normal path any more).

- **Equities**: `config.TRADING_CAPITAL` (default ₹200,000) is split equally
  across `config.TOP_N_STOCKS` (default 5) concurrent slots —
  `config.CAPITAL_PER_TRADE = TRADING_CAPITAL / TOP_N_STOCKS` (₹40,000/slot
  by default). A weakness-event ADD spends this SAME per-slot amount again
  (it's a fresh, separately-justified signal, not a top-up of the original
  size), so a stock that adds once can use up to 2x its slot's share.
- **NIFTY options**: `config.NIFTY_OPTIONS_CAPITAL` — its own, independent
  bucket (see "NIFTY INDEX options trading" below).
- **BANK NIFTY options**: `config.BANKNIFTY_OPTIONS_CAPITAL` — likewise its
  own, independent bucket, separate from NIFTY options' (see "BANK NIFTY
  options" below).
- **Commodities**: `config.MCX_CAPITAL`, split equally across however many
  commodities actually make the shortlist that day (not a fixed split
  across the whole configured universe, so an under-filled shortlist
  doesn't leave capital sitting idle) — see "MCX commodities trading" below.

These four buckets are deliberately SEPARATE, not drawn from one shared
pool — running more than one strategy at once would otherwise silently
double- (or triple-) spend the same real money. If you only have ONE real
capital pool, divide it across `TRADING_CAPITAL` / `NIFTY_OPTIONS_CAPITAL` /
`BANKNIFTY_OPTIONS_CAPITAL` / `MCX_CAPITAL` yourself, and/or leave the
strategies you're not funding disabled (`ENABLE_STOCK_TRADING` /
`ENABLE_NIFTY_OPTIONS_TRADING` / `ENABLE_BANKNIFTY_OPTIONS_TRADING` /
`ENABLE_MCX_COMMODITY_TRADING` are **all four `False` by default** — the
agent trades nothing out of the box until you turn at least one on).

If a calculated quantity comes back 0 (the allocated capital can't afford
even one share/lot at the current price), the engine logs a loud warning and
**skips that entry/add entirely** — it never opens a position at qty=0, and
a skipped ADD does not count as a square-off (the existing position just
stays open at its current size).

`TradeBook` rows now also carry `qty` (how many shares/lots that specific
row transacted) and `pnl_amount` (rupee P&L = points × qty, alongside the
existing `points`/`pnl_pct`), and `TradeBook.summary()` reports a
`total_pnl_amount` across all closed rows, not just points captured.

## NIFTY INDEX options trading (`options_engine.py`) — off by default

**Single-source signal, BOTH-SIDES, single-leg execution** — the entry/exit
*signal* comes from ONE instrument (the index future) that is never itself
traded; a separate option contract is what's actually bought/sold, and the
index itself is read only to pick which strike that is — fresh, at the
moment an entry signal actually fires, never upfront. Enable with
`config.ENABLE_NIFTY_OPTIONS_TRADING = True` — **read `options_engine.py`'s
module docstring in full first**, it covers these load-bearing design
decisions in detail:

1. **PRICE/VWAP condition AND VOLUME condition are BOTH read from
   `config.NIFTY_FUTURES_SYMBOL`'s** (the current front-month NIFTY future)
   own 09:15 opening-range candle, its own running VWAP, and its own
   20-period volume MA — watched for a breakout in EITHER direction, every
   candle: 2 consecutive candles closing above the opening-range HIGH *and*
   above VWAP *and* with volume above its MA (upside), or below the
   opening-range LOW *and* below VWAP *and* with volume above its MA
   (downside). **The index itself typically reports ZERO volume in Fyers'
   feed** (it isn't a tradable instrument, so there's nothing to sum up a
   real VWAP or volume MA from — a VWAP computed off zero volume silently
   collapses to 0) — the future is used for the whole signal specifically
   because of this. Verify what your own `NIFTY_INDEX_SYMBOL` quote/candle
   actually reports before pointing the signal back at the index.
2. **`config.NIFTY_INDEX_SYMBOL` is read for exactly one thing**: its spot
   LTP — fetched FRESH at the moment an entry signal actually fires, not
   back at today's ~09:20 engine setup — to resolve which strike to buy
   (ITM/ATM/OTM, see point 6). It plays no part in the entry/exit decision
   above.
3. **Entry fires when EITHER side's condition holds** on 2 consecutive
   candles → an upside breakout buys a CALL (CE), a downside one buys a PUT
   (PE) — **decided by the strategy's own entry criteria at the moment it
   fires, not by a pre-computed daily bias any more.** Because both sides
   are watched all day, this engine can legitimately take a second,
   differently-resolved leg later the same day if it exits one side and the
   other side's criteria later confirms (e.g. a CE in the morning, a PE
   later if the futures genuinely reverse). **Exit** is the mirrored
   condition on the same instrument (future closing back across VWAP with
   volume above its MA), keyed off whichever side THAT open leg actually
   entered from — the whole position closes at once; there's no separate
   add-on step here (unlike equities/commodities), since doubling into an
   already-leveraged option contract on a single weakness event wasn't
   asked for and is a different risk than doing so for an equity.
4. **BUY-ONLY**, same reasoning as the previous revision of this feature:
   risk capped to premium paid, never writes/sells an option naked.
5. **Direction for the day** reuses the SAME sentiment pipeline as equities
   (`bootstrap.decide_todays_direction()`, called once and passed to both
   `job_opening_range_scan()` and `job_nifty_options_scan()` in `main.py`),
   but now only to gate the opening-range "closing strength" quality filter
   below (a per-day go/no-go check, same mechanism equities/MCX use) — it no
   longer pre-selects CE vs PE or a strike (see point 3).
6. **Strike, expiry, lot size, and the exact Fyers trading-symbol string of
   the OPTION leg are resolved from Fyers' own published F&O symbol master
   file**, never hard-coded — NIFTY's expiry day, lot size, and strike gap
   have all changed more than once under SEBI/exchange rule changes.
   Download Fyers' NSE F&O instrument master CSV yourself (check their
   current API docs for the link — it has moved before), keep it
   refreshed, and point `config.NIFTY_FO_SYMBOL_MASTER_CSV` at your local
   copy. `config.NIFTY_FO_MASTER_COLUMNS` is a **best-effort guess** at
   that file's column names — print the real file's `df.columns` once and
   correct this dict before trusting it; a wrong mapping fails loudly
   rather than silently resolving the wrong contract.
   `config.OPTION_STRIKE_MODE` (default `"OTM"`, shared with BANKNIFTY
   options below) picks **ITM**, **ATM**, or **OTM** — `config.
   NIFTY_OTM_STRIKES_AWAY` (default 1) then controls how many strikes away
   from the at-the-money strike, in whichever direction that mode implies
   (ignored entirely for `"ATM"`, which always takes the single nearest
   strike).
7. **`config.NIFTY_FUTURES_SYMBOL` is a plain string you must roll
   yourself** each month (unlike the option, it is NOT resolved from the
   symbol master here) — e.g. `"NSE:NIFTY25DECFUT"` is only valid for that
   one expiry cycle.

Position sizing: `config.NIFTY_OPTIONS_CAPITAL` (own bucket, see above) /
the resolved option contract's own lot size (read from the symbol master,
not guessed). Scheduling: `job_nifty_options_scan()` runs at the same
`OPENING_RANGE_SCAN_TIME` as equities (the future's own opening-range candle
is available at the same time); squares off alongside equities at
`SQUARE_OFF_TIME` (same NSE session).

### BANK NIFTY (NIFTYBANK) options — the exact same engine, off by default

`options_engine.NiftyOptionsEngine` isn't hard-wired to NIFTY — every place
it previously read `config.NIFTY_FUTURES_SYMBOL` directly now takes it as a
constructor/function parameter instead, with NIFTY's config value as the
default (`config.NIFTY_INDEX_SYMBOL`/`BANKNIFTY_INDEX_SYMBOL` are passed
separately, straight into `resolve_option_symbol()`, since they're only
ever used for the strike-selection spot, not the engine itself).
`run_banknifty_options_engine()` is a thin wrapper that calls the exact same
underlying logic (`run_index_options_engine()`) with Bank Nifty's own values
substituted in, so **every rule in the section above applies verbatim** —
same single-source signal shape, same ITM/ATM/OTM-strike/BUY-ONLY/
no-add-on-step decisions, same shared sentiment pipeline for the
opening-range quality gate — just pointed at a different index/future/strike
universe:

- Enable independently with `config.ENABLE_BANKNIFTY_OPTIONS_TRADING = True`
  — it runs as a completely separate engine instance from NIFTY options
  (own `NiftyOptionsEngine` object, own trade book, own CSV
  `*_banknifty_options.csv`), so you can run NIFTY options, BANKNIFTY
  options, both, or neither, independently.
- `config.BANKNIFTY_INDEX_SYMBOL` (default `"NSE:NIFTYBANK-INDEX"`) —
  **verify this exact string** against your own Fyers symbol
  master/quote before enabling; exchange symbol strings do change.
- `config.BANKNIFTY_FUTURES_SYMBOL` — same manual-monthly-roll caveat as
  `NIFTY_FUTURES_SYMBOL` above (plain string, not master-resolved).
- `config.OPTION_STRIKE_MODE` is **shared** with NIFTY options (one knob,
  both engines) — `config.BANKNIFTY_OTM_STRIKES_AWAY` (default 1) is its own,
  independently configurable strikes-away count, since BANKNIFTY's strike
  gap (100) is wider than NIFTY's (50).
- `config.BANKNIFTY_OPTIONS_CAPITAL` — its own, independent capital
  bucket, same reasoning as `NIFTY_OPTIONS_CAPITAL`.
- **Shares** `config.NIFTY_FO_SYMBOL_MASTER_CSV`/`NIFTY_FO_MASTER_COLUMNS`
  with NIFTY options — Fyers' F&O symbol master is one combined file
  covering every NSE underlying, so `resolve_option_symbol()` just
  filters that same file's underlying column for `"BANKNIFTY"` rows
  instead of `"NIFTY"` ones (`underlying="BANKNIFTY"` is passed through
  internally — you don't need to do anything extra here beyond making sure
  your downloaded master file actually has BANKNIFTY rows, which Fyers'
  combined NSE F&O master does).
- Scheduling mirrors NIFTY options exactly: `job_banknifty_options_scan()`
  runs at the same `OPENING_RANGE_SCAN_TIME`, is checked every candle
  alongside equities/NIFTY-options in `job_check_signals()`, and squares
  off alongside them in `job_square_off_and_report()`.

If you only have ONE real pool of money, divide it across
`TRADING_CAPITAL` / `NIFTY_OPTIONS_CAPITAL` / `BANKNIFTY_OPTIONS_CAPITAL` /
`MCX_CAPITAL` yourself and only enable the strategies you're actually
funding — same caveat as every other capital bucket in this repo.

## MCX commodities trading (`commodity_engine.py`) — off by default

Applies the SAME ORB + VWAP + volume-MA strategy directly to MCX commodity
**futures** contracts (not commodity options — thin liquidity on most
strikes, and futures keep the equities mapping exact: one tradable
instrument per commodity, its own continuous OHLCV series, no strike/expiry
selection step). Unlike options, this trades long AND short — today's
sentiment-derived bias is used as-is, exactly like equities. Enable with
`config.ENABLE_MCX_COMMODITY_TRADING = True` after reading
`commodity_engine.py`'s module docstring and verifying every value below.

- **Trades the FULL `config.MCX_COMMODITY_UNIVERSE` directly — no scan, no
  shortlist.** MCX has nothing equivalent to the NSE-500-plus-sector-filter
  screen equities use, and doesn't need one:
  `config.MCX_COMMODITY_UNIVERSE` IS the trading universe, an explicit,
  short, hand-maintained list of `{symbol, lot_size}`. Every symbol in it
  is tracked every day — there is no sector step and no gain/loss-band
  shortlisting (unlike equities' `opening_range_scanner.load_universe()` +
  `scan_opening_range_accurate()`); `commodity_engine.fetch_mcx_universe_opening_ranges()`
  fetches each symbol's own opening-range candle directly and a symbol is
  only ever left out for a genuine data-availability gap (no candle yet
  today), never because of its price move.
- **Direction comes from a COMMODITY-specific sentiment read, not the
  equity one.** `bootstrap.decide_todays_commodity_direction()` scores
  `global_sentiment.COMMODITY_GLOBAL_TICKERS` (gold, silver, WTI/Brent
  crude, natural gas, copper, the dollar index) via
  `score_commodity_global_bias()` — a completely separate read from
  `decide_todays_direction()`'s equity-cue sentiment (S&P 500, Nasdaq, VIX,
  …) that drives equities/NIFTY/BANKNIFTY options. The two can (and
  sometimes will) point in different directions on the same day — that's
  intentional, since MCX trades against global commodity prices, not global
  equity indices. See `global_sentiment.py`'s `COMMODITY_SENTIMENT_WEIGHTS`
  comment for the weighting rationale (and its stated simplifications).
- **`symbol` values roll monthly/bi-monthly** — e.g. a `...DECFUT` symbol is
  only valid for one expiry cycle — update this list yourself before each
  contract rolls.
- **`lot_size` values shipped in `config.py` are illustrative placeholders**
  (approximate, commonly-cited MCX contract sizes) — VERIFY every one
  against MCX's current contract specifications before trusting them for
  real position sizing; MCX revises lot sizes too, and a wrong lot size
  here directly mis-sizes every order for that commodity.
- **MCX's session runs well past NSE's** (`config.MCX_MARKET_OPEN_TIME` /
  `MCX_MARKET_CLOSE_TIME`, currently placeholder-set to 09:00–23:30 IST) —
  verify current timings on MCX's own site/circulars, they are not fixed.

Position sizing: `config.MCX_CAPITAL` split equally across whichever
universe symbols actually had an opening-range candle available today (see
"Capital-based position sizing" above) — normally the whole universe —
each tracker's `lot_size` taken from `config.MCX_COMMODITY_UNIVERSE`.
Scheduling: `job_mcx_commodity_scan()` runs at
`config.MCX_OPENING_RANGE_SCAN_TIME` (mirrors the equity opening-range job,
just later in the day); squares off separately, much later, at
`config.MCX_SQUARE_OFF_TIME` via its own `job_mcx_square_off_and_report()`
job — `job_check_signals()`'s own cron range widens automatically to cover
MCX's later close whenever `ENABLE_MCX_COMMODITY_TRADING` is `True` (see
`main()` in `main.py`).

Trade books for options and MCX are saved to their own CSVs
(`trade_book_YYYYMMDD_options.csv` / `..._mcx.csv`), separate from the
equity trade book, so P&L across the three markets is never conflated.

## Backtesting

`backtest.py` replays the exact same strategy — unmodified — against
historical Fyers data. It doesn't reimplement the rules: `SignalEngine` has
a public `feed_candle()` method that lets historical candles be pushed
through the identical VWAP/volume/weakness-event logic `main.py` uses live,
so a backtest can't silently drift out of sync with what live trading
actually does. If you change the strategy in `signal_engine.py`, both live
trading and the backtest pick it up automatically.

**Direction is decided fresh for each day**, from historical global
sentiment as of that day's pre-market (`bootstrap.decide_direction_for_date()`
— the same function, same `ALLOW_LONG_ON_NEUTRAL`/`ALLOW_SHORT_ON_NEUTRAL`
fallback policy, that live trading uses), not one fixed side for the whole
range. This matters for the backtest to actually mean something: the
deployed strategy switches between LONG and SHORT daily, so replaying one
fixed direction across a multi-month range tests a *different, simpler*
strategy than the one that runs live. A day with no clear sentiment read is
skipped entirely — zero trades that day — matching live exactly.

```bash
python backtest.py --start 2026-06-01 --end 2026-08-31 --out backtest_trades.csv
```
Pass `--direction LONG` or `--direction SHORT` to override this and force
one fixed side across the whole range instead — useful for deliberately
asking "what if this had always traded long", but it's now an explicit
opt-in, not the default.

It also computes and logs each day's **sector leaderboard** — the
historical counterpart to `sector_scanner.get_sector_leaderboard()`,
computed from the same pre-fetched daily candles rather than live quotes
(there's no "live quote" for a historical date) — for parity with what the
live system reports each morning. Available per-day in the returned
`results["sector_leaderboards"]` dict, and a top-3 preview is logged for
each day so a long backtest's console output isn't flooded with full
tables.

It prints a per-day summary (now including that day's `direction` and
`sentiment` alongside trade count/points/win rate) and an overall total,
and optionally saves the combined trade log as CSV.

**How it stays fast**: rather than one API call per stock per day (500
stocks × N days), it fetches each universe symbol's full intraday and daily
price history for the whole requested range **once**, chunked to respect
Fyers' actual documented per-request limits (confirmed via their docs and
community forum — 100 days per request for intraday resolutions, ~365 days
for daily), then slices out each day's opening-range candle and previous
close from that in memory. Total API calls end up roughly proportional to
universe size × chunks, not universe size × trading days — backtesting 5
days costs about the same as backtesting 60. The intraday fetch range is
also extended `INTRADAY_SEED_LOOKBACK_DAYS` (7) calendar days before
`--start` — purely so day 1 has real prior-session candles to seed its
volume-MA window from (see "Volume-MA warm-up" below) — at zero extra API
cost, since it's sliced from the same pre-fetched data every other day
already uses. It logs an estimated call count and time before starting the
(still slow, it's still rate-limited) pre-fetch; the per-day historical
sentiment fetch adds a separate, smaller burst of `yfinance` calls (one set
per day being backtested) that isn't part of that estimate.

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
- A second, more significant bug caught later and now fixed (see "Volume-MA
  warm-up" below): every backtested day used to start with a cold,
  completely empty volume-MA window, meaning no entry could fire before
  roughly 100 minutes into each simulated day — silently understating
  activity in the first couple of hours of every single backtested day. Any
  backtest results generated before this fix should be treated as
  unreliable and re-run.

Tested (`test_backtest.py`, `test_historical_sentiment.py`) covering: the
per-day direction switch (a bullish day trades LONG, a bearish day trades
SHORT, a neutral day is skipped), the `--direction` override correctly
bypassing the sentiment fetch entirely, the historical sector leaderboard's
arithmetic, and — directly — that prior-session seeding lets a backtested
day's entry fire on just 2 replayed candles rather than needing 22. Plus the
original end-to-end coverage: sector filtering feeding into the shortlist,
a BUY trigger, EOD square-off with a correctly-timestamped exit, a day with
no qualifying stocks, and a symbol with no returned data.

## Volume-MA warm-up and prior-session seeding

**A real bug, caught by close review of the actual code rather than its
documentation**: `StockTracker.volume_window` starts completely empty every
time a `SignalEngine` is constructed — live, catch-up, or per-backtested-day
— and the 20-period volume MA required for every entry (`volume_ma()`)
returns `None` until 20 candles have been fed. Since an entry requires
`vol_ma_slow is not None`, that meant **no entry could fire before candle
#22** (20 to warm up the window, plus the 2 consecutive qualifying candles)
— roughly **11:00–11:10 AM**, not the documented "earliest entry at the
09:30 candle close." This silently undercut the strategy every single day,
live and in every backtest run before this fix.

The fix, `signal_engine.seed_volume_windows_from_candles()`, pre-fills both
the slow (20-period) and fast (2-period) volume windows from the prior
trading session's real candle volumes, so the window is already warm before
a single candle of the new session has been fed:

- **Live** (`bootstrap.fetch_and_seed_volume_windows()`, called in both
  `bootstrap_and_catchup()` before replay and `main.job_opening_range_scan()`
  before the live hand-off): fetches the most recent trading session(s)
  before today via `FyersDataService.get_candles_bulk()` — one bulk call per
  lookback day actually needed, looking back up to 7 calendar days
  (skipping weekends, not NSE holidays — same known limitation as
  elsewhere), stopping once 20 candles are collected or the budget runs
  out. Never raises — a fetch failure degrades to "no seed, warms up live"
  rather than blocking engine construction.
- **Backtest** (`backtest.py`): slices the needed candles directly out of
  data it already pre-fetched (see `INTRADAY_SEED_LOOKBACK_DAYS` above) —
  zero extra API calls.

Both paths call the same pure `seed_volume_windows_from_candles()` function,
so there's exactly one place this logic lives. A symbol with no prior-session
data available at all (a brand-new listing, or a data source that doesn't
support the bulk fetch) degrades gracefully to the original behavior —
starts empty, warms up live over the first 20 candles — rather than
crashing.

Tested directly in `test_signal_engine.py` (the pure seeding function, plus
a before/after pair proving a BUY now fires on just 2 live candles instead
of 22) and `test_bootstrap.py` (the live multi-day lookback logic: weekend
skipping, falling back to an earlier day when the first doesn't have enough
candles, and a data-service failure on one day not blocking the others).

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
  **and** each have volume above the **same 20-period volume MA**
  (`VOLUME_MA_PERIOD` in `config.py`) used for entries — exit now requires
  the identical volume conviction as entry, on the way out as well as the
  way in.
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
   regimes, and sanity-check every logged BUY/EXIT (and its logged `qty=`)
   against a chart.
2. **Add risk controls** the current scaffold intentionally leaves out:
   a hard stop-loss per trade, a max position size, a max number of
   concurrent positions, and a daily loss limit that halts new entries.
   Capital-based sizing (see "Capital-based position sizing" above) bounds
   how much any ONE trade can spend, but does NOT cap your total realised
   loss across a bad day the way a daily loss limit would.
3. **Check the current SEBI/exchange rules for API-based retail algo
   trading** before placing live orders through the API — brokers,
   including Fyers, have been rolling out requirements (such as
   registering a unique algo/strategy ID) for API-driven order placement.
   Confirm the current requirements directly with Fyers before flipping
   `config.DRY_RUN = False`.
4. **`fyers_service.FyersDataService.place_order()` is a real, live order
   call** once `config.DRY_RUN = False` — there is no separate "wire it up"
   step left to do. Start with a tiny `TRADING_CAPITAL` and only ONE
   strategy enabled (`ENABLE_STOCK_TRADING`, `ENABLE_NIFTY_OPTIONS_TRADING`,
   `ENABLE_BANKNIFTY_OPTIONS_TRADING`, and `ENABLE_MCX_COMMODITY_TRADING` are
   **all four `False` by default** — remember to turn at least ONE on, or
   the agent will run but trade nothing) the first time you flip `DRY_RUN`,
   and watch the logs closely.
5. If you do enable options or MCX, re-read those two sections above in
   full first — both depend on data (a current F&O symbol master, current
   MCX lot sizes/contract symbols) this repo cannot keep current for you.

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
