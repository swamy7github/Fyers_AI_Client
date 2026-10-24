"""
backtest_options.py
---------------------
Replays NIFTY, BANKNIFTY, and MCX commodity OPTIONS signals against historical
data, one past trading day at a time, and ALWAYS writes every simulated trade
to a CSV file automatically (not an opt-in flag - see save_trades_csv()/the
__main__ block's --out handling). For every signal, resolves and prices BOTH
the current-WEEK expiry contract AND the current-MONTH expiry contract as two
separate, independently-capitalised legs, so you can compare how a weekly vs.
a monthly contract would each have performed on the exact same entries/exits.

Run with no arguments at all to backtest just TODAY:

    python3 backtest_options.py

That's the common case this was built for: run it any time after today's
candles have formed (i.e. after market close, or the next morning) and it
replays today's NIFTY/BANKNIFTY/MCX options signals end-to-end and drops the
resulting trade log in logs/options_backtest_<date>.csv. Running it DURING
market hours instead replays whatever candles exist up to that moment (a
partial day) - still useful as a progress check, just not the final word on
today until the session closes.

Why this is a SEPARATE module from backtest.py
-----------------------------------------------
backtest.py's equity/MCX-futures replay feeds historical candles straight
into signal_engine.SignalEngine - no special-casing needed there because the
SIGNAL instrument and the TRADED instrument are the exact same thing, so the
replayed candle's own close IS the fill price.

Options are different: options_engine.NiftyOptionsEngine's signal comes
entirely from the FUTURES symbol's own candles, but the instrument actually
bought/sold is a DIFFERENT, separately-priced option contract, resolved at
signal time from spot and fetched live via data_service.get_quotes() -
there's no "fill price" sitting in the futures candle being replayed. This
module makes options_engine.NiftyOptionsEngine (completely UNMODIFIED - see
_BacktestQuoteDataService below) produce historically-correct fills anyway,
by handing it a stand-in data_service whose get_quotes() answers from
pre-fetched (or lazily-fetched, for option symbols - unknowable in advance)
historical candle data instead of the broker.

Weekly vs. monthly: how two legs come out of ONE signal
----------------------------------------------------------
The entry/exit SIGNAL (2 consecutive futures candles confirming a VWAP +
volume-MA breakout) depends ONLY on the futures tracker - never on which
option ends up resolved. So a weekly-expiry engine and a monthly-expiry
engine, fed the EXACT SAME futures candles in the exact same order, always
enter and exit at the exact same candles - they can only ever disagree on
WHICH contract (and therefore what price) each leg actually traded. This
module exploits that: for every (underlying, day) it builds TWO
options_engine.NiftyOptionsEngine instances - one with expiry_select=
"NEAREST" (current week), one with expiry_select="MONTHLY" (current month,
see options_engine._select_expiry's own docstring) - and replays the SAME
futures candle stream through both. Each leg gets its OWN full capital
allocation (as if you'd run either strategy alone, not split between them) -
see OptionsUnderlyingConfig - so the weekly-vs-monthly comparison isn't
muddied by a capital-split effect neither single-leg strategy would
actually have.

For BANKNIFTY and every MCX commodity (monthly-only listings, no weekly
contract at all), "NEAREST" and "MONTHLY" resolve to the exact same listed
contract - both legs end up identical. That's not a bug, it's the correct
answer for an underlying with no weekly contract to compare against; this
module still runs and reports both rows rather than special-casing them away,
so the output's shape never depends on which underlying you ran.

What this does NOT do (read before trusting a result)
--------------------------------------------------------
- Backtest arbitrarily far into the past. The F&O symbol master
  (NIFTY_FO_SYMBOL_MASTER_CSV / MCX_FO_SYMBOL_MASTER_CSV) is a SNAPSHOT of
  whatever's CURRENTLY listed - it has no row for a contract that's already
  expired and rolled off the exchange's active list. A backtested day whose
  needed weekly/monthly contract isn't in today's snapshot resolves to
  nothing for that leg (logged, not crashed - exactly resolve_option_symbol's
  existing no-crash contract) and contributes zero trades. In practice this
  means: today, and days within the CURRENT weekly/monthly expiry cycle, work
  correctly; a date from several expiry cycles ago generally will not. This
  is a real, structural limitation of resolving historical option symbols
  from a current-only instrument master, not something a config value fixes -
  see the module docstring above for why "including current day" is this
  module's actual sweet spot, not an afterthought.
- Fetch each option's own historical candles before it's known which option
  that even is. The weekly/monthly symbol is only knowable once the futures
  breakout confirms and spot is read at THAT candle - so each option symbol's
  own intraday history is fetched LAZILY, the first time _BacktestQuoteDataService
  is asked for its price, and cached from then on (never re-fetched for the
  same symbol within a single day's replay).
- Model slippage, brokerage, or bid/ask spread - same simplifying assumption
  backtest.py already makes; see that module's own docstring.
- Guarantee Fyers actually HAS historical intraday candle data for every
  option symbol resolve_option_symbol() names. If it doesn't (illiquid
  strike, data gap), that leg's entry/exit price lookup returns None and the
  entry is skipped - logged, not crashed, same contract as every other
  no-data case in this codebase.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import pandas as pd
import pytz

import config
import mcx_options_engine
import options_engine
from backtest import (
    INTRADAY_SEED_LOOKBACK_DAYS,
    _candles_on_date,
    _fetch_full_range_candles,
    _seed_candles_before_date,
    trading_days_between,
)
from bootstrap import decide_commodity_direction_for_date, decide_direction_for_date
from fyers_service import FyersDataService
from opening_range_scanner import or_candle_wick_pct
from signal_engine import seed_opening_range_candle, seed_volume_windows_from_candles
from trade_direction import TradeDirection

logger = logging.getLogger("orb_agent.backtest_options")

LEG_EXPIRY_SELECT = {"WEEKLY": "NEAREST", "MONTHLY": "MONTHLY"}
DEFAULT_LEGS = ("WEEKLY", "MONTHLY")
DEFAULT_MARKETS = ("nifty", "banknifty", "mcx")


# ----------------------------------------------------------------------------
# Historical "live quote" stand-in - see module docstring for why this exists.
# ----------------------------------------------------------------------------
class _BacktestQuoteDataService:
    """
    Minimal data_service STAND-IN that makes options_engine.NiftyOptionsEngine's unmodified
    live code produce historically-correct fills during a backtest, instead of hitting the
    real broker. Every live network call that engine makes funnels through exactly ONE
    FyersDataService method - get_quotes([symbol]) - called by options_engine._fetch_index_spot()
    (to resolve a strike from spot AT SIGNAL TIME) and by NiftyOptionsEngine._fetch_option_price()
    (to price every entry/exit). This class answers BOTH from pre-fetched/lazily-fetched
    historical candle data instead.

    "AT THAT MOMENT" means the latest candle at-or-before self.current_epoch - the backtest
    driver (_run_options_leg_for_date below) advances current_epoch to match whichever futures
    candle is currently being replayed through feed_candle(), so a get_quotes() call made
    mid-replay answers exactly what a live LTP read would have returned at that same
    wall-clock moment, never a future candle's price.

    NOT a general-purpose FyersDataService substitute - get_candle()/place_order() raise
    NotImplementedError if ever reached, by design, so a bug that accidentally routes this
    backtest through a LIVE code path (e.g. NiftyOptionsEngine.check_all(), which this module
    never calls - it always calls feed_candle() directly, like backtest.py does for equities)
    fails loudly instead of silently hitting the broker or a live candle-fetch endpoint.
    """

    def __init__(self, real_data_service: FyersDataService, resolution: str, trading_date: date):
        self._real = real_data_service
        self._resolution = resolution
        self._date_str = str(trading_date)
        self.current_epoch: Optional[int] = None
        self._candles_by_symbol: dict[str, list] = {}

    def preload(self, symbol: str, candles: list) -> None:
        """Seed this adapter with ALREADY-FETCHED candles for `symbol` (the futures/index
        symbol, pre-fetched once for the whole backtest date range by the caller) - avoids a
        redundant fetch for a symbol the caller already has in memory."""
        self._candles_by_symbol[symbol] = candles

    def _candles_for(self, symbol: str) -> list:
        if symbol not in self._candles_by_symbol:
            try:
                self._candles_by_symbol[symbol] = self._real.get_candles_range(
                    symbol, self._resolution, self._date_str, self._date_str)
            except Exception as exc:
                logger.error("Historical candle fetch failed for %s on %s: %s - treating as "
                              "no data available for this symbol today.", symbol, self._date_str, exc)
                self._candles_by_symbol[symbol] = []
        return self._candles_by_symbol[symbol]

    def _price_as_of(self, symbol: str) -> Optional[float]:
        if self.current_epoch is None:
            return None
        candles = self._candles_for(symbol)
        at_or_before = [c for c in candles if c[0] <= self.current_epoch]
        if not at_or_before:
            return None
        return at_or_before[-1][4]  # close of the latest candle at/under current_epoch

    def get_quotes(self, symbols: list[str]) -> dict:
        d = []
        for symbol in symbols:
            price = self._price_as_of(symbol)
            if price is not None:
                d.append({"n": symbol, "v": {"lp": price}})
        return {"d": d}

    def get_candle(self, *args, **kwargs):
        raise NotImplementedError("_BacktestQuoteDataService only serves get_quotes() - "
                                   "see class docstring; this backtest replays candles "
                                   "directly via feed_candle(), it should never call check_all().")

    def place_order(self, *args, **kwargs):
        raise NotImplementedError("_BacktestQuoteDataService never places real orders - "
                                   "place_or_log_order() is already gated by is_backfill=True "
                                   "(always passed here) and config.DRY_RUN; reaching this is a bug.")


# ----------------------------------------------------------------------------
# Per-underlying wiring - generic across NIFTY/BANKNIFTY/an MCX commodity, built by the
# three small factory functions below rather than constructed by hand, so a typo in one of
# these fields can't silently backtest the wrong instrument.
# ----------------------------------------------------------------------------
@dataclass
class OptionsUnderlyingConfig:
    name: str                  # "NIFTY", "BANKNIFTY", or an MCX option_underlying code
    futures_symbol: str
    index_symbol: str          # NIFTY_INDEX_SYMBOL/BANKNIFTY_INDEX_SYMBOL, or (MCX) the SAME
                                # futures_symbol - see mcx_options_engine.py's module docstring
                                # for why MCX has no separate spot index.
    capital_allocated: float
    otm_strikes_away: int
    master_columns: dict
    candle_resolution: str = "5"


def nifty_underlying_config() -> OptionsUnderlyingConfig:
    return OptionsUnderlyingConfig(
        name="NIFTY", futures_symbol=config.NIFTY_FUTURES_SYMBOL,
        index_symbol=config.NIFTY_INDEX_SYMBOL, capital_allocated=config.NIFTY_OPTIONS_CAPITAL,
        otm_strikes_away=config.NIFTY_OTM_STRIKES_AWAY, master_columns=config.NIFTY_FO_MASTER_COLUMNS)


def banknifty_underlying_config() -> OptionsUnderlyingConfig:
    return OptionsUnderlyingConfig(
        name="BANKNIFTY", futures_symbol=config.BANKNIFTY_FUTURES_SYMBOL,
        index_symbol=config.BANKNIFTY_INDEX_SYMBOL, capital_allocated=config.BANKNIFTY_OPTIONS_CAPITAL,
        otm_strikes_away=config.BANKNIFTY_OTM_STRIKES_AWAY, master_columns=config.NIFTY_FO_MASTER_COLUMNS)


def mcx_underlying_configs() -> list[OptionsUnderlyingConfig]:
    """One per config.MCX_COMMODITY_UNIVERSE entry that actually has an option_underlying set
    (see mcx_options_engine.py) - entries missing it are skipped, same as the live MCX
    options engine. Capital is split equally across the ELIGIBLE commodities only (mirrors
    mcx_options_engine.MCXOptionsEngine's own position_sizing.capital_per_slot() call), not
    across the full universe, so a commodity with no option_underlying configured yet doesn't
    quietly shrink everyone else's allocation."""
    import position_sizing  # local import: matches mcx_options_engine.py's own style

    eligible = [row for row in config.MCX_COMMODITY_UNIVERSE if row.get("option_underlying")]
    if not eligible:
        logger.warning("No MCX_COMMODITY_UNIVERSE entry has an option_underlying set - "
                        "MCX options backtest will have nothing to trade.")
        return []
    per_slot_capital = position_sizing.capital_per_slot(config.MCX_OPTIONS_CAPITAL, len(eligible))
    return [
        OptionsUnderlyingConfig(
            name=row["option_underlying"], futures_symbol=row["symbol"], index_symbol=row["symbol"],
            capital_allocated=per_slot_capital, otm_strikes_away=config.MCX_OTM_STRIKES_AWAY,
            master_columns=config.MCX_FO_MASTER_COLUMNS,
            candle_resolution=str(config.MCX_CANDLE_INTERVAL_MINUTES))
        for row in eligible
    ]


# ----------------------------------------------------------------------------
# One (underlying, leg, day) replay
# ----------------------------------------------------------------------------
def _run_options_leg_for_date(data_service: FyersDataService, underlying_cfg: OptionsUnderlyingConfig,
                               leg_label: str, target_date: date, futures_day_candles: list,
                               seed_candles: list, symbol_master: pd.DataFrame, tz,
                               direction: TradeDirection) -> pd.DataFrame:
    """
    Replays ONE (underlying, leg) combination for ONE day - e.g. (NIFTY, WEEKLY). Builds a
    fresh options_engine.NiftyOptionsEngine (completely UNMODIFIED - see module docstring),
    wired to a _BacktestQuoteDataService instead of the real data_service, and feeds it
    `futures_day_candles` via feed_candle() in order - exactly backtest.py's own equity/MCX
    replay pattern, just for a single-symbol options engine instead of a multi-symbol
    SignalEngine.

    is_backfill=True is passed on every feed_candle() call (unlike backtest.py's equity/MCX
    replay, which relies solely on config.DRY_RUN) - belt-and-suspenders: this module
    involves a SECOND real, separately brokered instrument (the option itself), so a
    misconfigured DRY_RUN=False should still never place a real order from a backtest.

    Returns that leg's trade_book DataFrame (possibly empty), with 'date', 'underlying', and
    'expiry_leg' columns attached so weekly/monthly/day/underlying results can be freely
    concatenated afterwards - see run_options_backtest().
    """
    if not futures_day_candles:
        return pd.DataFrame()

    or_candle = futures_day_candles[0]
    _, or_open, or_high, or_low, or_close, or_vol = or_candle
    wick_pct = or_candle_wick_pct(or_open, or_high, or_low, or_close, direction)
    if wick_pct > config.MAX_OR_CANDLE_WICK_PCT:
        logger.info("%s %s %s: opening-range closing-strength filter failed (wick_pct=%.1f%% > "
                    "config.MAX_OR_CANDLE_WICK_PCT=%.1f%%, %s bias) - skipping this leg.",
                    target_date, underlying_cfg.name, leg_label, wick_pct,
                    config.MAX_OR_CANDLE_WICK_PCT, direction.value)
        return pd.DataFrame()

    quote_service = _BacktestQuoteDataService(data_service, underlying_cfg.candle_resolution, target_date)
    engine = options_engine.NiftyOptionsEngine(
        quote_service, underlying_cfg.futures_symbol, or_high, or_low, underlying_cfg.capital_allocated,
        index_symbol=underlying_cfg.index_symbol, underlying=underlying_cfg.name,
        strikes_away=underlying_cfg.otm_strikes_away, symbol_master=symbol_master,
        master_columns=underlying_cfg.master_columns, candle_resolution=underlying_cfg.candle_resolution,
        expiry_select=LEG_EXPIRY_SELECT[leg_label],
    )

    seed_opening_range_candle(engine.futures_tracker, or_candle)
    seed_volume_windows_from_candles(engine.trackers, {underlying_cfg.futures_symbol: seed_candles})

    # Pre-load the adapter with the index/futures symbol's own candles (ALREADY fetched by
    # the caller for the whole date range - see _run_underlying_options_backtest) so strike
    # resolution's spot lookup (_fetch_index_spot -> get_quotes) has data to answer from
    # without a second fetch. For MCX, index_symbol IS futures_symbol, so this is one preload,
    # not two.
    quote_service.preload(underlying_cfg.index_symbol, futures_day_candles)
    if underlying_cfg.index_symbol != underlying_cfg.futures_symbol:
        quote_service.preload(underlying_cfg.futures_symbol, futures_day_candles)

    # Start at index 1: index 0 is the opening-range candle itself, already consumed above -
    # matches live/backtest.py's own convention.
    for i in range(1, len(futures_day_candles)):
        candle = futures_day_candles[i]
        quote_service.current_epoch = candle[0]
        candle_time_str = datetime.fromtimestamp(candle[0], tz).strftime("%H:%M:%S")
        engine.feed_candle(underlying_cfg.futures_symbol, candle, candle_time_str, is_backfill=True)

    quote_service.current_epoch = futures_day_candles[-1][0]
    eod_time_str = datetime.fromtimestamp(futures_day_candles[-1][0], tz).strftime("%H:%M:%S")
    engine.square_off_all(at_time_str=eod_time_str)

    trades_df = engine.trade_book.as_dataframe()
    if not trades_df.empty:
        trades_df.insert(0, "date", str(target_date))
        trades_df.insert(1, "underlying", underlying_cfg.name)
        trades_df.insert(2, "expiry_leg", leg_label)
    return trades_df


def _run_underlying_options_backtest(data_service: FyersDataService, underlying_cfg: OptionsUnderlyingConfig,
                                      trading_days: list[date], direction_fn: Callable,
                                      symbol_master: pd.DataFrame, legs: tuple[str, ...], tz) -> tuple[list, list]:
    """Backtests ONE underlying (NIFTY, BANKNIFTY, or one MCX commodity) across every trading
    day, for every requested leg (WEEKLY and/or MONTHLY). Pre-fetches the futures symbol's
    OWN full-range candle history ONCE (chunked, like backtest.py's equity/MCX pre-fetch) -
    not per-day, not per-leg - then slices per day locally, with zero extra API calls beyond
    that single pre-fetch plus whichever option symbols actually get resolved along the way.

    Returns (list_of_per_leg_trade_dataframes, list_of_daily_summary_dicts).
    """
    resolution = underlying_cfg.candle_resolution
    intraday_fetch_start = trading_days[0] - timedelta(days=INTRADAY_SEED_LOOKBACK_DAYS)
    futures_candles = _fetch_full_range_candles(
        data_service, underlying_cfg.futures_symbol, resolution, intraday_fetch_start,
        trading_days[-1], config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK)

    all_trades: list[pd.DataFrame] = []
    daily_summaries: list[dict] = []

    for d in trading_days:
        day_direction, sentiment_label, sentiment_score = direction_fn(d)
        if day_direction is None:
            logger.info("%s %s: no clear direction (%s, score %.2f) - skipping, matching live.",
                        d, underlying_cfg.name, sentiment_label, sentiment_score)
            daily_summaries.append({"date": str(d), "underlying": underlying_cfg.name, "direction": None,
                                     "sentiment": sentiment_label, "sentiment_score": sentiment_score,
                                     "total_trades": 0})
            continue

        day_candles = _candles_on_date(futures_candles, d, tz)
        if not day_candles:
            logger.info("%s %s: no futures candle data available - skipping (holiday, data "
                        "gap, or a date outside Fyers' retained history).", d, underlying_cfg.name)
            daily_summaries.append({"date": str(d), "underlying": underlying_cfg.name,
                                     "direction": day_direction.value, "sentiment": sentiment_label,
                                     "sentiment_score": sentiment_score, "total_trades": 0})
            continue

        seed_candles = _seed_candles_before_date(futures_candles, d, tz, config.VOLUME_MA_PERIOD)
        day_total = 0
        for leg in legs:
            trades_df = _run_options_leg_for_date(data_service, underlying_cfg, leg, d, day_candles,
                                                    seed_candles, symbol_master, tz, day_direction)
            if not trades_df.empty:
                all_trades.append(trades_df)
                day_total += len(trades_df)

        daily_summaries.append({"date": str(d), "underlying": underlying_cfg.name,
                                 "direction": day_direction.value, "sentiment": sentiment_label,
                                 "sentiment_score": sentiment_score, "total_trades": day_total})

    return all_trades, daily_summaries


# ----------------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------------
def _compute_overall(combined_trades: pd.DataFrame, daily_summary_df: pd.DataFrame) -> dict:
    overall: dict = {
        "days_tested": int(len(daily_summary_df)),
        "days_skipped_no_direction": (int(daily_summary_df["direction"].isna().sum())
                                       if "direction" in daily_summary_df else 0),
        "total_trades": int(len(combined_trades)),
    }
    if combined_trades.empty:
        overall.update(total_points=0.0, total_pnl_amount=0.0, win_rate_pct=0.0,
                        by_underlying_and_leg=pd.DataFrame())
        return overall

    closed = combined_trades[combined_trades["exit_price"].notna()]
    overall["total_points"] = round(float(closed["points"].sum()), 2) if not closed.empty else 0.0
    overall["total_pnl_amount"] = round(float(closed["pnl_amount"].sum()), 2) if not closed.empty else 0.0
    overall["win_rate_pct"] = (round(float((closed["points"] > 0).mean() * 100), 2)
                                if not closed.empty else 0.0)

    if closed.empty:
        overall["by_underlying_and_leg"] = pd.DataFrame()
        return overall

    grouped = (
        closed.groupby(["underlying", "expiry_leg"])
        .agg(trades=("points", "size"),
             total_points=("points", "sum"),
             total_pnl_amount=("pnl_amount", "sum"),
             win_rate_pct=("points", lambda s: round(float((s > 0).mean() * 100), 2)))
        .reset_index()
    )
    overall["by_underlying_and_leg"] = grouped
    return overall


def run_options_backtest(data_service: FyersDataService, start_date: date, end_date: date, *,
                          direction: Optional[TradeDirection] = None,
                          legs: tuple[str, ...] = DEFAULT_LEGS,
                          markets: tuple[str, ...] = DEFAULT_MARKETS) -> dict:
    """
    Backtests NIFTY/BANKNIFTY/MCX OPTIONS over every weekday in [start_date, end_date].

    direction=None (the default): decided FRESH FOR EACH DAY, exactly matching live - NIFTY/
    BANKNIFTY options from historical EQUITY sentiment (bootstrap.decide_direction_for_date,
    the SAME read job_nifty_options_scan()/job_banknifty_options_scan() use live - see
    main.py), MCX options from historical COMMODITY sentiment
    (bootstrap.decide_commodity_direction_for_date, the same read job_mcx_options_scan() uses
    live). direction=<a fixed TradeDirection> overrides both and forces one side for the
    whole range - same opt-in semantics as backtest.run_backtest()'s own `direction` param.

    legs: subset of ("WEEKLY", "MONTHLY") - which expiry leg(s) to resolve and trade per
    signal. markets: subset of ("nifty", "banknifty", "mcx") - which underlyings to include.

    Returns {"trades": combined per-trade DataFrame (with 'date'/'underlying'/'expiry_leg'
    columns - see _run_options_leg_for_date), "daily_summary": per-(day, underlying)
    DataFrame, "overall": {totals..., "by_underlying_and_leg": a weekly-vs-monthly/
    underlying-vs-underlying breakdown DataFrame}}.
    """
    trading_days = trading_days_between(start_date, end_date)
    tz = pytz.timezone(config.TIMEZONE)
    if not trading_days:
        logger.warning("No weekdays in the requested range (%s to %s).", start_date, end_date)
        return {"trades": pd.DataFrame(), "daily_summary": pd.DataFrame(), "overall": {}}

    forced = direction is not None

    def equity_direction_fn(d: date):
        return (direction, "FORCED", 0.0) if forced else decide_direction_for_date(d)

    def commodity_direction_fn(d: date):
        return (direction, "FORCED", 0.0) if forced else decide_commodity_direction_for_date(d)

    all_trades: list[pd.DataFrame] = []
    daily_summaries: list[dict] = []

    if "nifty" in markets or "banknifty" in markets:
        nse_master = options_engine.load_fo_symbol_master()
        if nse_master.empty:
            logger.error("NSE F&O symbol master is empty/unreadable - skipping NIFTY/"
                          "BANKNIFTY options backtest entirely (see load_fo_symbol_master's "
                          "own logs above for why).")
        else:
            if "nifty" in markets:
                trades, summaries = _run_underlying_options_backtest(
                    data_service, nifty_underlying_config(), trading_days, equity_direction_fn,
                    nse_master, legs, tz)
                all_trades.extend(trades)
                daily_summaries.extend(summaries)
            if "banknifty" in markets:
                trades, summaries = _run_underlying_options_backtest(
                    data_service, banknifty_underlying_config(), trading_days, equity_direction_fn,
                    nse_master, legs, tz)
                all_trades.extend(trades)
                daily_summaries.extend(summaries)

    if "mcx" in markets:
        mcx_master = mcx_options_engine.load_mcx_fo_symbol_master()
        if mcx_master.empty:
            logger.error("MCX F&O symbol master is empty/unreadable - skipping MCX options "
                          "backtest entirely.")
        else:
            for commodity_cfg in mcx_underlying_configs():
                trades, summaries = _run_underlying_options_backtest(
                    data_service, commodity_cfg, trading_days, commodity_direction_fn,
                    mcx_master, legs, tz)
                all_trades.extend(trades)
                daily_summaries.extend(summaries)

    combined_trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    daily_summary_df = pd.DataFrame(daily_summaries)
    overall = _compute_overall(combined_trades, daily_summary_df)
    return {"trades": combined_trades, "daily_summary": daily_summary_df, "overall": overall}


# ----------------------------------------------------------------------------
# CSV I/O - ALWAYS saved, not an opt-in flag (see module docstring).
# ----------------------------------------------------------------------------
def default_csv_path(start_date: date, end_date: date) -> str:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    if start_date == end_date:
        return os.path.join(config.LOG_DIR, f"options_backtest_{start_date.strftime('%Y%m%d')}.csv")
    return os.path.join(
        config.LOG_DIR,
        f"options_backtest_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.csv")


def save_trades_csv(trades_df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    trades_df.to_csv(path, index=False)
    logger.info("Saved %d options backtest trade row(s) to %s", len(trades_df), path)


if __name__ == "__main__":
    import argparse

    from fyers_service import FyersAuthManager

    today = datetime.now(pytz.timezone(config.TIMEZONE)).date()

    parser = argparse.ArgumentParser(
        description="Backtest NIFTY/BANKNIFTY/MCX OPTIONS (options_engine.NiftyOptionsEngine, "
                    "unmodified) over a historical date range, resolving BOTH the current-WEEK "
                    "and current-MONTH expiry contract for every signal by default, and ALWAYS "
                    "writing every simulated trade to a CSV file. Run with no arguments to "
                    "backtest just TODAY - the common case: run this any time after today's "
                    "candles have formed (e.g. after market close) to see what options trades "
                    "today's signals would have produced.")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--end", default=None,
                         help="YYYY-MM-DD (default: --start if given, else today)")
    parser.add_argument("--market", default="all", choices=["nifty", "banknifty", "mcx", "all"])
    parser.add_argument("--legs", default="weekly,monthly",
                         help="Comma-separated subset of weekly,monthly (default: both)")
    parser.add_argument("--direction", default=None, choices=["LONG", "SHORT"],
                         help="Force one fixed direction across the whole range, overriding "
                              "the default per-day sentiment-based decision.")
    parser.add_argument("--out", default=None,
                         help="Override the automatic CSV output path (logs/options_backtest_"
                              "<date(s)>.csv by default - a file is ALWAYS written either way).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else today
    end = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end
           else (datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else today))
    markets = ("nifty", "banknifty", "mcx") if args.market == "all" else (args.market,)
    legs = tuple(leg.strip().upper() for leg in args.legs.split(",") if leg.strip())
    direction = TradeDirection[args.direction] if args.direction else None

    auth = FyersAuthManager()
    access_token = auth.get_access_token()
    data_service = FyersDataService(access_token)

    results = run_options_backtest(data_service, start, end, direction=direction,
                                    legs=legs, markets=markets)

    print("\n=== Daily Summary ===")
    print(results["daily_summary"].to_string(index=False) if not results["daily_summary"].empty
          else "No trading days in range.")

    by_leg = results["overall"].get("by_underlying_and_leg") if results["overall"] else None
    print("\n=== By Underlying & Expiry Leg ===")
    print(by_leg.to_string(index=False) if (by_leg is not None and not by_leg.empty) else "No trades.")

    print("\n=== Overall ===")
    print({k: v for k, v in results["overall"].items() if k != "by_underlying_and_leg"})

    out_path = args.out or default_csv_path(start, end)
    save_trades_csv(results["trades"], out_path)
    print(f"\nSaved combined options trade log to {out_path}")
