"""Transport changes must retain every scored outcome and original schedule slot."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from advance_terminal_native_transport import carry_cc_runtime, promote_smoke, remaining_schedule, validated_source
from run_terminal_native_campaign import run_serial_efforts


def slot(task, effort, stage=None, score=None):
    result = {"task": task, "repeat": 1, "effort": effort}
    if stage:
        result.update(stage=stage, artifact_score=score)
    return result


class TransportTransitionTests(unittest.TestCase):
    def test_bridge_only_rollout_keeps_host_runtime_and_cli_without_aliasing(self):
        runtime = {"kind": "path-native-host", "host_cli": "/frozen/claude", "identity": {"sha256": "a"}}
        plan = {"cc_runtime_by_stage": {"before": runtime}, "cc_versions_by_stage": {"before": "2.1.322"}}
        carry_cc_runtime(plan, "before", "after")
        self.assertEqual(plan["cc_runtime_by_stage"]["after"], runtime)
        self.assertEqual(plan["cc_versions_by_stage"]["after"], "2.1.322")
        plan["cc_runtime_by_stage"]["after"]["identity"]["sha256"] = "b"
        self.assertEqual(runtime["identity"]["sha256"], "a")

    def test_smoke_promotion_keeps_old_evidence_and_actual_new_smoke(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            rollout = base / "rollout"
            for root, content in ((base, b"old"), (rollout, b"new")):
                (root / "smoke/high").mkdir(parents=True)
                (root / "smoke/high/evidence").write_bytes(content)
                (root / "smoke-result.json").write_bytes(content)
            history = promote_smoke(base, rollout, "old-stage")
            self.assertEqual((history / "smoke/high/evidence").read_bytes(), b"old")
            self.assertEqual((history / "smoke-result.json").read_bytes(), b"old")
            self.assertEqual((base / "smoke/high/evidence").read_bytes(), b"new")
            self.assertEqual((base / "smoke-result.json").read_bytes(), b"new")
            self.assertEqual((rollout / "smoke-result.json").read_bytes(), b"new")

    def test_source_validation_rejects_a_later_bridge_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "validation.json"
            path.write_text(json.dumps({"passed": True, "source_module_sha256": {"bridge": "old"}}))
            with patch("advance_terminal_native_transport.module_hashes", return_value={"bridge": "new"}):
                with self.assertRaisesRegex(RuntimeError, "do not match"):
                    validated_source(path)

    def test_restart_request_during_high_does_not_queue_max_and_finishes_high(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            calls = []

            def attempt(effort):
                calls.append(("attempt", effort))
                (base / "bridge-restart-request.json").write_text(json.dumps({
                    "requested_by_user": True, "from_stage": "before"}))
                return {"effort": effort, "grade": {"score": 0}}

            def finish(result):
                calls.append(("preserve_and_archive", result["effort"], result["grade"]["score"]))

            self.assertTrue(run_serial_efforts(base, "before", ["high", "max"], attempt, finish))
            self.assertEqual(calls, [("attempt", "high"), ("preserve_and_archive", "high", 0)])

    def test_completed_restart_request_cannot_stop_the_new_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "bridge-restart-request.json").write_text(json.dumps({
                "requested_by_user": True, "from_stage": "before"}))
            called = []
            self.assertFalse(run_serial_efforts(base, "after", ["high", "max"],
                                               lambda effort: called.append(effort), self.fail))
            self.assertEqual(called, ["high", "max"])

    def test_failure_is_preserved_without_rerunning_it_or_mutating_the_plan(self):
        formal = {"schedule": [slot("a", "high"), slot("a", "max"), slot("b", "high")]}
        original = copy.deepcopy(formal)
        report = {"attempts": [slot("a", "high", "old", 0), slot("a", "max", "old", 1)]}
        remaining, preserved = remaining_schedule(formal, report)
        self.assertEqual(remaining, [slot("b", "high")])
        self.assertEqual(preserved, [{key: row[key] for key in ("task", "repeat", "effort", "stage")}
                                     for row in report["attempts"]])
        self.assertEqual(formal, original)

    def test_another_transition_also_keeps_earlier_local_stages(self):
        formal = {"schedule": [slot("b", "high"), slot("b", "max")],
                  "preserved_local_schedule": [slot("a", "max", "oldest", 0)]}
        report = {"attempts": [slot("a", "max", "oldest", 0), slot("b", "high", "old", 0)]}
        remaining, preserved = remaining_schedule(formal, report)
        self.assertEqual(remaining, [slot("b", "max")])
        self.assertEqual([row["stage"] for row in preserved], ["oldest", "old"])

    def test_duplicate_or_unplanned_results_cannot_be_hidden_by_a_transition(self):
        formal = {"schedule": [slot("a", "high")]}
        for attempts in ([slot("b", "high", "old", 0)],
                         [slot("a", "high", "old", 0), slot("a", "high", "new", 1)]):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                remaining_schedule(formal, {"attempts": attempts})

    def test_duplicate_schedule_slots_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate scheduled"):
            remaining_schedule({"schedule": [slot("a", "max"), slot("a", "max")]}, {"attempts": []})


if __name__ == "__main__":
    unittest.main()
