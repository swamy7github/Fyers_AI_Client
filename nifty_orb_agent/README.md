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
| `sector_scanner.py` | 09:10 sector strength/weakness snapshot |
| `opening_range_scanner.py` | 09:20:02 top-5 opening-range gainer shortlist |
| `signal_engine.py` | VWAP + volume-MA(20) buy/sell logic, trade book, P&L |
| `main.py` | Wires it all together with APScheduler and starts the day |
| `data/ind_nifty500list.csv` | **You provide this** — Nifty 500 constituents (see below) |
| `data/sector_index_map.csv` | Sample symbol→sector mapping — expand it to your full universe |
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
   `data/ind_nifty500list.csv`. It needs at minimum a `Symbol` column.

5. **Expand `data/sector_index_map.csv`** to cover your full universe with
   a `symbol,sector` row per stock. The sample file only has a handful of
   illustrative rows per sector.

6. **Run it** on a trading day, ideally started a few minutes before 09:00 IST:
   ```bash
   python main.py
   ```
   Logs stream to the console and to `logs/orb_agent.log`. The day's trade
   book is written to `logs/trade_book_<YYYYMMDD>.csv`.

## Stop-loss, position sizing, and the "weakness event" rule

On top of the original single-candle exit, the engine now uses one shared
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

- **Sector snapshot at 09:10 is a proxy.** Exchange-computed sector indices
  don't update during the 09:00–09:15 pre-open window — only individual
  stocks get an indicative price. `sector_scanner.py` approximates a sector
  move by averaging its constituents' pre-open indicative prices vs.
  previous close. Full details are in the module's docstring.
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
