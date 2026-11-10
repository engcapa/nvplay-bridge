"""Expanded evidence is reclaimed only after a verified, restorable archive exists."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from archive_terminal_native_roots import archive_root


class NativeArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "agent-root"
        self.root.mkdir()
        (self.root / "artifact").write_bytes(os.urandom(8192))
        (self.root / "artifact").chmod(0o640)
        (self.root / "link").symlink_to("artifact")

    def test_archive_roundtrip_preserves_bytes_permissions_and_symlinks(self):
        expected = (self.root / "artifact").read_bytes()
        metadata = archive_root(self.root)
        self.assertFalse(self.root.exists())
        archive = self.base / metadata["archive"]
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), metadata["sha256"])
        subprocess.run(["tar", "-xpf", str(archive), "-C", str(self.base)], check=True)
        self.assertEqual((self.root / "artifact").read_bytes(), expected)
        self.assertEqual((self.root / "artifact").stat().st_mode & 0o777, 0o640)
        self.assertEqual(os.readlink(self.root / "link"), "artifact")

    def test_compression_failure_keeps_original_evidence(self):
        with patch("archive_terminal_native_roots.subprocess.run", side_effect=subprocess.CalledProcessError(2, "tar")):
            with self.assertRaises(subprocess.CalledProcessError):
                archive_root(self.root)
        self.assertTrue((self.root / "artifact").is_file())


if __name__ == "__main__":
    unittest.main()
