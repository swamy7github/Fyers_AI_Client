"""
test_live_tick_stream.py
----------------------------
Tests for live_tick_stream.py's _handle_tick() message-validation logic and
start()'s safe no-socket test path (on_tick_override). Doesn't touch a real
WebSocket or fyers_apiv3.FyersWebsocket at all - that import only happens
inside start() when on_tick_override is None, which none of these tests
trigger.

_handle_tick() is currently a no-op placeholder (tick-level risk management
was removed - see live_tick_stream.py's module docstring), so these tests
only confirm it validates message shape and never raises, for a tracked or
untracked symbol, well-formed or malformed messages alike.

Run with:
    python test_live_tick_stream.py
"""

from __future__ import annotations

import unittest

from live_tick_stream import LiveTickStream


class FakeEngine:
    """Only implements what LiveTickStream actually touches: .trackers."""

    def __init__(self, symbols):
        self.trackers = {s: object() for s in symbols}


class TestHandleTick(unittest.TestCase):
    """_handle_tick() has nothing left to call on the engine - these just confirm it
    never raises, for every combination of tracked/untracked symbol and well-formed/
    malformed message."""

    def test_valid_tick_for_tracked_symbol_does_not_raise(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({"symbol": "NSE:TEST-EQ", "ltp": 101.5})  # should not raise

    def test_ltp_as_a_string_does_not_raise(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({"symbol": "NSE:TEST-EQ", "ltp": "101.5"})  # string, as some feeds send it

    def test_untracked_symbol_is_silently_ignored(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({"symbol": "NSE:OTHER-EQ", "ltp": 50.0})  # should not raise

    def test_missing_symbol_field_does_not_crash(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({"ltp": 101.5})  # no 'symbol' key

    def test_missing_ltp_field_does_not_crash(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({"symbol": "NSE:TEST-EQ"})  # no 'ltp' key

    def test_empty_message_does_not_crash(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({})


class TestStart(unittest.TestCase):
    def test_start_with_override_does_not_touch_the_real_websocket_module(self):
        # If start() tried to `from fyers_apiv3.FyersWebsocket import data_ws` here, it would
        # raise ModuleNotFoundError in this test environment (not installed/stubbed) - the
        # fact that this completes cleanly proves the on_tick_override early-return works.
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream.start()  # should not raise

    def test_start_with_no_tracked_symbols_logs_and_returns(self):
        engine = FakeEngine([])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream.start()  # should not raise even without override, since it returns before the import

    def test_stop_with_no_socket_established_does_not_raise(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream.stop()  # _socket is still None - should be a safe no-op


if __name__ == "__main__":
    unittest.main(verbosity=2)
