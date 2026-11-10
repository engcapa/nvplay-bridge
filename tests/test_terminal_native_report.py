"""Native evaluation reports keep budgets and infrastructure failures distinct."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from report_terminal_native import summarize, transport_evidence  # noqa: E402


class NativeReportTests(unittest.TestCase):
    def test_http_failure_time_uses_each_attempt_and_includes_exhausted_errors(self):
        events = [
            {"id": "a", "event": "browser_start", "attempt": 1, "ts": 0},
            {"id": "a", "event": "browser_http_error", "attempt": 1, "ts": 50, "sec": 50, "status": 500},
            {"id": "a", "event": "browser_start", "attempt": 2, "ts": 55},
            {"id": "a", "event": "browser_http_error", "attempt": 2, "ts": 100, "sec": 100, "status": 500},
            {"id": "a", "event": "browser_start", "attempt": 3, "ts": 110},
            {"id": "a", "event": "browser_done", "attempt": 3, "ts": 150, "sec": 150},
            {"id": "b", "event": "browser_start", "attempt": 1, "ts": 160},
            {"id": "b", "event": "browser_http_error", "attempt": 1, "ts": 205, "sec": 45, "status": 503},
            {"id": "b", "event": "browser_error", "attempt": 1, "ts": 206, "sec": 46, "status": 503},
            {"id": "other", "event": "browser_http_error", "attempt": 1, "ts": 210, "status": 500},
        ]
        records = {}
        for event in events:
            records.setdefault(event["id"], []).append(event)
        result = transport_evidence(records, ["a", "b"])
        self.assertEqual(result["http_status_counts"], {"500": 2, "503": 1})
        self.assertEqual(result["timed_http_failures"], 3)
        self.assertEqual(result["http_failed_attempt_seconds"], 140)
        self.assertEqual(result["completed_requests"], 1)
        self.assertEqual(result["longest_completed_request_seconds"], 150)

    def test_missing_transport_log_or_request_ids_are_unknown(self):
        self.assertIsNone(transport_evidence(None, ["a"]))
        self.assertIsNone(transport_evidence({}, None))
        result = transport_evidence({"a": [{"event": "browser_http_error", "attempt": 2,
                                            "ts": 10, "status": 500}]}, ["a"])
        self.assertEqual(result["http_status_counts"], {"500": 1})
        self.assertEqual(result["timed_http_failures"], 0)
        self.assertEqual(result["http_failed_attempt_seconds"], 0)

    def test_cancelled_usage_keeps_last_observation_and_marks_it_partial(self):
        records = {
            "cancelled": [
                {"event": "browser_progress", "usage": {
                    "prompt_tokens": 10, "completion_tokens": 40, "total_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 4},
                    "completion_tokens_details": {"reasoning_tokens": 35}}},
                {"event": "browser_cancelled"}],
            "done": [
                {"event": "browser_progress", "usage": {"prompt_tokens": 20, "completion_tokens": 2}},
                {"event": "browser_done", "sec": 50, "usage": {
                    "prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25,
                    "prompt_tokens_details": {"cached_tokens": 10},
                    "completion_tokens_details": {"reasoning_tokens": 3}}}],
            "unreported": [{"event": "browser_error", "usage": {}}],
        }
        result = transport_evidence(records, list(records))
        self.assertEqual(result["observed_usage"], {
            "prompt_tokens": 30, "completion_tokens": 45, "total_tokens": 75,
            "cached_prompt_tokens": 14, "reasoning_tokens": 38})
        self.assertEqual(result["requests_with_partial_usage"], 1)
        self.assertEqual(result["requests_without_observed_usage"], 1)
        self.assertEqual(result["completed_requests"], 1)

    def test_denominators_keep_failed_infrastructure_and_separate_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = {"task": "fixture", "stage": "pilot", "effort": "high", "repeat": 1,
                      "budget_seconds": 1800, "grade": {"score": 1, "infrastructure_error": None},
                      "end_to_end_success": True,
                      "agent": {"duration_seconds": 2, "timed_out": False},
                      "cc": {"subtype": "success", "num_turns": 2}, "source_head": "fixture",
                      "bridge": {"logical_requests": 2, "upstream_attempts": 2, "errors": [],
                                 "retries": [], "cancelled": [], "unfinished": [],
                                 "usage_by_request": {"a": {"prompt_tokens": 20, "completion_tokens": 3}}}}
            variants = [sample,
                        {**sample, "infrastructure_error": "verifier_timeout", "end_to_end_success": False,
                         "grade": {"score": None, "infrastructure_error": "verifier_timeout"}},
                        {**sample, "budget_seconds": 28800,
                         "bridge": {**sample["bridge"], "retries": [{"id": "a", "status": 500},
                                                                    {"id": "b", "status": 500}],
                                    "cancelled": [{"id": "b"}]}}]
            for index, data in enumerate(variants):
                directory = root / "runs/pilot" / str(index)
                directory.mkdir(parents=True)
                (directory / "result.json").write_text(json.dumps(data))
            directory = root / "runs/formal/pending"
            directory.mkdir(parents=True)
            (directory / "status.json").write_text('{"status":"agent_running"}')
            report = summarize(root)
            small = report["totals"]["pilot/1800/high"]
            self.assertEqual(small["attempts"], 2)
            self.assertEqual(small["valid_grades"], 1)
            self.assertEqual(small["artifact_pass_at_1"], 1)
            self.assertEqual(small["end_to_end_pass_rate"], 0.5)
            self.assertEqual(small["usage"]["prompt_tokens"], 40)
            self.assertEqual(report["totals"]["pilot/28800/high"]["attempts"], 1)
            retry = report["totals"]["pilot/28800/high"]
            self.assertEqual(retry["bridge_errors"], 0)
            self.assertEqual(retry["http_failures_retried"], 2)
            self.assertEqual(retry["requests_recovered_after_retry"], 1)
            self.assertEqual(retry["cancelled_requests"], 1)
            self.assertEqual(len(report["pending"]), 1)
            self.assertNotIn("result", report["attempts"][0])


if __name__ == "__main__":
    unittest.main()
