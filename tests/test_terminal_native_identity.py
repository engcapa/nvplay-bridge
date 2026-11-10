"""A running evaluator must not relabel loaded code with later disk contents."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import eval_terminal_native as runner


class NativeIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        plan = {"bridge_source_head": "running-bridge", "cc_version": "fixture",
                "source_commit": "pinned-dataset"}
        (self.base / "evaluation-plan.json").write_text(json.dumps(plan))
        self.sources = [self.base / name for name in ("runner.py", "support.py")]
        for source in self.sources:
            source.write_text("initial source\n")
        source_patch = patch.object(runner, "TOOLING_SOURCES", self.sources)
        source_patch.start()
        self.addCleanup(source_patch.stop)
        with patch.object(runner.subprocess, "check_output", side_effect=[
                "tooling-start-head\n", " M docs/progress.md\n?? tools/new.py\n"]):
            self.evaluation = runner.Evaluation(self.base)

    def test_running_bridge_and_tooling_repository_are_distinct(self):
        with patch.object(runner.subprocess, "check_output", side_effect=AssertionError("must stay frozen")):
            identity = self.evaluation.identity()
        self.assertEqual(identity["bridge_head"], "running-bridge")
        self.assertEqual(identity["tooling_repository_head"], "tooling-start-head")
        self.assertEqual(identity["tooling_worktree_status"], [" M docs/progress.md", "?? tools/new.py"])
        identity["runner_sha256"].clear()
        self.assertEqual(len(self.evaluation.identity()["runner_sha256"]), 2)

    def test_changed_runner_or_support_refuses_another_attempt(self):
        self.evaluation.verify_source = Mock(side_effect=AssertionError("source identity must be checked first"))
        for source in self.sources:
            with self.subTest(source=source.name):
                source.write_text("later disk contents; running code remains loaded\n")
                with self.assertRaisesRegex(RuntimeError, "runner source changed"):
                    self.evaluation.attempt("fixture", "high", "formal", 1, 60, 20000)
                self.assertFalse((self.base / "runs").exists())
                source.write_text("initial source\n")
        self.evaluation.verify_source.assert_not_called()

    def test_formal_stage_records_its_actual_restarted_bridge(self):
        runtime = {"source_head": "formal-bridge", "request_timeout_seconds": 28800}
        self.evaluation.plan["bridge_runtime_by_stage"] = {"formal": runtime}
        self.assertEqual(self.evaluation.identity("pilot")["bridge_head"], "running-bridge")
        formal = self.evaluation.identity("formal")
        self.assertEqual(formal["bridge_head"], "formal-bridge")
        self.assertEqual(formal["bridge_runtime"]["request_timeout_seconds"], 28800)
        formal["bridge_runtime"].clear()
        self.assertEqual(runtime["source_head"], "formal-bridge")


if __name__ == "__main__":
    unittest.main()
