"""Rebuilding must keep package pins when a live mirror drops an older index entry."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from provision_terminal_native import fixed_apt_archive


class FixedAptArchiveTests(unittest.TestCase):
    package = "perl=5.38.2-3.2ubuntu0.6"
    archived = {package: {"filename": "perl_5.38.2-3.2ubuntu0.6_amd64.deb", "sha256": "archived-sha256"}}

    def test_removed_index_entry_uses_recorded_checksum_and_same_version(self):
        newer = "Package: perl\nVersion: 5.38.2-3.2ubuntu0.7\nFilename: newer.deb\nSHA256: newer-digest\n"
        for description in ("", newer, "Package: perl\nVersion: 5.38.2-3.2ubuntu0.6\nStatus: install ok installed\n"):
            with self.subTest(description=description):
                result = fixed_apt_archive(self.package, description, self.archived)
                self.assertEqual(result["sha256"], "archived-sha256")
                self.assertEqual(result["filename"], "pool/main/p/perl/perl_5.38.2-3.2ubuntu0.6_amd64.deb")

    def test_searches_all_stanzas_and_rejects_an_unknown_unpinned_archive(self):
        description = ("Package: perl\nVersion: 5.38.2-3.2ubuntu0.6\nStatus: install ok installed\n\n"
                       "Package: perl\nVersion: 5.38.2-3.2ubuntu0.6\nFilename: pinned.deb\nSHA256: signed-index-digest\n")
        self.assertEqual(fixed_apt_archive(self.package, description, {})["sha256"], "signed-index-digest")
        with self.assertRaisesRegex(ValueError, "missing from both"):
            fixed_apt_archive(self.package, "", {})


if __name__ == "__main__":
    unittest.main()
