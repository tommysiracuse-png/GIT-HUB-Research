import unittest

from src.code_evolution import validate_strict_recommendation_schema


class StrictRecommendationSchemaGuardTests(unittest.TestCase):
    def test_rejects_incomplete_packet(self) -> None:
        ok, reason = validate_strict_recommendation_schema(
            {
                "action": "request_market_adapter",
                "priority": 60,
                "title": "Paper-only no-trade recommendation due to missing validated market signal",
            }
        )
        self.assertFalse(ok)
        self.assertIn("missing required fields", reason)

    def test_accepts_complete_packet(self) -> None:
        ok, reason = validate_strict_recommendation_schema(
            {
                "action": "request_market_adapter",
                "priority": 60,
                "title": "Paper-only no-trade recommendation due to missing validated market signal",
                "rationale": "The prior output was incomplete.",
                "market_key": "paper:global:no_valid_signal",
                "evidence": {"source_status": "unverified"},
                "proposed_change": {"decision": "maintain no position"},
            }
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")
