"""test_nvplay_bridge.py — nvplay_bridge 关键转换/占位符重试单元测试。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("UPSTREAM_KIND", "browser")
os.environ.setdefault("UPSTREAM_MODEL", "moonshotai/kimi-k3.1-pro")
os.environ.setdefault("BROWSER_DEBUG_LOG", os.path.join(tempfile.gettempdir(), "bridge-unit.jsonl"))
os.environ.setdefault("PLACEHOLDER_RETRY", "1")

import nvplay_bridge as bridge  # noqa: E402


class ConversionTests(unittest.TestCase):
    def test_thinking_passed_back_as_reasoning_content(self) -> None:
        body = {
            "model": "kimi-k3.1-pro",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "2+2=4"},
                        {"type": "text", "text": "4"},
                    ],
                }
            ],
        }
        messages = bridge._convert_messages(body)
        self.assertEqual(messages[0]["content"], "4")
        self.assertEqual(messages[0]["reasoning_content"], "2+2=4")

    def test_no_forced_effort_when_absent(self) -> None:
        body = {"model": "kimi-k3.1-pro", "max_tokens": 128, "messages": [{"role": "user", "content": "hi"}]}
        payload = bridge.to_openai_request(body)
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["max_tokens"], 128)

    def test_default_max_tokens_is_131072(self) -> None:
        body = {"model": "kimi-k3.1-pro", "messages": [{"role": "user", "content": "hi"}]}
        payload = bridge.to_openai_request(body)
        self.assertEqual(payload["max_tokens"], 131072)

    def test_max_tokens_capped_at_131072(self) -> None:
        body = {"model": "kimi-k3.1-pro", "max_tokens": 999999, "messages": [{"role": "user", "content": "hi"}]}
        payload = bridge.to_openai_request(body)
        self.assertEqual(payload["max_tokens"], 131072)

    def test_output_config_effort_max(self) -> None:
        body = {
            "model": "kimi-k3.1-pro",
            "max_tokens": 64,
            "output_config": {"effort": "max"},
            "messages": [{"role": "user", "content": "hi"}],
        }
        payload = bridge.to_openai_request(body)
        self.assertEqual(payload["reasoning_effort"], "max")

    def test_output_config_xhigh_maps_max(self) -> None:
        body = {
            "model": "kimi-k3.1-pro",
            "max_tokens": 64,
            "output_config": {"effort": "xhigh"},
            "messages": [{"role": "user", "content": "hi"}],
        }
        payload = bridge.to_openai_request(body)
        self.assertEqual(payload["reasoning_effort"], "max")

    def test_adaptive_with_effort(self) -> None:
        body = {
            "model": "kimi-k3.1-pro",
            "max_tokens": 64,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
            "messages": [{"role": "user", "content": "hi"}],
        }
        payload = bridge.to_openai_request(body)
        self.assertEqual(payload["reasoning_effort"], "high")

    def test_thinking_budget_low_and_max(self) -> None:
        low = bridge.to_openai_request(
            {
                "model": "kimi-k3.1-pro",
                "max_tokens": 64,
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        high = bridge.to_openai_request(
            {
                "model": "kimi-k3.1-pro",
                "max_tokens": 64,
                "thinking": {"type": "enabled", "budget_tokens": 31999},
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        self.assertEqual(low["reasoning_effort"], "low")
        self.assertEqual(high["reasoning_effort"], "max")

    def test_model_allowlist(self) -> None:
        self.assertEqual(bridge.validate_model("kimi-k3.1-pro"), "kimi-k3.1-pro")
        self.assertEqual(bridge.validate_model("moonshotai/kimi-k3.1-pro"), "moonshotai/kimi-k3.1-pro")
        with self.assertRaises(bridge.ModelNotSupported):
            bridge.validate_model("claude-sonnet-4-5-20250929")

    def test_placeholder_assistant_dropped(self) -> None:
        body = {
            "model": "kimi-k3.1-pro",
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "[Tool use interrupted]"}]}
            ],
        }
        self.assertEqual(bridge._convert_messages(body), [])

    def test_consecutive_duplicate_user_messages_collapsed(self) -> None:
        body = {
            "model": "kimi-k3.1-pro",
            "messages": [
                {"role": "user", "content": "继续"},
                {"role": "user", "content": "继续"},
                {"role": "user", "content": "继续"},
            ],
        }
        messages = bridge._convert_messages(body)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "继续")

    def test_empty_assistant_turn_dropped(self) -> None:
        body = {
            "model": "kimi-k3.1-pro",
            "messages": [
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "only thinking"}]}
            ],
        }
        self.assertEqual(bridge._convert_messages(body), [])

    def test_tool_loop_breaker(self) -> None:
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "Bash", "arguments": "{\"command\": \"test\"}"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "OK"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_2", "type": "function", "function": {"name": "Bash", "arguments": "{\"command\": \"test\"}"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_2", "content": "OK"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_3", "type": "function", "function": {"name": "Bash", "arguments": "{\"command\": \"test\"}"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_3", "content": "OK"},
            ]
        }
        with patch.object(bridge, "LOOP_BREAKER", 1):
            bridge._maybe_break_tool_loop(payload)
        self.assertEqual(payload["messages"][-1]["role"], "user")
        self.assertIn("[工具调用提醒]", payload["messages"][-1]["content"])
        self.assertNotIn("最终总结", payload["messages"][-1]["content"])
        count_before = len(payload["messages"])
        with patch.object(bridge, "LOOP_BREAKER", 1):
            bridge._maybe_break_tool_loop(payload)
        self.assertEqual(len(payload["messages"]), count_before)

    def test_tool_loop_breaker_ignores_description(self) -> None:
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": "Bash",
                                "arguments": json.dumps({"command": "python -m unittest -v", "description": f"desc {i}"}),
                            },
                        }
                    ],
                }
                for i in range(3)
            ]
        }
        completed = []
        for index, message in enumerate(payload["messages"]):
            completed.extend([message, {"role": "tool", "tool_call_id": f"call_{index}", "content": "OK"}])
        payload["messages"] = completed
        with patch.object(bridge, "LOOP_BREAKER", 1):
            bridge._maybe_break_tool_loop(payload)
        self.assertIn("[工具调用提醒]", payload["messages"][-1]["content"])

    def test_resent_history_does_not_count_as_new_executions(self) -> None:
        payload = {
            "messages": [
                {"role": "user", "content": "stateful-loop-test"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "Bash", "arguments": json.dumps({"command": "python -m unittest"})},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "OK"},
            ]
        }
        with patch.object(bridge, "LOOP_BREAKER", 1):
            for _ in range(5):
                bridge._maybe_break_tool_loop(payload)
        self.assertEqual(len(payload["messages"]), 3)
        self.assertEqual(payload["messages"][-1]["content"], "OK")

    def test_duplicate_tool_ids_get_unique_mapping(self) -> None:
        body = {
            "model": "kimi-k3.1-pro",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "Bash:0", "name": "Bash", "input": {"command": "test"}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "Bash:0", "content": "first result"}
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "Bash:0", "name": "Bash", "input": {"command": "test"}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "Bash:0", "content": "second result"}
                    ],
                },
            ],
        }
        messages = bridge._convert_messages(body)
        call_ids = [
            call["id"]
            for msg in messages
            if msg.get("role") == "assistant"
            for call in msg.get("tool_calls", [])
        ]
        result_ids = [msg["tool_call_id"] for msg in messages if msg.get("role") == "tool"]
        self.assertEqual(len(call_ids), 2)
        self.assertEqual(len(result_ids), 2)
        self.assertNotEqual(call_ids[0], call_ids[1])
        self.assertEqual(call_ids, result_ids)

    def test_placeholder_sse_detection(self) -> None:
        text = 'data: {"choices":[{"delta":{"content":"[Tool use interrupted]"},"finish_reason":"stop"}]}\n\n'
        self.assertEqual(bridge._sse_text_and_tools(text), ("[Tool use interrupted]", False))


class FakeBrowser:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def stream(self, body, timeout=3600, cancel=None, trace=None):
        yield self.generate(body, timeout)

    def generate(self, body: dict, timeout: float = 180.0) -> str:
        self.calls.append(body)
        if len(self.calls) == 1:
            return 'data: {"choices":[{"delta":{"content":"[Tool use interrupted]"},"finish_reason":"stop"}]}\n\n'
        return 'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'


class PlaceholderRetryTests(unittest.TestCase):
    def test_retry_replaces_placeholder(self) -> None:
        fake = FakeBrowser()
        old = bridge._browser
        bridge._browser = fake
        try:
            async def collect():
                events = []
                async for event in bridge.iter_events(
                    {"messages": [{"role": "user", "content": "continue"}]}, {}
                ):
                    events.append(event)
                return events

            events = asyncio.run(collect())
        finally:
            bridge._browser = old

        self.assertEqual(len(fake.calls), 2)
        corrective = fake.calls[1]["messages"][-1]["content"]
        self.assertIn("[系统纠正]", corrective)
        self.assertTrue(any(e.get("type") == "text" and e.get("text") == "OK" for e in events))
        self.assertFalse(any(e.get("type") == "text" and e.get("text") == "[Tool use interrupted]" for e in events))


class EmptyFakeBrowser:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def stream(self, body, timeout=3600, cancel=None, trace=None):
        yield self.generate(body, timeout)

    def generate(self, body: dict, timeout: float = 180.0) -> str:
        self.calls.append(body)
        if len(self.calls) == 1:
            return 'data: {"choices":[{"delta":{"reasoning_content":"only thinking"},"finish_reason":"stop"}]}\n\n'
        return 'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'


class EmptyRetryTests(unittest.TestCase):
    def test_thinking_only_is_retried(self) -> None:
        fake = EmptyFakeBrowser()
        old = bridge._browser
        bridge._browser = fake
        try:
            async def collect():
                events = []
                async for event in bridge.iter_events(
                    {"messages": [{"role": "user", "content": "continue"}]}, {}
                ):
                    events.append(event)
                return events

            events = asyncio.run(collect())
        finally:
            bridge._browser = old

        self.assertEqual(len(fake.calls), 2)
        self.assertIn("[系统纠正]", fake.calls[1]["messages"][-1]["content"])
        self.assertTrue(any(e.get("type") == "text" and e.get("text") == "OK" for e in events))


class SlowFakeBrowser:
    def stream(self, body, timeout=3600, cancel=None, trace=None):
        yield self.generate(body, timeout)

    def generate(self, body: dict, timeout: float = 180.0) -> str:
        time.sleep(0.3)
        return 'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'


class HeartbeatTests(unittest.TestCase):
    def test_ping_while_waiting_for_browser(self) -> None:
        fake = SlowFakeBrowser()
        old_browser = bridge._browser
        old_heartbeat = bridge.BROWSER_HEARTBEAT_SEC
        bridge._browser = fake
        bridge.BROWSER_HEARTBEAT_SEC = 0.05
        try:
            async def collect() -> str:
                chunks = []
                async for chunk in bridge.stream_anthropic(
                    {"messages": [{"role": "user", "content": "hi"}]},
                    {},
                    "kimi-k3.1-pro",
                ):
                    chunks.append(chunk)
                return "".join(chunks)

            raw = asyncio.run(collect())
        finally:
            bridge._browser = old_browser
            bridge.BROWSER_HEARTBEAT_SEC = old_heartbeat

        self.assertIn("event: ping", raw)
        self.assertIn("event: message_stop", raw)
        self.assertIn("OK", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
