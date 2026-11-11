"""Reboot shutdown includes direct host reference setup and verifier processes."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from stop_terminal_native_at import unfinished_control_directories


class StopControlTests(unittest.TestCase):
    def test_active_host_reference_and_unfinished_grade_are_included(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            active = base / "controls/task/oracle"
            active.mkdir(parents=True)
            (active / "runtime-root.json").write_text(json.dumps({"phase": "active", "kind": "path-native-host"}))
            grading = base / "controls/task/baseline"
            (grading / "agent-root").mkdir(parents=True)
            self.assertEqual(unfinished_control_directories(base), [grading, active])
            (active / "runtime-root.json").write_text('{"phase":"returned"}')
            (active / "grade.json").write_text('{"score":1}')
            (grading / "grade.json").write_text('{"score":0}')
            self.assertEqual(unfinished_control_directories(base), [])


if __name__ == "__main__":
    unittest.main()
