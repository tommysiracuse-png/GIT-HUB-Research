import unittest

from src.code_evolution import normalize_recommendation_response, validate_strict_recommendation_schema


class CodeEvolutionRecommendationSchemaTests(unittest.TestCase):
    def test_normalize_recommendation_response_returns_valid_object_for_json_string(self):
        payload = (
            '{"action":"monitor_only","priority":1,"title":"Safe paper-only recommendation",'
            '"rationale":"Valid schema should pass through.","market_key":"paper_only_execution_route_hunter",'
            '"evidence":{"status":"ok"},"proposed_change":{"summary":"No live execution changes.","safety":"paper_only"}}'
        )

        normalized = normalize_recommendation_response(payload)

        valid, reason = validate_strict_recommendation_schema(normalized)
        self.assertTrue(valid, reason)
        self.assertEqual(normalized["action"], "monitor_only")

    def test_normalize_recommendation_response_falls_back_on_invalid_text(self):
        normalized = normalize_recommendation_response("not valid json")

        valid, reason = validate_strict_recommendation_schema(normalized)
        self.assertTrue(valid, reason)
        self.assertEqual(normalized["action"], "monitor_only")
        self.assertEqual(normalized["market_key"], "paper_global_macro_radar")


if __name__ == "__main__":
    unittest.main()
