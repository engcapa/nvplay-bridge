"""Transport changes must retain every scored outcome and original schedule slot."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from advance_terminal_native_transport import remaining_schedule


def slot(task, effort, stage=None, score=None):
    result = {"task": task, "repeat": 1, "effort": effort}
    if stage:
        result.update(stage=stage, artifact_score=score)
    return result


class TransportTransitionTests(unittest.TestCase):
    def test_failure_is_preserved_without_rerunning_it_or_mutating_the_plan(self):
        formal = {"schedule": [slot("a", "high"), slot("a", "max"), slot("b", "high")]}
        original = copy.deepcopy(formal)
        report = {"attempts": [slot("a", "high", "old", 0), slot("a", "max", "old", 1)]}
        remaining, preserved = remaining_schedule(formal, report)
        self.assertEqual(remaining, [slot("b", "high")])
        self.assertEqual(preserved, [{key: row[key] for key in ("task", "repeat", "effort", "stage")}
                                     for row in report["attempts"]])
        self.assertEqual(formal, original)

    def test_another_transition_also_keeps_earlier_local_stages(self):
        formal = {"schedule": [slot("b", "high"), slot("b", "max")],
                  "preserved_local_schedule": [slot("a", "max", "oldest", 0)]}
        report = {"attempts": [slot("a", "max", "oldest", 0), slot("b", "high", "old", 0)]}
        remaining, preserved = remaining_schedule(formal, report)
        self.assertEqual(remaining, [slot("b", "max")])
        self.assertEqual([row["stage"] for row in preserved], ["oldest", "old"])

    def test_duplicate_or_unplanned_results_cannot_be_hidden_by_a_transition(self):
        formal = {"schedule": [slot("a", "high")]}
        for attempts in ([slot("b", "high", "old", 0)],
                         [slot("a", "high", "old", 0), slot("a", "high", "new", 1)]):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                remaining_schedule(formal, {"attempts": attempts})

    def test_duplicate_schedule_slots_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate scheduled"):
            remaining_schedule({"schedule": [slot("a", "max"), slot("a", "max")]}, {"attempts": []})


if __name__ == "__main__":
    unittest.main()
