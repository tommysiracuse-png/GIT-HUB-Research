import unittest

from src.frontier_crypto_adapter import paper_only_cross_market_confirmation_gate


class CrossMarketConfirmationGateTests(unittest.TestCase):
    def test_requires_score_and_freshness(self):
        result = paper_only_cross_market_confirmation_gate(0.82, confirmation_age_seconds=90)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")

    def test_rejects_low_score(self):
        result = paper_only_cross_market_confirmation_gate(0.5, confirmation_age_seconds=30)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "below_minimum_score")

    def test_rejects_neutral_alignment_by_default(self):
        result = paper_only_cross_market_confirmation_gate(0.0, confirmation_age_seconds=30)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "neutral_alignment_disallowed")

    def test_rejects_stale_confirmation(self):
        result = paper_only_cross_market_confirmation_gate(0.9, confirmation_age_seconds=999)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "stale_confirmation")


if __name__ == "__main__":
    unittest.main()
