import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from report_terminal_native_continuation import combine


class ContinuationReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def row(self, task="a", score=0, stage="old"):
        return {"task": task, "repeat": 1, "effort": "high", "stage": stage,
                "artifact_score": score, "end_to_end_success": score == 1,
                "infrastructure_error": None}

    def save(self, name, rows, parent=None, preserved=None):
        data = {"new_formal_attempts_planned": 2, "new_formal_attempts_completed": len(rows),
                "results": {"attempts": rows, "pending": [{"task": "never-graded"}]}}
        if parent:
            data["protocol"] = {"continuation": {
                "source_checkpoint": str(parent),
                "source_checkpoint_sha256": hashlib.sha256(parent.read_bytes()).hexdigest(),
                "preserved_completed_attempts": len(preserved), "preserved_positions": preserved,
                "total_plan_positions": 2}}
        path = self.root / name
        path.write_text(json.dumps(data))
        return path

    def test_preserves_failure_and_original_stage_excludes_pending(self):
        old = self.row()
        parent = self.save("parent.json", [old])
        path = self.save("child.json", [self.row("b", 1, "new")], parent, [old])
        result = combine(path, self.root)
        self.assertEqual([(row["task"], row["artifact_score"], row["stage"])
                          for row in result["attempts"]], [("a", 0, "old"), ("b", 1, "new")])
        self.assertEqual(result["totals"]["completed_positions"], 2)
        self.assertEqual(result["totals"]["artifact_passes"], 1)
        self.assertEqual(result["remaining_positions"], 0)
        self.assertEqual(len(result["checkpoint_chain"]), 2)

    def test_rejects_modified_ancestor(self):
        old = self.row()
        parent = self.save("parent.json", [old])
        path = self.save("child.json", [], parent, [old])
        parent.write_text(parent.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            combine(path, self.root)

    def test_rejects_selecting_a_successful_rerun(self):
        old = self.row()
        parent = self.save("parent.json", [old])
        path = self.save("child.json", [self.row(score=1, stage="new")], parent, [old])
        with self.assertRaisesRegex(ValueError, "duplicate completed position"):
            combine(path, self.root)

    def test_rejects_replacing_an_inherited_position(self):
        parent = self.save("parent.json", [self.row()])
        path = self.save("child.json", [], parent, [self.row("other")])
        with self.assertRaisesRegex(ValueError, "inherited positions differ"):
            combine(path, self.root)

    def test_keeps_null_infrastructure_result_without_assigning_grade(self):
        row = self.row(score=None)
        row["infrastructure_error"] = "private failure body"
        path = self.save("parent.json", [row])
        result = combine(path, self.root)
        self.assertEqual(result["totals"], {"completed_positions": 1, "valid_grades": 0,
                                          "artifact_passes": 0, "end_to_end_passes": 0,
                                          "infrastructure_errors": 1})
        self.assertNotIn("private failure body", json.dumps(result))

    def test_resolves_relocated_checkpoint_by_verified_sibling(self):
        old = self.row()
        parent = self.save("parent.json", [old])
        path = self.save("child.json", [], parent, [old])
        data = json.loads(path.read_text())
        data["protocol"]["continuation"]["source_checkpoint"] = "/previous-container/parent.json"
        path.write_text(json.dumps(data))
        self.assertEqual(combine(path, self.root)["totals"]["completed_positions"], 1)

    def test_rejects_completed_position_outside_schedule(self):
        path = self.save("parent.json", [self.row("unexpected")])
        data = json.loads(path.read_text())
        data["protocol"] = {"formal_native_subset": {"schedule": [self.row("a"), self.row("b")]}}
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "outside the frozen schedule"):
            combine(path, self.root)


if __name__ == "__main__":
    unittest.main()
