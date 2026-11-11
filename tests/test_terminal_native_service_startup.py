"""Service records must let recovery and shutdown reject a recycled PID."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from eval_terminal_native import process_identity
from pause_terminal_native_services import identify
from start_terminal_native_services import launch


class ServiceStartupTests(unittest.TestCase):
    def test_browser_identity_is_usable_for_recovery_and_rejects_pid_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            child = launch([sys.executable, "-c", "import time; time.sleep(30)",
                            f"--user-data-dir={base}/brave-profile"], base, "brave")
            try:
                path = base / "brave-process.json"
                record = json.loads(path.read_text())
                actual = process_identity(child.pid)
                self.assertEqual({key: record[key] for key in actual}, actual)
                self.assertEqual(identify(base, path.name, "time.sleep")["identity"], actual)
                record["start_ticks"] = str(int(record["start_ticks"]) + 1)
                path.write_text(json.dumps(record))
                with self.assertRaisesRegex(RuntimeError, "reused PID"):
                    identify(base, path.name, "time.sleep")
                self.assertIsNone(child.poll())
            finally:
                child.terminate()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
