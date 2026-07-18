import unittest

from src.code_evolution import _recommendation_or_paper_hold, _recommendation_schema_error


class CodeEvolutionStrictValidationTests(unittest.TestCase):
    def test_incomplete_recommendation_defaults_to_paper_only_hold(self) -> None:
        payload = {
            "action": "hold",
            "priority": "medium",
            "title": "",
            "rationale": " ",
            "market_key": "paper_only.cross_market",
            "evidence": {},
            "proposed_change": {},
        }

        finalized = _recommendation_or_paper_hold(payload)

        self.assertEqual(finalized["action"], "hold")
        self.assertEqual(finalized["market_key"], "paper_only.cross_market")
        self.assertIn("validation_error", finalized["evidence"])
        self.assertIn("title must contain a non-empty value", finalized["evidence"]["validation_error"])

    def test_schema_error_rejects_empty_required_fields(self) -> None:
        payload = {
            "action": "",
            "priority": "medium",
            "title": "Valid",
            "rationale": "Valid",
            "market_key": "paper_only.cross_market",
            "evidence": {},
            "proposed_change": {},
        }

        self.assertEqual(_recommendation_schema_error(payload), "action must contain a non-empty value")
