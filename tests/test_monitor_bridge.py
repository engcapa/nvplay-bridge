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


if __name__ == "__main__":
    unittest.main()
