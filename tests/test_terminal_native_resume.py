"""Restart recovery preserves failures and never touches a live attempt."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from eval_terminal_native import Evaluation, process_identity


class NativeResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.evaluation = Evaluation.__new__(Evaluation)
        self.evaluation.base = self.base
        self.evaluation.verify_source = lambda _: None
        self.output = self.base / "runs/formal/fixture--high--r1"
        self.output.mkdir(parents=True)

    def resume(self, recover=False):
        return self.evaluation.resume_attempt("fixture", "high", "formal", 1, 28800, recover)

    def test_completed_failure_is_preserved_byte_for_byte(self):
        result = {"task": "fixture", "effort": "high", "stage": "formal", "repeat": 1,
                  "budget_seconds": 28800, "agent": {"exit_code": 1}, "grade": {"score": 0}}
        data = json.dumps(result).encode()
        (self.output / "result.json").write_bytes(data)
        self.assertTrue(self.resume())
        self.assertEqual((self.output / "result.json").read_bytes(), data)
        result["budget_seconds"] = 1800
        (self.output / "result.json").write_text(json.dumps(result))
        with self.assertRaisesRegex(RuntimeError, "does not match resumed protocol"):
            self.resume()

    def test_incomplete_attempt_requires_explicit_recovery(self):
        with self.assertRaisesRegex(RuntimeError, "requires --recover-interrupted"):
            self.resume()
        self.assertTrue(self.output.exists())

    def test_live_controller_refuses_archival(self):
        owner = {"pid": 42, "boot_id": "boot", "start_ticks": "123"}
        (self.output / "owner.json").write_text(json.dumps(owner))
        with patch("eval_terminal_native.process_identity", return_value=owner):
            with self.assertRaisesRegex(RuntimeError, "live controller"):
                self.resume(recover=True)
        self.assertTrue(self.output.exists())

    def test_dead_attempt_keeps_raw_evidence_and_is_explicitly_ungraded(self):
        (self.output / "owner.json").write_text('{"pid":42,"boot_id":"old","start_ticks":"1"}')
        data = b"original incomplete evidence\n"
        (self.output / "cc.jsonl").write_bytes(data)
        with patch("eval_terminal_native.process_identity", return_value=None):
            self.assertFalse(self.resume(recover=True))
        archived = next((self.base / "runs-interrupted/formal").iterdir())
        self.assertFalse(self.output.exists())
        self.assertEqual((archived / "cc.jsonl").read_bytes(), data)
        interruption = json.loads((archived / "interruption.json").read_text())
        self.assertIsNone(interruption["score"])
        self.assertTrue(interruption["excluded_from_model_grades"])

    def test_process_identity_reads_real_start_time(self):
        import os
        identity = process_identity(os.getpid())
        self.assertEqual(identity["pid"], os.getpid())
        self.assertTrue(int(identity["start_ticks"]) > 0)
        self.assertEqual(identity, process_identity(os.getpid()))


if __name__ == "__main__":
    unittest.main()
