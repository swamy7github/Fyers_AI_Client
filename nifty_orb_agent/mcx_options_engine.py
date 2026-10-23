"""
mcx_options_engine.py
------------------------
MCX commodity OPTIONS trading - the OPTIONS counterpart to commodity_engine.py, the same way
options_engine.py is the options counterpart to signal_engine.py's equity engine. Off by
default (config.ENABLE_MCX_OPTIONS_TRADING = False) - read this whole module, options_engine.py's
module docstring (the exact single-source signal/strike-resolution rules this module reuses
verbatim), and the "MCX commodity OPTIONS trading" block in config.py before turning it on.

Why this is a SEPARATE module from commodity_engine.py, not a flag on it
---------------------------------------------------------------------------
commodity_engine.py trades FUTURES directly: one tracked instrument per commodity, long or
short, on ONE shared daily direction (config.MCX_DIRECTION_REFERENCE_SYMBOL or commodity
sentiment - see that config value's own docstring). This module trades OPTIONS instead -
BUY-ONLY CE/PE, ITM/ATM/OTM (config.OPTION_STRIKE_MODE, the SAME shared knob NIFTY/BANKNIFTY
options use) - and, critically, each commodity decides its OWN entry direction from its OWN
futures candles, bidirectionally, exactly like options_engine.py's NiftyOptionsEngine already
does for NIFTY/BANKNIFTY (see that module's docstring for the full PRICE/VWAP/VOLUME rule set -
it is NOT repeated here, because this module doesn't reimplement it: see MCXOptionsEngine below).
That's a different enough shape (BUY-only, strike-resolution step, per-commodity bidirectional
signal vs one shared daily long/short bias) that bolting it onto commodity_engine.py's
SignalEngine-based FUTURES engine would have meant two incompatible behaviours gated by one
flag - two separate engines, two separate ENABLE_* flags, two separate capital buckets and
trade books (can run together or independently - see config.py), mirrors every other pair of
strategies in this agent.

Why there's no separate MCX "spot index" for strike resolution
---------------------------------------------------------------------
NIFTY/BANKNIFTY options resolve their ITM/ATM/OTM strike from the INDEX's own spot LTP
(config.NIFTY_INDEX_SYMBOL/BANKNIFTY_INDEX_SYMBOL - see options_engine.py's module docstring) -
a separate instrument from the futures contract that supplies the entry/exit signal. MCX
commodities have no such separate spot index Fyers quotes directly; MCX commodity options are
themselves options ON THE FUTURE, so each commodity's OWN futures contract is used as BOTH the
signal source AND the strike-resolution "spot" reference (see MCXOptionsEngine below - it
passes `index_symbol=futures_symbol` to each commodity's underlying NiftyOptionsEngine, which
just means resolve_option_symbol() fetches that futures contract's own current LTP instead of
a separate index's).

Why there's no per-commodity reference-direction config like MCX_DIRECTION_REFERENCE_SYMBOL
---------------------------------------------------------------------------------------------
config.MCX_DIRECTION_REFERENCE_SYMBOL exists because commodity_engine.py's FUTURES engine needs
ONE shared direction for the whole universe (SignalEngine's own direction-per-construction
shape - see that config value's docstring for the full contrast). This module needs no such
thing: every commodity watches its OWN candles for its OWN breakout in EITHER direction, so
"which direction does commodity X trade today" is never a question this module has to answer
from a reference symbol at all - it's answered fresh, per commodity, at signal time, the same
way options_engine.py already answers it for NIFTY/BANKNIFTY. decide_todays_commodity_direction()
still runs once (reused from the SAME call commodity_engine.run_mcx_commodity_engine() makes)
purely to gate each commodity's own opening-range quality filter - see run_mcx_options_engine()'s
docstring.
"""

from __future__ import annotations

import logging

import pandas as pd

import commodity_engine
import config
import options_engine
from fyersclient.fyers_client import get_fyers_client, FyersDataService
from signal_engine import StockTracker, TradeBook, seed_opening_range_candle
from trade_direction import TradeDirection

logger = logging.getLogger("orb_agent.mcx_options")


