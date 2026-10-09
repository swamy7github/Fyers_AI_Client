"""
options_engine.py
--------------------
NIFTY INDEX options, DUAL-SOURCE signal / single-leg execution. The exact same
engine/rules apply verbatim to BANK NIFTY (NIFTYBANK) via
run_banknifty_options_engine() / config.BANKNIFTY_* - see that function's
docstring; everything below is written for NIFTY but is equally true for
BANKNIFTY with its own index/futures/strike universe substituted in:

  - The ENTRY/EXIT *signal* is computed from TWO instruments, neither of
    which is the option contract itself:
      1. config.NIFTY_INDEX_SYMBOL (the NIFTY 50 index) supplies the
         PRICE condition - its own 09:15 opening-range candle's high/low,
         and its own running VWAP.
      2. config.NIFTY_FUTURES_SYMBOL (the current front-month NIFTY
         future) supplies the VOLUME condition - two consecutive candles'
         volume above its own 20-period volume MA. The index itself is
         typically reported with ZERO volume in Fyers' feed (it isn't a
         tradable instrument), so a volume-MA check needs a real,
         continuously-traded proxy instead - the future is the natural
         one, since it tracks the index almost 1:1. VERIFY this against a
         raw quote/candle for your own NIFTY_INDEX_SYMBOL before relying
         on it - if your feed DOES report real index volume, nothing here
         needs to change, this module just doesn't assume it does.
  - ENTRY (today's bias is decided once, by the SAME sentiment pipeline
    equities/commodities use - bootstrap.decide_todays_direction(), passed
    in as `bias_direction` - not recomputed here):
      LONG bias:  2 consecutive index candles close ABOVE the index's own
                  opening-range HIGH *and* above its own VWAP, AND (at
                  those same two candles) the FUTURE's volume is above its
                  own 20-period volume MA on both -> BUY an OTM CALL (CE).
      SHORT bias: the mirror image - 2 consecutive index candles close
                  BELOW the opening-range LOW *and* below VWAP, with the
                  same future-volume confirmation -> BUY an OTM PUT (PE).
  - EXIT - once a position is open, the mirrored condition on the SAME two
    instruments: 2 consecutive index candles closing back across VWAP
    (below it for a LONG/CE position, above it for a SHORT/PE position),
    again with the future's volume above its own 20-period MA on both,
    closes the ENTIRE option position. There is no separate add-on/
    multi-stage stop-loss here (unlike equities/commodities' weakness-event
    ADD rule) - the request behind this module described a single entry and
    a single exit, and doubling into an already-leveraged option contract
    on "weakness" is a materially different risk than doing so for an
    equity, so that wasn't carried over uninvited.
  - The instrument actually BOUGHT/SOLD is a single OTM (out-of-the-money)
    NIFTY option, resolved fresh each day from Fyers' own F&O symbol master
    file (never a hard-coded strike/expiry string - see
    config.NIFTY_FO_SYMBOL_MASTER_CSV/NIFTY_FO_MASTER_COLUMNS). BUY-ONLY,
    same reasoning as before this rewrite: risk capped to premium paid,
    never writes/sells an option naked.

NiftyOptionsEngine (below) exposes the same duck-typed surface as
signal_engine.SignalEngine (`.trade_book`, `.check_all()`,
`.square_off_all()`, `.trackers`) so main.py's job_check_signals()/
job_square_off_and_report() drive it exactly like the equity/commodity
engines, with zero special-casing there.

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
from fyers_service import FyersDataService
from signal_engine import StockTracker, TradeBook, place_or_log_order
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


def fetch_index_opening_range(data_service: FyersDataService,
                               index_symbol: str | None = None) -> tuple[float, float] | None:
    """
    Fetches `index_symbol`'s (config.NIFTY_INDEX_SYMBOL by default - pass
    config.BANKNIFTY_INDEX_SYMBOL for Bank Nifty) own 09:15-09:20
    opening-range candle and returns (or_high, or_low) - the PRICE-condition
    baseline for the whole day (see module docstring). Returns None (never
    raises) if that candle isn't available yet.
    """
    index_symbol = index_symbol or config.NIFTY_INDEX_SYMBOL
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    start = tz.localize(datetime(now.year, now.month, now.day, 9, 15, 0))
    end = tz.localize(datetime(now.year, now.month, now.day, 9, 20, 0))
    candles = data_service.get_5min_candle(index_symbol, now.strftime("%Y-%m-%d"),
                                            int(start.timestamp()), int(end.timestamp()))
    if not candles:
        logger.warning("No opening-range candle yet for %s - can't start today's options signal.",
                        index_symbol)
        return None
    _, _, h, l, _, _ = candles[0]
    return h, l


# ----------------------------------------------------------------------------
# The dual-source engine itself
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
    Drives one OTM NIFTY option position from the PRICE condition on
    config.NIFTY_INDEX_SYMBOL and the VOLUME condition on
    config.NIFTY_FUTURES_SYMBOL (see module docstring for the exact rules).

    Exposes `.trade_book`, `.check_all(window_start_epoch, window_end_epoch)`,
    `.square_off_all(at_time_str=None)`, and `.trackers` with the same
    signatures signal_engine.SignalEngine uses, so main.py's generic
    per-engine loop (job_check_signals/job_square_off_and_report) can drive
    this with no special-casing - see main.py's state dict.
    """

    def __init__(self, data_service: FyersDataService, option_symbol: str, option_lot_size: int,
                 bias_direction: TradeDirection, index_or_high: float, index_or_low: float,
                 capital_allocated: float, index_symbol: str | None = None,
                 futures_symbol: str | None = None, underlying: str = "NIFTY"):
        self.data_service = data_service
        self.bias_direction = bias_direction  # today's sentiment bias - decides the mirrored
                                               # price/VWAP comparison direction below; the
                                               # CE-vs-PE choice is already baked into option_symbol
        self.underlying = underlying  # "NIFTY" or "BANKNIFTY" - purely cosmetic, used in log/reason
                                       # strings below; the actual signal sources are index_symbol/
                                       # futures_symbol, resolved independently of this label.
        index_symbol = index_symbol or config.NIFTY_INDEX_SYMBOL
        futures_symbol = futures_symbol or config.NIFTY_FUTURES_SYMBOL
        self.index_tracker = StockTracker(symbol=index_symbol, or_high=index_or_high,
                                           or_low=index_or_low, direction=bias_direction)
        self.futures_tracker = StockTracker(symbol=futures_symbol, direction=bias_direction)
        self.option = _OptionPosition(symbol=option_symbol, lot_size=option_lot_size,
                                       capital_allocated=capital_allocated)
        self.trade_book = TradeBook()

    @property
    def trackers(self) -> dict[str, StockTracker]:
        """Only the FUTURES tracker needs volume-MA warm-up seeding (see
        bootstrap.fetch_and_seed_volume_windows / signal_engine.seed_volume_windows_from_candles,
        both of which only ever touch `.volume_window`) - the index tracker's VWAP is never
        pre-seeded, exactly like equities' own VWAP isn't. Exposed as a property (not a plain
        dict) so main.py's informational prints and the seeding helper both "just work" without
        this engine needing to know which one actually wants it."""
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
        (is_backfill=False); see feed_candles() for the historical-replay equivalent."""
        now_str = datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%H:%M:%S")
        index_candle = self._fetch_latest_candle(self.index_tracker.symbol, window_start_epoch, window_end_epoch)
        futures_candle = self._fetch_latest_candle(self.futures_tracker.symbol, window_start_epoch, window_end_epoch)
        if index_candle is None or futures_candle is None:
            logger.warning("Missing a candle this tick (index=%s, futures=%s) - skipping this "
                            "check rather than acting on a partial signal.",
                            "ok" if index_candle is not None else "MISSING",
                            "ok" if futures_candle is not None else "MISSING")
            return
        self.feed_candles(index_candle, futures_candle, now_str, is_backfill=False)

    def feed_candles(self, index_candle: list, futures_candle: list, now_str: str, is_backfill: bool = False):
        """
        Public entry point for feeding ONE already-fetched (index_candle, futures_candle) pair
        directly - mirrors signal_engine.SignalEngine.feed_candle()'s role for historical replay/
        backtesting (bootstrap.py's catch-up pipeline would call this with is_backfill=True).
        """
        # --- PRICE/VWAP source: the index's own candle ---
        _, _, ih, il, ic, iv = index_candle
        i_typical = (ih + il + ic) / 3
        self.index_tracker.candles.append(index_candle)
        self.index_tracker.cum_pv += i_typical * iv
        self.index_tracker.cum_vol += iv
        index_vwap = self.index_tracker.vwap()

        # --- VOLUME source: the future's own candle. MA computed BEFORE this candle joins its
        # window (trailing average, excludes the current bar), same convention signal_engine
        # uses for equities/commodities. ---
        _, _, _, _, _, fv = futures_candle
        vol_ma_slow = self.futures_tracker.volume_ma()
        self.futures_tracker.candles.append(futures_candle)
        self.futures_tracker.volume_window.append(fv)

        if self.option.is_open:
            self._check_exit(ic, index_vwap, fv, vol_ma_slow, now_str, is_backfill)
        else:
            self._check_entry(ic, index_vwap, fv, vol_ma_slow, now_str, is_backfill)

    # ------------------------------------------------------------------
    # Entry / exit
    # ------------------------------------------------------------------
    def _check_entry(self, index_close: float, index_vwap: float, futures_vol: float,
                      vol_ma_slow: Optional[float], now_str: str, is_backfill: bool):
        tr = self.index_tracker
        if self.bias_direction == TradeDirection.LONG:
            qualifies = (vol_ma_slow is not None and tr.or_high is not None
                         and index_close > tr.or_high and index_close > index_vwap
                         and futures_vol > vol_ma_slow)
        else:
            qualifies = (vol_ma_slow is not None and tr.or_low is not None
                         and index_close < tr.or_low and index_close < index_vwap
                         and futures_vol > vol_ma_slow)
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
        logger.info("BUY OPTION %s qty=%d @ %.2f (%s index 2-candle breakout confirmed by "
                    "%s volume > 20-period MA)%s", self.option.symbol, qty, option_price,
                    self.underlying, self.futures_tracker.symbol, " [BACKFILL]" if is_backfill else "")
        place_or_log_order(self.data_service, self.option.symbol, "BUY", qty, option_price, is_backfill)

    def _check_exit(self, index_close: float, index_vwap: float, futures_vol: float,
                     vol_ma_slow: Optional[float], now_str: str, is_backfill: bool):
        if self.bias_direction == TradeDirection.LONG:
            is_weak = (vol_ma_slow is not None) and (index_close < index_vwap) and (futures_vol > vol_ma_slow)
            vs_vwap = "below"
        else:
            is_weak = (vol_ma_slow is not None) and (index_close > index_vwap) and (futures_vol > vol_ma_slow)
            vs_vwap = "above"

        self.option.consecutive_weak = self.option.consecutive_weak + 1 if is_weak else 0
        if self.option.consecutive_weak < config.CONSECUTIVE_CANDLES_FOR_STOPLOSS:
            return
        self.option.consecutive_weak = 0
        reason = (f"2 consecutive {self.underlying} index candles closed {vs_vwap} VWAP {index_vwap:.2f} with "
                  f"{self.futures_tracker.symbol} volume > its 20-period MA")
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
    returns a NiftyOptionsEngine wired to `underlying`'s own index/futures/strike universe;
    see those two wrappers' docstrings for the bias_direction contract.

    Returns None (never raises) if the contract couldn't be resolved or the index's
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

    opening_range = fetch_index_opening_range(data_service, index_symbol=index_symbol)
    if opening_range is None:
        return None
    or_high, or_low = opening_range

    engine = NiftyOptionsEngine(data_service, option_symbol, lot_size, bias_direction,
                                 or_high, or_low, capital_allocated, index_symbol=index_symbol,
                                 futures_symbol=futures_symbol, underlying=underlying)
    logger.info("%s options engine ready: BUY-ONLY %s %s (lot_size=%d, capital_allocated=%.2f), "
                "signal from index=%s (OR high=%.2f low=%.2f) + futures=%s",
                underlying, option_type, option_symbol, lot_size, capital_allocated,
                index_symbol, or_high, or_low, futures_symbol)
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
    index's opening-range candle isn't available yet - callers should treat
    that like equities' "empty shortlist" case: skip options for today,
    other markets keep going independently.
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
    Identical rule set to run_nifty_options_engine() (dual-source signal: PRICE/VWAP from
    config.BANKNIFTY_INDEX_SYMBOL, VOLUME from config.BANKNIFTY_FUTURES_SYMBOL - see this
    module's docstring), pointed at BANKNIFTY instead of NIFTY. bias_direction has the same
    contract: pass in today's ALREADY-DECIDED sentiment direction, shared across every market
    this agent trades - never recompute it per market.

    LONG bias -> buy an OTM BANKNIFTY call (CE). SHORT bias -> buy an OTM BANKNIFTY put (PE).
    Returns None (never raises) under the same conditions as run_nifty_options_engine().
    """
    return run_index_options_engine(data_service, bias_direction, underlying="BANKNIFTY",
                                     index_symbol=config.BANKNIFTY_INDEX_SYMBOL,
                                     futures_symbol=config.BANKNIFTY_FUTURES_SYMBOL,
                                     capital_allocated=config.BANKNIFTY_OPTIONS_CAPITAL,
                                     otm_strikes_away=config.BANKNIFTY_OTM_STRIKES_AWAY,
                                     symbol_master=symbol_master)
