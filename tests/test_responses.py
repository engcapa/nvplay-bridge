"""Responses conversion, SSE lifecycle, real HTTP tool loops and cancellation."""
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
import nvplay_bridge as bridge
import responses_support as rs


def frame(delta=None, finish=None, usage=None):
    return "data: " + json.dumps({"choices": [] if delta is None else [{"delta": delta, "finish_reason": finish}],
                                  **({"usage": usage} if usage else {})}, ensure_ascii=False) + "\n\n"


FUNCTION = {"type": "function", "name": "weather", "parameters": {"type": "object", "properties": {
    "city": {"type": "string"}}, "required": ["city"]}}
CUSTOM = {"type": "custom", "name": "apply_patch", "description": "Apply a patch", "format": {"type": "text"}}
WEB_SEARCH = {"type": "web_search", "external_web_access": False}


class ConversionTests(unittest.TestCase):
    def prepare(self, body):
        return rs.prepare_request({"model": "kimi-k3.1-pro", **body}, bridge)

    def test_instructions_developer_images_and_effort(self):
        payload, _, _ = self.prepare({"instructions": "Be concise", "max_output_tokens": 150000,
                                     "reasoning": {"effort": "xhigh"}, "input": [
            {"role": "developer", "content": [{"type": "input_text", "text": "Use tools"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "Describe"},
                                          {"type": "input_image", "image_url": "data:image/png;base64,eA==", "detail": "low"}]}]})
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "Be concise"})
        self.assertEqual(payload["messages"][1], {"role": "system", "content": "Use tools"})
        self.assertEqual(payload["messages"][2]["content"][1]["image_url"]["detail"], "low")
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(payload["max_tokens"], bridge.BROWSER_MAX_TOKENS)

    def test_function_and_custom_roundtrip_preserves_call_ids_and_reasoning(self):
        payload, _, _ = self.prepare({"tools": [FUNCTION, CUSTOM], "input": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "check inputs"}]},
            {"type": "function_call", "name": "weather", "arguments": '{"city":"上海"}', "call_id": "call_1"},
            {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
            {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch\n中文🙂", "call_id": "call_2"},
            {"type": "custom_tool_call_output", "call_id": "call_2", "output": [{"type": "input_text", "text": "done"}]}]})
        messages = payload["messages"]
        self.assertEqual(messages[0]["reasoning_content"], "check inputs")
        self.assertEqual(messages[0]["tool_calls"][0]["id"], messages[1]["tool_call_id"])
        self.assertEqual(messages[2]["tool_calls"][0]["id"], messages[3]["tool_call_id"])
        self.assertEqual(json.loads(messages[2]["tool_calls"][0]["function"]["arguments"])["input"], "*** Begin Patch\n中文🙂")

    def test_namespace_mapping_choice_and_custom_grammar(self):
        tool = {"type": "namespace", "name": "functions", "tools": [FUNCTION, dict(CUSTOM, format={
            "type": "grammar", "syntax": "lark", "definition": "start: /.+/"})]}
        payload, _, registry = self.prepare({"tools": [tool], "tool_choice": {
            "type": "function", "namespace": "functions", "name": "weather"}, "parallel_tool_calls": False, "input": "Hi"})
        self.assertEqual(payload["tool_choice"]["function"]["name"], "functions__weather")
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertEqual(registry["functions__apply_patch"].kind, "custom")
        self.assertIn("start: /.+/", payload["tools"][1]["function"]["description"])

    def test_structured_output_and_no_forced_effort(self):
        payload, _, _ = self.prepare({"input": "Hi", "text": {"format": {
            "type": "json_schema", "name": "result", "schema": {"type": "object"}, "strict": True}}})
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "result")
        self.assertNotIn("reasoning_effort", payload)

    def test_codex_optional_nameless_search_does_not_block_client_tools(self):
        # Default Codex adds this built-in advertisement after its client tools.
        tools = [dict(FUNCTION, name=f"tool_{index}") for index in range(12)] + [WEB_SEARCH]
        payload, _, registry = self.prepare({"input": "hello", "tools": tools})
        self.assertEqual(len(payload["tools"]), 12)
        self.assertEqual(len(registry), 12)
        self.assertTrue(all(tool["function"]["name"].startswith("tool_") for tool in payload["tools"]))
        self.assertIn("web search is unavailable", payload["messages"][0]["content"])
        state = rs.ResponseEvents({"model": "kimi-k3.1-pro", "tools": tools}, registry)
        self.assertEqual(len(state.response["tools"]), 12)

    def test_search_only_advertisement_allows_text_but_explicit_search_fails(self):
        for kind in ("web_search", "web_search_preview", "web_search_preview_2025_03_11", "web_search_2025_08_26"):
            with self.subTest(kind=kind):
                payload, _, registry = self.prepare({"input": "hello", "tools": [{"type": kind}]})
                self.assertNotIn("tools", payload)
                self.assertFalse(registry)
                with self.assertRaises(rs.RequestError) as caught:
                    self.prepare({"input": "Search now", "tools": [{"type": kind}], "tool_choice": {"type": kind}})
                self.assertEqual(caught.exception.param, "tool_choice")
        with self.assertRaises(rs.RequestError):
            self.prepare({"input": "Search now", "tools": [WEB_SEARCH], "tool_choice": "required"})

    def test_unknown_builtins_and_malformed_named_tools_keep_precise_errors(self):
        for tool, param in (({"type": "tool_search"}, "tools[0].type"),
                            ({"type": "function"}, "tools[0].name"),
                            ({"type": "custom", "name": ""}, "tools[0].name"),
                            ({"type": "namespace", "name": "n", "tools": [WEB_SEARCH]}, "tools[0].tools[0].type")):
            with self.subTest(tool=tool), self.assertRaises(rs.RequestError) as caught:
                self.prepare({"input": "hello", "tools": [tool]})
            self.assertEqual(caught.exception.param, param)

    def test_prior_instructions_are_replaced_and_history_is_immutable(self):
        prior = {"items": rs.input_items("Original") + [{"type": "message", "role": "assistant", "content": "Answer"}], "registry": {}}
        before = deepcopy(prior)
        payload, items, _ = rs.prepare_request({"model": "kimi-k3.1-pro", "input": "Next", "instructions": "New instruction"}, bridge, prior)
        self.assertEqual([m["content"] for m in payload["messages"]], ["New instruction", "Original", "Answer", "Next"])
        self.assertEqual(prior, before)
        self.assertEqual(len(items), 3)

    def test_unsupported_features_and_invalid_types(self):
        for body in ({"input": 1}, {"input": [1]}, {"input": "x", "max_output_tokens": True},
                     {"input": "x", "reasoning": "high"}, {"input": "x", "tools": [{}]},
                     {"input": "x", "text": {"format": {}}}, {"input": "x", "background": True},
                     {"input": "x", "tools": [{"type": "file_search"}]},
                     {"input": [{"type": "compaction", "encrypted_content": "x"}]},
                     {"input": "x", "stream": "true"}):
            with self.subTest(body=body), self.assertRaises(rs.RequestError):
                self.prepare(body)


