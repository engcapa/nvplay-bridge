"""Native CLI path translation must preserve real UID isolation and evidence."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import native_path_cc as runtime
from native_eval_support import run_logged


class PathRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def test_command_drops_real_privileges_before_proot_and_clears_host_environment(self):
        command = runtime.shell_command(Path("/var/tmp/fixture/root"), "printf fixture", uid=20004,
                  spec={"proot_path": "/verified/proot"}, environment={"API_TIMEOUT_MS": "29400000"})
        self.assertEqual(command[:6], ["setpriv", "--reuid=20004", "--regid=20004", "--clear-groups",
                                      "--no-new-privs", "/verified/proot"])
        self.assertIn("/proc", command)
        self.assertIn("-i", command)
        self.assertIn("API_TIMEOUT_MS=29400000", command)
        self.assertFalse(any(arg in ("-0", "--root-id", "--fake-root") for arg in command))
        for uid in (0, 1000, 30000):
            with self.assertRaises(ValueError):
                runtime.shell_command(self.base, "true", uid=uid, spec={})

    def test_rejects_changed_cli_or_path_translator_before_launch(self):
        cli, proot = self.base / "cli", self.base / "proot"
        cli.write_bytes(b"exact PATH executable")
        proot.write_bytes(b"verified PRoot")
        spec = {"kind": "path-native-proot", "snapshot_path": str(cli), "sha256": runtime.sha256(cli),
                "proot_path": str(proot), "proot_sha256": runtime.sha256(proot)}
        self.assertEqual(runtime.verify_runtime(spec), spec)
        for path in (cli, proot):
            original = path.read_bytes()
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                runtime.verify_runtime(spec)
            path.write_bytes(original)

    def test_live_native_uid_cannot_be_returned_or_archived(self):
        marker = {"phase": "active", "uid": 20004, "root": "/var/tmp/fixture/root"}
        (self.base / "runtime-root.json").write_text(json.dumps(marker))
        with patch.object(runtime, "runtime_root", return_value=(Path(marker["root"]), 20004)), \
             patch.object(runtime, "uid_processes", return_value=[12345]):
            with self.assertRaisesRegex(RuntimeError, "live native CC"):
                runtime.return_agent_root(self.base)
        self.assertEqual(json.loads((self.base / "runtime-root.json").read_text()), marker)

    def test_native_root_marker_cannot_refer_to_another_attempt_or_uid(self):
        marker = {"phase": "active", "kind": "path-native-proot", "uid": 20004,
                  "root": "/var/tmp/nvplay-bridge-cc-20004-fixture/root", "canonical_root": "/another/attempt/agent-root"}
        (self.base / "runtime-root.json").write_text(json.dumps(marker))
        with self.assertRaisesRegex(ValueError, "another attempt"):
            runtime.runtime_root(self.base)
        marker.update(uid=0, canonical_root=str(self.base / "agent-root"))
        (self.base / "runtime-root.json").write_text(json.dumps(marker))
        with self.assertRaisesRegex(ValueError, "invalid native CC root marker"):
            runtime.runtime_root(self.base)

    def test_child_host_cwd_does_not_inherit_private_repository(self):
        log, state = self.base / "out.log", self.base / "state.json"
        result = run_logged([sys.executable, "-I", "-S", "-c", "import os; print(os.getcwd())"],
                            log, None, state, cwd=self.base)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(log.read_text().strip(), str(self.base))
        self.assertEqual(json.loads(state.read_text())["host_cwd"], str(self.base))


if __name__ == "__main__":
    unittest.main()
