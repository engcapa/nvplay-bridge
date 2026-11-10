"""Rejected native verifier controls must remain reviewable and fail closed."""

import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from eval_terminal_native import Evaluation, source_task_hashes  # noqa: E402


class NativeControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.evaluation = Evaluation.__new__(Evaluation)
        self.evaluation.base = self.base
        self.evaluation.verify_source = lambda _: None
        self.task = "session-window-debug"
        self.path = self.base / "controls" / self.task
        self.path.mkdir(parents=True)

    def test_cached_rejection_is_never_reported_as_valid_control(self):
        (self.path / "result.json").write_text('{"valid":false}')
        with self.assertRaisesRegex(RuntimeError, "cached controls are invalid"):
            self.evaluation.control(self.task)

    def test_retry_preserves_rejected_evidence(self):
        evidence = b'{"valid":false}\n'
        (self.path / "result.json").write_bytes(evidence)
        (self.path / "diagnostic.log").write_text("original failure")
        with patch("eval_terminal_native.clone_root", side_effect=RuntimeError("fixture stop")):
            with self.assertRaisesRegex(RuntimeError, "fixture stop"):
                self.evaluation.control(self.task, retry=True)
        archived = list((self.base / "controls-rejected").glob("*"))
        self.assertEqual(len(archived), 1)
        self.assertEqual((archived[0] / "result.json").read_bytes(), evidence)
        self.assertEqual((archived[0] / "diagnostic.log").read_text(), "original failure")

    def test_incomplete_control_requires_explicit_retry(self):
        (self.path / "partial.log").write_text("interrupted")
        with self.assertRaisesRegex(RuntimeError, "incomplete controls"):
            self.evaluation.control(self.task)
        self.assertEqual((self.path / "partial.log").read_text(), "interrupted")

    def test_source_hashes_are_bound_to_archive_contents(self):
        task = self.base / "dataset" / self.task
        task.mkdir(parents=True)
        (task / "fixture.txt").write_text("original source")
        archive = self.base / "fixture.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(task, arcname=f"release/tasks/{self.task}")
        self.assertIn("fixture.txt", source_task_hashes(archive, task.parent, [self.task])[self.task])
        (task / "fixture.txt").write_text("tampered source")
        with self.assertRaisesRegex(ValueError, "differs from pinned archive"):
            source_task_hashes(archive, task.parent, [self.task])


if __name__ == "__main__":
    unittest.main()
