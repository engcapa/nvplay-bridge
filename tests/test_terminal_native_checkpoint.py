"""Public checkpoints must omit private CC content and upstream error bodies."""

import json
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from checkpoint_terminal_native import collect
import checkpoint_terminal_native as checkpoint


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

    def pause(self, count=2):
        (self.base / "pause-state.json").write_text(json.dumps({
            "status": "paused_for_reboot", "completed": count, "interrupted_attempts": 0,
            "reason": "user requested reboot", "extra": "PRIVATE_PAUSE_FIELD"}))

    def test_explicit_completed_boundary_pause_is_distinct_from_unexpected_stop(self):
        self.pause()
        with self.partial_report():
            result = self.collect()
        self.assertEqual(result["status"], "formal_paused")
        self.assertEqual(result["administrative_pause"]["interrupted_attempts"], 0)
        self.assertNotIn("PRIVATE_PAUSE_FIELD", json.dumps(result))

    def test_stale_pause_cannot_hide_new_results_or_restarted_controller(self):
        self.pause(count=1)
        with self.partial_report():
            self.assertEqual(self.collect()["status"], "formal_incomplete")
        self.pause()
        owner = self.campaign()
        with self.partial_report(), patch("checkpoint_terminal_native.process_identity", return_value=owner):
            self.assertEqual(self.collect()["status"], "formal_running")

    def test_pending_or_fully_completed_campaign_takes_precedence_over_pause(self):
        self.pause()
        with patch("checkpoint_terminal_native.summarize", return_value={
                "attempts": [{}, {}], "pending": [{"status": "agent_running"}], "totals": {}}):
            self.assertEqual(self.collect()["status"], "formal_running")
        self.pause(count=6)
        with self.partial_report(count=6):
            self.assertEqual(self.collect()["status"], "formal_complete")

    def test_continuation_counts_only_missing_positions_without_losing_prior_progress(self):
        plan = json.loads((self.base / "evaluation-plan.json").read_text())
        plan.update(formal_native_subset={"schedule": [{}, {}]},
                    continuation={"preserved_completed_attempts": 4})
        (self.base / "evaluation-plan.json").write_text(json.dumps(plan))
        with self.partial_report(count=2):
            result = self.collect()
        self.assertEqual(result["new_formal_attempts_planned"], 2)
        self.assertEqual(result["combined_plan_positions_completed"], 6)
        self.assertEqual(result["status"], "formal_complete")


class CheckpointPushTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo, self.remote, self.base = (self.root / name for name in ("repo", "remote.git", "logs"))
        self.repo.mkdir()
        self.base.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Checkpoint fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo / "unrelated.txt").write_text("operator's staged work\n")
        self.git("add", "unrelated.txt")
        self.output = self.repo / "docs/checkpoint.json"
        self.data = {"new_formal_attempts_completed": 1, "new_formal_attempts_planned": 25,
                     "status": "formal_running"}

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True).strip()

    def save(self):
        with patch.object(checkpoint, "REPO", self.repo), patch.object(checkpoint, "collect", return_value=self.data):
            checkpoint.save(self.base, self.output, "unused", commit=True, push=True)

    def test_checkpoint_is_pushed_without_committing_unrelated_staged_work(self):
        self.save()
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "unrelated.txt")
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "HEAD"), "docs/checkpoint.json")
        remote = subprocess.check_output(["git", "--git-dir", str(self.remote), "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual(self.git("rev-parse", "HEAD"), remote)
        self.assertTrue(json.loads((self.base / "checkpoint-git-sync.json").read_text())["pushed"])

    def test_failed_push_keeps_checkpoint_and_retries_even_without_new_changes(self):
        self.git("remote", "set-url", "origin", str(self.root / "missing.git"))
        self.save()
        first = self.git("rev-parse", "HEAD")
        self.assertFalse(json.loads((self.base / "checkpoint-git-sync.json").read_text())["pushed"])
        self.git("remote", "set-url", "origin", str(self.remote))
        self.save()
        self.assertEqual(self.git("rev-parse", "HEAD"), first)
        self.assertTrue(json.loads((self.base / "checkpoint-git-sync.json").read_text())["pushed"])


if __name__ == "__main__":
    unittest.main()
