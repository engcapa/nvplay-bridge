"""Regression coverage for lost tool history and premature agent termination."""
from __future__ import annotations

from contextlib import aclosing
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("UPSTREAM_KIND", "browser")
import httpx
import nvplay_bridge as bridge


class ToolIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_policy_preserves_visible_history_and_current_reasoning_in_all_formats(self):
        seen = []

        async def source(payload, headers, *, buffered=True, defer_output=False):
            seen.append(deepcopy(payload))
            yield {"type": "thinking", "text": "CURRENT_REASONING"}
            yield {"type": "text", "text": "CURRENT_ANSWER"}
            yield {"type": "finish", "reason": "stop"}

        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app), base_url="http://local") as client:
            with patch.object(bridge, "_iter_upstream_events", source):
                for protocol in ("messages", "responses"):
                    for stream in (False, True):
                        body = {"model": "kimi-k3.1-pro", "stream": stream}
                        if protocol == "messages":
                            body.update(max_tokens=65536, tools=[{"name": "read", "input_schema": schema}], messages=[
                                {"role": "user", "content": "USER_REQUIREMENT"},
                                {"role": "assistant", "content": [
                                    {"type": "thinking", "thinking": "OLD_REASONING", "signature": "local"},
                                    {"type": "text", "text": "VISIBLE_PLAN"},
                                    {"type": "tool_use", "id": "old_read", "name": "read", "input": {"path": "a.txt"}}]},
                                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old_read", "content": "ACTUAL_RESULT"}]}])
                        else:
                            body.update(store=False, tools=[{"type": "function", "name": "read", "parameters": schema}], input=[
                                {"role": "user", "content": "USER_REQUIREMENT"},
                                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "OLD_REASONING"}]},
                                {"role": "assistant", "content": "VISIBLE_PLAN"},
                                {"type": "function_call", "call_id": "old_read", "name": "read", "arguments": '{"path":"a.txt"}'},
                                {"type": "function_call_output", "call_id": "old_read", "output": "ACTUAL_RESULT"}])
                        original = deepcopy(body)
                        policies = []
                        for policy in ("preserve", "omit"):
                            with patch.object(bridge, "HISTORY_REASONING", policy):
                                response = await client.post("/v1/" + protocol, json=body)
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertIn("CURRENT_REASONING", response.text)
                            self.assertIn("CURRENT_ANSWER", response.text)
                            self.assertEqual(body, original)
                            messages = seen[-1]["messages"]
                            self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool"])
                            self.assertEqual(messages[0]["content"], "USER_REQUIREMENT")
                            self.assertEqual(messages[1]["content"], "VISIBLE_PLAN")
                            call = messages[1]["tool_calls"][0]
                            self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "a.txt"})
                            self.assertEqual(messages[2]["tool_call_id"], call["id"])
                            self.assertEqual(messages[2]["content"], "ACTUAL_RESULT")
                            self.assertEqual(messages[1].get("reasoning_content"), "OLD_REASONING" if policy == "preserve" else None)
                            policies.append(messages)
                        left = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in policies[0]]
                        # Messages histories receive fresh input-side IDs on each request.
                        left[1]["tool_calls"][0]["id"] = policies[1][1]["tool_calls"][0]["id"]
                        left[2]["tool_call_id"] = policies[1][2]["tool_call_id"]
                        self.assertEqual(left, policies[1])

    async def test_omitting_history_does_not_mutate_the_request_or_other_controls(self):
        payload = {"messages": [{"role": "assistant", "content": "VISIBLE", "reasoning_content": "OLD"}],
                   "reasoning_effort": "max", "max_tokens": 65536, "seed": 0}
        original = deepcopy(payload)

        async def source(request, headers, *, buffered=True, defer_output=False):
            self.assertNotIn("reasoning_content", request["messages"][0])
            self.assertEqual({k: v for k, v in request.items() if k != "messages"},
                             {k: v for k, v in original.items() if k != "messages"})
            yield {"type": "finish", "reason": "stop"}

        with patch.object(bridge, "HISTORY_REASONING", "omit"), patch.object(bridge, "_iter_upstream_events", source):
            async for _ in bridge.iter_events(payload, {}):
                pass
        self.assertEqual(payload, original)

    async def test_invalid_tool_arguments_fail_without_silent_empty_object(self):
        value = {"args": '{"path":'}

        async def source(payload, headers, *, buffered=True, defer_output=False):
            yield {"type": "tool", "index": 0, "id": "Read:0", "name": "read", "args": value["args"]}
            yield {"type": "finish", "reason": "tool_calls"}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app), base_url="http://local") as client:
            with patch.object(bridge, "_iter_upstream_events", source):
                for args in ('{"path":', '[]', '42'):
                    value["args"] = args
                    response = await client.post("/v1/messages", json={"model": "kimi-k3.1-pro", "max_tokens": 32,
                        "messages": [{"role": "user", "content": "probe"}]})
                    self.assertEqual(response.status_code, 502, response.text)
                    self.assertIn("tool", response.json()["error"]["message"])

    async def test_explicit_seed_zero_is_forwarded_and_invalid_types_rejected(self):
        seen = []

        async def source(payload, headers, *, buffered=True, defer_output=False):
            seen.append(deepcopy(payload))
            yield {"type": "text", "text": "OK"}
            yield {"type": "finish", "reason": "stop"}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app), base_url="http://local") as client:
            with patch.object(bridge, "_iter_upstream_events", source):
                for protocol in ("messages", "responses"):
                    base = {"model": "kimi-k3.1-pro"}
                    base.update(messages=[{"role": "user", "content": "probe"}], max_tokens=32) if protocol == "messages" else base.update(input="probe", store=False)
                    for value in (0, None):
                        response = await client.post("/v1/" + protocol, json={**base, "seed": value})
                        self.assertEqual(response.status_code, 200, response.text)
                        if value is None:
                            self.assertNotIn("seed", seen[-1])
                        else:
                            self.assertEqual(seen[-1]["seed"], 0)
                    count = len(seen)
                    for value in (False, "0", 1.5):
                        response = await client.post("/v1/" + protocol, json={**base, "seed": value})
                        self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(len(seen), count)

    async def test_ids_unique_in_all_routes_and_formats_with_unchanged_arguments(self):
        closed = []

        async def source(payload, headers, *, buffered=True, defer_output=False):
            try:
                yield {"type": "text", "text": "正文 中文🙂"}
                for index in (0, 1):
                    # Reused provider ID, arguments arriving before metadata.
                    yield {"type": "tool", "index": index, "id": None, "name": None, "args": '{"path":'}
                    yield {"type": "tool", "index": index, "id": "Read:0", "name": "read", "args": '"中文.txt"}'}
                yield {"type": "finish", "reason": "tool_calls"}
            finally:
                closed.append(True)

        ids = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app), base_url="http://local") as client:
            with patch.object(bridge, "_iter_upstream_events", source):
                for protocol in ("messages", "responses"):
                    for stream in (False, True):
                        for _ in range(2):
                            body = {"model": "kimi-k3.1-pro", "stream": stream}
                            schema = {"type": "object", "properties": {"path": {"type": "string"}}}
                            if protocol == "messages":
                                body.update(messages=[{"role": "user", "content": "probe"}], max_tokens=65536,
                                            tools=[{"name": "read", "input_schema": schema}])
                            else:
                                body.update(input="probe", store=False,
                                            tools=[{"type": "function", "name": "read", "parameters": schema}])
                            response = await client.post("/v1/" + protocol, json=body)
                            self.assertEqual(response.status_code, 200, response.text)
                            if stream:
                                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                                if protocol == "messages":
                                    blocks = [event["content_block"] for event in events if event.get("content_block", {}).get("type") == "tool_use"]
                                    received = [block["id"] for block in blocks]
                                    arguments = {}
                                    for event in events:
                                        if event.get("delta", {}).get("type") == "input_json_delta":
                                            key = event["index"]
                                            arguments[key] = arguments.get(key, "") + event["delta"]["partial_json"]
                                    text = "".join(event.get("delta", {}).get("text", "") for event in events)
                                    parsed = [json.loads(value) for value in arguments.values()]
                                    self.assertEqual(events[-1]["type"], "message_stop")
                                else:
                                    final = events[-1]["response"]
                                    blocks = [item for item in final["output"] if item["type"] == "function_call"]
                                    received = [block["call_id"] for block in blocks]
                                    parsed = [json.loads(block["arguments"]) for block in blocks]
                                    text = "".join(event.get("delta", "") for event in events if event.get("type") == "response.output_text.delta")
                                    self.assertEqual(events[-1]["type"], "response.completed")
                            else:
                                data = response.json()
                                if protocol == "messages":
                                    blocks = [block for block in data["content"] if block["type"] == "tool_use"]
                                    received = [block["id"] for block in blocks]
                                    parsed = [block["input"] for block in blocks]
                                    text = "".join(block.get("text", "") for block in data["content"])
                                else:
                                    blocks = [item for item in data["output"] if item["type"] == "function_call"]
                                    received = [block["call_id"] for block in blocks]
                                    parsed = [json.loads(block["arguments"]) for block in blocks]
                                    text = "".join(part.get("text", "") for item in data["output"] for part in item.get("content", []))
                            self.assertEqual(text, "正文 中文🙂")
                            self.assertEqual(parsed, [{"path": "中文.txt"}] * 2)
                            self.assertEqual([block["name"] for block in blocks], ["read", "read"])
                            ids.extend(received)
        self.assertEqual(len(ids), 16)
        self.assertEqual(len(set(ids)), 16)
        self.assertNotIn("Read:0", ids)
        self.assertEqual(len(closed), 8)

    async def test_early_close_releases_upstream_source(self):
        closed = []

        async def source(payload, headers, *, buffered=True, defer_output=False):
            try:
                yield {"type": "thinking", "text": "partial"}
                yield {"type": "finish", "reason": "stop"}
            finally:
                closed.append(True)

        with patch.object(bridge, "_iter_upstream_events", source):
            async with aclosing(bridge.iter_events({}, {})) as events:
                async for _ in events:
                    break
        self.assertEqual(closed, [True])


