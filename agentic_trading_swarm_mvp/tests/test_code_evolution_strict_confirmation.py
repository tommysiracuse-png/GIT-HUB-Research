import unittest

from src.code_evolution import validate_strict_recommendation_schema


class StrictConfirmationSchemaTests(unittest.TestCase):
    def _base_packet(self):
        return {
            "action": "propose_code_change",
            "priority": 60,
            "title": "Tighten paper-trade entry confirmation",
            "rationale": "Paper-only tightening reduces low-conviction alerts.",
            "market_key": "paper_us_equities_intraday",
            "evidence": {"source": "planner_recovery"},
            "proposed_change": {"execution_mode": "paper_only"},
        }

    def test_accepts_paper_only_confirm_score_at_threshold(self):
        packet = self._base_packet()
        packet["variant_config"] = {"confirm_score_min": "0.68", "paper_only": "true"}

        ok, error = validate_strict_recommendation_schema(packet)

        self.assertTrue(ok)
        self.assertEqual(error, "")

    def test_rejects_paper_only_confirm_score_below_threshold(self):
        packet = self._base_packet()
        packet["variant_config"] = {"confirm_score_min": 0.5, "paper_only": "true"}

        ok, error = validate_strict_recommendation_schema(packet)

        self.assertFalse(ok)
        self.assertIn("confirm_score_min must be at least 0.68", error)

    def test_rejects_non_numeric_confirm_score(self):
        packet = self._base_packet()
        packet["variant_config"] = {"confirm_score_min": "high", "paper_only": "true"}

        ok, error = validate_strict_recommendation_schema(packet)

        self.assertFalse(ok)
        self.assertIn("confirm_score_min must be a number", error)


if __name__ == "__main__":
    unittest.main()
