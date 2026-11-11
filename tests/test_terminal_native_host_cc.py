"""Direct host execution must preserve UID, path ownership and recovery rules."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import native_host_cc as host
import native_path_cc as native


class DirectHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        for name, value in (("APP_PATH", self.base / "app-alias"),
                            ("SOLUTION_PATH", self.base / "solution-alias"),
                            ("LEASE_PATH", self.base / "workspace.lock")):
            patcher = patch.object(host, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_workspace_is_exclusive_and_cannot_replace_a_foreign_path(self):
        first, second = self.base / "first", self.base / "second"
        host.claim_workspace(first)
        try:
            self.assertEqual(host.APP_PATH.readlink(), first / "app")
            with self.assertRaises(BlockingIOError):
                host.claim_workspace(second)
            with self.assertRaisesRegex(RuntimeError, "another owner"):
                host.release_workspace(second)
            self.assertEqual(host.APP_PATH.readlink(), first / "app")
        finally:
            host.release_workspace(first)
        host.APP_PATH.write_text("existing host data")
        with self.assertRaisesRegex(RuntimeError, "occupied"):
            host.claim_workspace(second)
        self.assertEqual(host.APP_PATH.read_text(), "existing host data")
        host.APP_PATH.unlink()
        host.claim_workspace(second)
        host.release_workspace(second)

    def test_direct_command_uses_host_tools_and_a_clean_unprivileged_environment(self):
        root = self.base / "workspace"
        command = host.shell_command(root, "python3 -V", uid=20004,
                                    spec={"host_toolchain": "/verified/host-tools"},
                                    environment={"API_FORCE_IDLE_TIMEOUT": "0"})
        self.assertEqual(command[:5], ["setpriv", "--reuid=20004", "--regid=20004",
                                      "--clear-groups", "--no-new-privs"])
        self.assertIn("--chdir", command)
        self.assertIn(str(root), command)
        self.assertIn(f"HOME={root}/home/agent", command)
        self.assertIn("API_FORCE_IDLE_TIMEOUT=0", command)
        self.assertIn("-i", command)
        self.assertNotIn("proot", command)
        self.assertNotIn("chroot", command)
        for uid in (0, 1000, 30000):
            with self.assertRaises(ValueError):
                host.shell_command(root, "true", uid=uid, spec={})

    def test_native_runtime_rejects_changes_to_public_host_tools(self):
        tools = self.base / "tools"
        tools.mkdir()
        cli = tools / "claude"
        cli.write_bytes(b"exact frozen Claude executable")
        spec = {"kind": host.KIND, "snapshot_path": str(cli), "host_cli": str(cli),
                "sha256": native.sha256(cli), "host_toolchain": str(tools),
                "host_toolchain_identity": host.tree_identity(tools)}
        self.assertEqual(native.verify_runtime(spec), spec)
        (tools / "python").write_bytes(b"unexpected change")
        with self.assertRaisesRegex(RuntimeError, "toolchain identity changed"):
            native.verify_runtime(spec)

    def test_returned_host_workspace_releases_aliases_before_moving_root(self):
        output = self.base / "attempt"
        output.mkdir()
        root = self.base / "external/root"
        root.mkdir(parents=True)
        (root / "app").mkdir()
        (root / "solution").mkdir()
        host.claim_workspace(root)
        host.link_solution(root)
        (output / "runtime-root.json").write_text(json.dumps({"kind": host.KIND, "phase": "active"}))
        with patch.object(native, "runtime_root", return_value=(root, 20004)), \
             patch.object(native, "uid_processes", return_value=[]):
            returned = native.return_agent_root(output)
        self.assertEqual(returned, output / "agent-root")
        self.assertTrue((returned / "app").is_dir())
        self.assertFalse(host.APP_PATH.is_symlink())
        self.assertFalse(host.SOLUTION_PATH.is_symlink())
        self.assertEqual(json.loads((output / "runtime-root.json").read_text())["phase"], "returned")


if __name__ == "__main__":
    unittest.main()
