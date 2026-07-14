import unittest

from src.recommendation_schema import (
    paper_only_fallback_recommendation,
    validate_recommendation_object,
)


class RecommendationSchemaTests(unittest.TestCase):
    def test_fallback_recommendation_has_required_keys(self) -> None:
        payload = paper_only_fallback_recommendation()

        self.assertTrue(validate_recommendation_object(payload))
        self.assertEqual(payload["action"], "hold")
        self.assertEqual(payload["priority"], "medium")
        self.assertEqual(payload["proposed_change"]["paper_trade_instruction"], "Simulation only; no execution.")

    def test_validation_rejects_incomplete_payload(self) -> None:
        payload = {
            "action": "hold",
            "priority": "medium",
            "title": "Incomplete",
        }

        self.assertFalse(validate_recommendation_object(payload))
