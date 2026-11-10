"""Formal attempts must obey the fixed output, effort and client timeout protocol."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from eval_terminal_native import Evaluation


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


if __name__ == "__main__":
    unittest.main()
