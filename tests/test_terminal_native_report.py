"""Native evaluation reports keep budgets and infrastructure failures distinct."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from report_terminal_native import summarize  # noqa: E402


class NativeReportTests(unittest.TestCase):
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
