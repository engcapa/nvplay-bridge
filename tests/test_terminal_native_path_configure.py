"""A client upgrade preserves graded positions and requires a fresh live smoke."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import configure_terminal_native_path_cc as upgrade


class PathConfigureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.cli, self.proot = self.base / "path-claude", self.base / "proot"
        self.cli.write_bytes(b"\x7fELF-exact-current-PATH-binary")
        self.proot.write_bytes(b"verified-proot")
        slots = [{"task": "fixture", "repeat": 1, "effort": effort} for effort in ("high", "max")]
        self.plan = {"cc_version": "2.1.110", "executor": "legacy chroot",
                     "formal_native_subset": {"stage": "formal-legacy", "schedule": slots},
                     "bridge_runtime_by_stage": {"formal-legacy": {"source_head": "actual-bridge"}}}
        (self.base / "evaluation-plan.json").write_text(json.dumps(self.plan))
        self.report = {"attempts": [{**slots[0], "stage": "formal-legacy", "artifact_score": 0}], "pending": []}

    def configure(self, *, alive=False, disable_native_idle_timeout=False):
        with patch.object(upgrade, "summarize", return_value=self.report), \
             patch.object(upgrade, "campaign_progress", return_value={"controller_alive": alive}), \
             patch.object(upgrade.subprocess, "check_output", side_effect=["2.1.322 (Claude Code)", "PRoot 5.5.0"]):
            return upgrade.configure(self.base, self.cli, self.proot, "formal-path-new",
                                     disable_native_idle_timeout=disable_native_idle_timeout)

    def test_idle_fix_creates_a_distinct_protocol_and_keeps_old_results(self):
        for effort in ("high", "max"):
            (self.base / f"settings-formal-{effort}.json").write_text(json.dumps({
                "env": {"API_TIMEOUT_MS": "29400000", "KEEP_ORIGINAL": effort}}))
        self.configure(disable_native_idle_timeout=True)
        plan = json.loads((self.base / "evaluation-plan.json").read_text())
        self.assertTrue(plan["formal_native_subset"]["cc_native_idle_timeout_disabled"])
        self.assertNotIn("cc_native_idle_timeout_disabled",
                         plan["protocol_history"][0]["formal_native_subset"])
        self.assertEqual(plan["formal_native_subset"]["preserved_local_schedule"][0]["stage"],
                         "formal-legacy")
        for effort in ("high", "max"):
            env = json.loads((self.base / f"settings-formal-{effort}.json").read_text())["env"]
            self.assertEqual(env, {"API_TIMEOUT_MS": "29400000", "KEEP_ORIGINAL": effort,
                                   "API_FORCE_IDLE_TIMEOUT": "0"})

    def test_preserves_failed_completed_slot_and_retires_old_smoke(self):
        smoke = self.base / "smoke"
        smoke.mkdir()
        (smoke / "evidence").write_text("old smoke is preserved")
        (self.base / "smoke-result.json").write_text('{"passed":true}')
        result = self.configure()
        plan = json.loads((self.base / "evaluation-plan.json").read_text())
        self.assertEqual(result["remaining_positions"], 1)
        self.assertEqual(result["preserved_local_positions"], 1)
        self.assertEqual(plan["formal_native_subset"]["schedule"][0]["effort"], "max")
        self.assertEqual(plan["formal_native_subset"]["preserved_local_schedule"][0]["stage"], "formal-legacy")
        self.assertEqual(plan["protocol_history"][0]["cc_version"], "2.1.110")
        self.assertEqual(plan["bridge_runtime_by_stage"]["formal-path-new"]["source_head"], "actual-bridge")
        self.assertFalse((self.base / "smoke-result.json").exists())
        archived = next((self.base / "smoke-history").glob("*/smoke/evidence"))
        self.assertEqual(archived.read_text(), "old smoke is preserved")
        snapshot = Path(result["runtime"]["snapshot_path"])
        self.assertEqual(snapshot.read_bytes(), self.cli.read_bytes())
        self.assertNotEqual(snapshot.stat().st_ino, self.cli.stat().st_ino)

    def test_running_controller_or_pending_attempt_refuses_before_any_change(self):
        original = (self.base / "evaluation-plan.json").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "stop scheduling"):
            self.configure(alive=True)
        self.report["pending"] = [{"attempt": "unfinished"}]
        with self.assertRaisesRegex(RuntimeError, "stop scheduling"):
            self.configure()
        self.assertEqual((self.base / "evaluation-plan.json").read_bytes(), original)
        self.assertFalse((self.base / "path-cc-runtime").exists())

    def test_existing_frozen_stage_is_not_overwritten(self):
        self.configure()
        original = (self.base / "evaluation-plan.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "already frozen"):
            self.configure()
        self.assertEqual((self.base / "evaluation-plan.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
