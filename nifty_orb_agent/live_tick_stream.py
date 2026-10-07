"""
live_tick_stream.py
----------------------
Thin wrapper around Fyers' WebSocket tick feed (fyers_apiv3.FyersWebsocket.data_ws).

NOT CURRENTLY USED by main.py. This module originally routed every incoming
tick into SignalEngine.check_tick_for_exit() for fast, tick-level stop-loss/
target/trailing-SL management. That tick-level risk management has been
removed - stop-loss is now purely candle/VWAP-driven (see signal_engine.py's
module docstring): the ONLY way a position closes before EOD is 2
consecutive 5-min candles confirming the move against it, checked by
main.py's job_check_signals every CANDLE_INTERVAL_MINUTES, not by anything
tick-based. With no consumer left for tick data, main.py no longer
constructs or starts a LiveTickStream.

This class is kept, fully working and fully testable (via on_tick_override,
see below), in case you want to wire ticks to something else later - e.g.
a live LTP/unrealized-P&L display, or feeding a future, deliberately
reintroduced tick-level check. _handle_tick() below is currently a no-op
placeholder for that reason.

Verify against your installed fyers_apiv3 version before relying on this:
the WebSocket class name, callback signatures, and tick message field names
(symbol/ltp/...) are pinned to what Fyers documents for API v3 as of when
this was written - print one raw tick message once and compare against
ON_TICK_EXPECTED_FIELDS before trusting this in production. Broker
WebSocket APIs are exactly the kind of thing that changes between SDK
versions without much warning.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

import pytz

import config
from signal_engine import SignalEngine

logger = logging.getLogger("orb_agent.live_tick")

# What this module reads from each tick message - verify against your fyers_apiv3 version's
# actual "SymbolUpdate" message shape.
ON_TICK_EXPECTED_FIELDS = ("symbol", "ltp")


class LiveTickStream:
    """
    Wraps the Fyers WebSocket connection. Construct with an already-caught-up
    SignalEngine (see bootstrap.bootstrap_and_catchup()) and call start()
    once - every tick for a symbol the engine is tracking is routed to
    _handle_tick(), currently a no-op placeholder (see module docstring -
    tick-level exits were removed; risk management is candle-only now).

    Testable without a real socket: pass on_tick_override to bypass the
    actual WebSocket entirely (start() then just logs and returns), and
    feed synthetic tick dicts directly through _handle_tick() in tests.
    """

    def __init__(self, access_token: str, engine: SignalEngine,
                 on_tick_override: Optional[Callable[[dict], None]] = None):
        self.access_token = access_token
        self.engine = engine
        self._tz = pytz.timezone(config.TIMEZONE)
        self._on_tick_override = on_tick_override
        self._socket = None

    def _handle_tick(self, message: dict) -> None:
        """
        Currently a no-op placeholder: validates the message shape and
        drops anything for a symbol this engine isn't tracking, then
        returns. There is no tick-level exit to call any more - see the
        module docstring. Kept as the integration point for whatever a
        future tick consumer turns out to be (live LTP display, etc.) so
        that work is a one-line addition here, not new wiring in main.py.
        """
        symbol = message.get("symbol")
        ltp = message.get("ltp")
        if symbol is None or ltp is None:
            logger.warning("Tick message missing expected field(s) %s - got keys %s. "
                            "Verify your fyers_apiv3 version's tick message shape.",
                            ON_TICK_EXPECTED_FIELDS, list(message.keys()))
            return
        if symbol not in self.engine.trackers:
            return  # not one of today's tracked symbols - normal/expected, not an error
        now_str = datetime.now(self._tz).strftime("%H:%M:%S")
        logger.debug("Tick received for %s @ %.2f (%s) - no-op, risk management is candle-only now.",
                     symbol, float(ltp), now_str)

    def start(self) -> None:
        symbols = list(self.engine.trackers.keys())
        if not symbols:
            logger.warning("LiveTickStream.start() called with no tracked symbols - nothing to subscribe to.")
            return

        if self._on_tick_override is not None:
            logger.info("LiveTickStream started in test/override mode for %d symbol(s) - "
                        "no real WebSocket connection made.", len(symbols))
            return  # tests call _handle_tick directly instead

        # Imported here (not at module level) so this module stays importable - and unit-
        # testable via on_tick_override above - even in an environment where fyers_apiv3's
        # WebSocket extra isn't installed.
        from fyers_apiv3.FyersWebsocket import data_ws

        def on_open():
            self._socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
            self._socket.keep_running()

        def on_message(message):
            self._handle_tick(message)

        def on_error(message):
            logger.error("Fyers WebSocket error: %s", message)

        def on_close(message):
            logger.warning("Fyers WebSocket closed: %s", message)

        self._socket = data_ws.FyersDataSocket(
            access_token=self.access_token, log_path=config.LOG_DIR,
            litemode=False, write_to_file=False, reconnect=True,
            on_connect=on_open, on_close=on_close, on_error=on_error, on_message=on_message,
        )
        logger.info("Connecting live tick WebSocket for %d symbol(s): %s", len(symbols), symbols)
        self._socket.connect()

    def stop(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close_connection()
            except Exception as exc:
                logger.warning("Error closing WebSocket connection: %s", exc)