class EventTests(unittest.TestCase):
    def state(self, tools=()):
        return rs.ResponseEvents({"model": "kimi-k3.1-pro"}, rs.convert_tools(list(tools))[1])

    def test_reasoning_text_lifecycle_and_usage_tail(self):
        state = self.state()
        events = state.start()
        for value in ({"type": "thinking", "text": "consider"}, {"type": "text", "text": "中文🙂"},
                      {"type": "finish", "reason": "stop"}, {"type": "usage", "usage": {
                          "prompt_tokens": 9, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 5},
                          "completion_tokens_details": {"reasoning_tokens": 4}}}):
            events.extend(state.feed(value))
        events.extend(state.finish())
        self.assertEqual([e["sequence_number"] for e in events], list(range(len(events))))
        self.assertEqual(events[0]["response"]["output"], [])
        self.assertEqual(state.response["output"][0]["summary"][0]["text"], "consider")
        self.assertEqual(state.response["output"][1]["content"][0]["text"], "中文🙂")
        self.assertEqual(state.response["usage"]["input_tokens_details"]["cached_tokens"], 5)
        self.assertEqual(state.response["usage"]["output_tokens_details"]["reasoning_tokens"], 4)
        self.assertEqual(events[-1]["type"], "response.completed")

    def test_interleaved_tools_stream_independently_with_namespaces(self):
        state = self.state([FUNCTION, {"type": "namespace", "name": "fns", "tools": [FUNCTION]}])
        events = []
        for value in ({"type": "tool", "index": 0, "id": "a", "name": "weather", "args": '{"city":"'},
                      {"type": "tool", "index": 1, "id": "b", "name": "fns__weather", "args": '{"city":"北京"}'},
                      {"type": "tool", "index": 0, "args": '上海"}'}):
            events.extend(state.feed(value))
        self.assertEqual([e["output_index"] for e in events if e["type"] == "response.function_call_arguments.delta"], [0, 1, 0])
        state.feed({"type": "finish", "reason": "tool_calls"})
        state.finish()
        self.assertEqual(state.response["output"][1]["namespace"], "fns")
        self.assertEqual([i["call_id"] for i in state.response["output"]], ["a", "b"])

    def test_custom_tool_every_split_and_json_escape_boundary(self):
        text = '*** Begin Patch\n路径中文🙂\t"quoted"\\end'
        raw = json.dumps({"input": text}, ensure_ascii=True)
        for split in range(len(raw) + 1):
            state = self.state([CUSTOM])
            events = []
            for part in (raw[:split], raw[split:]):
                events.extend(state.feed({"type": "tool", "index": 0, "name": "apply_patch", "id": "a", "args": part}))
            state.feed({"type": "finish", "reason": "tool_calls"})
            events.extend(state.finish())
            self.assertEqual(state.response["output"][0]["input"], text)
            self.assertEqual("".join(e["delta"] for e in events if e["type"] == "response.custom_tool_call_input.delta"), text)

    def test_custom_tool_one_character_at_a_time_is_incremental(self):
        decoder = rs.CustomInputDecoder()
        raw = json.dumps({"input": "Hello\n🙂"}, ensure_ascii=True)
        parts = [decoder.feed(c) for c in raw]
        self.assertEqual("".join(parts), "Hello\n🙂")
        self.assertEqual(parts[raw.index('H')], "H")

    def test_length_is_incomplete_and_failures_cannot_complete(self):
        state = self.state()
        state.feed({"type": "thinking", "text": "partial"})
        state.feed({"type": "finish", "reason": "length"})
        self.assertEqual(state.finish()[-1]["type"], "response.incomplete")
        self.assertEqual(state.response["incomplete_details"]["reason"], "max_output_tokens")
        failed = self.state()
        failed.feed({"type": "text", "text": "partial"})
        self.assertEqual(failed.fail("broken")["response"]["status"], "failed")
        self.assertEqual(failed.response["output"][0]["content"][0]["text"], "partial")
        with self.assertRaises(ValueError):
            self.state().finish()

    def test_invalid_tool_arguments_and_unknown_tools_fail(self):
        for tools, name, args in (([FUNCTION], "weather", "{broken"), ([CUSTOM], "apply_patch", '{"input":123}')):
            state = self.state(tools)
            state.feed({"type": "tool", "index": 0, "id": "a", "name": name, "args": args})
            state.feed({"type": "finish", "reason": "tool_calls"})
            with self.assertRaises(ValueError):
                state.finish()
        with self.assertRaisesRegex(ValueError, "unknown tool"):
            self.state().feed({"type": "tool", "index": 0, "id": "a", "name": "missing", "args": "{}"})


