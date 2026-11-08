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
from browser_upstream import BrowserError, BrowserHTTPError
import nvplay_bridge as bridge


POOL_TIMEOUT = ('上游 500: data:{"status":500,"detail":"Pool#acquire(Duration) has been '
                'pending for more than the configured timeout of 45000ms"}\n\n')


def frame(delta=None, finish=None, usage=None):
    data = {"choices": [] if delta is None else [{"delta": delta, "finish_reason": finish}]}
    if usage is not None:
        data["usage"] = usage
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def pool_error():
    return BrowserHTTPError(500, POOL_TIMEOUT)


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
    async def collect(self, browser, buffered=False, payload=None):
        with patch.object(bridge, "_browser", browser):
            return [event async for event in bridge._browser_events_with_retry(payload or {}, buffered=buffered)]

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

    async def test_generic_http_errors_and_uncertain_network_failures_are_not_replayed(self):
        errors = [BrowserHTTPError(status, POOL_TIMEOUT) for status in (400, 429, 502, 503)]
        errors += [BrowserHTTPError(500, "Internal Server Error"), BrowserError(POOL_TIMEOUT),
                   BrowserError("上游响应读取失败: TypeError: network error"),
                   BrowserHTTPError(500, "Pool#acquire(Duration): an unrelated error")]
        for error in errors:
            with self.subTest(error=str(error), status=getattr(error, "status", None)):
                browser = ScriptBrowser([[error], [frame({"content": "WRONG"}, "stop")]])
                output = await self.collect(browser)
                self.assertEqual(browser.calls, [{}])
                self.assertEqual(output[-1]["type"], "error")

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
            with self.subTest(endpoint=endpoint):
                browser = ScriptBrowser([[pool_error()], [frame({"content": "RECOVERED"}, "stop")]])
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    with patch.multiple(bridge, _browser=browser, BROWSER_POOL_TIMEOUT_BACKOFF=0.2):
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
            with self.subTest(endpoint=endpoint):
                browser = ScriptBrowser([[pool_error()], [frame({"content": "JSON_OK"}, "stop")]])
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    with patch.object(bridge, "_browser", browser):
                        response = await client.post(self.base + endpoint, json=self.request_body(endpoint, stream=False))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("JSON_OK", response.text)
                self.assertNotIn("Pool#acquire", response.text)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
