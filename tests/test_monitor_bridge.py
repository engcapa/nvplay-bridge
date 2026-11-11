from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.monitor_bridge import LogTail, snapshot


class MonitorTests(unittest.TestCase):
    def test_restart_clears_orphaned_requests_and_ignores_late_old_process_events(self):
        records = [
            {"event": "browser_start", "id": "legacy", "ts": 1},
            {"event": "bridge_started", "instance_id": "old", "ts": 2},
            {"event": "browser_start", "id": "orphaned", "instance_id": "old", "ts": 3},
            {"event": "browser_error", "id": "failed", "instance_id": "old", "ts": 4, "error": "old failure"},
            {"event": "bridge_started", "instance_id": "new", "ts": 5},
            {"event": "browser_start", "id": "live", "instance_id": "new", "ts": 6},
            {"event": "browser_start", "id": "late", "instance_id": "old", "ts": 7},
            {"event": "bridge_stopped", "instance_id": "old", "ts": 8},
        ]
        result = snapshot(records, now=10)
        self.assertEqual([r["id"] for r in result["active"]], ["live"])
        self.assertEqual(result["recent_errors"], [records[3]])
        records.append({"event": "bridge_stopped", "instance_id": "new", "ts": 11})
        self.assertEqual(snapshot(records, now=12)["active"], [])

    def test_client_conversion_and_tool_failures_remain_visible_after_browser_done(self):
        records = [
            {"event": "browser_start", "id": "a", "ts": 10},
            {"event": "browser_done", "id": "a", "ts": 11},
            {"event": "messages_failed", "request_id": "a", "phase": "conversion", "ts": 12},
            {"event": "tool_call", "tool_id": "x", "anomaly": "identical_edit", "ts": 13},
            {"event": "tool_result", "tool_id": "x", "is_error": True, "ts": 14},
            {"event": "tool_result", "tool_id": "y", "is_error": False, "ts": 15},
        ]
        result = snapshot(records, now=16)
        self.assertEqual(result["active"], [])
        self.assertEqual(result["recent_errors"], [])
        self.assertEqual(result["recent_client_errors"], [records[2]])
        self.assertEqual(result["recent_tool_errors"], records[3:5])
        later = snapshot(records, now=30, since=20)
        self.assertEqual(later["recent_client_errors"], [])
        self.assertEqual(later["recent_tool_errors"], [])

    def test_split_utf8_record_is_delivered_once_after_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.jsonl"
            tail = LogTail(path)
            self.assertEqual(tail.read(), [])
            record = {"event": "browser_error", "error": "读取失败"}
            encoded = json.dumps(record, ensure_ascii=False).encode() + b"\n"
            split = encoded.index("读".encode()) + 1
            path.write_bytes(encoded[:split])
            self.assertEqual(tail.read(), [])
            with path.open("ab") as out:
                out.write(encoded[split:])
            self.assertEqual(tail.read(), [record])
            self.assertEqual(tail.read(), [])

    def test_truncation_and_replacement_reset_pending_data_and_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.jsonl"
            path.write_bytes(b'{"event":"old"}\n{"partial":"unfinished')
            tail = LogTail(path)
            self.assertEqual(tail.read(), [{"event": "old"}])
            path.write_bytes(b'{"event":"new"}\n')
            self.assertEqual(tail.read(), [{"event": "new"}])
            replacement = path.with_suffix(".new")
            replacement.write_bytes(b'{"event":"replacement"}\n')
            replacement.replace(path)
            self.assertEqual(tail.read(), [{"event": "replacement"}])

    def test_retry_preserves_age_and_failure_removes_only_its_request(self):
        result = snapshot([
            {"event": "browser_start", "id": "a", "ts": 10, "attempt": 1},
            {"event": "browser_start", "id": "b", "ts": 11, "attempt": 1},
            {"event": "upstream_retry", "id": "a", "ts": 12},
            {"event": "browser_start", "id": "a", "ts": 17, "attempt": 2},
            {"event": "browser_progress", "id": "a", "ts": 18, "usage": {"completion_tokens": 42}},
            {"event": "browser_error", "id": "b", "ts": 19, "error": "network error", "committed": True},
        ], now=20)
        self.assertEqual(len(result["active"]), 1)
        request = result["active"][0]
        self.assertEqual((request["id"], request["attempt"], request["age_seconds"]), ("a", 2, 10))
        self.assertEqual(request["activity_age_seconds"], 2)
        self.assertEqual(request["usage"]["completion_tokens"], 42)
        self.assertTrue(result["recent_errors"][0]["committed"])

    def test_lookback_keeps_long_running_and_silent_requests(self):
        result = snapshot([
            {"event": "browser_start", "id": "running", "ts": 10},
            {"event": "browser_start", "id": "silent", "ts": 11},
            {"event": "browser_tab", "id": "silent", "ts": 12, "tab_id": "kpool=2"},
            {"event": "browser_start", "id": "finished", "ts": 13},
            {"event": "browser_done", "id": "finished", "ts": 14},
            {"event": "browser_start", "id": "old-error", "ts": 15},
            {"event": "browser_error", "id": "old-error", "ts": 16, "error": "old"},
            {"event": "browser_progress", "id": "running", "ts": 98,
             "usage": {"completion_tokens": 42}},
            {"event": "browser_start", "id": "new-error", "ts": 98.5},
            {"event": "browser_error", "id": "new-error", "ts": 99, "error": "new"},
        ], now=100, since=95)
        active = {request["id"]: request for request in result["active"]}
        self.assertEqual(set(active), {"running", "silent"})
        self.assertEqual(active["running"]["age_seconds"], 90)
        self.assertEqual(active["running"]["activity_age_seconds"], 2)
        self.assertEqual(active["running"]["usage"]["completion_tokens"], 42)
        self.assertEqual(active["silent"]["age_seconds"], 89)
        self.assertEqual(active["silent"]["activity_age_seconds"], 88)
        self.assertEqual(active["silent"]["tab_id"], "kpool=2")
        self.assertEqual(result["events"], {"browser_progress": 1, "browser_start": 1, "browser_error": 1})
        self.assertEqual([event["error"] for event in result["recent_errors"]], ["new"])

    def test_upstream_failure_and_effort_remain_visible_across_retry_and_lookback(self):
        records = [
            {"event": "browser_start", "id": "max", "ts": 10, "attempt": 1, "reasoning_effort": "max"},
            {"event": "browser_start", "id": "high", "ts": 11, "attempt": 1, "reasoning_effort": "high"},
            {"event": "browser_http_error", "id": "max", "ts": 15, "status": 500},
            {"event": "upstream_retry", "id": "max", "ts": 16, "reason": "nvidia_pool_acquire_timeout",
             "status": 500, "delay": 5},
            {"event": "browser_start", "id": "max", "ts": 21, "attempt": 2},
        ]
        result = snapshot(records, now=22)
        active = {value["id"]: value for value in result["active"]}
        self.assertEqual(active["max"]["reasoning_effort"], "max")
        self.assertEqual(active["max"]["retry_count"], 1)
        self.assertEqual(active["max"]["last_retry"]["status"], 500)
        self.assertEqual(active["max"]["age_seconds"], 12)
        self.assertEqual(active["high"]["reasoning_effort"], "high")
        self.assertEqual(active["high"]["retry_count"], 0)
        self.assertEqual(result["recent_errors"], [])
        self.assertEqual(result["recent_upstream_errors"][0]["status"], 500)
        self.assertEqual(result["recent_retries"][0]["reasoning_effort"], "max")
        # Windowed histories may expire while the active retry stays visible.
        later = snapshot(records, now=100, since=95)
        self.assertEqual(later["recent_upstream_errors"], [])
        self.assertEqual(later["recent_retries"], [])
        self.assertEqual(later["active"][0]["last_retry"]["status"], 500)


if __name__ == "__main__":
    unittest.main()
