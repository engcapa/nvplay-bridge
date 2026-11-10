"""Evaluation evidence must not leak artifacts or turn infrastructure errors into passes."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from native_eval_support import (  # noqa: E402
    bridge_activity, cc_result, clone_root, copy_artifact, reward_result, run_logged,
)


class NativeEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_clone_keeps_independent_files(self):
        source, target = self.root / "source", self.root / "target"
        source.mkdir()
        (source / "code.py").write_text("original")
        clone_root(source, target)
        (target / "code.py").write_text("changed")
        self.assertEqual((source / "code.py").read_text(), "original")
        self.assertNotEqual((source / "code.py").stat().st_ino, (target / "code.py").stat().st_ino)
        with self.assertRaises(FileExistsError):
            clone_root(source, target)

    def test_artifact_cannot_follow_host_symlink(self):
        agent, verifier = self.root / "agent", self.root / "verifier"
        (agent / "app").mkdir(parents=True)
        private = self.root / "private"
        private.mkdir()
        (private / "secret").write_text("must not copy")
        (agent / "app/code").symlink_to(private, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            copy_artifact(agent, verifier, "/app/code/secret")
        self.assertFalse(verifier.exists())

    def test_artifact_rejects_nested_link_before_copy(self):
        agent, verifier = self.root / "agent", self.root / "verifier"
        (agent / "app").mkdir(parents=True)
        (agent / "app/code.py").write_text("valid")
        (agent / "app/link").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "unsupported artifact"):
            copy_artifact(agent, verifier, "/app/")
        self.assertFalse(verifier.exists())

    def test_artifact_hashes_bind_copied_bytes(self):
        agent, verifier = self.root / "agent", self.root / "verifier"
        (agent / "app").mkdir(parents=True)
        (agent / "app/code.py").write_text("print(42)\n")
        hashes = copy_artifact(agent, verifier, "/app/code.py")
        self.assertEqual((verifier / "app/code.py").read_bytes(), (agent / "app/code.py").read_bytes())
        self.assertEqual(len(hashes["app/code.py"]), 64)
        with self.assertRaises(ValueError):
            copy_artifact(agent, verifier, "/app/../etc/passwd")

    def test_reward_requires_consistent_terminal_evidence(self):
        p = self.root / "logs/verifier/reward.txt"
        p.parent.mkdir(parents=True)
        process = {"exit_code": 0, "timed_out": False}
        self.assertEqual(reward_result(self.root, process)["infrastructure_error"], "missing_reward")
        p.write_text("1\n")
        self.assertEqual(reward_result(self.root, process)["score"], 1)
        self.assertEqual(reward_result(self.root, {**process, "exit_code": 1})["infrastructure_error"],
                         "reward_exit_disagreement")
        self.assertEqual(reward_result(self.root, {**process, "timed_out": True})["infrastructure_error"],
                         "verifier_timeout")
        p.write_text("0\n")
        self.assertEqual(reward_result(self.root, {**process, "exit_code": 1})["score"], 0)
        p.write_text("nan")
        self.assertEqual(reward_result(self.root, process)["infrastructure_error"], "invalid_reward")

    def test_bridge_attribution_deduplicates_retries_and_efforts(self):
        records = [
            {"event": "browser_start", "ts": 10, "id": "old", "reasoning_effort": "high"},
            {"event": "browser_start", "ts": 20, "id": "high", "reasoning_effort": "high"},
            {"event": "browser_start", "ts": 21, "id": "max", "reasoning_effort": "max"},
            {"event": "upstream_retry", "ts": 22, "id": "high"},
            {"event": "browser_start", "ts": 23, "id": "high", "reasoning_effort": "high"},
            {"event": "browser_done", "ts": 24, "id": "high", "usage": {"total_tokens": 99}},
            {"event": "browser_error", "ts": 25, "id": "max", "error": "network"},
            {"event": "browser_start", "ts": 30, "id": "future", "reasoning_effort": "high"},
        ]
        path = self.root / "bridge.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in records) + "\npartial")
        high = bridge_activity(path, 19, 26, "high")
        self.assertEqual(high["logical_requests"], 1)
        self.assertEqual(high["upstream_attempts"], 2)
        self.assertEqual(len(high["retries"]), 1)
        self.assertEqual(high["unfinished"], [])
        self.assertEqual(high["usage_by_request"], {"high": {"total_tokens": 99}})
        self.assertEqual(len(bridge_activity(path, 19, 26, "max")["errors"]), 1)

    def test_client_result_ignores_noise_and_keeps_final_result(self):
        p = self.root / "cc.jsonl"
        p.write_text('warning\n' + json.dumps({"type": "result", "subtype": "success",
                                              "is_error": False, "num_turns": 2}) + "\n")
        self.assertEqual(cc_result(p)["num_turns"], 2)

    def test_timed_out_process_is_not_recorded_as_normal_exit(self):
        log = self.root / "process.log"
        result = run_logged([sys.executable, "-c", "import time; time.sleep(5)"], log, 0.1)
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["exit_code"], 0)

    def test_explicit_unlimited_wait_preserves_the_childs_natural_completion(self):
        log = self.root / "unlimited-process.log"
        result = run_logged([sys.executable, "-c", "import time; time.sleep(0.2); print('NATURAL_COMPLETION')"], log, None)
        self.assertIsNone(result["timeout_seconds"])
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])
        self.assertIn("NATURAL_COMPLETION", log.read_text())


if __name__ == "__main__":
    unittest.main()
