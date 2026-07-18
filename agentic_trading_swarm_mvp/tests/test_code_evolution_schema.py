import unittest

from src.code_evolution import (
    _extract_single_json_object,
    _recommendation_or_paper_hold,
    _recommendation_schema_error,
)


class CodeEvolutionSchemaTests(unittest.TestCase):
    def test_extract_single_json_object_rejects_non_object_payloads(self) -> None:
        self.assertIsNone(_extract_single_json_object('["bad"]'))
        self.assertIsNone(_extract_single_json_object('{"a": 1} trailing'))

    def test_recommendation_schema_rejects_arrays_anywhere(self) -> None:
        payload = {
            "action": "hold",
            "priority": "medium",
            "title": "Valid title",
            "rationale": "Valid rationale",
            "market_key": "paper_only_cross_market_research",
            "evidence": {"constraint": "paper-only"},
            "proposed_change": {"summary": "keep it paper-only", "flags": ["bad"]},
        }
        self.assertIn("arrays are not allowed", _recommendation_schema_error(payload))

    def test_invalid_recommendation_falls_back_to_paper_hold(self) -> None:
        payload = {
            "action": "hold",
            "priority": "medium",
            "title": "Valid title",
            "rationale": "Valid rationale",
            "market_key": "paper_only_cross_market_research",
            "evidence": {"constraint": "paper-only"},
            "proposed_change": {"summary": "keep it paper-only", "flags": ["bad"]},
        }
        fallback = _recommendation_or_paper_hold(payload)
        self.assertEqual(fallback["action"], "hold")
        self.assertIn("paper-only", fallback["rationale"])


if __name__ == "__main__":
    unittest.main()
