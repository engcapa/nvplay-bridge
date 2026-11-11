"""Messages delivery fidelity, honest truncation, and correlated diagnostics."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("UPSTREAM_KIND", "browser")

import httpx
import nvplay_bridge as bridge
from stream_support import SSEDecoder


class MessagesDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.log = Path(self.directory.name) / "diagnostics.jsonl"
        self.logging = patch.object(bridge, "BROWSER_DEBUG_LOG", str(self.log))
        self.logging.start()
        self.addCleanup(self.logging.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app), base_url="http://local")
        self.addAsyncCleanup(self.client.aclose)

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    async def test_large_edit_arguments_survive_fragments_and_truncation_is_honest(self):
        arguments = {"file_path": "/tmp/private-project/SessionResults.tsx",
                     "old_string": ('  const label = "中文🙂\\n";\r\n' * 140),
                     "new_string": ('  const label = "更新🙂\\n";\r\n' * 180)}
        serialized = json.dumps(arguments, ensure_ascii=False)

        async def source(payload, headers, *, buffered=True, defer_output=False):
            yield {"type": "thinking", "text": "reviewing"}
            for start in range(0, len(serialized), 17):
                yield {"type": "tool", "index": 0, "id": "upstream:0" if start == 0 else None,
                       "name": "Edit" if start == 0 else None, "args": serialized[start:start + 17]}
            yield {"type": "finish", "reason": "length"}

        body = {"model": "kimi-k3.1-pro", "messages": [{"role": "user", "content": "edit"}]}
        with patch.object(bridge, "_iter_upstream_events", source):
            for stream in (False, True):
                with self.subTest(stream=stream):
                    response = await self.client.post("/v1/messages", json={**body, "stream": stream})
                    self.assertEqual(response.status_code, 200, response.text)
                    if stream:
                        events = SSEDecoder().feed(response.text, final=True)
                        raw = "".join(e["delta"]["partial_json"] for e in events
                                      if e.get("delta", {}).get("type") == "input_json_delta")
                        actual = json.loads(raw)
                        self.assertEqual(next(e for e in events if e["type"] == "message_delta")
                                         ["delta"]["stop_reason"], "max_tokens")
                    else:
                        value = response.json()
                        actual = next(b["input"] for b in value["content"] if b["type"] == "tool_use")
                        self.assertEqual(value["stop_reason"], "max_tokens")
                    self.assertEqual(actual, arguments)
                    request_id = response.headers["request-id"]
                    self.assertEqual(request_id, response.headers["x-request-id"])
                    records = [r for r in self.records() if r.get("request_id") == request_id]
                    call = next(r for r in records if r["event"] == "tool_call")
                    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    self.assertEqual(call["arguments_sha256"], hashlib.sha256(canonical.encode()).hexdigest())
                    self.assertEqual(records[-1]["event"], "http_complete")
                    self.assertTrue(records[-1]["body_complete"])
                    self.assertFalse(records[-1]["client_disconnected"])
        log_text = self.log.read_text()
        self.assertNotIn("SessionResults.tsx", log_text)
        self.assertNotIn("const label", log_text)

    async def test_only_disconnects_before_response_completion_are_client_failures(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                async def app(scope, receive, send):
                    await send({"type": "http.response.start", "status": 200, "headers": []})
                    await send({"type": "http.response.body", "body": b"data", "more_body": not completed})
                    await receive()

                async def receive():
                    return {"type": "http.disconnect"}

                async def send(message):
                    pass

                await bridge.RequestDiagnosticsMiddleware(app)(
                    {"type": "http", "method": "POST", "path": "/v1/messages"}, receive, send)
                record = self.records()[-1]
                self.assertEqual(record["body_complete"], completed)
                self.assertEqual(record["client_disconnected"], not completed)

    async def test_lifespan_closes_browser_and_marks_process_boundaries(self):
        class Browser:
            closed = False

            def close(self):
                self.closed = True

        browser = Browser()
        with patch.object(bridge, "_browser", browser):
            async with bridge.app.router.lifespan_context(bridge.app):
                self.assertFalse(browser.closed)
                health = await bridge.health()
            self.assertTrue(browser.closed)
            self.assertIsNone(bridge._browser)
        records = self.records()
        self.assertEqual([r["event"] for r in records], ["bridge_started", "bridge_stopped"])
        self.assertEqual(records[0]["instance_id"], health["instance_id"])
        self.assertEqual(records[0]["instance_id"], records[1]["instance_id"])
        self.assertEqual(records[0]["pid"], os.getpid())

    async def test_conversion_failure_is_logged_before_deferred_output_is_delivered(self):
        class MalformedBrowser:
            def stream(self, body, timeout, cancel, trace=None):
                yield 'data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "upstream:0", "function": {"name": "Edit", "arguments": '{"old_string":'}
                }]}, "finish_reason": "tool_calls"}]}) + '\n\n'

        with patch.object(bridge, "_browser", MalformedBrowser()):
            response = await self.client.post("/v1/messages", json={"model": "kimi-k3.1-pro", "stream": True,
                "messages": [{"role": "user", "content": "edit"}]})
        self.assertIn("event: error", response.text)
        self.assertNotIn("event: message_stop", response.text)
        records = self.records()
        invalid = next(r for r in records if r["event"] == "browser_validation_failed")
        failed = next(r for r in records if r["event"] == "messages_failed")
        self.assertEqual(invalid["id"], response.headers["request-id"])
        self.assertEqual(failed["request_id"], invalid["id"])
        self.assertEqual(failed["phase"], "conversion")
        self.assertNotIn("content_block_start", response.text)
        self.assertFalse(any(r["event"] == "browser_done" for r in records))
        self.assertFalse(any(r["event"] == "messages_done" for r in records))

    async def test_json_missing_finish_or_tool_metadata_fails_explicitly(self):
        cases = [
            [{"type": "text", "text": "partial"}],
            [{"type": "tool", "index": 0, "id": "a", "name": None, "args": '{}'},
             {"type": "finish", "reason": "tool_calls"}],
        ]
        for events in cases:
            async def source(payload, headers, *, buffered=True, defer_output=False):
                for event in events:
                    yield event
            with patch.object(bridge, "_iter_upstream_events", source):
                response = await self.client.post("/v1/messages", json={"model": "kimi-k3.1-pro",
                    "messages": [{"role": "user", "content": "edit"}]})
            self.assertEqual(response.status_code, 502)
            self.assertIn("upstream", response.json()["error"]["message"])

    async def test_concurrent_requests_keep_separate_correlations(self):
        async def source(payload, headers, *, buffered=True, defer_output=False):
            name = payload["messages"][-1]["content"]
            yield {"type": "text", "text": name}
            await asyncio.sleep(0.01)
            yield {"type": "finish", "reason": "stop"}

        with patch.object(bridge, "_iter_upstream_events", source):
            responses = await asyncio.gather(*(self.client.post("/v1/messages", json={
                "model": "kimi-k3.1-pro", "stream": True,
                "messages": [{"role": "user", "content": name}]
            }) for name in ("first", "second")))
        ids = [r.headers["request-id"] for r in responses]
        self.assertEqual(len(set(ids)), 2)
        records = self.records()
        for request_id in ids:
            matching = [r for r in records if r.get("request_id") == request_id]
            self.assertEqual(sum(r["event"] == "messages_done" for r in matching), 1)
            self.assertEqual(sum(r["event"] == "http_complete" for r in matching), 1)
        self.assertIsNone(bridge._request_id.get())

    async def test_recent_tool_failures_are_classified_without_logging_file_contents(self):
        body = {"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "old", "name": "Read"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old", "is_error": True,
                                           "content": "OLD_ERROR"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "new", "name": "Edit"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "new", "is_error": True,
                "content": "<tool_use_error>String to replace not found in file.\nString: PRIVATE_SOURCE</tool_use_error>"}]},
        ]}
        original = deepcopy(body)
        bridge._log_recent_tool_results(body)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tool_id"], "new")
        self.assertEqual(records[0]["name"], "Edit")
        self.assertEqual(records[0]["error_kind"], "edit_string_not_found")
        self.assertNotIn("PRIVATE_SOURCE", self.log.read_text())
        self.assertEqual(body, original)


if __name__ == "__main__":
    unittest.main()
