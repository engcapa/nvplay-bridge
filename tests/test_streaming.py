"""Framing, real HTTP delivery, cancellation and CDP stream ownership tests."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
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
import browser_upstream as bu
import nvplay_bridge as bridge
from stream_support import AnthropicBlocks, SSEDecoder
from browser_events import browser_events


def frame(delta=None, finish=None, usage=None):
    data = {"choices": [] if delta is None else [{"delta": delta, "finish_reason": finish}]}
    if usage is not None:
        data["usage"] = usage
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


class FramingTests(unittest.TestCase):
    def test_every_split_and_unicode(self):
        wire = ': ping\r\ndata: {"choices":\r\ndata: [{"delta":{"content":"中文🙂"}}]}\r\n\r\ndata: [DONE]\r\n\r\n'
        for split in range(len(wire) + 1):
            decoder = SSEDecoder()
            values = decoder.feed(wire[:split]) + decoder.feed(wire[split:]) + decoder.feed("", final=True)
            self.assertEqual(values[0]["choices"][0]["delta"]["content"], "中文🙂")
            self.assertEqual(len(values), 1)
            self.assertTrue(decoder.ended)

    def test_one_character_at_a_time_and_usage_tail(self):
        decoder = SSEDecoder()
        values = []
        for character in frame({}, "stop") + frame(usage={"completion_tokens": 7}):
            values.extend(decoder.feed(character))
        values.extend(decoder.feed("", final=True))
        self.assertEqual(values[-1]["usage"]["completion_tokens"], 7)

    def test_truncated_json_is_error(self):
        decoder = SSEDecoder()
        decoder.feed('data: {"choices": [')
        with self.assertRaises(ValueError):
            decoder.feed("", final=True)

    def test_tool_metadata_late_and_interleaved_calls(self):
        blocks = AnthropicBlocks()
        outputs = []
        for event in (
            {"type": "tool", "index": 0, "id": None, "name": None, "args": '{"a":'},
            {"type": "tool", "index": 0, "id": "a", "name": "first", "args": ""},
            {"type": "tool", "index": 1, "id": "b", "name": "second", "args": '{"b":2}'},
            {"type": "tool", "index": 0, "id": None, "name": None, "args": "1}"},
            {"type": "finish", "reason": "tool_calls"},
        ):
            outputs.extend(blocks.feed(event))
        outputs.extend(blocks.finish())
        starts = [v for k, v in outputs if k == "content_block_start"]
        self.assertEqual([v["index"] for v in starts], [0, 1])
        for index, expected in ((0, {"a": 1}), (1, {"b": 2})):
            args = "".join(v["delta"]["partial_json"] for k, v in outputs
                           if k == "content_block_delta" and v["index"] == index)
            self.assertEqual(json.loads(args), expected)
        self.assertEqual([v["index"] for k, v in outputs if k == "content_block_stop"], [0, 1])

    def test_invalid_tool_json_never_successfully_finishes(self):
        blocks = AnthropicBlocks()
        blocks.feed({"type": "tool", "index": 0, "id": "a", "name": "f", "args": '{"a":'})
        blocks.feed({"type": "finish", "reason": "tool_calls"})
        with self.assertRaises(ValueError):
            blocks.finish()


class ScriptBrowser:
    def __init__(self, attempts):
        self.attempts = attempts
        self.calls = 0
        self.closed = threading.Event()

    def stream(self, body, timeout, cancel, trace=None):
        script = self.attempts[min(self.calls, len(self.attempts) - 1)]
        self.calls += 1
        try:
            yield from script
        finally:
            self.closed.set()


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_thinking_is_not_replaced(self):
        browser = ScriptBrowser([[frame({"reasoning_content": "thinking"}, "stop")],
                                 [frame({"content": "replacement"}, "stop")]])
        with patch.object(bridge, "_browser", browser):
            output = [value async for value in bridge.stream_anthropic({}, {}, "kimi-k3.1-pro")]
        wire = "".join(output)
        self.assertIn("thinking_delta", wire)
        self.assertIn("event: error", wire)
        self.assertNotIn("event: message_stop", wire)
        self.assertEqual(browser.calls, 1)

    async def test_split_placeholder_retried_before_content(self):
        browser = ScriptBrowser([[frame({"content": "[Tool use "}), frame({"content": "interrupted]"}, "stop")],
                                 [frame({"content": "OK"}, "stop")]])
        with patch.object(bridge, "_browser", browser):
            wire = "".join([value async for value in bridge.stream_anthropic({}, {}, "kimi-k3.1-pro")])
        self.assertNotIn("[Tool use interrupted]", wire)
        self.assertIn('"text": "OK"', wire)
        self.assertEqual(browser.calls, 2)

    async def test_thinking_token_limit_is_honest_truncation(self):
        browser = ScriptBrowser([[frame({"reasoning_content": "thinking"}, "length")]])
        with patch.object(bridge, "_browser", browser):
            wire = "".join([value async for value in bridge.stream_anthropic({}, {}, "kimi-k3.1-pro")])
        self.assertIn('"stop_reason": "max_tokens"', wire)
        self.assertIn("event: message_stop", wire)
        self.assertEqual(browser.calls, 1)

    async def test_missing_finish_and_partial_failure_are_errors(self):
        browser = ScriptBrowser([[frame({"content": "partial"})]])
        with patch.object(bridge, "_browser", browser):
            wire = "".join([value async for value in bridge.stream_anthropic({}, {}, "kimi-k3.1-pro")])
        self.assertIn("partial", wire)
        self.assertIn("event: error", wire)
        self.assertNotIn("event: message_stop", wire)


class FakeCDP(bu.BrowserUpstream):
    def __init__(self, states):
        super().__init__()
        self.states = iter(states)
        self.last = {"status": 200, "data": "", "done": False}
        self.aborted = 0

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
            self.last = next(self.states, self.last)
            return self.last
        if "__bridgeAbort" in expression:
            self.aborted += 1
            return True


class AsyncBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_backpressure_failure_closes_source_without_blocking_on_error(self):
        closed = threading.Event()

        class Flood:
            def stream(self, body, timeout, cancel, trace=None):
                try:
                    while True:
                        yield frame({"content": "x" * 8192})
                finally:
                    closed.set()

        with patch("browser_events.BACKPRESSURE_TIMEOUT", 0.03):
            async with aclosing(browser_events(Flood(), {}, 5, 0, bridge._chunk_events,
                                               lambda *a, **k: None)) as source:
                await anext(source)
                async with asyncio.timeout(1):
                    while not closed.is_set():
                        await asyncio.sleep(0.01)
                remaining = [event async for event in source]
                self.assertEqual(remaining[-1]["type"], "error")
                self.assertIn("读取过慢", remaining[-1]["message"])

    async def test_slow_consumer_has_bounded_queue_and_can_cancel(self):
        class Flood:
            produced = 0
            cancelled = False

            def stream(self, body, timeout, cancel, trace=None):
                try:
                    while not cancel.is_set():
                        self.produced += 1
                        yield frame({"content": "x" * 8192})
                finally:
                    self.cancelled = cancel.is_set()

        browser = Flood()
        source = browser_events(browser, {}, 5, 0, bridge._chunk_events, lambda *a, **k: None)
        await anext(source)
        await asyncio.sleep(0.2)
        self.assertLessEqual(browser.produced, 20)
        await source.aclose()
        self.assertTrue(browser.cancelled)

    async def test_usage_only_progress_does_not_suppress_client_heartbeats(self):
        class Usage:
            def stream(self, body, timeout, cancel, trace=None):
                for _ in range(100):
                    if cancel.is_set():
                        return
                    yield frame(usage={"completion_tokens": 1})
                    cancel.wait(0.01)

        async with aclosing(browser_events(Usage(), {}, 5, 0.04, bridge._chunk_events,
                                           lambda *a, **k: None)) as source:
            seen = False
            async with asyncio.timeout(0.5):
                async for event in source:
                    if event["type"] == "ping":
                        seen = True
                        break
            self.assertTrue(seen)

    async def test_openai_and_nvcf_conversion_still_stream(self):
        original_client = httpx.AsyncClient
        wire = frame({"reasoning_content": "think"}) + frame({"content": "answer"}, "stop") + frame(usage={"completion_tokens": 7})

        def factory(**kwargs):
            return original_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=wire)), **kwargs)

        for mode in ("openai", "nvcf"):
            with patch.multiple(bridge, UPSTREAM_KIND=mode, NVCF_URL="https://upstream.test/predict"):
                with patch.object(httpx, "AsyncClient", side_effect=factory):
                    output = "".join([chunk async for chunk in bridge.stream_anthropic({}, {}, "kimi-k3.1-pro")])
            self.assertIn("thinking_delta", output)
            self.assertIn("answer", output)
            self.assertIn('"output_tokens": 7', output)
            self.assertIn("event: message_stop", output)


class CDPStreamTests(unittest.TestCase):
    def test_first_data_before_completion_and_abort_on_close(self):
        browser = FakeCDP([{"status": 200, "data": "first", "done": False}])
        trace = []
        source = browser.stream({}, timeout=1, trace=lambda event, **data: trace.append((event, data)))
        self.assertEqual(next(source), "first")
        self.assertTrue(browser._lock.locked())
        source.close()
        self.assertEqual(browser.aborted, 1)
        self.assertFalse(browser._lock.locked())
        self.assertTrue(any(event == "browser_abort" and data["confirmed"] for event, data in trace))

    def test_total_timeout_still_applies_to_busy_stream(self):
        browser = FakeCDP([{"status": 200, "data": "data", "done": False}])
        with patch.multiple(bu, POLL_INTERVAL=0.005, IDLE_TIMEOUT=0.03):
            with self.assertRaisesRegex(bu.BrowserError, "总时限"):
                list(browser.stream({}, timeout=0.08))
        self.assertEqual(browser.aborted, 1)

    def test_failed_abort_marks_tab_for_recovery(self):
        browser = FakeCDP([{"status": 200, "data": "first", "done": False}])
        original = browser._eval

        def failed_abort(expression, timeout=5):
            return False if expression.startswith("window.__bridgeAbort(") else original(expression, timeout)

        browser._eval = failed_abort
        source = browser.stream({})
        next(source)
        source.close()
        self.assertTrue(browser.needs_refresh)
        self.assertFalse(browser._lock.locked())

    def test_idle_and_first_byte_timeout(self):
        for states, message in (([], "首个数据"),
                                ([{"status": 200, "data": "first", "done": False},
                                  {"status": 200, "data": "", "done": False}], "空闲")):
            browser = FakeCDP(states)
            with patch.multiple(bu, POLL_INTERVAL=0.005, FIRST_BYTE_TIMEOUT=0.03, IDLE_TIMEOUT=0.03):
                with self.assertRaisesRegex(bu.BrowserError, message):
                    list(browser.stream({}, timeout=1))
            self.assertEqual(browser.aborted, 1)

    def test_success_status_does_not_hide_reader_failure(self):
        browser = FakeCDP([{"status": 200, "data": "partial", "done": True, "error": "broken"}])
        with self.assertRaisesRegex(bu.BrowserError, "broken"):
            list(browser.stream({}))
        self.assertEqual(browser.aborted, 1)

    def test_pool_returns_tab_after_cleanup_and_threshold_checks(self):
        pool = bu.BrowserPool(size=1)
        browser = FakeCDP([{"status": 200, "data": "ok", "done": True}])
        browser.hint = "test=1"
        pool._ready = True
        pool._ups = [browser]
        pool._queue.put(browser)

        def text_len():
            self.assertTrue(pool._queue.empty())
            self.assertFalse(browser._lock.locked())
            return 999999

        browser.page_text_len = text_len
        self.assertEqual(pool.generate({}, timeout=1), "ok")
        self.assertEqual(pool._queue.qsize(), 0)
        self.assertEqual(pool.status()["tabs"][browser.hint], "pending")

    def test_cancel_while_waiting_for_pool(self):
        pool = bu.BrowserPool(size=1)
        pool._ready = True
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(bu.BrowserCancelled):
            list(pool.stream({}, cancel=cancel))


class GatedBrowser:
    def __init__(self, tool=False):
        self.gate = threading.Event()
        self.closed = threading.Event()
        self.cancelled = False
        self.tool = tool

    def stream(self, body, timeout, cancel, trace=None):
        try:
            if self.tool:
                yield frame({"tool_calls": [{"index": 0, "id": "call_test", "function": {
                    "name": "weather", "arguments": '{"city":"'}}]})
            else:
                yield frame({"reasoning_content": "thinking now"})
            while not self.gate.wait(0.01):
                if cancel.is_set():
                    return
            if self.tool:
                yield frame({"tool_calls": [{"index": 0, "function": {"arguments": '上海"}'}}]}, "tool_calls")
            else:
                yield frame({"content": "answer"}, "stop")
            yield frame(usage={"prompt_tokens": 9, "completion_tokens": 3})
        finally:
            self.cancelled = cancel.is_set()
            self.closed.set()


class HTTPStreamingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.socket = socket.socket()
        cls.socket.bind(("127.0.0.1", 0))
        cls.port = cls.socket.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(bridge.app, log_level="error", lifespan="off"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.socket]}, daemon=True)
        cls.thread.start()
        end = time.monotonic() + 5
        while not cls.server.started and time.monotonic() < end:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("test HTTP server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.socket.close()

    async def wait_closed(self, browser):
        async with asyncio.timeout(3):
            while not browser.closed.is_set():
                await asyncio.sleep(0.01)

    async def run_gate(self, tool):
        browser = GatedBrowser(tool)
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.object(bridge, "_browser", browser):
                async with client.stream("POST", f"http://127.0.0.1:{self.port}/v1/messages",
                                         json={"model": "kimi-k3.1-pro", "stream": True,
                                               "messages": [{"role": "user", "content": "hi"}]}) as response:
                    self.assertEqual(response.status_code, 200)
                    lines = response.aiter_lines()
                    seen = []
                    expected = "input_json_delta" if tool else "thinking_delta"
                    async with asyncio.timeout(2):
                        async for line in lines:
                            seen.append(line)
                            if expected in line:
                                break
                    self.assertFalse(browser.closed.is_set())
                    self.assertFalse(browser.gate.is_set())
                    browser.gate.set()
                    seen.extend([line async for line in lines])
                    self.assertIn("event: message_stop", seen)
                    self.assertTrue(any('"output_tokens": 3' in line for line in seen))
                await self.wait_closed(browser)

    async def test_thinking_arrives_over_http_before_generation_finishes(self):
        await self.run_gate(False)

    async def test_tool_arguments_arrive_over_http_before_generation_finishes(self):
        await self.run_gate(True)

    async def test_http_disconnect_cancels_upstream(self):
        browser = GatedBrowser()
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.object(bridge, "_browser", browser):
                async with client.stream("POST", f"http://127.0.0.1:{self.port}/v1/messages",
                                         json={"model": "kimi-k3.1-pro", "stream": True,
                                               "messages": [{"role": "user", "content": "hi"}]}) as response:
                    async for line in response.aiter_lines():
                        if "thinking_delta" in line:
                            break
                await self.wait_closed(browser)
                self.assertTrue(browser.cancelled)
                self.assertFalse(browser.gate.is_set())

    async def test_nonstream_disconnect_also_cancels_upstream(self):
        browser = GatedBrowser()
        with patch.object(bridge, "_browser", browser):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            body = json.dumps({"model": "kimi-k3.1-pro", "messages": [{"role": "user", "content": "hi"}]}).encode()
            writer.write(f"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            await asyncio.sleep(0.15)
            writer.close()
            await writer.wait_closed()
            await self.wait_closed(browser)
            self.assertTrue(browser.cancelled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
