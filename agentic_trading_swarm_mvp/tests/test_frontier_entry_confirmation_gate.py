import unittest

from src.frontier_crypto_adapter import paper_only_entry_confirmation_gate


class TestPaperOnlyEntryConfirmationGate(unittest.TestCase):
    def test_rejects_below_confidence_threshold(self):
        result = paper_only_entry_confirmation_gate(
            entry_confidence=0.71,
            trend_confirmation=True,
            liquidity_confirmation=True,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "confidence_below_threshold")

    def test_rejects_missing_trend_confirmation(self):
        result = paper_only_entry_confirmation_gate(
            entry_confidence=0.9,
            trend_confirmation=False,
            liquidity_confirmation=True,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "trend_confirmation_missing")

    def test_rejects_missing_liquidity_confirmation(self):
        result = paper_only_entry_confirmation_gate(
            entry_confidence=0.9,
            trend_confirmation=True,
            liquidity_confirmation=False,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "liquidity_confirmation_missing")

    def test_accepts_confirmed_entry(self):
        result = paper_only_entry_confirmation_gate(
            entry_confidence=0.72,
            trend_confirmation=True,
            liquidity_confirmation=True,
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")


if __name__ == "__main__":
    unittest.main()
