import unittest

from src.code_evolution import coerce_paper_only_recommendation


class CodeEvolutionContractTests(unittest.TestCase):
    def test_coerce_paper_only_recommendation_rejects_truncated_output(self) -> None:
        result = coerce_paper_only_recommendation('{"action":"propose_code_change"')
        self.assertEqual(result["market_key"], "paper.execution_route_hunter.output_contract")
        self.assertIn("fallback", result["title"].lower())

    def test_coerce_paper_only_recommendation_passes_through_valid_packet(self) -> None:
        payload = {
            "action": "propose_code_change",
            "priority": 60,
            "title": "Valid packet",
            "rationale": "OK",
            "market_key": "paper.global.execution_route_hunter",
            "evidence": {"source": "unit_test"},
            "proposed_change": {"summary": "Keep paper-only"},
        }
        self.assertEqual(coerce_paper_only_recommendation(payload), payload)

    def test_coerce_paper_only_recommendation_falls_back_on_schema_violation(self) -> None:
        payload = {
            "action": "propose_code_change",
            "priority": 60,
            "title": "Bad packet",
            "rationale": "OK",
            "market_key": "paper.global.execution_route_hunter",
            "evidence": [],
            "proposed_change": {"summary": "Keep paper-only"},
        }
        result = coerce_paper_only_recommendation(payload)
        self.assertEqual(result["priority"], 0)
        self.assertIn("evidence must be a JSON object", result["evidence"]["schema_error"])
