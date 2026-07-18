import unittest

from src.frontier_crypto_adapter import paper_only_cross_market_confirmation_gate


class PaperOnlyCrossMarketConfirmationGateTests(unittest.TestCase):
    def test_requires_stronger_default_confirmation(self):
        result = paper_only_cross_market_confirmation_gate(0.66, confirmation_age_seconds=30)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "below_minimum_score")

    def test_accepts_fresh_high_confirmation(self):
        result = paper_only_cross_market_confirmation_gate(0.70, confirmation_age_seconds=30)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")

    def test_rejects_stale_confirmation(self):
        result = paper_only_cross_market_confirmation_gate(0.90, confirmation_age_seconds=121)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "stale_confirmation")


if __name__ == "__main__":
    unittest.main()
