"""Formal attempts must obey the fixed output, effort and client timeout protocol."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from eval_terminal_native import Evaluation, attempt_batches


class NativeRequestSettingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.evaluation = Evaluation.__new__(Evaluation)
        self.evaluation.base = self.base
        self.evaluation.plan = {"formal_native_subset": {
            "max_output_tokens": 131072, "cc_output_environment_tokens": 128000,
            "cc_api_timeout_ms": 29400000}}
        for effort in ("high", "max"):
            settings = {"env": {
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000", "API_TIMEOUT_MS": "29400000",
                "CLAUDE_CODE_EXTRA_BODY": json.dumps({
                    "max_tokens": 131072, "output_config": {"effort": effort}})}}
            (self.base / f"settings-formal-{effort}.json").write_text(json.dumps(settings))
            (self.base / f"settings-{effort}.json").write_text(
                '{"env":{"original_pilot":"retained"}}')

    def test_both_formal_efforts_use_128k_while_pilot_keeps_original_settings(self):
        for effort in ("high", "max"):
            settings = self.evaluation.request_settings(effort, "formal")
            body = json.loads(settings["env"]["CLAUDE_CODE_EXTRA_BODY"])
            self.assertEqual(body, {"max_tokens": 131072, "output_config": {"effort": effort}})
            self.assertEqual(self.evaluation.request_settings(effort, "pilot")["env"],
                             {"original_pilot": "retained"})

    def test_mismatched_output_effort_or_timeout_refuses_formal_attempt(self):
        path = self.base / "settings-formal-high.json"
        original = json.loads(path.read_text())
        mismatches = [
            ("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "32000"),
            ("API_TIMEOUT_MS", "3900000"),
            ("CLAUDE_CODE_EXTRA_BODY", json.dumps({
                "max_tokens": 32000, "output_config": {"effort": "high"}})),
            ("CLAUDE_CODE_EXTRA_BODY", json.dumps({
                "max_tokens": 131072, "output_config": {"effort": "max"}})),
        ]
        for field, value in mismatches:
            with self.subTest(field=field, value=value):
                changed = json.loads(json.dumps(original))
                changed["env"][field] = value
                path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, "frozen output/effort/timeout"):
                    self.evaluation.request_settings("high", "formal")
        path.write_text(json.dumps(original))

    def test_revised_formal_stage_uses_the_same_frozen_settings(self):
        self.evaluation.plan["formal_native_subset"]["stage"] = "formal-128k-fixed"
        for effort in ("high", "max"):
            self.assertEqual(self.evaluation.request_settings(effort, "formal-128k-fixed"),
                             self.evaluation.request_settings(effort, "formal"))

    def test_native_idle_override_is_required_by_protocol_and_present_before_cli_start(self):
        stage = "formal-native-idle-fixed"
        self.evaluation.plan["formal_native_subset"].update(
            stage=stage, cc_native_idle_timeout_disabled=True)
        self.evaluation.plan["cc_runtime_by_stage"] = {
            stage: {"kind": "path-native-proot", "proot_path": "/verified/proot"}}
        path = self.base / "settings-formal-high.json"
        settings = json.loads(path.read_text())
        settings["env"]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8083"
        for value in (None, "1", "false"):
            with self.subTest(value=value):
                settings["env"].pop("API_FORCE_IDLE_TIMEOUT", None)
                if value is not None:
                    settings["env"]["API_FORCE_IDLE_TIMEOUT"] = value
                path.write_text(json.dumps(settings))
                with self.assertRaisesRegex(ValueError, "native idle timeout protocol"):
                    self.evaluation.request_settings("high", stage)
        settings["env"]["API_FORCE_IDLE_TIMEOUT"] = "0"
        path.write_text(json.dumps(settings))
        settings = self.evaluation.request_settings("high", stage)
        _, environment, adapter = self.evaluation.cc_bootstrap(self.base, stage, settings)
        command = self.evaluation.agent_command(self.base, "claude -p fixture", stage=stage,
                  uid=20004, environment=environment)
        self.assertIsNone(adapter)
        self.assertIn("API_FORCE_IDLE_TIMEOUT=0", command)
        self.assertIn("API_TIMEOUT_MS=29400000", command)
        self.assertIn("-i", command)
        del self.evaluation.plan["cc_runtime_by_stage"][stage]
        with self.assertRaisesRegex(ValueError, "native idle timeout protocol"):
            self.evaluation.request_settings("high", stage)

    def test_partial_configuration_cannot_apply_idle_override_to_old_protocol(self):
        path = self.base / "settings-formal-high.json"
        settings = json.loads(path.read_text())
        settings["env"]["API_FORCE_IDLE_TIMEOUT"] = "0"
        path.write_text(json.dumps(settings))
        with self.assertRaisesRegex(ValueError, "not recorded in the frozen protocol"):
            self.evaluation.request_settings("high", "formal")

    def test_fixed_protocol_refuses_an_unconfigured_bridge_runtime(self):
        self.evaluation.plan["formal_native_subset"].update(
            stage="formal-128k-fixed", bridge_fixed_max_output_tokens=131072)
        for runtime in ({}, {"fixed_max_output_tokens": 0}, {"fixed_max_output_tokens": 64000}):
            with self.subTest(runtime=runtime):
                self.evaluation.plan["bridge_runtime_by_stage"] = {"formal-128k-fixed": runtime}
                with self.assertRaisesRegex(ValueError, "frozen fixed output protocol"):
                    self.evaluation.request_settings("high", "formal-128k-fixed")
        self.evaluation.plan["bridge_runtime_by_stage"]["formal-128k-fixed"] = {
            "fixed_max_output_tokens": 131072}
        self.evaluation.request_settings("high", "formal-128k-fixed")

    def context_settings(self):
        formal = self.evaluation.plan["formal_native_subset"]
        formal.update(stage="formal-1m-720k", cc_model="kimi-k3.1-pro[1m]",
                      automatic_compaction_enabled=True, cc_auto_compact_window_tokens=720000)
        path = self.base / "settings-formal-high.json"
        settings = json.loads(path.read_text())
        settings["model"] = "kimi-k3.1-pro[1m]"
        settings["env"].update(ANTHROPIC_MODEL="kimi-k3.1-pro[1m]", CLAUDE_CODE_AUTO_COMPACT_WINDOW="720000")
        path.write_text(json.dumps(settings))
        return path, settings

    def test_context_stage_launch_enables_compaction_and_keeps_legacy_behavior(self):
        self.context_settings()
        settings = self.evaluation.request_settings("high", "formal-1m-720k")
        launch = self.evaluation.cc_launch_configuration(settings, "formal-1m-720k")
        self.assertEqual(launch["model"], "kimi-k3.1-pro[1m]")
        self.assertEqual(launch["auto_compact_window_tokens"], 720000)
        self.assertNotIn("DISABLE_AUTO_COMPACT", settings["env"])
        legacy = {"env": {}}
        self.assertEqual(self.evaluation.cc_launch_configuration(legacy, "pilot")["model"], "kimi-k3.1-pro")
        self.assertEqual(legacy["env"]["DISABLE_AUTO_COMPACT"], "1")

    def test_context_protocol_rejects_window_model_and_disabling_flag_mismatches(self):
        path, settings = self.context_settings()
        for field, value in (("ANTHROPIC_MODEL", "kimi-k3.1-pro"),
                             ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "200000"),
                             ("DISABLE_AUTO_COMPACT", "1"), ("DISABLE_COMPACT", "true"),
                             ("CLAUDE_CODE_DISABLE_1M_CONTEXT", "1")):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(settings))
                changed["env"][field] = value
                path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, "model/context/compaction"):
                    self.evaluation.request_settings("high", "formal-1m-720k")

    def test_continuation_schedule_preserves_failed_completed_positions(self):
        plan = {"pilot_tasks": ["first", "second"], "efforts": ["high", "max"],
                "formal_native_subset": {"stage": "new", "schedule": [
                    {"task": "first", "effort": "max", "repeat": 1},
                    {"task": "second", "effort": "high", "repeat": 1},
                    {"task": "second", "effort": "max", "repeat": 1}]}}
        self.assertEqual(attempt_batches(plan, "new", 3),
                         [("first", 1, ["max"]), ("second", 1, ["high", "max"])])
        plan["formal_native_subset"]["schedule"].append({"task": "first", "effort": "max", "repeat": 1})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            attempt_batches(plan, "new", 3)

    def test_no_session_deadline_is_explicit_and_does_not_change_legacy_stages(self):
        self.evaluation.plan["formal_native_subset"].update(stage="formal-1m-720k", agent_wall_clock_timeout_seconds=None)
        self.assertIsNone(self.evaluation.agent_timeout("formal-1m-720k", 28800))
        self.assertEqual(self.evaluation.agent_timeout("pilot", 1800), 1800)
        self.evaluation.plan["formal_native_subset"]["agent_wall_clock_timeout_seconds"] = 0
        with self.assertRaises(ValueError):
            self.evaluation.agent_timeout("formal-1m-720k", 28800)


if __name__ == "__main__":
    unittest.main()
