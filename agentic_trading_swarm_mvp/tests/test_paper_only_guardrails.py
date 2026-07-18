import unittest

from src.frontier_crypto_adapter import paper_only_validate_recommendation


class PaperOnlyGuardrailsTests(unittest.TestCase):
    def test_rejects_live_destination_and_tags_simulation_only(self):
        result = paper_only_validate_recommendation(
            recommendation={"signal": "buy", "confidence": 0.91, "live_route": True},
            execution_destination="prod",
        )

        self.assertTrue(result["paper_only"])
        self.assertTrue(result["simulation_only"])
        self.assertTrue(result["rejected"])
        self.assertEqual(result["action"], "no_op")
        self.assertIsNone(result["execution_destination"])
        self.assertEqual(result["warning"], "live destination rejected")

    def test_defaults_uncertain_recommendation_to_no_op(self):
        result = paper_only_validate_recommendation(
            recommendation={"signal": "", "confidence": None},
            execution_destination="paper",
        )

        self.assertTrue(result["paper_only"])
        self.assertTrue(result["simulation_only"])
        self.assertFalse(result["approved"])
        self.assertEqual(result["action"], "no_op")
        self.assertEqual(result["execution_destination"], "paper")
        self.assertEqual(result["warning"], "uncertain recommendation defaulted to no-op")

    def test_allows_clear_paper_recommendation(self):
        result = paper_only_validate_recommendation(
            recommendation={"signal": "sell", "confidence": 0.73},
            execution_destination="paper",
        )

        self.assertTrue(result["paper_only"])
        self.assertTrue(result["simulation_only"])
        self.assertTrue(result["approved"])
        self.assertFalse(result["rejected"])
        self.assertEqual(result["action"], "simulate_only")
        self.assertEqual(result["execution_destination"], "paper")
        self.assertIsNone(result["warning"])


if __name__ == "__main__":
    unittest.main()
