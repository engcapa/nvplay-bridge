"""CDP response failures retry safely without delivering abandoned tool calls."""
from __future__ import annotations

from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch

import websocket

import browser_upstream as browser
import nvplay_bridge as bridge


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


class DrainTab(browser.BrowserUpstream):
    def __init__(self, responses, cancel_on_failure=None):
        super().__init__()
        self.responses = iter(responses)
        self.cancel_on_failure = cancel_on_failure
        self.aborts = 0
        self.drain_timeouts = []

    def _prepare_for_send(self, cancel, deadline):
        pass

    def _eval(self, expression, timeout=5):
        if expression == browser.TRIGGER_JS:
            return "clicked"
        if "__bridgeDrain" in expression:
            self.drain_timeouts.append(timeout)
            response = next(self.responses)
            if isinstance(response, Exception):
                if self.cancel_on_failure is not None:
                    self.cancel_on_failure.set()
                raise response
            return {"status": 200, "data": response, "done": False}
        if "__bridgeAbort" in expression:
            self.aborts += 1
            return True


def cdp_timeout():
    error = websocket.WebSocketTimeoutException("Connection timed out")
    error.cdp_phase = "receive"
    error.cdp_method = "Runtime.evaluate"
    return error


class CDPStreamFailureTests(unittest.TestCase):
    def test_slow_cdp_reply_wait_is_bounded_by_stream_and_request_deadlines(self):
        for total, first_byte, expected in ((60, 60, 15), (0.2, 60, 0.2), (60, 0.1, 0.1)):
            with self.subTest(total=total, first_byte=first_byte):
                tab = DrainTab([browser.BrowserError("stop after measuring timeout")])
                with patch.object(browser, "FIRST_BYTE_TIMEOUT", first_byte):
                    with self.assertRaisesRegex(browser.BrowserError, "stop after measuring"):
                        list(tab.stream({}, timeout=total))
                self.assertGreater(tab.drain_timeouts[0], expected - 0.05)
                self.assertLessEqual(tab.drain_timeouts[0], expected)

    def test_transport_failure_preserves_delivered_bytes_and_quarantines_tab(self):
        for error in (cdp_timeout(), websocket.WebSocketConnectionClosedException("closed"),
                      ConnectionResetError("reset")):
            with self.subTest(error=type(error).__name__):
                tab = DrainTab(["first bytes", error])
                with patch.object(browser, "POLL_INTERVAL", 0):
                    stream = tab.stream({}, timeout=1)
                    self.assertEqual(next(stream), "first bytes")
                    with self.assertRaises(browser.BrowserStreamError) as caught:
                        next(stream)
                self.assertEqual(caught.exception.reason, "cdp_read")
                self.assertIs(caught.exception.__cause__, error)
                self.assertEqual(caught.exception.cdp_error_type, type(error).__name__)
                self.assertTrue(tab.needs_refresh)
                self.assertEqual(tab.aborts, 1)
                self.assertFalse(tab._lock.locked())

    def test_cancellation_takes_precedence_over_retry_classification(self):
        cancel = threading.Event()
        tab = DrainTab([cdp_timeout()], cancel_on_failure=cancel)
        with self.assertRaises(browser.BrowserCancelled):
            list(tab.stream({}, timeout=1, cancel=cancel))
        self.assertEqual(tab.aborts, 1)
        self.assertFalse(tab._lock.locked())

    def test_page_logic_errors_are_not_reclassified_as_transport_failures(self):
        error = browser.BrowserError("bridge request id mismatch")
        tab = DrainTab([error])
        with self.assertRaises(browser.BrowserError) as caught:
            list(tab.stream({}, timeout=1))
        self.assertIs(caught.exception, error)


class CDPStreamRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_discards_abandoned_tools_and_retains_cdp_diagnostics(self):
        for buffered in (False, True):
            with self.subTest(buffered=buffered):
                tab = DrainTab([
                    frame({"reasoning_content": "first thinking"}),
                    frame({"content": "DISCARD_TEXT"}),
                    frame({"tool_calls": [{"index": 0, "id": "old", "function": {
                        "name": "Bash", "arguments": '{"command":"DO_NOT_RUN"}'}}]}),
                    cdp_timeout(),
                ])

                class Source:
                    def __init__(self):
                        self.calls = []

                    def stream(self, body, timeout, cancel, trace=None):
                        self.calls.append(deepcopy(body))
                        if len(self.calls) == 1:
                            yield from tab.stream(body, timeout, cancel, trace)
                        else:
                            yield frame({"reasoning_content": "fresh thinking"})
                            yield frame({"tool_calls": [{"index": 0, "id": "fresh", "function": {
                                "name": "Read", "arguments": '{"file_path":"safe.rs"}'}}]}, "tool_calls")

                source = Source()
                payload = {"messages": [{"role": "user", "content": "continue"}],
                           "max_tokens": 128000, "reasoning_effort": "max"}
                with patch.multiple(bridge, _browser=source, BROWSER_TIMEOUT=2,
                                    BROWSER_STREAM_BACKOFF=0, BROWSER_STREAM_RETRIES=1), \
                        patch.object(bridge, "_dbg") as debug, \
                        patch.object(browser, "POLL_INTERVAL", 0):
                    output = [event async for event in bridge._browser_events_with_retry(
                        payload, buffered=buffered, defer_output=True)]
                self.assertEqual(source.calls, [payload, payload])
                self.assertFalse(any(event["type"] in ("text", "error") for event in output))
                tools = [event for event in output if event["type"] == "tool"]
                self.assertEqual(len(tools), 1)
                self.assertEqual(tools[0]["name"], "Read")
                self.assertEqual(json.loads(tools[0]["args"]), {"file_path": "safe.rs"})
                retries = [call.kwargs for call in debug.call_args_list if call.args[0] == "upstream_retry"]
                self.assertEqual(len(retries), 1)
                self.assertEqual(retries[0]["stream_failure"], "cdp_read")
                self.assertEqual(retries[0]["cdp_error_type"], "WebSocketTimeoutException")
                self.assertEqual(retries[0]["cdp_phase"], "receive")
                self.assertEqual(retries[0]["cdp_method"], "Runtime.evaluate")
                self.assertTrue(tab.needs_refresh)
                self.assertEqual(tab.aborts, 1)


if __name__ == "__main__":
    unittest.main()
