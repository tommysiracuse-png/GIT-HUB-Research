import unittest

from src.code_evolution import (
    normalize_recommendation_response,
    validate_strict_recommendation_schema,
)


class CodeEvolutionStrictOutputTests(unittest.TestCase):
    def test_validate_accepts_single_object_recommendation(self) -> None:
        packet = {
            "action": "monitor_only",
            "priority": 10,
            "title": "Valid paper-only recommendation",
            "rationale": "Keeps the workflow paper-only.",
            "market_key": "paper_global_market_radar",
            "evidence": {"status": "ok"},
            "proposed_change": {"summary": "No-op", "safety": "paper_only"},
        }
        valid, reason = validate_strict_recommendation_schema(packet)
        self.assertTrue(valid, reason)

    def test_validate_rejects_array_values(self) -> None:
        packet = {
            "action": "monitor_only",
            "priority": 10,
            "title": "Invalid recommendation",
            "rationale": "Arrays are forbidden.",
            "market_key": "paper_global_market_radar",
            "evidence": {"status": "ok", "signals": [1, 2]},
            "proposed_change": {"summary": "No-op", "safety": "paper_only"},
        }
        valid, reason = validate_strict_recommendation_schema(packet)
        self.assertFalse(valid)
        self.assertIn("array values are not allowed", reason)

    def test_normalize_rejects_markdown_wrapped_response(self) -> None:
        response = "```json\n{\"action\": \"monitor_only\"}\n```"
        normalized = normalize_recommendation_response(response)
        self.assertEqual(normalized["action"], "monitor_only")
        self.assertEqual(normalized["market_key"], "paper_global_macro_radar")
