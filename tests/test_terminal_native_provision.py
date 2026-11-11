"""Rebuild a benchmark without losing its package pins or completed positions."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from eval_terminal_native import DEFAULT_PILOT
from provision_terminal_native import continue_schedule, fixed_apt_archive


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


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "checkpoint.json"
        self.formal = {"tasks": DEFAULT_PILOT, "repeats_per_effort": 3}
        self.checkpoint = {"protocol": {"source_commit": "452bf305c6daa62fc59061d22133a7cbc7c1572e",
            "cc_version": "2.1.110", "pilot_tasks": DEFAULT_PILOT, "continuation": {
                "preserved_positions": [{"task": task, "repeat": 1, "effort": effort}
                                        for task in DEFAULT_PILOT[:2] for effort in ("high", "max")],
                "preserved_completed_attempts": 4, "total_plan_positions": 30}},
            "results": {"attempts": [{"task": DEFAULT_PILOT[2], "repeat": 1,
                                      "effort": "high", "artifact_score": 0}]},
            "combined_plan_positions_completed": 5}

    def resume(self):
        self.path.write_text(json.dumps(self.checkpoint))
        return continue_schedule(self.formal, self.path)

    def test_second_reboot_keeps_inherited_results_and_local_failures(self):
        continuation = self.resume()
        self.assertEqual(continuation["preserved_completed_attempts"], 5)
        self.assertEqual(len(self.formal["schedule"]), 25)
        self.assertEqual(self.formal["schedule"][0], {"task": DEFAULT_PILOT[2], "repeat": 1, "effort": "max"})

    def test_rejects_result_that_duplicates_an_inherited_position(self):
        self.checkpoint["results"]["attempts"][0]["task"] = DEFAULT_PILOT[0]
        self.checkpoint["combined_plan_positions_completed"] = 4
        with self.assertRaisesRegex(ValueError, "duplicate completed positions"):
            self.resume()

    def test_native_path_checkpoint_continues_without_relabeling_earlier_results(self):
        protocol = self.checkpoint["protocol"]
        protocol.update(cc_version="2.1.322", formal_native_subset={"stage": "path-native"},
                        cc_runtime_by_stage={"path-native": {"kind": "path-native-proot",
                            "version": "2.1.322", "sha256": "a" * 64}})
        continuation = self.resume()
        self.assertEqual(continuation["source_cc_version"], "2.1.322")
        self.assertEqual(continuation["preserved_completed_attempts"], 5)
        self.assertEqual(len(self.formal["schedule"]), 25)
        protocol["cc_runtime_by_stage"]["path-native"]["version"] = "mismatch"
        with self.assertRaisesRegex(ValueError, "different dataset, CLI or task set"):
            self.resume()


if __name__ == "__main__":
    unittest.main()