def load_mcx_fo_symbol_master(csv_path: str | None = None) -> pd.DataFrame:
    """
    Thin wrapper over options_engine.load_fo_symbol_master(), pointed at MCX's OWN options
    instrument master file/column-mapping (config.MCX_FO_SYMBOL_MASTER_CSV/MCX_FO_MASTER_COLUMNS)
    instead of NSE's (config.NIFTY_FO_SYMBOL_MASTER_CSV/NIFTY_FO_MASTER_COLUMNS, NIFTY/BANKNIFTY's
    shared default) - MCX publishes its own, separate F&O instrument master (a different
    exchange segment entirely from NSE's), so this can't just call load_fo_symbol_master() with
    no arguments the way NIFTY/BANKNIFTY options do. Returns an empty DataFrame (never raises)
    under the same conditions as options_engine.load_fo_symbol_master() - see its docstring.
    """
    return options_engine.load_fo_symbol_master(csv_path or config.MCX_FO_SYMBOL_MASTER_CSV,
                                                  master_columns=config.MCX_FO_MASTER_COLUMNS)


class MCXOptionsEngine:
    """
    Runs options_engine.NiftyOptionsEngine's exact single-source, BOTH-SIDES, BUY-ONLY signal
    and strike-resolution logic INDEPENDENTLY for every commodity in config.MCX_COMMODITY_UNIVERSE
    at once. This class holds no strategy/signal logic of its own - it's a thin multiplexer: one
    NiftyOptionsEngine instance per commodity (that commodity's own futures_symbol as BOTH the
    signal source AND the index_symbol/strike-resolution "spot" reference - see this module's
    docstring for why there's no separate spot index for MCX), all sharing ONE TradeBook so
    main.py's reporting sees every commodity's trades in one place (mirrors SignalEngine's
    one-book-many-trackers shape for equities/MCX futures, rather than NiftyOptionsEngine's own
    one-book-one-leg shape, which only fits a SINGLE market).

    Exposes the exact same duck-typed surface every other engine in this codebase does -
    `.trade_book`, `.trackers`, `.check_all(window_start_epoch, window_end_epoch)`,
    `.feed_candle(symbol, candle, now_str, is_backfill=False)`, `.square_off_all(at_time_str=None)`
    - so main.py's generic per-engine loops AND bootstrap.py's generic _replay_intraday_history()/
    fetch_and_seed_volume_windows() drive this with ZERO special-casing, exactly like every other
    engine here (equities' SignalEngine, MCX futures' SignalEngine, NIFTY/BANKNIFTY options'
    NiftyOptionsEngine).
    """

    def __init__(self, data_service: FyersDataService, universe_df: pd.DataFrame,
                 capital_allocated: float, *, symbol_master: pd.DataFrame | None = None,
                 strike_mode: str | None = None, strikes_away: int | None = None):
        """
        universe_df: one row per commodity ALREADY traded-today-eligible (symbol, open, high,
        low, close, volume - exactly commodity_engine.fetch_mcx_universe_opening_ranges()'s own
        return shape, already past its opening-range wick/quality filter - see
        run_mcx_options_engine() below, which builds this the same way
        commodity_engine.run_mcx_commodity_engine() does for futures). `symbol` must match an
        entry in config.MCX_COMMODITY_UNIVERSE so this can look up that commodity's own
        option_underlying code.

        capital_allocated: the TOTAL MCX options capital bucket (config.MCX_OPTIONS_CAPITAL) -
        split EVENLY across every commodity actually present in `universe_df` via
        position_sizing.capital_per_slot(), mirroring commodity_engine.run_mcx_commodity_engine()'s
        own per-contract split for futures (and NOT the same pool as MCX_CAPITAL - see that
        config value's own docs on why these are deliberately separate buckets).
        """
        import position_sizing  # local import: scoped to here, same reasoning as
                                 # commodity_engine.run_mcx_commodity_engine()'s own comment on
                                 # why this doesn't need a module-level dependency.

        self.data_service = data_service
        self.trade_book = TradeBook()
        self.symbol_master = symbol_master if symbol_master is not None else load_mcx_fo_symbol_master()

        underlying_lookup = {row["symbol"]: row.get("option_underlying")
                              for row in config.MCX_COMMODITY_UNIVERSE}
        per_slot_capital = position_sizing.capital_per_slot(capital_allocated, len(universe_df))

        # One full NiftyOptionsEngine per commodity - see class docstring for why this reuses
        # that class wholesale instead of reimplementing its entry/exit rules here.
        self._engines: dict[str, options_engine.NiftyOptionsEngine] = {}
        for _, row in universe_df.iterrows():
            futures_symbol = row["symbol"]
            option_underlying = underlying_lookup.get(futures_symbol)
            if not option_underlying:
                logger.error("%s has no (or an empty) 'option_underlying' entry in "
                              "config.MCX_COMMODITY_UNIVERSE - can't tell which symbol_master "
                              "underlying_symbol rows are this commodity's own options. Skipping "
                              "MCX OPTIONS for this contract today (its FUTURES engine, if "
                              "config.ENABLE_MCX_COMMODITY_TRADING is on, is unaffected - that's "
                              "a completely independent engine - see this module's docstring).",
                              futures_symbol)
                continue

            sub = options_engine.NiftyOptionsEngine(
                data_service, futures_symbol, row["high"], row["low"], per_slot_capital,
                index_symbol=futures_symbol,  # no separate spot index for a commodity - the
                                               # future's own LTP IS the strike-resolution spot
                                               # (see this module's docstring).
                underlying=option_underlying,
                strikes_away=strikes_away, strike_mode=strike_mode,
                symbol_master=self.symbol_master, master_columns=config.MCX_FO_MASTER_COLUMNS,
                # MCX's OWN configurable candle width (config.MCX_CANDLE_INTERVAL_MINUTES), NOT
                # NiftyOptionsEngine's "5" default - without this, this sub-engine's LIVE
                # check_all() loop would always fetch 5-min bars regardless of what MCX is
                # actually configured to (see NiftyOptionsEngine.__init__'s own comment).
                candle_resolution=str(config.MCX_CANDLE_INTERVAL_MINUTES),
            )
            # Seed VWAP from the SAME opening-range candle already used for OR high/low above -
            # mirrors options_engine.run_index_options_engine()'s own seed_opening_range_candle()
            # call (volume_window is still seeded separately, right after this engine is built,
            # by bootstrap.fetch_and_seed_volume_windows() - same convention as every other
            # engine in this codebase, see seed_opening_range_candle()'s own docstring).
            seed_opening_range_candle(
                sub.futures_tracker,
                [0, row["open"], row["high"], row["low"], row["close"], row["volume"]])
            sub.trade_book = self.trade_book  # ONE shared book across every commodity - see class docstring.
            self._engines[futures_symbol] = sub

    @property
    def trackers(self) -> dict[str, StockTracker]:
        """Every commodity's own single futures tracker, merged into one dict - see
        NiftyOptionsEngine.trackers' own docstring for why each sub-engine only ever has one.
        Exposed for the exact same reasons SignalEngine/NiftyOptionsEngine expose theirs: main.py's
        informational prints, bootstrap.fetch_and_seed_volume_windows(), and
        bootstrap._replay_intraday_history() all "just work" without any of them knowing this
        engine is secretly N separate sub-engines underneath."""
        merged: dict[str, StockTracker] = {}
        for sub in self._engines.values():
            merged.update(sub.trackers)
        return merged

    def check_all(self, window_start_epoch: int, window_end_epoch: int):
        """Called every MCX_CANDLE_INTERVAL_MINUTES by the scheduler, exactly like
        commodity_engine's SignalEngine.check_all() - delegates to every commodity's own
        sub-engine in turn, each fetching and checking its OWN futures candle independently."""
        for sub in self._engines.values():
            sub.check_all(window_start_epoch, window_end_epoch)

    def feed_candle(self, symbol: str, candle: list, now_str: str, is_backfill: bool = False):
        """Public entry point for feeding ONE already-fetched futures candle for ONE commodity
        directly - mirrors SignalEngine.feed_candle()/NiftyOptionsEngine.feed_candle()'s
        signature exactly, so bootstrap.py's generic _replay_intraday_history() can replay this
        engine's historical candles with no MCX-options-specific replay helper needed."""
        for sub in self._engines.values():
            if symbol in sub.trackers:
                sub.feed_candle(symbol, candle, now_str, is_backfill=is_backfill)
                return
        raise KeyError(f"{symbol!r} is not one of this engine's tracked symbols "
                        f"({list(self.trackers)}).")

    def square_off_all(self, at_time_str: str | None = None):
        """EOD square-off - mirrors every other engine's square_off_all()'s role/signature. A
        no-op for any commodity with nothing open (delegated straight to NiftyOptionsEngine's
        own no-op guard)."""
        for sub in self._engines.values():
            sub.square_off_all(at_time_str)

    def open_positions_count(self) -> int:
        """How many commodities currently have an OPEN option leg - purely informational (used
        by bootstrap.bootstrap_mcx_options_catchup()'s own log line, mirrors the equivalent
        `sum(1 for t in engine.trackers.values() if t.position_open)` every SignalEngine-based
        catch-up log computes), since this engine's N separate `.option` legs aren't reachable
        through `.trackers` (that only ever holds FUTURES trackers - see that property's own
        docstring)."""
        return sum(1 for sub in self._engines.values() if sub.option.is_open)


