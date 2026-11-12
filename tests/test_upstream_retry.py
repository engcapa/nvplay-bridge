"""Offline retry policy and real HTTP delivery tests; no live browser is used."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
import socket
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("UPSTREAM_KIND", "browser")

import httpx
import uvicorn
from browser_upstream import BrowserError, BrowserFetchError, BrowserHTTPError, BrowserStreamError
import nvplay_bridge as bridge
from stream_support import SSEDecoder


POOL_TIMEOUT = ('上游 500: data:{"status":500,"detail":"Pool#acquire(Duration) has been '
                'pending for more than the configured timeout of 45000ms"}\n\n')


def frame(delta=None, finish=None, usage=None):
    data = {"choices": [] if delta is None else [{"delta": delta, "finish_reason": finish}]}
    if usage is not None:
        data["usage"] = usage
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def pool_error():
    return BrowserHTTPError(500, POOL_TIMEOUT)


def sse_error(code=503):
    return 'data: ' + json.dumps({"error": {"message": "Service temporarily unavailable",
        "type": "service_unavailable", "code": code}}) + '\n\n'


class ScriptBrowser:
    def __init__(self, scripts):
        self.scripts = scripts
        self.calls, self.timeouts, self.closed = [], [], []

    def stream(self, body, timeout, cancel, trace=None):
        # A retry must release the preceding stream before opening another.
        if len(self.closed) != len(self.calls):
            raise AssertionError("Previous stream has not been closed")
        index = len(self.calls)
        self.calls.append(deepcopy(body))
        self.timeouts.append(timeout)
        try:
            for part in self.scripts[min(index, len(self.scripts) - 1)]:
                if isinstance(part, Exception):
                    raise part
                yield part
        finally:
            self.closed.append(index)


class RetrySettings:
    def setUp(self):
        super().setUp()
        self.settings = patch.multiple(bridge, UPSTREAM_KIND="browser", BROWSER_TIMEOUT=3,
            BROWSER_POOL_TIMEOUT_RETRIES=2, BROWSER_POOL_TIMEOUT_BACKOFF=0.01,
            BROWSER_HTTP_5XX_RETRIES=1, BROWSER_HTTP_5XX_BACKOFF=0.01,
            BROWSER_FETCH_RETRIES=1, BROWSER_FETCH_BACKOFF=0.01,
            BROWSER_STREAM_RETRIES=2, BROWSER_STREAM_BACKOFF=0.01,
            BROWSER_MESSAGES_DEFER_OUTPUT=False,
            BROWSER_HEARTBEAT_SEC=0.01, PLACEHOLDER_RETRY=1)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        jitter = patch.object(bridge.random, "uniform", return_value=0)
        jitter.start()
        self.addCleanup(jitter.stop)
        debug = patch.object(bridge, "_dbg")
        self.debug = debug.start()
        self.addCleanup(debug.stop)

    def logs(self, event):
        return [call.kwargs for call in self.debug.call_args_list if call.args[0] == event]


class RetryPolicyTests(RetrySettings, unittest.IsolatedAsyncioTestCase):
    async def collect(self, browser, buffered=False, payload=None, defer_output=False):
        with patch.object(bridge, "_browser", browser):
            return [event async for event in bridge._browser_events_with_retry(
                payload or {}, buffered=buffered, defer_output=defer_output)]

    async def test_two_transient_failures_preserve_payload_cleanup_and_total_budget(self):
        browser = ScriptBrowser([[pool_error()], [pool_error()],
            [frame({"content": "RECOVERED"}, "stop"), frame(usage={"completion_tokens": 7})]])
        payload = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 128,
                   "reasoning_effort": "high", "tools": [{"type": "function"}]}
        original = deepcopy(payload)
        output = await self.collect(browser, payload=payload)
        self.assertFalse(any(event["type"] == "error" for event in output))
        self.assertEqual([event["text"] for event in output if event["type"] == "text"], ["RECOVERED"])
        self.assertEqual(browser.calls, [original] * 3)
        self.assertEqual(payload, original)
        self.assertEqual(browser.closed, [0, 1, 2])
        self.assertTrue(all(0 < value <= 3 for value in browser.timeouts))
        self.assertGreater(browser.timeouts[0], browser.timeouts[1])
        self.assertGreater(browser.timeouts[1], browser.timeouts[2])
        retries = self.logs("upstream_retry")
        self.assertEqual([value["delay"] for value in retries], [0.01, 0.02])
        self.assertEqual([value["retry"] for value in retries], [1, 2])
        self.assertEqual(len({value["id"] for value in retries}), 1)
        self.assertEqual(self.logs("browser_done")[0]["usage"]["completion_tokens"], 7)

    async def test_retry_limit_and_disable_keep_original_error(self):
        for limit in (0, 2):
            with self.subTest(limit=limit), patch.object(bridge, "BROWSER_POOL_TIMEOUT_RETRIES", limit):
                browser = ScriptBrowser([[pool_error()]])
                output = await self.collect(browser)
                errors = [event for event in output if event["type"] == "error"]
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["message"], POOL_TIMEOUT)
                self.assertEqual(browser.calls, [{}] * (1 + limit))
        self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "retry_limit")

    async def test_disabled_generic_http_policy_and_uncertain_failures_are_not_replayed(self):
        errors = [BrowserHTTPError(status, POOL_TIMEOUT) for status in (400, 429, 502, 503)]
        errors += [BrowserHTTPError(500, "Internal Server Error"), BrowserError(POOL_TIMEOUT),
                   BrowserError("上游响应读取失败: TypeError: network error"),
                   BrowserHTTPError(500, "Pool#acquire(Duration): an unrelated error")]
        for error in errors:
            with self.subTest(error=str(error), status=getattr(error, "status", None)), \
                    patch.object(bridge, "BROWSER_HTTP_5XX_RETRIES", 0):
                browser = ScriptBrowser([[error], [frame({"content": "WRONG"}, "stop")]])
                output = await self.collect(browser)
                self.assertEqual(browser.calls, [{}])
                self.assertEqual(output[-1]["type"], "error")

    async def test_confirmed_http_server_errors_retry_once_with_unchanged_payload(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                browser = ScriptBrowser([[BrowserHTTPError(status, "empty error response body")],
                                         [frame({"content": "recovered"}, "stop")]])
                payload = {"messages": [{"role": "user", "content": "hello"}], "seed": 0}
                output = await self.collect(browser, payload=payload)
                self.assertEqual(browser.calls, [payload, payload])
                self.assertEqual(browser.closed, [0, 1])
                self.assertEqual([event["text"] for event in output if event["type"] == "text"], ["recovered"])
                self.assertEqual(self.logs("upstream_retry")[-1]["reason"], "upstream_http_5xx")

    async def test_generic_retry_requires_a_confirmed_retryable_http_status(self):
        errors = [BrowserHTTPError(status, "not transient") for status in (400, 401, 403, 404, 413, 422, 429, 501, 505)]
        errors += [BrowserError("upstream 503: network error"), BrowserError("fetch failed")]
        scripts = [[error] for error in errors]
        scripts.append(['data: {"error":{"message":"Internal Server Error"}}\n\n'])
        for script in scripts:
            with self.subTest(script=str(script)):
                browser = ScriptBrowser([script, [frame({"content": "WRONG"}, "stop")]])
                output = await self.collect(browser)
                self.assertEqual(len(browser.calls), 1)
                self.assertEqual(output[-1]["type"], "error")

    async def test_typed_fetch_rejection_retries_with_unchanged_payload_and_cleanup(self):
        failure = BrowserFetchError("上游请求未建立: TypeError: Failed to fetch")
        browser = ScriptBrowser([[failure], [frame({"content": "recovered"}, "stop")]])
        payload = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 128000}
        output = await self.collect(browser, payload=payload)
        self.assertEqual(browser.calls, [payload, payload])
        self.assertEqual(browser.closed, [0, 1])
        self.assertEqual([event["text"] for event in output if event["type"] == "text"], ["recovered"])
        self.assertEqual(self.logs("upstream_retry")[-1]["reason"], "browser_fetch_failed")
        self.assertGreater(browser.timeouts[0], browser.timeouts[1])

    async def test_fetch_retry_limit_disable_and_deadline_keep_original_failure(self):
        message = "上游请求未建立: TypeError: Failed to fetch"
        for limit in (0, 1, 2):
            with self.subTest(limit=limit), patch.object(bridge, "BROWSER_FETCH_RETRIES", limit):
                browser = ScriptBrowser([[BrowserFetchError(message)]])
                output = await self.collect(browser)
                self.assertEqual(len(browser.calls), limit + 1)
                self.assertEqual(output[-1]["message"], message)
                self.assertTrue(output[-1]["fetch_failed"])
                self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "retry_limit")
        browser = ScriptBrowser([[BrowserFetchError(message)]])
        with patch.multiple(bridge, BROWSER_TIMEOUT=0.05, BROWSER_FETCH_BACKOFF=0.1):
            output = await self.collect(browser)
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(output[-1]["message"], message)
        self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "deadline")

    async def test_fetch_retry_never_replays_delivered_thinking_text_or_tool(self):
        for delta in ({"reasoning_content": "thinking"}, {"content": "partial"},
                      {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read", "arguments": '{}'}}]}):
            with self.subTest(delta=delta):
                browser = ScriptBrowser([[frame(delta), BrowserFetchError("fetch rejected")],
                                         [frame({"content": "WRONG"}, "stop")]])
                output = await self.collect(browser)
                self.assertEqual(len(browser.calls), 1)
                self.assertEqual(output[-1]["message"], "fetch rejected")
                self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "content_committed")

    async def test_generic_retry_exhaustion_and_deadline_preserve_failure(self):
        browser = ScriptBrowser([[BrowserHTTPError(500, "original HTTP failure")]])
        output = await self.collect(browser)
        self.assertEqual(len(browser.calls), 2)
        self.assertEqual(output[-1]["message"], "original HTTP failure")
        self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "retry_limit")
        browser = ScriptBrowser([[BrowserHTTPError(503, "overloaded")]])
        with patch.multiple(bridge, BROWSER_TIMEOUT=0.05, BROWSER_HTTP_5XX_BACKOFF=0.1):
            output = await self.collect(browser)
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(output[-1]["status"], 503)
        self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "deadline")

    async def test_http_retries_exhausted_uses_exponential_backoff_and_releases_each_attempt(self):
        browser = ScriptBrowser([[BrowserHTTPError(500, 'Retries exhausted: 3/3')]])
        with patch.multiple(bridge, BROWSER_HTTP_5XX_RETRIES=3, BROWSER_HTTP_5XX_BACKOFF=0.01):
            output = await self.collect(browser)
        self.assertEqual(len(browser.calls), 4)
        self.assertEqual(browser.closed, [0, 1, 2, 3])
        self.assertEqual([r['delay'] for r in self.logs('upstream_retry')], [0.01, 0.02, 0.04])
        self.assertEqual(output[-1]['message'], 'Retries exhausted: 3/3')

    async def test_generic_http_retry_never_replays_delivered_content(self):
        for delta in ({"reasoning_content": "thinking"}, {"content": "partial"},
                      {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read", "arguments": '{}'}}]}):
            with self.subTest(delta=delta):
                browser = ScriptBrowser([[frame(delta), BrowserHTTPError(500, "server error")],
                                         [frame({"content": "WRONG"}, "stop")]])
                output = await self.collect(browser)
                self.assertEqual(len(browser.calls), 1)
                self.assertEqual(output[-1]["message"], "server error")
                self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "content_committed")

    async def test_http_pool_timeout_and_fetch_retry_budgets_are_independent(self):
        browser = ScriptBrowser([[BrowserHTTPError(500, "server error")],
                                 [BrowserFetchError("fetch rejected")], [pool_error()],
                                 [frame({"content": "recovered"}, "stop")]])
        output = await self.collect(browser)
        self.assertEqual(len(browser.calls), 4)
        self.assertEqual([event["text"] for event in output if event["type"] == "text"], ["recovered"])
        self.assertEqual([event["reason"] for event in self.logs("upstream_retry")],
                         ["upstream_http_5xx", "browser_fetch_failed", "nvidia_pool_acquire_timeout"])

    async def test_thinking_text_and_tool_delivery_each_prevent_replay(self):
        for delta in ({"reasoning_content": "thinking"}, {"content": "partial"},
                      {"tool_calls": [{"index": 0, "id": "call_1", "function": {
                          "name": "read_file", "arguments": '{"path":"'}}]}):
            with self.subTest(delta=delta):
                browser = ScriptBrowser([[frame(delta), pool_error()], [frame({"content": "WRONG"}, "stop")]])
                output = await self.collect(browser)
                self.assertEqual(len(browser.calls), 1)
                self.assertEqual(output[-1]["message"], POOL_TIMEOUT)
                self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "content_committed")

    async def test_nonstream_discards_undelivered_failed_attempt_state(self):
        browser = ScriptBrowser([[frame({"reasoning_content": "discard this"}),
                                  frame(usage={"completion_tokens": 999}), pool_error()],
                                 [frame({"content": "fresh"}, "stop"), frame(usage={"completion_tokens": 4})]])
        output = await self.collect(browser, buffered=True)
        self.assertEqual([event["text"] for event in output if event["type"] == "text"], ["fresh"])
        self.assertFalse(any(event["type"] == "thinking" for event in output))
        self.assertEqual([event["usage"] for event in output if event["type"] == "usage"], [{"completion_tokens": 4}])

    async def test_deferred_output_retries_sse_error_without_leaking_text_or_tools(self):
        for buffered in (False, True):
            with self.subTest(buffered=buffered):
                browser = ScriptBrowser([[
                    frame({"reasoning_content": "first thinking"}),
                    frame({"content": "DISCARD_TEXT"}),
                    frame({"tool_calls": [{"index": 0, "id": "old", "function": {
                        "name": "Bash", "arguments": '{"command":"DO_NOT_RUN"}'}}]}),
                    frame(usage={"completion_tokens": 999}), sse_error(),
                ], [frame({"reasoning_content": "fresh thinking"}),
                    frame({"content": "fresh answer"}, "stop"),
                    frame(usage={"completion_tokens": 7})]])
                payload = {"messages": [{"role": "user", "content": "hi"}],
                           "max_tokens": 128000, "reasoning_effort": "max", "seed": 0}
                output = await self.collect(browser, buffered=buffered, payload=payload, defer_output=True)
                self.assertEqual(browser.calls, [payload, payload])
                self.assertEqual(browser.closed, [0, 1])
                self.assertGreater(browser.timeouts[0], browser.timeouts[1])
                self.assertFalse(any(event["type"] in ("error", "tool") for event in output))
                self.assertEqual([e["text"] for e in output if e["type"] == "text"], ["fresh answer"])
                self.assertEqual([e["text"] for e in output if e["type"] == "thinking"],
                    ["fresh thinking"] if buffered else ["first thinking", "fresh thinking"])
                self.assertEqual([e["usage"] for e in output if e["type"] == "usage"], [{"completion_tokens": 7}])
                retry = self.logs("upstream_retry")[-1]
                self.assertEqual(retry["reason"], "upstream_stream_failure")
                self.assertEqual(retry["sse_error_code"], 503)
                self.assertEqual(retry["status"], 502)

    async def test_typed_stream_failures_retry_but_delivered_content_is_never_replayed(self):
        for reason in ("read", "cdp_read", "first_byte_timeout", "idle_timeout", "missing_finish"):
            for deferred in (False, True):
                with self.subTest(reason=reason, deferred=deferred):
                    browser = ScriptBrowser([[frame({"content": "PARTIAL"}),
                        BrowserStreamError("transport stopped", reason)], [frame({"content": "fresh"}, "stop")]])
                    output = await self.collect(browser, defer_output=deferred)
                    self.assertEqual(len(browser.calls), 2 if deferred else 1)
                    self.assertEqual([e["text"] for e in output if e["type"] == "text"],
                                     ["fresh"] if deferred else ["PARTIAL"])
                    self.assertEqual(any(e["type"] == "error" for e in output), not deferred)

    async def test_clean_eof_without_finish_can_recover_before_output_delivery(self):
        browser = ScriptBrowser([[frame({"content": "DISCARD"})], [frame({"content": "complete"}, "stop")]])
        output = await self.collect(browser, defer_output=True)
        self.assertEqual([e["text"] for e in output if e["type"] == "text"], ["complete"])
        self.assertEqual(self.logs("upstream_retry")[-1]["stream_failure"], "missing_finish")

    async def test_unclassified_or_nontransient_stream_errors_are_not_retried(self):
        scripts = [[sse_error(code)] for code in (400, 401, 403, 429, 501, 505, "503-like")]
        scripts += [[BrowserError("503 network failure")], ['data: {"error":"service unavailable"}\n\n']]
        for script in scripts:
            with self.subTest(script=str(script)):
                browser = ScriptBrowser([script, [frame({"content": "WRONG"}, "stop")]])
                output = await self.collect(browser, defer_output=True)
                self.assertEqual(len(browser.calls), 1)
                self.assertEqual(output[-1]["type"], "error")

    async def test_stream_retry_limit_disable_and_deadline_keep_last_error(self):
        for limit in (0, 1, 2):
            with self.subTest(limit=limit), patch.object(bridge, "BROWSER_STREAM_RETRIES", limit):
                browser = ScriptBrowser([[frame({"content": "DISCARD"}), sse_error()]])
                output = await self.collect(browser, defer_output=True)
                self.assertEqual(len(browser.calls), limit + 1)
                self.assertFalse(any(e["type"] == "text" for e in output))
                self.assertEqual(output[-1]["sse_error_code"], 503)
                self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "retry_limit")
        browser = ScriptBrowser([[sse_error()]])
        with patch.multiple(bridge, BROWSER_TIMEOUT=0.05, BROWSER_STREAM_BACKOFF=0.1):
            output = await self.collect(browser, defer_output=True)
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(output[-1]["sse_error_code"], 503)
        self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "deadline")

    async def test_invalid_tools_are_rejected_before_delivery_without_regeneration(self):
        for name, arguments in (("Edit", '{"old_string":'), ("Edit", '[]'), (None, '{}')):
            with self.subTest(name=name, arguments=arguments):
                browser = ScriptBrowser([[frame({"reasoning_content": "thinking"}),
                    frame({"content": "WITHHELD"}),
                    frame({"tool_calls": [{"index": 0, "id": "a", "function": {
                        "name": name, "arguments": arguments}}]}, "tool_calls")]])
                output = await self.collect(browser, defer_output=True)
                self.assertEqual(len(browser.calls), 1)
                self.assertFalse(any(e["type"] in ("text", "tool") for e in output))
                self.assertEqual(output[-1]["phase"], "conversion")

    async def test_large_failed_output_spills_to_disk_then_is_discarded_and_closed(self):
        from stream_support import DeferredEvents

        closed = []

        class ObservedBuffer(DeferredEvents):
            def close(self):
                closed.append((self.file._rolled, self.file))
                super().close()

        browser = ScriptBrowser([[frame({"content": "WITHHELD" * 50000}), sse_error()],
                                 [frame({"content": "small successful answer"}, "stop")]])
        with patch.object(bridge, "DeferredEvents", ObservedBuffer):
            output = await self.collect(browser, defer_output=True)
        self.assertEqual([e["text"] for e in output if e["type"] == "text"], ["small successful answer"])
        self.assertEqual([rolled for rolled, _ in closed], [True, False])
        self.assertTrue(all(file.closed for _, file in closed))

    async def test_staging_limit_fails_without_leaking_output_or_retrying(self):
        import stream_support

        browser = ScriptBrowser([[frame({"content": "TOO_LARGE" * 100}, "stop")]])
        with patch.object(stream_support, "MAX_DEFERRED_OUTPUT", 100):
            output = await self.collect(browser, defer_output=True)
        self.assertEqual(len(browser.calls), 1)
        self.assertFalse(any(e["type"] == "text" for e in output))
        self.assertIn("exceeds", output[-1]["message"])

    async def test_independent_budgets_preserve_original_payload_through_stream_retry(self):
        browser = ScriptBrowser([[BrowserHTTPError(500, "server error")], [pool_error()],
            [BrowserFetchError("fetch rejected")], [sse_error()], [frame({"content": "OK"}, "stop")]])
        payload = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 128000}
        output = await self.collect(browser, payload=payload, defer_output=True)
        self.assertEqual(browser.calls, [payload] * 5)
        self.assertFalse(any(e["type"] == "error" for e in output))
        self.assertEqual([r["reason"] for r in self.logs("upstream_retry")],
            ["upstream_http_5xx", "nvidia_pool_acquire_timeout", "browser_fetch_failed", "upstream_stream_failure"])

    async def test_diagnostics_count_invocations_instead_of_argument_fragments(self):
        browser = ScriptBrowser([[
            frame({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read", "arguments": '{"path":'}}]}),
            frame({"tool_calls": [{"index": 0, "function": {"arguments": '"file"}'}}]}),
            frame({"tool_calls": [{"index": 1, "id": "b", "function": {"name": "read", "arguments": '{}'}}]}),
            frame({}, "tool_calls"),
        ]])
        await self.collect(browser)
        self.assertEqual(self.logs("browser_done")[-1]["counts"]["tool_calls"], 2)

    async def test_failure_diagnostics_preserve_delivery_status_and_usage(self):
        browser = ScriptBrowser([[frame({"reasoning_content": "thinking"}),
                                  frame(usage={"completion_tokens": 7}), pool_error()]])
        await self.collect(browser)
        failure = self.logs("browser_error")[-1]
        self.assertEqual(failure["status"], 500)
        self.assertTrue(failure["committed"])
        self.assertEqual(failure["counts"]["thinking_chars"], 8)
        self.assertEqual(failure["usage"], {"completion_tokens": 7})

    async def test_placeholder_and_pool_timeout_have_independent_retry_limits(self):
        browser = ScriptBrowser([[pool_error()], [frame({"content": "[Tool use interrupted]"}, "stop")],
                                 [pool_error()], [frame({"content": "OK"}, "stop")]])
        original = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
        output = await self.collect(browser, payload=original)
        self.assertEqual(len(browser.calls), 4)
        self.assertEqual(browser.calls[0], browser.calls[1])
        self.assertEqual(browser.calls[2], browser.calls[3])
        self.assertEqual(len(browser.calls[3]["messages"]), 2)
        self.assertEqual(len(original["messages"]), 1)
        self.assertEqual([event["text"] for event in output if event["type"] == "text"], ["OK"])

    async def test_backoff_must_fit_remaining_total_budget(self):
        browser = ScriptBrowser([[pool_error()]])
        with patch.multiple(bridge, BROWSER_TIMEOUT=0.05, BROWSER_POOL_TIMEOUT_BACKOFF=0.1):
            output = await self.collect(browser)
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(output[-1]["message"], POOL_TIMEOUT)
        self.assertEqual(self.logs("upstream_retry_skipped")[-1]["reason"], "deadline")

    async def test_expired_request_never_opens_another_stream(self):
        browser = ScriptBrowser([[pool_error()]])
        with patch.object(bridge, "BROWSER_TIMEOUT", 0):
            output = await self.collect(browser)
        self.assertFalse(browser.calls)
        self.assertIn("总时限", output[-1]["message"])

    async def test_backoff_is_cancellable_after_failed_stream_cleanup(self):
        browser = ScriptBrowser([[pool_error()]])
        with patch.multiple(bridge, _browser=browser, BROWSER_POOL_TIMEOUT_BACKOFF=1):
            source = bridge._browser_events_with_retry({}, buffered=False)
            self.assertEqual((await anext(source))["type"], "ping")
            self.assertEqual(browser.closed, [0])
            await source.aclose()
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(len(self.logs("browser_cancelled")), 1)

    async def test_backoff_clamps_to_deadline_and_can_disable_heartbeats(self):
        with patch.object(bridge, "BROWSER_HEARTBEAT_SEC", 0):
            started = time.monotonic()
            output = [event async for event in bridge._browser_retry_pause(5, started + 0.02)]
        self.assertFalse(output)
        self.assertLess(time.monotonic() - started, 0.5)


class HTTPRetryTests(RetrySettings, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        cls.base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        cls.server = uvicorn.Server(uvicorn.Config(bridge.app, log_level="error", lifespan="off"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [listener]}, daemon=True)
        cls.thread.start()
        until = time.monotonic() + 5
        while not cls.server.started and time.monotonic() < until:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("Test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(5)

    def request_body(self, endpoint, stream=True):
        body = {"model": "kimi-k3.1-pro", "stream": stream}
        if endpoint == "/v1/messages":
            body["messages"] = [{"role": "user", "content": "hi"}]
        else:
            body.update(input="hi", store=False)
        return body

    async def test_both_protocols_keep_same_stream_open_with_heartbeats_during_retry(self):
        for endpoint, ping, terminal, start in (
            ("/v1/messages", "event: ping", "event: message_stop", "event: message_start"),
            ("/v1/responses", ": ping", "event: response.completed", "event: response.created"),
        ):
            for failure in (pool_error(), BrowserHTTPError(500, "Internal Server Error"),
                            BrowserFetchError("上游请求未建立: TypeError: Failed to fetch")):
                with self.subTest(endpoint=endpoint, failure=str(failure)):
                    browser = ScriptBrowser([[failure], [frame({"content": "RECOVERED"}, "stop")]])
                    async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                        with patch.multiple(bridge, _browser=browser, BROWSER_POOL_TIMEOUT_BACKOFF=0.2,
                                            BROWSER_HTTP_5XX_BACKOFF=0.2, BROWSER_FETCH_BACKOFF=0.2):
                            async with client.stream("POST", self.base + endpoint, json=self.request_body(endpoint)) as response:
                                self.assertEqual(response.status_code, 200)
                                lines, seen = response.aiter_lines(), []
                                async with asyncio.timeout(2):
                                    async for line in lines:
                                        seen.append(line)
                                        if line == ping:
                                            break
                                self.assertIn(ping, seen)
                                self.assertEqual(len(browser.calls), 1)
                                self.assertEqual(browser.closed, [0])
                                seen.extend([line async for line in lines])
                    self.assertEqual(seen.count(start), 1)
                    self.assertEqual(seen.count(terminal), 1)
                    self.assertIn("RECOVERED", "\n".join(seen))
                    self.assertNotIn("Pool#acquire", "\n".join(seen))
                    self.assertEqual(len(browser.calls), 2)

    async def test_both_protocols_emit_one_terminal_error_after_retry_exhaustion(self):
        for endpoint, failed, completed in (("/v1/messages", "event: error", "event: message_stop"),
                                           ("/v1/responses", "event: response.failed", "event: response.completed")):
            with self.subTest(endpoint=endpoint):
                browser = ScriptBrowser([[pool_error()]])
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    with patch.object(bridge, "_browser", browser):
                        response = await client.post(self.base + endpoint, json=self.request_body(endpoint))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text.count(failed), 1)
                self.assertNotIn(completed, response.text)
                self.assertIn("Pool#acquire(Duration)", response.text)
                self.assertEqual(len(browser.calls), 3)

    async def test_both_protocols_never_replay_a_delivered_partial_response(self):
        for endpoint, failed, completed in (("/v1/messages", "event: error", "event: message_stop"),
                                           ("/v1/responses", "event: response.failed", "event: response.completed")):
            with self.subTest(endpoint=endpoint):
                browser = ScriptBrowser([[frame({"reasoning_content": "partial thinking"}), pool_error()],
                                         [frame({"content": "WRONG"}, "stop")]])
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    with patch.object(bridge, "_browser", browser):
                        response = await client.post(self.base + endpoint, json=self.request_body(endpoint))
                self.assertIn("partial thinking", response.text)
                self.assertIn(failed, response.text)
                self.assertNotIn(completed, response.text)
                self.assertNotIn("WRONG", response.text)
                self.assertEqual(len(browser.calls), 1)

    async def test_both_nonstream_protocols_recover_without_leaking_failed_attempt(self):
        for endpoint in ("/v1/messages", "/v1/responses"):
            for failure in (pool_error(), BrowserFetchError("上游请求未建立: TypeError: Failed to fetch")):
                with self.subTest(endpoint=endpoint, failure=str(failure)):
                    browser = ScriptBrowser([[failure], [frame({"content": "JSON_OK"}, "stop")]])
                    async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                        with patch.object(bridge, "_browser", browser):
                            response = await client.post(self.base + endpoint, json=self.request_body(endpoint, stream=False))
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("JSON_OK", response.text)
                    self.assertNotIn(str(failure), response.text)
                    self.assertEqual(len(browser.calls), 2)

    async def test_retry_sleep_allows_other_requests_to_use_service(self):
        browser = ScriptBrowser([[pool_error()], [frame({"content": "OTHER_CLIENT"}, "stop")],
                                 [frame({"content": "RETRIED_CLIENT"}, "stop")]])
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.multiple(bridge, _browser=browser, BROWSER_POOL_TIMEOUT_BACKOFF=0.3):
                async with client.stream("POST", self.base + "/v1/responses",
                                         json=self.request_body("/v1/responses")) as response:
                    lines = response.aiter_lines()
                    async for line in lines:
                        if line == ": ping":
                            break
                    other = await client.post(self.base + "/v1/messages",
                                              json=self.request_body("/v1/messages", stream=False))
                    self.assertEqual(other.status_code, 200)
                    self.assertIn("OTHER_CLIENT", other.text)
                    rest = "\n".join([line async for line in lines])
                    self.assertIn("RETRIED_CLIENT", rest)
        self.assertEqual(len(browser.calls), 3)

    async def test_disconnect_during_backoff_stops_future_attempts(self):
        browser = ScriptBrowser([[pool_error()], [frame({"content": "WRONG"}, "stop")]])
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.multiple(bridge, _browser=browser, BROWSER_POOL_TIMEOUT_BACKOFF=0.3):
                async with client.stream("POST", self.base + "/v1/responses",
                                         json=self.request_body("/v1/responses")) as response:
                    async for line in response.aiter_lines():
                        if line == ": ping":
                            break
                await asyncio.sleep(0.4)
        self.assertEqual(len(browser.calls), 1)
        self.assertTrue(self.logs("browser_cancelled"))

    async def test_messages_keep_one_sse_connection_through_midresponse_503(self):
        arguments = {"file_path": "src/中文🙂.rs", "old_string": 'let a = "旧\\n";\r\n' * 800,
                     "new_string": 'let a = "新\\n";\r\n' * 1000}
        raw = json.dumps(arguments, ensure_ascii=False)
        success = [frame({"reasoning_content": "fresh thinking"}), frame({"content": "complete text"})]
        for start in range(0, len(raw), 113):
            success.append(frame({"tool_calls": [{"index": 0, "id": "reused:0" if start == 0 else None,
                "function": {"name": "Edit" if start == 0 else None, "arguments": raw[start:start+113]}}]}))
        success += [frame({}, "tool_calls"), frame(usage={"completion_tokens": 17})]
        browser = ScriptBrowser([[frame({"reasoning_content": "first thinking"}),
            frame({"content": "DISCARD"}), frame({"tool_calls": [{"index": 0, "id": "bad", "function": {
                "name": "Bash", "arguments": '{"command":"DO_NOT_RUN"}'}}]}), sse_error()], success])
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.multiple(bridge, _browser=browser, BROWSER_MESSAGES_DEFER_OUTPUT=True):
                response = await client.post(self.base + "/v1/messages", json=self.request_body("/v1/messages"))
        wire = response.text
        self.assertEqual(wire.count("event: message_start"), 1)
        self.assertEqual(wire.count("event: message_stop"), 1)
        self.assertNotIn("event: error", wire)
        self.assertNotIn("DISCARD", wire)
        self.assertNotIn("DO_NOT_RUN", wire)
        self.assertIn("first thinking", wire)
        self.assertIn("fresh thinking", wire)
        events = SSEDecoder().feed(wire, final=True)
        calls = [e["content_block"] for e in events if e["type"] == "content_block_start"
                 and e["content_block"]["type"] == "tool_use"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["id"].startswith("toolu_"))
        self.assertEqual(calls[0]["name"], "Edit")
        actual = ''.join(e["delta"]["partial_json"] for e in events
                         if e.get("delta", {}).get("type") == "input_json_delta")
        self.assertEqual(json.loads(actual), arguments)
        self.assertIn('"output_tokens": 17', wire)
        self.assertEqual(len(browser.calls), 2)
        self.assertEqual(self.logs("upstream_retry")[-1]["request_id"], response.headers["request-id"])

    async def test_messages_nonstream_discards_all_failed_attempt_content(self):
        browser = ScriptBrowser([[frame({"reasoning_content": "DISCARD_THINKING"}),
            frame({"content": "DISCARD_TEXT"}), sse_error()],
            [frame({"reasoning_content": "fresh thinking"}), frame({"content": "fresh text"}, "stop")]])
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.multiple(bridge, _browser=browser, BROWSER_MESSAGES_DEFER_OUTPUT=True):
                response = await client.post(self.base + "/v1/messages",
                    json=self.request_body("/v1/messages", stream=False))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("DISCARD", response.text)
        self.assertIn("fresh thinking", response.text)
        self.assertEqual(len(browser.calls), 2)

    async def test_exhaustion_reports_error_without_partial_output_or_false_success(self):
        browser = ScriptBrowser([[frame({"reasoning_content": "thinking"}),
                                  frame({"content": "WITHHELD"}), sse_error()]])
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.multiple(bridge, _browser=browser, BROWSER_MESSAGES_DEFER_OUTPUT=True):
                response = await client.post(self.base + "/v1/messages", json=self.request_body("/v1/messages"))
        self.assertEqual(len(browser.calls), 3)
        self.assertEqual(browser.closed, [0, 1, 2])
        self.assertEqual(response.text.count("event: error"), 1)
        self.assertNotIn("event: message_stop", response.text)
        self.assertNotIn("text_delta", response.text)
        self.assertNotIn("WITHHELD", response.text)

    async def test_reasoning_only_token_limit_remains_honest_truncation(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                browser = ScriptBrowser([[frame({"reasoning_content": "thinking"}, "length")]])
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    with patch.multiple(bridge, _browser=browser, BROWSER_MESSAGES_DEFER_OUTPUT=True):
                        response = await client.post(self.base + "/v1/messages",
                            json=self.request_body("/v1/messages", stream=stream))
                self.assertEqual(len(browser.calls), 1)
                self.assertIn('"stop_reason": "max_tokens"' if stream else '"stop_reason":"max_tokens"', response.text)

    async def test_messages_staging_does_not_change_responses_partial_delivery(self):
        browser = ScriptBrowser([[frame({"content": "PARTIAL"}), sse_error()],
                                 [frame({"content": "WRONG"}, "stop")]])
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.multiple(bridge, _browser=browser, BROWSER_MESSAGES_DEFER_OUTPUT=True):
                response = await client.post(self.base + "/v1/responses", json=self.request_body("/v1/responses"))
        self.assertIn("PARTIAL", response.text)
        self.assertIn("event: response.failed", response.text)
        self.assertNotIn("WRONG", response.text)
        self.assertEqual(len(browser.calls), 1)

    async def test_live_thinking_and_heartbeats_continue_while_text_is_staged(self):
        class ContinuousText:
            def __init__(self):
                self.gate, self.closed = threading.Event(), threading.Event()

            def stream(self, body, timeout, cancel, trace=None):
                try:
                    yield frame({"reasoning_content": "LIVE_THINKING"})
                    yield frame({"content": "STAGED_TEXT"})
                    while not self.gate.wait(0.005):
                        if cancel.is_set():
                            return
                        yield frame({"content": "."})
                    yield frame({}, "stop")
                finally:
                    self.closed.set()

        browser = ContinuousText()
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.multiple(bridge, _browser=browser, BROWSER_MESSAGES_DEFER_OUTPUT=True,
                                BROWSER_HEARTBEAT_SEC=0.03):
                try:
                    async with client.stream("POST", self.base + "/v1/messages",
                        json=self.request_body("/v1/messages")) as response:
                        lines, seen = response.aiter_lines(), []
                        async with asyncio.timeout(2):
                            async for line in lines:
                                seen.append(line)
                                if line == "event: ping" and any("LIVE_THINKING" in s for s in seen):
                                    break
                        before_finish = '\n'.join(seen)
                        self.assertIn("LIVE_THINKING", before_finish)
                        self.assertNotIn("text_delta", before_finish)
                        self.assertFalse(browser.closed.is_set())
                        browser.gate.set()
                        rest = '\n'.join([line async for line in lines])
                        self.assertIn("STAGED_TEXT", rest)
                        self.assertIn("event: message_stop", rest)
                finally:
                    browser.gate.set()

    async def test_disconnect_discards_staged_tool_and_closes_buffer_and_upstream(self):
        from stream_support import DeferredEvents

        buffers = []

        class TrackedBuffer(DeferredEvents):
            def __init__(self):
                super().__init__()
                buffers.append(self)

        class WaitingTool:
            def __init__(self):
                self.closed = threading.Event()

            def stream(self, body, timeout, cancel, trace=None):
                try:
                    yield frame({"tool_calls": [{"index": 0, "id": "a", "function": {
                        "name": "Bash", "arguments": '{"command":"NEVER_RUN"}'}}]})
                    cancel.wait(2)
                finally:
                    self.closed.set()

        browser = WaitingTool()
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.multiple(bridge, _browser=browser, BROWSER_MESSAGES_DEFER_OUTPUT=True,
                                DeferredEvents=TrackedBuffer):
                async with client.stream("POST", self.base + "/v1/messages",
                    json=self.request_body("/v1/messages")) as response:
                    seen = []
                    async for line in response.aiter_lines():
                        seen.append(line)
                        if line == "event: ping":
                            break
                    self.assertNotIn("tool_use", '\n'.join(seen))
                async with asyncio.timeout(3):
                    while not browser.closed.is_set() or not all(b.file.closed for b in buffers):
                        await asyncio.sleep(0.01)
        self.assertEqual(len(buffers), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
