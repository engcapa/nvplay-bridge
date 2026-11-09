from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.monitor_bridge import LogTail, snapshot


class MonitorTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
