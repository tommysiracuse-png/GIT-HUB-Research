import unittest

from src.frontier_crypto_adapter import paper_only_cross_market_signal_quality_gate


class PaperCrossMarketSignalQualityGateTests(unittest.TestCase):
    def test_promotes_only_when_all_paper_conditions_are_met(self):
        result = paper_only_cross_market_signal_quality_gate(
            confidence=0.71,
            primary_trigger_present=True,
            related_market_confirmed=True,
            signal_age_ms=1000.0,
        )

        self.assertTrue(result["promote"])
        self.assertFalse(result["observe_only"])
        self.assertEqual(result["state"], "promoted")

    def test_below_threshold_stays_observe_only(self):
        result = paper_only_cross_market_signal_quality_gate(
            confidence=0.67,
            primary_trigger_present=True,
            related_market_confirmed=True,
            signal_age_ms=1000.0,
        )

        self.assertFalse(result["promote"])
        self.assertTrue(result["observe_only"])
        self.assertEqual(result["state"], "observe_only")

    def test_missing_confirmation_does_not_promote(self):
        result = paper_only_cross_market_signal_quality_gate(
            confidence=0.90,
            primary_trigger_present=True,
            related_market_confirmed=False,
            signal_age_ms=1000.0,
        )

        self.assertFalse(result["promote"])
        self.assertTrue(result["observe_only"])


if __name__ == "__main__":
    unittest.main()