def history(count=3, *, result="OK", prefix="task"):
    messages = [{"role": "user", "content": prefix}]
    for index in range(count):
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": str(index), "type": "function", "function": {
                "name": "Read", "arguments": '{"file_path":"cache.py"}'}}]},
            {"role": "tool", "tool_call_id": str(index), "content": result},
        ])
    return {"messages": messages}


class LoopReminderTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(bridge, "LOOP_BREAKER", 1)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def unchanged(self, payload):
        original = deepcopy(payload)
        bridge._maybe_break_tool_loop(payload)
        self.assertEqual(payload, original)

    def test_distinct_sessions_and_retries_cannot_share_counters(self):
        common = "Shared harness preamble. " * 20
        for task in ("draw", "math", "code"):
            for _ in range(4):
                self.unchanged(history(1, prefix=common + task))
                self.unchanged(history(1, prefix=[{"type": "text", "text": task}]))

    def test_new_user_turn_resets_evidence_including_structured_content(self):
        for content in ("Next development stage", [{"type": "text", "text": "Next stage"}]):
            payload = history()
            payload["messages"].append({"role": "user", "content": content})
            self.unchanged(payload)

    def test_failed_incomplete_or_changing_results_do_not_trigger(self):
        for result in ("Error: file missing", "<tool_use_error>missing</tool_use_error>"):
            self.unchanged(history(result=result))
        incomplete = history()
        incomplete["messages"].pop()
        self.unchanged(incomplete)
        changed = history()
        changed["messages"][-1]["content"] = "File changed; new evidence"
        self.unchanged(changed)

    def test_default_off_preserves_payload_even_with_real_repetition(self):
        with patch.object(bridge, "LOOP_BREAKER", 0):
            self.unchanged(history())


if __name__ == "__main__":
    unittest.main(verbosity=2)
