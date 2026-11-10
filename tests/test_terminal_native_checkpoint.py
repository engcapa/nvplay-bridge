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

    def campaign(self, identity=True):
        owner = {"pid": 42, "boot_id": "boot", "start_ticks": "123"}
        state = {"status": "formal_running", "pid": 42, "task": "fixture", "repeat": 1,
                 "source_identity": {"private": "PRIVATE_CONTROLLER_FIELD"}}
        if identity:
            state["controller_identity"] = owner
        (self.base / "campaign-state.json").write_text(json.dumps(state))
        return owner

    def partial_report(self, count=2):
        return patch("checkpoint_terminal_native.summarize", return_value={
            "attempts": [{"artifact_score": index % 2} for index in range(count)], "pending": [], "totals": {}})

    def test_between_pairs_keeps_formal_status_and_finished_failure(self):
        owner = self.campaign()
        with self.partial_report(), patch("checkpoint_terminal_native.process_identity", return_value=owner):
            result = self.collect()
        self.assertEqual(result["status"], "formal_running")
        self.assertEqual(result["new_formal_attempts_completed"], 2)
        self.assertEqual(result["results"]["attempts"][0]["artifact_score"], 0)
        self.assertTrue(result["campaign"]["controller_alive"])
        self.assertNotIn("PRIVATE_CONTROLLER_FIELD", json.dumps(result))

    def test_first_pair_before_attempt_creation_is_formal_running(self):
        owner = self.campaign()
        with self.partial_report(count=0), patch("checkpoint_terminal_native.process_identity", return_value=owner):
            self.assertEqual(self.collect()["status"], "formal_running")

    def test_old_controller_uses_saved_attempt_boot_and_start_identity(self):
        owner = self.campaign(identity=False)
        attempt = self.base / "runs/formal/fixture--high--r1"
        attempt.mkdir(parents=True)
        (attempt / "owner.json").write_text(json.dumps(owner))
        with self.partial_report(), patch("checkpoint_terminal_native.process_identity", return_value=owner):
            self.assertEqual(self.collect()["status"], "formal_running")

    def test_dead_rebooted_or_recycled_controller_cannot_claim_active(self):
        owner = self.campaign()
        for actual in (None, {**owner, "boot_id": "new-boot"}, {**owner, "start_ticks": "999"}):
            with self.subTest(actual=actual), self.partial_report(), patch(
                    "checkpoint_terminal_native.process_identity", return_value=actual):
                result = self.collect()
            self.assertEqual(result["status"], "formal_incomplete")
            self.assertFalse(result["campaign"]["controller_alive"])

    def test_partial_results_without_campaign_are_not_controls_only(self):
        with self.partial_report():
            self.assertEqual(self.collect()["status"], "formal_incomplete")

    def test_all_finished_results_remain_complete_after_controller_exit(self):
        with self.partial_report(count=6):
            self.assertEqual(self.collect()["status"], "formal_complete")


if __name__ == "__main__":
    unittest.main()
