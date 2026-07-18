import unittest

from src.frontier_crypto_adapter import paper_only_entry_confirmation_gate


class PaperOnlyEntryConfirmationGateTests(unittest.TestCase):
    def test_rejects_missing_inputs(self):
        result = paper_only_entry_confirmation_gate(entry_confidence=None)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "incomplete_confirmation_inputs")

    def test_rejects_below_threshold(self):
        result = paper_only_entry_confirmation_gate(
            entry_confidence=0.71,
            trend_confirmation=True,
            liquidity_confirmation=True,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "confidence_below_threshold")

    def test_requires_trend_and_liquidity_confirmation(self):
        result = paper_only_entry_confirmation_gate(
            entry_confidence=0.9,
            trend_confirmation=True,
            liquidity_confirmation=False,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "liquidity_confirmation_missing")

    def test_allows_eligible_paper_entry(self):
        result = paper_only_entry_confirmation_gate(
            entry_confidence=0.9,
            trend_confirmation=True,
            liquidity_confirmation=True,
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")
        self.assertEqual(result["entry_confidence"], 0.9)


if __name__ == "__main__":
    unittest.main()
