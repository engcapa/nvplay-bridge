"""Public checkpoints must omit private CC content and upstream error bodies."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from checkpoint_terminal_native import collect


class NativeCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        (self.base / "evaluation-plan.json").write_text(json.dumps({
            "pilot_tasks": ["fixture"], "formal_repeats": 3, "task_identities": {"fixture": {"file": "digest"}}}))

    def collect(self):
        with patch("checkpoint_terminal_native.LOCAL_HTTP.open", side_effect=OSError("offline")):
            return collect(self.base, "http://127.0.0.1:1/health")

    def test_pending_tool_counts_do_not_publish_messages_or_error_bodies(self):
        attempt = self.base / "runs/formal/fixture--high--r1"
        attempt.mkdir(parents=True)
        (attempt / "status.json").write_text('{"status":"agent_running","task":"fixture","effort":"high"}')
        (attempt / "cc.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "PRIVATE_CC_ANSWER"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "PRIVATE_COMMAND"}}]}}) + "\n")
        (self.base / "bridge-debug.jsonl").write_text(json.dumps({
            "event": "browser_error", "id": "fixture", "ts": 10**10,
            "error": "PRIVATE_UPSTREAM_BODY", "committed": True}) + "\n")
        result = self.collect()
        encoded = json.dumps(result)
        for private in ("PRIVATE_CC_ANSWER", "PRIVATE_COMMAND", "PRIVATE_UPSTREAM_BODY"):
            self.assertNotIn(private, encoded)
        self.assertEqual(result["results"]["pending"][0]["tool_calls"], 1)
        self.assertIn("error_sha256", result["bridge_activity"]["recent_errors"][0])
        self.assertEqual(result["status"], "formal_running")

    def test_control_logs_and_extra_fields_are_not_archived_as_content(self):
        control = self.base / "controls/fixture"
        control.mkdir(parents=True)
        (control / "result.json").write_text(json.dumps({"valid": True,
            "baseline": {"score": 0, "infrastructure_error": None, "private": "PRIVATE_TESTS"},
            "oracle": {"score": 1, "infrastructure_error": None, "private": "PRIVATE_REFERENCE"}}))
        result = self.collect()
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertEqual(result["controls"]["fixture"]["oracle"]["score"], 1)
        self.assertEqual(result["status"], "controls_validated")
        self.assertEqual(result["new_formal_attempts_planned"], 6)

    def test_invalid_control_cannot_be_labelled_validated(self):
        control = self.base / "controls/fixture"
        control.mkdir(parents=True)
        (control / "result.json").write_text('{"valid":false,"baseline":{"score":0},"oracle":{"score":0}}')
        self.assertEqual(self.collect()["status"], "validating_controls")


if __name__ == "__main__":
    unittest.main()
