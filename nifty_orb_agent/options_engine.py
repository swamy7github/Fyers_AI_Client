"""
options_engine.py
--------------------
NIFTY INDEX options, SINGLE-SOURCE signal / single-leg execution. The exact
same engine/rules apply verbatim to BANK NIFTY (NIFTYBANK) via
run_banknifty_options_engine() / config.BANKNIFTY_* - see that function's
docstring; everything below is written for NIFTY but is equally true for
BANKNIFTY with its own index/futures/strike universe substituted in:

  - The ENTRY/EXIT *signal* is computed ENTIRELY from ONE instrument, which
    is NOT the option contract itself and NOT the index:
      config.NIFTY_FUTURES_SYMBOL (the current front-month NIFTY future)
      supplies BOTH the PRICE condition (its own 09:15 opening-range
      candle's high/low, and its own running VWAP) AND the VOLUME
      condition (two consecutive candles' volume above its own 20-period
      volume MA). The index itself is typically reported with ZERO volume
      in Fyers' feed (it isn't a tradable instrument), so neither a
      volume-MA check NOR a VWAP (which needs real cumulative
      price*volume) would be meaningful computed off the index's own
      candles - the future is a real, continuously-traded proxy instead,
      tracking the index almost 1:1. VERIFY the zero-volume assumption
      against a raw quote/candle for your own NIFTY_INDEX_SYMBOL - if your
      feed DOES report real index volume you could point this back at the
      index, but this module defaults to the futures-only read.
  - config.NIFTY_INDEX_SYMBOL is used for exactly ONE thing, entirely
    separate from the signal above: fetching the index's own spot LTP at
    entry time, to pick WHICH OTM strike to buy (see
    resolve_otm_option_symbol()/_fetch_index_spot() below). It plays no
    part in the entry/exit decision itself.
  - ENTRY (today's bias is decided once, by the SAME sentiment pipeline
    equities/commodities use - bootstrap.decide_todays_direction(), passed
    in as `bias_direction` - not recomputed here):
      LONG bias:  2 consecutive FUTURE candles close ABOVE the future's own
                  opening-range HIGH *and* above its own VWAP, *and* (on
                  those same two candles) its own volume is above its own
                  20-period volume MA -> BUY an OTM CALL (CE).
      SHORT bias: the mirror image - 2 consecutive FUTURE candles close
                  BELOW the opening-range LOW *and* below VWAP, with the
                  same volume confirmation -> BUY an OTM PUT (PE).
  - EXIT - once a position is open, the mirrored condition on the SAME
    instrument: 2 consecutive FUTURE candles closing back across VWAP
    (below it for a LONG/CE position, above it for a SHORT/PE position),
    again with volume above its own 20-period MA on both, closes the
    ENTIRE option position. There is no separate add-on/multi-stage
    stop-loss here (unlike equities/commodities' weakness-event ADD rule)
    - the request behind this module described a single entry and a
    single exit, and doubling into an already-leveraged option contract on
    "weakness" is a materially different risk than doing so for an
    equity, so that wasn't carried over uninvited.
  - The instrument actually BOUGHT/SOLD is a single OTM (out-of-the-money)
    NIFTY option, resolved fresh each day from Fyers' own F&O symbol master
    file (never a hard-coded strike/expiry string - see
    config.NIFTY_FO_SYMBOL_MASTER_CSV/NIFTY_FO_MASTER_COLUMNS). BUY-ONLY,
    same reasoning as before this rewrite: risk capped to premium paid,
    never writes/sells an option naked.

NiftyOptionsEngine (below) exposes the same duck-typed surface as
signal_engine.SignalEngine (`.trade_book`, `.check_all()`, `.feed_candle()`,
`.square_off_all()`, `.trackers`) so main.py's job_check_signals()/
job_square_off_and_report() AND bootstrap.py's generic
_replay_intraday_history() drive it exactly like the equity/commodity
engines, with zero special-casing there - a single-symbol signal means this
engine's candle intake has the exact same one-symbol-at-a-time shape
SignalEngine does (an earlier dual-source version of this module needed a
bespoke two-symbol feed_candles()/replay helper, back when the signal was
split across the index AND the future).

Off by default (config.ENABLE_NIFTY_OPTIONS_TRADING = False) - read this
whole module and the NIFTY config block in config.py before turning it on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd
import pytz

import config
import position_sizing
import os
import sys
from opening_range_scanner import or_candle_wick_pct
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fyersclient.fyers_client as fyres_client
from fyersclient.fyers_client import get_fyers_client, FyersDataService

from signal_engine import StockTracker, TradeBook, place_or_log_order, seed_opening_range_candle
from trade_direction import TradeDirection

logger = logging.getLogger("orb_agent.options")


# ----------------------------------------------------------------------------
# F&O symbol master (option contract resolution) - unchanged responsibility
# from before this rewrite, just now picking an OTM strike instead of ATM.
# ----------------------------------------------------------------------------
def load_fo_symbol_master(csv_path: str | None = None) -> pd.DataFrame:
    """
    Loads Fyers' NSE F&O instrument master CSV from disk - download/refresh
    it yourself periodically (see README) and point
    config.NIFTY_FO_SYMBOL_MASTER_CSV at it. Column names are normalised
    via config.NIFTY_FO_MASTER_COLUMNS, a best-effort {logical name: actual
    column name} mapping you MUST verify against your own downloaded
    file's real header before relying on this.

    Returns an empty DataFrame (never raises) if the file doesn't exist
    yet or is missing an expected column, so a misconfigured/missing
    master file degrades to "no options traded today" rather than
    crashing the whole agent.
    """
    path = csv_path or config.NIFTY_FO_SYMBOL_MASTER_CSV
    try:
        df = pd.read_csv(path)
    except (FileNotFoundError, OSError) as exc:
        logger.error("NIFTY F&O symbol master not found at %s (%s) - options trading disabled "
                      "for today. Download/refresh it and set config.NIFTY_FO_SYMBOL_MASTER_CSV.",
                      path, exc)
        return pd.DataFrame()

    cols = config.NIFTY_FO_MASTER_COLUMNS
    missing = [logical for logical, actual in cols.items() if actual not in df.columns]
    if missing:
        logger.error("NIFTY F&O symbol master at %s is missing expected column(s) %s (looked for "
                      "%s) - check config.NIFTY_FO_MASTER_COLUMNS against this file's real header "
                      "(columns found: %s). Options trading disabled for today.",
                      path, missing, {m: cols[m] for m in missing}, list(df.columns))
        return pd.DataFrame()
    return df


def _fetch_index_spot(data_service: FyersDataService, index_symbol: str) -> float | None:
    quote = data_service.get_quotes([index_symbol])
    try:
        return next(item["v"]["lp"] for item in quote.get("d", [])
                    if item.get("n") == index_symbol)
    except (StopIteration, KeyError, TypeError):
        logger.error("Could not fetch %s spot LTP - can't resolve an OTM strike.", index_symbol)
        return None


def resolve_otm_option_symbol(data_service: FyersDataService, option_type: str,
                               symbol_master: pd.DataFrame | None = None,
                               strikes_away: int | None = None,
                               underlying: str = "NIFTY",
                               index_symbol: str | None = None) -> tuple[str, int] | None:
    """
    option_type: "CE" or "PE". Returns (fyers_trading_symbol, lot_size) for
    the NEAREST-EXPIRY `underlying` contract of that type (default "NIFTY" -
    pass underlying="BANKNIFTY" for Bank Nifty, reading its spot from
    `index_symbol`, e.g. config.BANKNIFTY_INDEX_SYMBOL), `strikes_away`
    strikes OUT of the money (config.NIFTY_OTM_STRIKES_AWAY if not given
    explicitly and underlying is "NIFTY" - 1 = the nearest listed OTM
    strike; pass it explicitly for any other underlying).

    OTM direction depends on option_type: a CALL is out-of-the-money ABOVE
    spot (you'd rather own the right to buy at a strike above where it's
    trading), a PUT is out-of-the-money BELOW spot - so CE strikes are
    sorted ascending above spot and PE strikes descending below it, taking
    the (strikes_away - 1)'th from the money.

    Returns None (never raises) if it couldn't be resolved (missing/empty
    master file, no spot LTP, no matching rows, or fewer OTM strikes
    listed than `strikes_away` asks for - in which case it falls back to
    the FARTHEST available OTM strike and logs a warning rather than
    failing outright) - callers must treat None as "skip options trading
    today", not retry indefinitely. lot_size is read straight from the
    master file, never hard-coded.
    """
    index_symbol = index_symbol or config.NIFTY_INDEX_SYMBOL
    strikes_away = strikes_away if strikes_away is not None else config.NIFTY_OTM_STRIKES_AWAY
    if strikes_away < 1:
        logger.error("strikes_away must be >= 1 (got %d) - treating as 1.", strikes_away)
        strikes_away = 1

    master = symbol_master if symbol_master is not None else load_fo_symbol_master()
    if master.empty:
        return None

    cols = config.NIFTY_FO_MASTER_COLUMNS
    underlying_col, opttype_col = cols["underlying"], cols["option_type"]
    expiry_col, strike_col = cols["expiry"], cols["strike"]
    symbol_col, lot_col = cols["trading_symbol"], cols["lot_size"]

    spot = _fetch_index_spot(data_service, index_symbol)
    if spot is None:
        return None

    candidates = master[(master[underlying_col].astype(str).str.upper() == underlying.upper()) &
                         (master[opttype_col].astype(str).str.upper() == option_type.upper())]
    if candidates.empty:
        logger.error("No %s %s option rows found in the symbol master.", option_type, underlying)
        return None

    nearest_expiry = candidates[expiry_col].min()
    candidates = candidates[candidates[expiry_col] == nearest_expiry]

    if option_type.upper() == "CE":
        otm_candidates = candidates[candidates[strike_col] > spot].sort_values(strike_col)
    else:
        otm_candidates = candidates[candidates[strike_col] < spot].sort_values(strike_col, ascending=False)

    if otm_candidates.empty:
        logger.error("No OTM %s strikes found above/below spot=%.2f for %s's nearest expiry (%s).",
                      option_type, spot, underlying, nearest_expiry)
        return None

    index = min(strikes_away - 1, len(otm_candidates) - 1)
    if index != strikes_away - 1:
        logger.warning("Only %d OTM %s strike(s) listed (nearest expiry %s) - requested "
                        "%d-away, using the farthest available instead.",
                        len(otm_candidates), option_type, nearest_expiry, strikes_away)
    best = otm_candidates.iloc[index]

    symbol = str(best[symbol_col])
    lot_size = int(best[lot_col])
    logger.info("Resolved OTM(%d) %s %s: spot=%.2f -> strike=%s, expiry=%s, symbol=%s, lot_size=%d",
                strikes_away, underlying, option_type, spot, best[strike_col], nearest_expiry, symbol, lot_size)
    return symbol, lot_size


def fetch_opening_range_candle(data_service: FyersDataService, symbol: str) -> list | None:
    """
    Fetches `symbol`'s own 09:15-09:20 opening-range candle as a raw [epoch, o, h, l, c, v]
    row - the lower-level building block fetch_index_opening_range() below is built from.
    Generic over `symbol`: run_index_options_engine() calls this with the FUTURES symbol
    (config.NIFTY_FUTURES_SYMBOL/BANKNIFTY_FUTURES_SYMBOL) to get the full candle it needs -
    or_high/or_low for the breakout trigger, AND open/close/volume to seed the futures
    tracker's own VWAP (see signal_engine.seed_opening_range_candle) and run the opening-range
    wick filter. The index symbol is never passed here any more - see module docstring for why
    the index plays no part in the signal. Returns None (never raises) if that candle isn't
    available yet.
    """
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    # ========= need to revisit================
    #now = get_last_traded
    start = tz.localize(datetime(now.year, now.month, now.day, 9, 15, 0))
    end = tz.localize(datetime(now.year, now.month, now.day, 9, 20, 0))
    candles = data_service.get_5min_candle(symbol, now.strftime("%Y-%m-%d"),
                                            int(start.timestamp()), int(end.timestamp()))
    return candles[0] if candles else None


def fetch_index_opening_range(data_service: FyersDataService,
                               index_symbol: str | None = None) -> tuple[float, float] | None:
    """
    Generic (symbol, not just index-specific, despite the name kept for backward
    compatibility): fetches `index_symbol`'s own 09:15-09:20 opening-range candle and
    returns (or_high, or_low). NOT called by run_index_options_engine() any more (it needs
    the FULL futures candle - open/close/volume too - so it calls fetch_opening_range_candle()
    directly instead); kept as a standalone (or_high, or_low)-only convenience for callers
    (and tests) that only need the two breakout levels for any given symbol, index or
    otherwise. Returns None (never raises) if that candle isn't available yet.
    """
    index_symbol = index_symbol or config.NIFTY_INDEX_SYMBOL
    candle = fetch_opening_range_candle(data_service, index_symbol)
    if candle is None:
        logger.warning("No opening-range candle yet for %s.", index_symbol)
        return None
    _, _, h, l, _, _ = candle
    return h, l


# ----------------------------------------------------------------------------
# The single-source engine itself
# ----------------------------------------------------------------------------
@dataclass
class _OptionPosition:
    """Bookkeeping for the single option leg this engine ever holds at once -
    deliberately NOT a StockTracker (that's for a symbol that's both the signal
    source AND the traded instrument; here they're different things)."""
    symbol: str
    lot_size: int
    capital_allocated: float
    is_open: bool = False
    entry_price: Optional[float] = None
    qty: int = 0
    consecutive_weak: int = 0


class NiftyOptionsEngine:
    """
    Drives one OTM NIFTY option position entirely from config.NIFTY_FUTURES_SYMBOL's own
    candles - PRICE/VWAP condition AND VOLUME condition both (see module docstring for the
    exact rules). config.NIFTY_INDEX_SYMBOL plays no part in this engine at all; it's read
    once, separately, by resolve_otm_option_symbol()/_fetch_index_spot() to pick the OTM
    strike BEFORE this engine is even constructed (see run_index_options_engine() below).

    Exposes `.trade_book`, `.check_all(window_start_epoch, window_end_epoch)`,
    `.feed_candle(symbol, candle, now_str, is_backfill=False)`,
    `.square_off_all(at_time_str=None)`, and `.trackers` with the same
    signatures signal_engine.SignalEngine uses, so main.py's generic
    per-engine loop (job_check_signals/job_square_off_and_report) AND
    bootstrap.py's generic _replay_intraday_history() can drive this with no
    special-casing - see main.py's state dict.
    """

    def __init__(self, data_service: FyersDataService, option_symbol: str, option_lot_size: int,
                 bias_direction: TradeDirection, or_high: float, or_low: float,
                 capital_allocated: float, futures_symbol: str | None = None,
                 underlying: str = "NIFTY"):
        self.data_service = data_service
        self.bias_direction = bias_direction  # today's sentiment bias - decides the mirrored
                                               # price/VWAP comparison direction below; the
                                               # CE-vs-PE choice is already baked into option_symbol
        self.underlying = underlying  # "NIFTY" or "BANKNIFTY" - purely cosmetic, used in log/reason
                                       # strings below; the actual signal source is futures_symbol,
                                       # resolved independently of this label.
        futures_symbol = futures_symbol or config.NIFTY_FUTURES_SYMBOL
        self.futures_tracker = StockTracker(symbol=futures_symbol, or_high=or_high,
                                             or_low=or_low, direction=bias_direction)
        self.option = _OptionPosition(symbol=option_symbol, lot_size=option_lot_size,
                                       capital_allocated=capital_allocated)
        self.trade_book = TradeBook()

    @property
    def trackers(self) -> dict[str, StockTracker]:
        """The ONE futures tracker - both its VWAP (seeded from its own opening-range candle
        at construction, see run_index_options_engine()) and its volume-MA window (seeded
        separately right after, by bootstrap.fetch_and_seed_volume_windows()) live here.
        Exposed as a property (not a plain dict) purely for parity with
        signal_engine.SignalEngine's own `.trackers`, so main.py's informational prints,
        the seeding helper, AND bootstrap.py's generic replay helper all "just work" without
        any of them needing to know this engine only ever has one tracker."""
        return {self.futures_tracker.symbol: self.futures_tracker}

    # ------------------------------------------------------------------
    # Candle intake
    # ------------------------------------------------------------------
    def _fetch_latest_candle(self, symbol: str, window_start_epoch: int, window_end_epoch: int):
        candles = self.data_service.get_5min_candle(symbol, datetime.now().strftime("%Y-%m-%d"),
                                                      window_start_epoch, window_end_epoch)
        return candles[0] if candles else None

    def check_all(self, window_start_epoch: int, window_end_epoch: int):
        """Called every 5 minutes, exactly like SignalEngine.check_all() - always a LIVE call
        (is_backfill=False); see feed_candle() for the historical-replay equivalent."""
        now_str = datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%H:%M:%S")
        candle = self._fetch_latest_candle(self.futures_tracker.symbol, window_start_epoch, window_end_epoch)
        if candle is None:
            logger.warning("No %s candle this tick - skipping this check rather than acting "
                            "on a partial signal.", self.futures_tracker.symbol)
            return
        self.feed_candle(self.futures_tracker.symbol, candle, now_str, is_backfill=False)

    def feed_candle(self, symbol: str, candle: list, now_str: str, is_backfill: bool = False):
        """
        Public entry point for feeding ONE already-fetched futures candle directly - mirrors
        signal_engine.SignalEngine.feed_candle()'s signature/role exactly (same (symbol,
        candle, now_str, is_backfill) shape), so bootstrap.py's generic
        _replay_intraday_history() can replay this engine's historical candles the same way
        it replays any SignalEngine, with no options-specific replay helper needed.
        """
        tr = self.futures_tracker
        if symbol != tr.symbol:
            raise KeyError(f"{symbol!r} is not this engine's signal symbol ({tr.symbol!r}).")

        _, _, h, l, c, v = candle
        typical_price = (h + l + c) / 3
        tr.candles.append(candle)
        tr.cum_pv += typical_price * v
        tr.cum_vol += v
        vwap = tr.vwap()

        # MA computed BEFORE this candle joins its window (trailing average, excludes the
        # current bar), same convention signal_engine uses for equities/commodities.
        vol_ma_slow = tr.volume_ma()
        tr.volume_window.append(v)

        if self.option.is_open:
            self._check_exit(c, vwap, v, vol_ma_slow, now_str, is_backfill)
        else:
            self._check_entry(c, vwap, v, vol_ma_slow, now_str, is_backfill)

    # ------------------------------------------------------------------
    # Entry / exit
    # ------------------------------------------------------------------
    def _check_entry(self, close: float, vwap: float, volume: float,
                      vol_ma_slow: Optional[float], now_str: str, is_backfill: bool):
        tr = self.futures_tracker
        if self.bias_direction == TradeDirection.LONG:
            qualifies = (vol_ma_slow is not None and tr.or_high is not None
                         and close > tr.or_high and close > vwap
                         and volume > vol_ma_slow)
        else:
            qualifies = (vol_ma_slow is not None and tr.or_low is not None
                         and close < tr.or_low and close < vwap
                         and volume > vol_ma_slow)
        tr.consecutive_qualifying = tr.consecutive_qualifying + 1 if qualifies else 0

        if tr.consecutive_qualifying >= config.CONSECUTIVE_CANDLES_FOR_BUY:
            tr.consecutive_qualifying = 0  # reset so it doesn't immediately re-fire
            self._enter_option_position(now_str, is_backfill)

    def _enter_option_position(self, now_str: str, is_backfill: bool):
        option_price = self._fetch_option_price(self.option.symbol)
        if option_price is None:
            logger.error("Entry signal confirmed, but couldn't fetch a current price for %s - "
                          "skipping this entry.", self.option.symbol)
            return
        qty = position_sizing.calculate_quantity(self.option.capital_allocated, option_price, self.option.lot_size)
        if qty == 0:
            logger.warning("Entry signal confirmed for %s @ %.2f, but capital_allocated=%.2f "
                            "(lot_size=%d) can't afford even one lot - SKIPPING this entry.",
                            self.option.symbol, option_price, self.option.capital_allocated,
                            self.option.lot_size)
            return

        self.option.is_open = True
        self.option.entry_price = option_price
        self.option.qty = qty
        self.option.consecutive_weak = 0
        self.trade_book.record_entry(self.option.symbol, now_str, option_price, entry_type="INITIAL",
                                      is_backfill=is_backfill, direction=TradeDirection.LONG, qty=qty)
        logger.info("@ %s BUY OPTION %s qty=%d @ %.2f (%s 2-candle breakout confirmed by its own "
                    "volume > 20-period MA)%s", now_str, self.option.symbol, qty, option_price,
                    self.futures_tracker.symbol, " [BACKFILL]" if is_backfill else "")
        place_or_log_order(self.data_service, self.option.symbol, "BUY", qty, option_price, is_backfill)

    def _check_exit(self, close: float, vwap: float, volume: float,
                     vol_ma_slow: Optional[float], now_str: str, is_backfill: bool):
        if self.bias_direction == TradeDirection.LONG:
            is_weak = (vol_ma_slow is not None) and (close < vwap) and (volume > vol_ma_slow)
            vs_vwap = "below"
        else:
            is_weak = (vol_ma_slow is not None) and (close > vwap) and (volume > vol_ma_slow)
            vs_vwap = "above"

        self.option.consecutive_weak = self.option.consecutive_weak + 1 if is_weak else 0
        if self.option.consecutive_weak < config.CONSECUTIVE_CANDLES_FOR_STOPLOSS:
            return
        self.option.consecutive_weak = 0
        reason = (f"2 consecutive {self.futures_tracker.symbol} candles closed {vs_vwap} VWAP "
                  f"{vwap:.2f} with volume > its 20-period MA")
        self._exit_option_position(now_str, is_backfill, reason=reason)

    def _exit_option_position(self, now_str: str, is_backfill: bool, reason: str):
        option_price = self._fetch_option_price(self.option.symbol)
        if option_price is None:
            logger.error("Exit signal confirmed for %s, but couldn't fetch a current price - "
                          "position stays OPEN this tick; will retry next candle.", self.option.symbol)
            return
        qty = self.option.qty
        self.trade_book.close_all_open(self.option.symbol, now_str, option_price, exit_is_backfill=is_backfill)
        logger.info("EXIT OPTION %s qty=%d @ %.2f (%s)%s", self.option.symbol, qty, option_price,
                    reason, " [BACKFILL]" if is_backfill else "")
        place_or_log_order(self.data_service, self.option.symbol, "SELL", qty, option_price, is_backfill)
        self.option.is_open = False
        self.option.entry_price = None
        self.option.qty = 0
        self.option.consecutive_weak = 0

    def _fetch_option_price(self, symbol: str) -> float | None:
        quote = self.data_service.get_quotes([symbol])
        try:
            return next(item["v"]["lp"] for item in quote.get("d", []) if item.get("n") == symbol)
        except (StopIteration, KeyError, TypeError):
            return None

    def square_off_all(self, at_time_str: str | None = None):
        """EOD square-off - mirrors signal_engine.SignalEngine.square_off_all()'s role/signature.
        A no-op if nothing is open."""
        if not self.option.is_open:
            return
        now_str = at_time_str or datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%H:%M:%S")
        self._exit_option_position(now_str, is_backfill=False, reason="EOD square-off")


# ----------------------------------------------------------------------------
# Orchestration entry points
# ----------------------------------------------------------------------------
def run_index_options_engine(data_service: FyersDataService, bias_direction: TradeDirection, *,
                              underlying: str, index_symbol: str, futures_symbol: str,
                              capital_allocated: float, otm_strikes_away: int | None = None,
                              symbol_master: pd.DataFrame | None = None) -> NiftyOptionsEngine | None:
    """
    Generic version of run_nifty_options_engine()/run_banknifty_options_engine() - both are
    thin wrappers around this, supplying their own underlying's config values. Builds and
    returns a NiftyOptionsEngine wired to `underlying`'s own futures-only signal; see those two
    wrappers' docstrings for the bias_direction contract.

    index_symbol is used for exactly one thing here: resolve_otm_option_symbol() reads its
    spot LTP to pick which OTM strike to buy. Everything else - the opening-range candle, the
    wick filter, and the entry/exit signal itself - comes from futures_symbol alone (see
    options_engine.py's module docstring for why).

    Returns None (never raises) if the contract couldn't be resolved or the futures symbol's
    opening-range candle isn't available yet - callers should treat that like equities'
    "empty shortlist" case: skip this market for today, other markets keep going
    independently.
    """
    option_type = "CE" if bias_direction == TradeDirection.LONG else "PE"
    resolved = resolve_otm_option_symbol(data_service, option_type, symbol_master=symbol_master,
                                          strikes_away=otm_strikes_away, underlying=underlying,
                                          index_symbol=index_symbol)
    if resolved is None:
        return None
    option_symbol, lot_size = resolved

    futures_or_candle = fetch_opening_range_candle(data_service, futures_symbol)
    if futures_or_candle is None:
        logger.warning("No opening-range candle yet for %s - can't start today's options signal.",
                        futures_symbol)
        return None
    _, or_open, or_high, or_low, or_close, or_vol = futures_or_candle

    # Opening-range "closing strength" gate (see config.MAX_OR_CANDLE_WICK_PCT's docs) - a
    # per-DAY go/no-go check on the FUTURE's own opening-range candle, the options counterpart
    # to equities' per-STOCK wick filter in opening_range_scanner.py. Too wide a reversal wick
    # against bias_direction here means the signal instrument itself already showed a reversal
    # in its first 5 minutes - skip options entirely for today rather than trade a signal built
    # on a weak opening candle. Checked BEFORE building the engine, same as the "no candle yet"
    # case above, so no engine/OTM-resolution work is wasted on a day we're skipping anyway.
    wick_pct = or_candle_wick_pct(or_open, or_high, or_low, or_close, bias_direction)
    if wick_pct > config.MAX_OR_CANDLE_WICK_PCT:
        logger.warning("%s opening-range candle closing strength failed the filter (wick_pct=%.1f%% > "
                        "config.MAX_OR_CANDLE_WICK_PCT=%.1f%%, %s bias) - skipping %s options for "
                        "today.", futures_symbol, wick_pct, config.MAX_OR_CANDLE_WICK_PCT,
                        bias_direction.value, underlying)
        return None

    engine = NiftyOptionsEngine(data_service, option_symbol, lot_size, bias_direction,
                                 or_high, or_low, capital_allocated,
                                 futures_symbol=futures_symbol, underlying=underlying)

    # Seed the futures tracker's VWAP from its own opening-range candle (see
    # signal_engine.seed_opening_range_candle's docstring for why this is necessary and why
    # it deliberately never touches volume_window - that's still seeded separately, right
    # after this call returns, by main.py's fetch_and_seed_volume_windows(engine, ...)). Already
    # fetched above (both to get or_high/or_low and to run the wick filter), so no second fetch
    # is needed here.
    seed_opening_range_candle(engine.futures_tracker, futures_or_candle)

    logger.info("%s options engine ready: BUY-ONLY %s %s (lot_size=%d, capital_allocated=%.2f), "
                "signal from futures=%s (OR high=%.2f low=%.2f), OTM strike resolved from %s spot",
                underlying, option_type, option_symbol, lot_size, capital_allocated,
                futures_symbol, or_high, or_low, index_symbol)
    return engine


def run_nifty_options_engine(data_service: FyersDataService, bias_direction: TradeDirection,
                              symbol_master: pd.DataFrame | None = None) -> NiftyOptionsEngine | None:
    """
    bias_direction: today's sentiment-derived directional bias - pass in
    EXACTLY what bootstrap.decide_todays_direction() returned for equities
    today (main.py's job_opening_range_scan() already computes this once;
    reuse that same value, don't recompute sentiment here, so the two
    strategies can never silently disagree on today's bias).

    LONG bias -> buy an OTM call (CE). SHORT bias -> buy an OTM put (PE).
    Returns None (never raises) if the contract couldn't be resolved or the
    futures symbol's opening-range candle isn't available yet - callers
    should treat that like equities' "empty shortlist" case: skip options
    for today, other markets keep going independently.
    """
    return run_index_options_engine(data_service, bias_direction, underlying="NIFTY",
                                     index_symbol=config.NIFTY_INDEX_SYMBOL,
                                     futures_symbol=config.NIFTY_FUTURES_SYMBOL,
                                     capital_allocated=config.NIFTY_OPTIONS_CAPITAL,
                                     otm_strikes_away=config.NIFTY_OTM_STRIKES_AWAY,
                                     symbol_master=symbol_master)


def run_banknifty_options_engine(data_service: FyersDataService, bias_direction: TradeDirection,
                                  symbol_master: pd.DataFrame | None = None) -> NiftyOptionsEngine | None:
    """
    Identical rule set to run_nifty_options_engine() (single-source signal: PRICE/VWAP/VOLUME
    all from config.BANKNIFTY_FUTURES_SYMBOL, OTM strike spot from config.BANKNIFTY_INDEX_SYMBOL
    - see this module's docstring), pointed at BANKNIFTY instead of NIFTY. bias_direction has
    the same contract: pass in today's ALREADY-DECIDED sentiment direction, shared across every
    market this agent trades - never recompute it per market.

    LONG bias -> buy an OTM BANKNIFTY call (CE). SHORT bias -> buy an OTM BANKNIFTY put (PE).
    Returns None (never raises) under the same conditions as run_nifty_options_engine().
    """
    return run_index_options_engine(data_service, bias_direction, underlying="BANKNIFTY",
                                     index_symbol=config.BANKNIFTY_INDEX_SYMBOL,
                                     futures_symbol=config.BANKNIFTY_FUTURES_SYMBOL,
                                     capital_allocated=config.BANKNIFTY_OPTIONS_CAPITAL,
                                     otm_strikes_away=config.BANKNIFTY_OTM_STRIKES_AWAY,
                                     symbol_master=symbol_master)
