import unittest

from src.frontier_crypto_adapter import paper_only_cross_market_confirmation_gate


class PaperOnlyCrossMarketConfirmationGateTest(unittest.TestCase):
    def test_requires_minimum_score(self):
        result = paper_only_cross_market_confirmation_gate(0.64, confirmation_age_seconds=10)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "below_minimum_score")

    def test_rejects_stale_confirmation(self):
        result = paper_only_cross_market_confirmation_gate(0.8, confirmation_age_seconds=121)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "stale_confirmation")

    def test_allows_fresh_strong_confirmation(self):
        result = paper_only_cross_market_confirmation_gate(0.7, confirmation_age_seconds=60)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")

    def test_rejects_neutral_alignment_by_default(self):
        result = paper_only_cross_market_confirmation_gate(0.0, confirmation_age_seconds=1)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "neutral_alignment_disallowed")

    def test_can_allow_neutral_alignment_when_flagged(self):
        result = paper_only_cross_market_confirmation_gate(
            0.0,
            confirmation_age_seconds=1,
            allow_neutral_alignment=True,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "below_minimum_score")

