import unittest

from src.code_evolution import (
    _contains_exactly_one_json_object,
    _is_paper_scoped_market_key,
    _recommendation_schema_error,
    normalize_recommendation_response,
    validate_strict_recommendation_schema,
)


class CodeEvolutionRecommendationSchemaTests(unittest.TestCase):
    def test_market_key_must_be_paper_scoped(self) -> None:
        self.assertTrue(_is_paper_scoped_market_key("paper_cross_market_default"))
        self.assertTrue(_is_paper_scoped_market_key("paper.cross_market_default"))
        self.assertTrue(_is_paper_scoped_market_key("paper:cross_market_default"))
        self.assertFalse(_is_paper_scoped_market_key("live_cross_market_default"))

    def test_schema_rejects_non_paper_market_key(self) -> None:
        candidate = {
            "action": "propose_code_change",
            "priority": "90",
            "title": "t",
            "rationale": "r",
            "market_key": "live_cross_market_default",
            "evidence": "e",
            "proposed_change": "p",
        }
        self.assertEqual(_recommendation_schema_error(candidate), "market_key_out_of_scope")

    def test_exactly_one_json_object_guard_rejects_commentary(self) -> None:
        self.assertFalse(_contains_exactly_one_json_object('{"a":1} trailing commentary'))
        self.assertTrue(_contains_exactly_one_json_object('{"a":1}'))

    def test_normalize_recommendation_response_returns_valid_object_for_json_string(self) -> None:
        payload = (
            '{"action":"monitor_only","priority":1,"title":"Safe paper-only recommendation",'
            '"rationale":"Valid schema should pass through.","market_key":"paper_only_execution_route_hunter",'
            '"evidence":{"status":"ok"},"proposed_change":{"summary":"No live execution changes.","safety":"paper_only"}}'
        )

        normalized = normalize_recommendation_response(payload)

        valid, reason = validate_strict_recommendation_schema(normalized)
        self.assertTrue(valid, reason)
        self.assertEqual(normalized["action"], "monitor_only")

    def test_normalize_recommendation_response_falls_back_on_invalid_text(self) -> None:
        normalized = normalize_recommendation_response("not valid json")

        valid, reason = validate_strict_recommendation_schema(normalized)
        self.assertTrue(valid, reason)
        self.assertEqual(normalized["action"], "monitor_only")
        self.assertEqual(normalized["market_key"], "paper_global_macro_radar")


if __name__ == "__main__":
    unittest.main()
