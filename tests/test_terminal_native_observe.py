import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import observe_terminal_native as observer


class ObserveTests(unittest.TestCase):
    def observe(self, records, now=100):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "bridge-debug.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
            with patch.object(observer, "summarize", return_value={"attempts": [], "totals": {}, "pending": []}), \
                    patch.object(observer.time, "time", return_value=now), \
                    patch.object(observer.urllib.request, "build_opener", side_effect=OSError("offline test")):
                return observer.observe(base, None, "http://127.0.0.1:1/health", 10)

    def test_cancellation_remains_visible_after_request_leaves_active_set(self):
        result = self.observe([
            {"event": "browser_start", "id": "old", "ts": 1},
            {"event": "browser_cancelled", "id": "old", "ts": 2},
            {"event": "browser_start", "id": "current", "ts": 91},
            {"event": "browser_cancelled", "id": "current", "ts": 92},
        ])
        self.assertEqual(result["active"], [])
        self.assertEqual(result["recent_errors"], [])
        self.assertEqual(result["cancelled_requests_in_lookback"], 1)

    def test_client_failures_are_counted_and_redacted_after_browser_completion(self):
        result = self.observe([
            {"event": "messages_failed", "ts": 1, "error": "expired private body"},
            {"event": "browser_start", "id": "a", "ts": 91},
            {"event": "browser_done", "id": "a", "ts": 92},
            {"event": "messages_failed", "ts": 93, "phase": "conversion", "error": "private body one"},
            {"event": "responses_failed", "ts": 94, "phase": "stream", "error": "private body two"},
        ])
        self.assertEqual(result["active"], [])
        self.assertEqual(result["client_errors_in_lookback"], 2)
        self.assertEqual(len(result["recent_client_errors"]), 2)
        self.assertEqual(result["recent_client_errors"][0]["phase"], "conversion")
        self.assertIn("error_sha256", result["recent_client_errors"][0])
        self.assertNotIn("private body", json.dumps(result))

    def test_reasoning_only_output_limit_is_visible_without_transport_error(self):
        result = self.observe([
            {"event": "browser_done", "id": "expired", "ts": 1, "finish": "length"},
            {"event": "browser_start", "id": "reasoning", "ts": 91},
            {"event": "browser_done", "id": "reasoning", "ts": 92, "finish": "length",
             "counts": {"thinking_chars": 100, "text_chars": 0, "tool_calls": 0},
             "usage": {"completion_tokens": 131072}},
            {"event": "browser_start", "id": "tool", "ts": 93},
            {"event": "browser_done", "id": "tool", "ts": 94, "finish": "length",
             "counts": {"thinking_chars": 10, "text_chars": 0, "tool_calls": 1}},
            {"event": "browser_done", "id": "normal", "ts": 95, "finish": "stop",
             "counts": {"thinking_chars": 10, "text_chars": 1, "tool_calls": 0}},
        ])
        self.assertEqual(result["active"], [])
        self.assertEqual(result["recent_errors"], [])
        self.assertEqual(result["output_limit_requests_in_lookback"], 2)
        self.assertEqual(result["reasoning_only_output_limits_in_lookback"], 1)
        self.assertEqual([row["id"] for row in result["recent_output_limits"]], ["reasoning", "tool"])
        self.assertEqual(result["recent_output_limits"][0]["usage"]["completion_tokens"], 131072)
        self.assertEqual(result["recent_output_limits"][0]["finish"], "length")


if __name__ == "__main__":
    unittest.main()