class StoreTests(unittest.TestCase):
    def test_storage_limit_rejects_large_history_and_replacement_does_not_leak(self):
        store = rs.ResponseStore(max_bytes=600)
        response = {"id": "a", "output": []}
        with self.assertRaisesRegex(ValueError, "memory limit"):
            store.put(response, rs.input_items("x" * 800), {})
        with self.assertRaises(rs.RequestError):
            store.get("a")
        self.assertEqual(store.size, 0)
        store.put(response, rs.input_items("small"), {})
        size = store.size
        store.put(response, rs.input_items("small"), {})
        self.assertEqual(store.size, size)
        self.assertEqual(len(store.entries), 1)

    def test_ttl_eviction_delete_and_copy_isolation(self):
        store = rs.ResponseStore(max_entries=1, ttl=0.03)
        response = {"id": "a", "output": []}
        store.put(response, rs.input_items("secret"), {})
        value = store.get("a")
        value["items"].clear()
        self.assertEqual(len(store.get("a")["items"]), 1)
        store.put(dict(response, id="b"), [], {})
        with self.assertRaises(rs.RequestError):
            store.get("a")
        store.delete("b")
        self.assertEqual(store.size, 0)
        store.put(response, [], {})
        time.sleep(0.04)
        with self.assertRaises(rs.RequestError):
            store.get("a")


