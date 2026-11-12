"""A deferred installer must preserve the active source and proxy boundary."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import advance_terminal_native_stream_idle as transition


class DeferredIdleInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source.py"
        self.source.write_bytes(b"validated source")
        self.validation = {"candidate_commit": "candidate",
            "candidate_source_sha256": {"source.py": hashlib.sha256(self.source.read_bytes()).hexdigest()}}
        patcher = patch.object(transition, "REPO", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_invalid_candidate_object_is_rejected_before_any_install(self):
        with patch.object(transition, "git_command", return_value=["git"]), \
                patch.object(transition.subprocess, "check_output", return_value=b"different candidate"), \
                patch.object(transition.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "differs from its passing validation"):
                transition.install_candidate(self.base, self.validation)
        run.assert_not_called()
        self.assertEqual(self.source.read_bytes(), b"validated source")

    def test_matching_installed_files_do_not_reapply_a_commit(self):
        with patch.object(transition, "git_command", return_value=["git"]), \
                patch.object(transition.subprocess, "check_output", side_effect=[b"validated source", "installed-head\n"]), \
                patch.object(transition.subprocess, "run") as run:
            self.assertEqual(transition.install_candidate(self.base, self.validation), "installed-head")
        run.assert_not_called()

    def test_modified_source_is_preserved_instead_of_installing_over_it(self):
        self.source.write_bytes(b"local operator change")
        with patch.object(transition, "git_command", return_value=["git"]), \
                patch.object(transition.subprocess, "check_output", return_value=b"validated source"), \
                patch.object(transition.subprocess, "run", return_value=Mock(returncode=1)) as run:
            with self.assertRaisesRegex(RuntimeError, "local changes"):
                transition.install_candidate(self.base, self.validation)
        self.assertEqual(self.source.read_bytes(), b"local operator change")
        self.assertFalse(any("cherry-pick" in call.args[0] for call in run.call_args_list))

    def test_proxy_probe_requires_a_successful_connect_and_uses_the_specified_proxy(self):
        for code, response, expected in ((28, "000,000", False), (0, "502,000", False),
                                         (0, "200,000", False), (0, "200,403", True), (0, "200,200", True)):
            with self.subTest(response=response), patch.object(transition.subprocess, "run",
                    return_value=Mock(returncode=code, stdout=response)) as run:
                self.assertEqual(transition.proxy_connects(), expected)
                args = run.call_args.args[0]
                self.assertEqual(args[args.index("--proxy") + 1], "http://127.0.0.1:31128")
                self.assertEqual(args[args.index("--noproxy") + 1], "")


if __name__ == "__main__":
    unittest.main()
