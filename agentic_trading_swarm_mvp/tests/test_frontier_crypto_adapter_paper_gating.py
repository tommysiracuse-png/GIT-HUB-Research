import unittest

from src.frontier_crypto_adapter import paper_only_radar_alert_gate


class PaperOnlyRadarAlertGateTest(unittest.TestCase):
    def test_rejects_missing_required_fields(self):
        result = paper_only_radar_alert_gate({"paper_mode": True})
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "missing_required_fields")
        self.assertIn("confidence", result["missing_required_fields"])

    def test_rejects_non_paper_mode(self):
        result = paper_only_radar_alert_gate({"paper_mode": False, "confidence": 0.9})
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "paper_mode_disabled")

    def test_rejects_below_minimum_confidence(self):
        result = paper_only_radar_alert_gate({"paper_mode": True, "confidence": 0.67})
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "below_minimum_confidence")

    def test_accepts_paper_mode_with_sufficient_confidence(self):
        result = paper_only_radar_alert_gate({"paper_mode": True, "confidence": 0.68})
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")


if __name__ == "__main__":
    unittest.main()
