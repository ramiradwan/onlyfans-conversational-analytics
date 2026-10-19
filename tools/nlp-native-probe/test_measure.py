"""Pure completion checks; these tests never start the native executable."""
import copy
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("native_measure", Path(__file__).with_name("measure.py"))
MEASURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEASURE)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.inputs = [{"id": "one", "text": "echo", "supported": True}]
        self.recipe = {"max_input_tokens": 100, "max_source_words": 10, "max_source_codepoints": 100,
                       "topics": [{"id": "topic.synthetic"}], "concepts": [{"id": "concept.synthetic"}]}
        self.rows = [{"event": "ready", "provider": "CPUExecutionProvider", "intra_threads": 2,
                      "tokenizer_parallel": False, "affinity_mask": 3, "runtime_info": "synthetic",
                      "peak_working_set": 100, "cpu_arena": False, "encoded_token_limit": 100,
                      "source_words_limit": 10, "source_codepoints_limit": 100},
                     {"id": "one", "topics": [], "mentions": [], "outcome": "assessed",
                      "input_tokens": 12, "elapsed_ms": 1, "peak_working_set": 200},
                     {"event": "shutdown", "peak_working_set": 200}]

    def errors(self, code=0, timeout=False):
        return MEASURE.completion_errors(self.inputs, self.rows, self.recipe, code, timeout)

    def test_complete_run(self):
        self.assertEqual(self.errors(), [])

    def test_candidate_configuration_matches_recipe(self):
        for field in ("cpu_arena", "encoded_token_limit", "source_words_limit", "source_codepoints_limit"):
            original = self.rows[0][field]
            self.rows[0][field] = True
            self.assertIn("candidate_configuration_mismatch", self.errors())
            self.rows[0][field] = original

    def test_failed_process(self):
        self.assertIn("process_incomplete", self.errors(code=1))
        self.assertIn("process_incomplete", self.errors(timeout=True))

    def test_missing_or_duplicate_output(self):
        self.rows.insert(1, copy.deepcopy(self.rows[1]))
        self.assertIn("result_identity_mismatch", self.errors())
        self.rows = self.rows[:1] + self.rows[-1:]
        self.assertIn("result_identity_mismatch", self.errors())

    def test_expected_ids_must_be_unique(self):
        self.inputs.append(copy.deepcopy(self.inputs[0]))
        self.rows.insert(1, copy.deepcopy(self.rows[1]))
        self.assertIn("duplicate_expected_ids", self.errors())

    def test_token_counts_are_required_integers(self):
        for value in (None, True, 1.5, "12"):
            with self.subTest(value=value):
                self.rows[1]["input_tokens"] = value
                self.assertIn("invalid_result_types", self.errors())
        del self.rows[1]["input_tokens"]
        self.assertIn("invalid_result_types", self.errors())

    def test_events_are_ordered(self):
        self.rows[0], self.rows[-1] = self.rows[-1], self.rows[0]
        self.assertIn("invalid_event_order", self.errors())

    def test_partial_and_unsupported_cannot_publish_findings(self):
        for supported, text, outcome in ((False, "echo", "unsupported"), (True, "x" * 101, "partial")):
            with self.subTest(outcome=outcome):
                self.inputs[0].update(supported=supported, text=text)
                self.rows[1].update(outcome=outcome, input_tokens=0,
                                    topics=[{"topic_id": "topic.synthetic", "score": .9}])
                self.assertIn("withheld_findings", self.errors())

    def test_exact_token_boundary_and_withheld_overflow(self):
        self.rows[1]["input_tokens"] = 100
        self.assertEqual(self.errors(), [])
        self.rows[1].update(input_tokens=101, outcome="partial")
        self.assertEqual(self.errors(), [])

    def test_invalid_mentions_and_scores(self):
        self.rows[1]["mentions"] = [{"concept_id": "concept.synthetic", "start": True, "end": 4, "score": float("nan")}]
        self.assertIn("invalid_mention", self.errors())
        self.assertIn("invalid_score", self.errors())


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.reference = [{"example_id": "one", "split": "refinement", "text": "echo",
                           "language_assumption": "en", "strata": []}]
        self.inputs = [{"id": "one", "text": "echo", "supported": True}]

    def test_known_unsupported_stratum_overrides_english_assumption(self):
        self.reference[0]["strata"] = ["known_unsupported_language"]
        self.assertEqual(MEASURE.fixture_eligibility_errors(self.inputs, self.reference, "refinement"),
                         ["reference_eligibility_mismatch"])
        self.inputs[0]["supported"] = False
        self.assertEqual(MEASURE.fixture_eligibility_errors(self.inputs, self.reference, "refinement"), [])

    def test_gold_findings_and_uncertainty_do_not_filter_english_input(self):
        self.reference[0].update(topic_outcome="uncertain", mention_outcome="uncertain", topic_ids=[], mentions=[])
        self.assertEqual(MEASURE.fixture_eligibility_errors(self.inputs, self.reference, "refinement"), [])

    def test_source_text_and_membership_are_exact(self):
        self.inputs[0]["text"] = "changed"
        self.assertEqual(MEASURE.fixture_eligibility_errors(self.inputs, self.reference, "refinement"),
                         ["reference_eligibility_mismatch"])
        self.assertEqual(MEASURE.fixture_eligibility_errors([], self.reference, "refinement"),
                         ["reference_membership_mismatch"])


class SourceViewTests(unittest.TestCase):
    def setUp(self):
        self.freeze = {"files": {"reference.v3.json": "reference-hash"},
                       "split_members": {"refinement": ["one"]}}
        self.view = {"schema": "refinement-source-view.v1", "origin_reference_sha256": "reference-hash",
                     "origin_freeze_sha256": "freeze-hash", "records": [{"example_id": "one",
                     "split": "refinement", "text": "echo", "language_assumption": "en", "strata": []}]}

    def test_valid_projection(self):
        self.assertEqual(MEASURE.source_view_errors(self.view, self.freeze, "freeze-hash"), [])

    def test_gold_fields_are_rejected(self):
        self.view["records"][0]["topic_ids"] = []
        self.assertEqual(MEASURE.source_view_errors(self.view, self.freeze, "freeze-hash"),
                         ["invalid_source_view_fields"])

    def test_origin_and_membership_are_exact(self):
        self.view["origin_reference_sha256"] = "changed"
        self.assertEqual(MEASURE.source_view_errors(self.view, self.freeze, "freeze-hash"),
                         ["source_view_origin_mismatch"])
        self.view["origin_reference_sha256"] = "reference-hash"
        self.view["records"][0]["example_id"] = "other"
        self.assertEqual(MEASURE.source_view_errors(self.view, self.freeze, "freeze-hash"),
                         ["source_view_membership_mismatch"])


if __name__ == "__main__":
    unittest.main()