class ScriptBrowser:
    def __init__(self, scripts, gate=False):
        self.scripts, self.calls = scripts, []
        self.gate = threading.Event()
        self.gated, self.closed, self.cancelled = gate, threading.Event(), False

    def stream(self, body, timeout, cancel, trace=None):
        script = self.scripts[min(len(self.calls), len(self.scripts) - 1)]
        self.calls.append(deepcopy(body))
        try:
            for i, part in enumerate(script):
                yield part
                if self.gated and i == 0:
                    while not self.gate.wait(0.005):
                        if cancel.is_set():
                            return
        finally:
            self.cancelled = cancel.is_set()
            self.closed.set()


class HTTPTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_default_codex_search_advertisement_in_json_and_sse(self):
        browser = ScriptBrowser([[frame({"content": "hello"}, "stop")]])
        tools = [dict(FUNCTION, name=f"tool_{index}") for index in range(12)] + [WEB_SEARCH]
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.object(bridge, "_browser", browser):
                for stream in (False, True):
                    result = await client.post(self.base + "/v1/responses", json={
                        "model": "kimi-k3.1-pro", "input": "hello", "stream": stream, "store": False, "tools": tools})
                    self.assertEqual(result.status_code, 200, result.text)
                    if stream:
                        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
                        self.assertEqual(events[-1]["type"], "response.completed")
                        response = events[-1]["response"]
                    else:
                        response = result.json()
                    self.assertEqual(len(response["tools"]), 12)
                    self.assertEqual(response["output"][0]["content"][0]["text"], "hello")
                result = await client.post(self.base + "/v1/responses", json={"input": "Search now", "tools": [WEB_SEARCH],
                    "tool_choice": {"type": "web_search"}, "stream": True})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.json()["error"]["param"], "tool_choice")
                self.assertIn("Hosted web search is unavailable", result.json()["error"]["message"])
        self.assertEqual(len(browser.calls), 2)

    async def test_nonstream_and_previous_response_tools_retrieval_delete(self):
        browser = ScriptBrowser([[frame({"tool_calls": [{"index": 0, "id": "call_1", "function": {
            "name": "weather", "arguments": '{"city":"上海"}'}}]}, "tool_calls")],
                                 [frame({"content": "sunny"}, "stop")]])
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.object(bridge, "_browser", browser):
                first = await client.post(self.base + "/v1/responses", json={"model": "kimi-k3.1-pro", "input": "weather?", "tools": [FUNCTION]})
                self.assertEqual(first.status_code, 200, first.text)
                response_id = first.json()["id"]
                retrieved = await client.get(self.base + "/v1/responses/" + response_id)
                self.assertEqual(retrieved.json(), first.json())
                second = await client.post(self.base + "/v1/responses", json={"model": "kimi-k3.1-pro", "previous_response_id": response_id,
                    "input": [{"type": "function_call_output", "call_id": "call_1", "output": "sunny"}], "store": False})
                self.assertEqual(second.status_code, 200, second.text)
                self.assertEqual(second.json()["output"][0]["content"][0]["text"], "sunny")
                self.assertEqual(browser.calls[1]["messages"][-1]["tool_call_id"], "call_1")
                self.assertEqual((await client.get(self.base + "/v1/responses/" + second.json()["id"])).status_code, 404)
                self.assertTrue((await client.delete(self.base + "/v1/responses/" + response_id)).json()["deleted"])
                self.assertEqual((await client.get(self.base + "/v1/responses/" + response_id)).status_code, 404)

    async def test_text_reasoning_and_function_arguments_arrive_before_finish(self):
        deltas = [("response.output_text.delta", {"content": "first"}, None),
                  ("response.reasoning_summary_text.delta", {"reasoning_content": "first"}, None),
                  ("response.function_call_arguments.delta", {"tool_calls": [{"index": 0, "id": "call_1", "function": {
                      "name": "weather", "arguments": '{"city":"'}}]}, [FUNCTION])]
        for expected, delta, tools in deltas:
            with self.subTest(expected=expected):
                tail = (frame({"tool_calls": [{"index": 0, "function": {"arguments": '上海"}'}}]}, "tool_calls")
                        if tools else frame({"content": "answer"}, "stop"))
                browser = ScriptBrowser([[frame(delta), tail]], gate=True)
                body = {"model": "kimi-k3.1-pro", "input": "Hello", "stream": True, "store": False}
                if tools:
                    body["tools"] = tools
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    with patch.object(bridge, "_browser", browser):
                        async with client.stream("POST", self.base + "/v1/responses", json=body) as response:
                            lines = response.aiter_lines()
                            async for line in lines:
                                if line == "event: " + expected:
                                    break
                            self.assertFalse(browser.gate.is_set())
                            self.assertFalse(browser.closed.is_set())
                            browser.gate.set()
                            rest = "\n".join([line async for line in lines])
                            self.assertIn("event: response.completed", rest)

    async def test_stream_disconnect_cancels_upstream(self):
        browser = ScriptBrowser([[frame({"reasoning_content": "thinking"})]], gate=True)
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.object(bridge, "_browser", browser):
                async with client.stream("POST", self.base + "/v1/responses", json={"input": "Hi", "stream": True}) as response:
                    async for line in response.aiter_lines():
                        if "response.reasoning_summary_text.delta" in line:
                            break
                async with asyncio.timeout(2):
                    while not browser.closed.is_set():
                        await asyncio.sleep(0.01)
                self.assertTrue(browser.cancelled)

    async def test_nonstream_disconnect_cancels_upstream(self):
        browser = ScriptBrowser([[frame({"reasoning_content": "thinking"})]], gate=True)
        port = int(self.base.rsplit(":", 1)[1])
        with patch.object(bridge, "_browser", browser):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"input": "Hi", "store": False}).encode()
            writer.write(f"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.close()
            await writer.wait_closed()
            async with asyncio.timeout(2):
                while not browser.closed.is_set():
                    await asyncio.sleep(0.01)
            self.assertTrue(browser.cancelled)

    async def test_partial_failure_and_truncation_are_honest(self):
        for finish, expected in ((None, "response.failed"), ("length", "response.incomplete")):
            browser = ScriptBrowser([[frame({"reasoning_content": "partial"}, finish)]])
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                with patch.object(bridge, "_browser", browser):
                    result = await client.post(self.base + "/v1/responses", json={"input": "Hi", "stream": True, "store": False})
            self.assertIn("event: " + expected, result.text)
            self.assertNotIn("event: response.completed", result.text)
            self.assertEqual(len(browser.calls), 1)

    async def test_storage_failure_never_reports_completed_or_retrievable(self):
        browser = ScriptBrowser([[frame({"content": "answer"}, "stop")]])
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.object(bridge, "_browser", browser), \
                 patch.object(rs.ResponseStore, "put", side_effect=ValueError("Storage is full")):
                result = await client.post(self.base + "/v1/responses", json={"input": "Hi", "stream": True})
                events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
                self.assertEqual(events[-1]["type"], "response.failed")
                self.assertNotIn("event: response.completed", result.text)
                response_id = events[-1]["response"]["id"]
                self.assertEqual((await client.get(self.base + "/v1/responses/" + response_id)).status_code, 404)
                result = await client.post(self.base + "/v1/responses", json={"input": "Hi"})
                self.assertEqual(result.status_code, 502)
                self.assertEqual(result.json()["error"]["message"], "Storage is full")
        self.assertEqual(len(browser.calls), 2)

    async def test_heartbeats_during_upstream_wait_have_no_fake_event_type(self):
        browser = ScriptBrowser([[frame({"reasoning_content": "thinking"}), frame({"content": "done"}, "stop")]], gate=True)
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            with patch.multiple(bridge, _browser=browser, BROWSER_HEARTBEAT_SEC=0.02):
                async with client.stream("POST", self.base + "/v1/responses", json={"input": "Hi", "stream": True, "store": False}) as response:
                    lines = response.aiter_lines()
                    async with asyncio.timeout(2):
                        async for line in lines:
                            if line == ": ping":
                                break
                    self.assertFalse(browser.closed.is_set())
                    browser.gate.set()
                    rest = "\n".join([line async for line in lines])
                    self.assertIn("event: response.completed", rest)

    async def test_validation_returns_openai_errors_before_generation(self):
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            for body, status in (([], 400), ({}, 400), ({"model": "unknown", "input": "Hi"}, 404),
                                 ({"model": [], "input": "Hi"}, 400),
                                 ({"input": "Hi", "previous_response_id": "resp_missing"}, 404)):
                response = await client.post(self.base + "/v1/responses", json=body)
                self.assertEqual(response.status_code, status, response.text)
                self.assertIn("message", response.json()["error"])
            response = await client.post(self.base + "/v1/responses", content="{broken", headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 400)
            models = (await client.get(self.base + "/v1/models")).json()
            self.assertEqual(models["object"], "list")

    async def test_official_openai_sdk_json_and_stream_accumulator(self):
        try:
            from openai import AsyncOpenAI
        except ImportError:
            self.skipTest("Optional OpenAI SDK is not installed")
        browser = ScriptBrowser([[frame({"reasoning_content": "consider"}), frame({"content": "SDK_OK"}, "stop"),
                                  frame(usage={"prompt_tokens": 9, "completion_tokens": 7})]])
        async with AsyncOpenAI(api_key="local-test-only", base_url=self.base + "/v1", max_retries=0) as client:
            models = await client.models.list()
            self.assertTrue(any(model.id == "kimi-k3.1-pro" and model.created == 1767225600 for model in models.data))
            with patch.object(bridge, "_browser", browser):
                result = await client.responses.create(model="kimi-k3.1-pro", input="Hi", store=False)
                self.assertEqual(result.output_text, "SDK_OK")
                async with client.responses.stream(model="kimi-k3.1-pro", input="Hi", store=False) as stream:
                    events = [event async for event in stream]
                    final = await stream.get_final_response()
                self.assertEqual(final.output_text, "SDK_OK")
                self.assertTrue(any(event.type == "response.reasoning_summary_text.delta" for event in events))
                self.assertEqual(final.usage.output_tokens, 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
