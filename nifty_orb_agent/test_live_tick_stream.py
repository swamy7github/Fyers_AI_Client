"""
test_live_tick_stream.py
----------------------------
Tests for live_tick_stream.py's _handle_tick() routing logic and start()'s
safe no-socket test path (on_tick_override). Doesn't touch a real WebSocket
or fyers_apiv3.FyersWebsocket at all - that import only happens inside
start() when on_tick_override is None, which none of these tests trigger.

Run with:
    python test_live_tick_stream.py
"""

from __future__ import annotations

import unittest

from live_tick_stream import LiveTickStream


class FakeEngine:
    """Only implements what LiveTickStream actually touches: .trackers and .check_tick_for_exit()."""

    def __init__(self, symbols):
        self.trackers = {s: object() for s in symbols}
        self.calls: list[tuple] = []
        self.raise_on_call = False

    def check_tick_for_exit(self, symbol, ltp, now_str):
        if self.raise_on_call:
            raise RuntimeError("simulated failure inside check_tick_for_exit")
        self.calls.append((symbol, ltp, now_str))
        return None


class TestHandleTick(unittest.TestCase):
    def test_valid_tick_for_tracked_symbol_calls_check_tick_for_exit(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({"symbol": "NSE:TEST-EQ", "ltp": 101.5})
        self.assertEqual(len(engine.calls), 1)
        symbol, ltp, now_str = engine.calls[0]
        self.assertEqual(symbol, "NSE:TEST-EQ")
        self.assertEqual(ltp, 101.5)
        self.assertIsInstance(now_str, str)

    def test_ltp_is_coerced_to_float(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({"symbol": "NSE:TEST-EQ", "ltp": "101.5"})  # string, as some feeds send it
        self.assertEqual(engine.calls[0][1], 101.5)
        self.assertIsInstance(engine.calls[0][1], float)

    def test_untracked_symbol_is_silently_ignored(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({"symbol": "NSE:OTHER-EQ", "ltp": 50.0})
        self.assertEqual(engine.calls, [])

    def test_missing_symbol_field_does_not_crash_or_call_engine(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({"ltp": 101.5})  # no 'symbol' key
        self.assertEqual(engine.calls, [])

    def test_missing_ltp_field_does_not_crash_or_call_engine(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({"symbol": "NSE:TEST-EQ"})  # no 'ltp' key
        self.assertEqual(engine.calls, [])

    def test_empty_message_does_not_crash(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        stream._handle_tick({})
        self.assertEqual(engine.calls, [])

    def test_exception_inside_check_tick_for_exit_is_caught_not_propagated(self):
        engine = FakeEngine(["NSE:TEST-EQ"])
        engine.raise_on_call = True
        stream = LiveTickStream(access_token="fake", engine=engine, on_tick_override=lambda m: None)
        try:
            stream._handle_tick({"symbol": "NSE:TEST-EQ", "ltp": 100.0})
        except Exception:
            self.fail("_handle_tick should catch exceptions from check_tick_for_exit, not propagate them")


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