def run_mcx_options_engine(data_service: FyersDataService, bias_direction: TradeDirection,
                            symbol_master: pd.DataFrame | None = None,
                            strike_mode: str | None = None) -> MCXOptionsEngine | None:
    """
    bias_direction: today's COMMODITY-specific sentiment-derived directional bias - pass in
    EXACTLY what bootstrap.decide_todays_commodity_direction() returned (the SAME read
    commodity_engine.run_mcx_commodity_engine() uses - never recomputed separately here, so the
    futures and options engines can never silently disagree on what counts as a clean opening
    candle today). Used for exactly ONE thing, mirroring options_engine.run_index_options_engine():
    gating EACH commodity's own opening-range "closing strength" quality filter
    (config.MAX_OR_CANDLE_WICK_PCT, the SAME shared threshold every market in this agent uses).
    It does NOT pick CE vs PE for any commodity, and it is NOT config.MCX_DIRECTION_REFERENCE_SYMBOL's
    role repeated here - see mcx_options_engine.py's module docstring and that config value's own
    docstring for the full contrast with the FUTURES engine.

    Builds the whole universe's opening ranges via commodity_engine.fetch_mcx_universe_opening_ranges()
    - the EXACT SAME function the futures engine uses (no gain/loss-band shortlisting, every
    MCX_COMMODITY_UNIVERSE symbol considered, the SAME wick-filter quality gate - see that
    function's own docstring) - so a commodity excluded from futures today (data gap, or failed
    the wick filter) is excluded from options today for the identical reason, never silently
    re-included or re-excluded by a second, slightly different fetch.

    Returns None (never raises) if the MCX options symbol master is empty/unreadable, OR if NOT
    A SINGLE commodity in the universe had its opening-range candle available today (mirrors
    run_mcx_commodity_engine()'s "nothing to trade" case) - callers should treat that like every
    other market's "empty shortlist" case: skip MCX options for today, other markets (including
    MCX futures, a completely independent engine) keep going independently.
    """
    master = symbol_master if symbol_master is not None else load_mcx_fo_symbol_master()
    if master.empty:
        return None

    universe_df = commodity_engine.fetch_mcx_universe_opening_ranges(data_service, direction=bias_direction)
    if universe_df.empty:
        logger.warning("No opening-range candle was available for ANY symbol in "
                        "MCX_COMMODITY_UNIVERSE today - nothing to trade (MCX options).")
        return None

    engine = MCXOptionsEngine(data_service, universe_df, config.MCX_OPTIONS_CAPITAL,
                               symbol_master=master, strike_mode=strike_mode,
                               strikes_away=config.MCX_OTM_STRIKES_AWAY)
    if not engine.trackers:
        logger.warning("MCX options: every commodity in today's universe was missing a usable "
                        "option_underlying (see per-contract errors above) - nothing to trade.")
        return None

    logger.info("MCX options engine ready: %s, %d/%d universe contract(s) tracked (BUY-ONLY "
                "CE/PE per commodity, each watching BOTH its own futures breakout directions "
                "independently; strike mode=%s, %s strikes away, resolved fresh from each "
                "commodity's own futures LTP AT SIGNAL TIME) - today's %s commodity bias only "
                "gated each contract's own opening-range quality filter, nothing else.",
                bias_direction.value, len(engine.trackers), len(config.MCX_COMMODITY_UNIVERSE),
                (strike_mode or config.OPTION_STRIKE_MODE), config.MCX_OTM_STRIKES_AWAY,
                bias_direction.value)
    return engine
