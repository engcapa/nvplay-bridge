"""CDP error headers can arrive before a split HTTP error response body."""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

import browser_upstream as bu


class HTTPErrorTab(bu.BrowserUpstream):
    def __init__(self, states):
        super().__init__()
        self.states = iter(states)
        self.last = {"status": 500, "data": "", "done": False}
        self.drains = self.aborts = 0

    def _connect(self):
        pass

    def _prepare_page(self, cancel, deadline):
        pass

    def _eval(self, expression, timeout=5):
        if expression == bu.PATCH_JS:
            return "installed"
        if expression == bu.TRIGGER_JS:
            return "clicked"
        if "__bridgeDrain" in expression:
            self.drains += 1
            self.last = next(self.states, self.last)
            return self.last
        if "__bridgeAbort" in expression:
            self.aborts += 1
            return True


class BrowserHTTPErrorTests(unittest.TestCase):
    def test_waits_for_error_body_after_headers_and_joins_split_detail(self):
        detail = ('data:{"detail":"Pool#acquire(Duration) has been pending for '
                  'more than the configured timeout of 45000ms"}\n\n')
        tab = HTTPErrorTab([
            {"status": 500, "data": "", "done": False},
            {"status": 500, "data": detail[:30], "done": False},
            {"status": 500, "data": detail[30:], "done": True},
        ])
        with patch.object(bu, "POLL_INTERVAL", 0.001):
            with self.assertRaises(bu.BrowserHTTPError) as caught:
                list(tab.stream({}, timeout=1))
        self.assertEqual(caught.exception.status, 500)
        self.assertIn(detail, str(caught.exception))
        self.assertEqual(tab.drains, 3)
        self.assertFalse(tab._lock.locked())

    def test_short_complete_error_needs_no_extra_poll(self):
        tab = HTTPErrorTab([{"status": 429, "data": "rate limited", "done": True}])
        with self.assertRaises(bu.BrowserHTTPError) as caught:
            list(tab.stream({}))
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("rate limited", str(caught.exception))
        self.assertEqual(tab.drains, 1)

    def test_empty_error_body_is_explicit(self):
        tab = HTTPErrorTab([{"status": 503, "data": "", "done": True}])
        with self.assertRaisesRegex(bu.BrowserHTTPError, "empty error response body"):
            list(tab.stream({}))

    def test_stalled_body_is_bounded_and_keeps_original_http_status(self):
        tab = HTTPErrorTab([])
        started = time.monotonic()
        with patch.multiple(bu, ERROR_BODY_TIMEOUT=0.02, POLL_INTERVAL=0.001):
            with self.assertRaises(bu.BrowserHTTPError) as caught:
                list(tab.stream({}, timeout=1))
        self.assertEqual(caught.exception.status, 500)
        self.assertIn("body_timeout", str(caught.exception))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(tab.aborts, 1)

    def test_oversized_body_is_bounded_even_when_already_complete(self):
        tab = HTTPErrorTab([{"status": 500, "data": "x" * 40000, "done": True}])
        with self.assertRaises(bu.BrowserHTTPError) as caught:
            list(tab.stream({}))
        self.assertEqual(str(caught.exception).count("x"), bu.ERROR_BODY_CHARS)
        self.assertIn("size_limit", str(caught.exception))
        self.assertEqual(tab.drains, 1)

    def test_body_read_failure_preserves_received_detail_and_http_status(self):
        tab = HTTPErrorTab([
            {"status": 500, "data": "upstream detail", "done": False},
            {"status": 500, "data": "", "done": True, "error": "network error"},
        ])
        with patch.object(bu, "POLL_INTERVAL", 0.001):
            with self.assertRaises(bu.BrowserHTTPError) as caught:
                list(tab.stream({}))
        self.assertEqual(caught.exception.status, 500)
        self.assertIn("upstream detail", str(caught.exception))
        self.assertIn("read_error", str(caught.exception))

    def test_cancellation_while_collecting_body_aborts_and_unlocks(self):
        tab = HTTPErrorTab([])
        cancel = threading.Event()
        original = tab._eval

        def evaluate(expression, timeout=5):
            state = original(expression, timeout)
            if tab.drains == 2:
                cancel.set()
            return state

        tab._eval = evaluate
        with patch.object(bu, "POLL_INTERVAL", 0.001):
            with self.assertRaises(bu.BrowserCancelled):
                list(tab.stream({}, timeout=1, cancel=cancel))
        self.assertEqual(tab.aborts, 1)
        self.assertFalse(tab._lock.locked())

    def test_total_request_deadline_bounds_error_body_collection(self):
        tab = HTTPErrorTab([])
        with patch.object(bu, "POLL_INTERVAL", 0.001):
            with self.assertRaisesRegex(bu.BrowserError, "总时限"):
                list(tab.stream({}, timeout=0.02))
        self.assertEqual(tab.aborts, 1)
        self.assertFalse(tab._lock.locked())

    def test_failed_fetch_is_not_misclassified_as_a_received_http_response(self):
        tab = HTTPErrorTab([{"status": -1, "data": "", "done": True, "error": "network error"}])
        with self.assertRaises(bu.BrowserError) as caught:
            list(tab.stream({}))
        self.assertNotIsInstance(caught.exception, bu.BrowserHTTPError)

    def test_fetch_typeerror_before_headers_has_its_own_failure_type(self):
        tab = HTTPErrorTab([{"status": -1, "data": "", "done": True,
                             "error": "TypeError: Failed to fetch"}])
        with self.assertRaises(bu.BrowserFetchError) as caught:
            list(tab.stream({}))
        self.assertNotIsInstance(caught.exception, bu.BrowserHTTPError)
        self.assertEqual(tab.aborts, 1)
        self.assertFalse(tab._lock.locked())

    def test_abort_construction_and_response_read_errors_are_not_fetch_rejections(self):
        for status, error in ((-1, "AbortError: signal is aborted"),
                              (0, "TypeError: invalid Request"),
                              (200, "TypeError: Failed to fetch")):
            with self.subTest(status=status, error=error):
                tab = HTTPErrorTab([{"status": status, "data": "", "done": True, "error": error}])
                with self.assertRaises(bu.BrowserError) as caught:
                    list(tab.stream({}))
                self.assertNotIsInstance(caught.exception, bu.BrowserFetchError)


class CDPRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_long_pool_timeout_body_reaches_retry_policy(self):
        import nvplay_bridge as bridge

        detail = ("x" * 3000 + "Pool#acquire(Duration) has been pending for more than "
                  "the configured timeout of 45000ms")
        tab = HTTPErrorTab([
            {"status": 500, "data": "", "done": False},
            {"status": 500, "data": detail[:3020], "done": False},
            {"status": 500, "data": detail[3020:], "done": True},
        ])

        class Browser:
            calls = 0

            def stream(self, body, timeout, cancel, trace=None):
                self.calls += 1
                if self.calls == 1:
                    yield from tab.stream(body, timeout, cancel, trace)
                else:
                    yield ('data: {"choices":[{"delta":{"content":"recovered"},'
                           '"finish_reason":"stop"}]}\n\n')

        browser = Browser()
        with patch.multiple(bridge, _browser=browser, BROWSER_TIMEOUT=2,
                            BROWSER_POOL_TIMEOUT_BACKOFF=0, BROWSER_POOL_TIMEOUT_RETRIES=1):
            with patch.object(bu, "POLL_INTERVAL", 0.001):
                output = [event async for event in bridge._browser_events_with_retry({}, buffered=False)]
        self.assertEqual(browser.calls, 2)
        self.assertEqual([event["text"] for event in output if event["type"] == "text"], ["recovered"])
        self.assertFalse(any(event["type"] == "error" for event in output))


if __name__ == "__main__":
    unittest.main()
