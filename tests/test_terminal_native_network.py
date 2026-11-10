"""A formal network adapter is opt-in and must match its frozen installed tree."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import eval_terminal_native as runner


class NativeNetworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.directory = self.base / "runtime"
        dependency = self.directory / "node_modules/undici/index.js"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("installed dependency fixture")
        (self.directory / "timeout.cjs").write_text("preload fixture")
        self.evaluation = runner.Evaluation.__new__(runner.Evaluation)
        self.evaluation.base = self.base
        self.evaluation.plan = {"formal_native_subset": {"stage": "formal-128k-fixed",
            "node_network_adapter": runner.network_tree_identity(self.directory)}}
        location = patch.object(runner, "NODE_NETWORK_DIRECTORY", self.directory)
        location.start()
        self.addCleanup(location.stop)

    def test_historical_stages_do_not_acquire_the_new_adapter(self):
        for stage in ("pilot", "formal"):
            self.assertIsNone(self.evaluation.network_adapter(stage))
        self.assertIsNotNone(self.evaluation.network_adapter("formal-128k-fixed"))

    def test_changed_or_missing_installed_dependency_refuses_before_attempt_creation(self):
        self.evaluation.identity = Mock(return_value={})
        self.evaluation.request_settings = Mock(return_value={})
        self.evaluation.verify_source = Mock(side_effect=AssertionError("must check runtime first"))
        dependency = self.directory / "node_modules/undici/index.js"
        dependency.write_text("different installed dependency")
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    dependency.unlink()
                with self.assertRaisesRegex(RuntimeError, "frozen installed tree"):
                    self.evaluation.attempt("fixture", "high", "formal-128k-fixed", 1, 60, 20000)
                self.assertFalse((self.base / "runs").exists())
        self.evaluation.verify_source.assert_not_called()


if __name__ == "__main__":
    unittest.main()
