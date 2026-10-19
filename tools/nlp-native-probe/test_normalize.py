"""Synthetic output-policy checks without model execution."""
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("normalize", Path(__file__).with_name("normalize.py"))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ThresholdTests(unittest.TestCase):
    def row(self):
        return {"outcome": "assessed", "topics": [{"topic_id": "topic.synthetic", "score": 0.49}],
                "mentions": [{"concept_id": "concept.synthetic", "start": 0, "end": 4, "score": 0.5},
                             {"concept_id": "concept.synthetic", "start": 5, "end": 9, "score": 0.51}]}

    def test_zero_topics_and_inclusive_boundary_preserve_occurrences(self):
        row = self.row()
        output = MODULE.normalize(row, 0.5, 0.5)
        self.assertEqual(output["topics"], [])
        self.assertEqual(output["topics_outcome"], "assessed_no_match")
        self.assertEqual(output["mentions"], row["mentions"])
        self.assertEqual(output["mentions_outcome"], "assessed_findings")
        self.assertEqual(output["calibration_status"], "not_calibrated")
        self.assertEqual(row["topics"][0]["score"], 0.49)

    def test_mentions_below_threshold_are_removed(self):
        output = MODULE.normalize(self.row(), 0.49, 0.52)
        self.assertEqual(len(output["topics"]), 1)
        self.assertEqual(output["mentions"], [])
        self.assertEqual(output["mentions_outcome"], "assessed_no_match")

    def test_invalid_scores_and_thresholds_are_rejected(self):
        for value in (True, float("nan"), float("inf"), -0.1, 1.1, "0.5"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "invalid_threshold"):
                    MODULE.normalize(self.row(), value, 0.5)
                row = self.row()
                row["topics"][0]["score"] = value
                with self.assertRaisesRegex(ValueError, "invalid_score"):
                    MODULE.normalize(row, 0.5, 0.5)

    def test_withheld_inputs_cannot_become_no_match(self):
        for outcome in ("partial", "unsupported"):
            row = {"outcome": outcome, "topics": [], "mentions": []}
            self.assertEqual(MODULE.normalize(row, 0.5, 0.5)["topics_outcome"], outcome)
            row["topics"] = self.row()["topics"]
            with self.assertRaisesRegex(ValueError, "findings_without_assessment"):
                MODULE.normalize(row, 0.5, 0.5)


if __name__ == "__main__":
    unittest.main()
