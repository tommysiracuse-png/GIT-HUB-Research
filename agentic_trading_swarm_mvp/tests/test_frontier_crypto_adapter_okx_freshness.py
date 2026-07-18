import unittest

from src.frontier_crypto_adapter import paper_only_signal_freshness_audit


class PaperOnlySignalFreshnessAuditTests(unittest.TestCase):
    def test_reports_tight_source_alignment_from_timestamps(self):
        audit = paper_only_signal_freshness_audit(
            {
                "data_age_seconds": 45,
                "forecast_horizon_seconds": 120,
                "spot_timestamp": "2026-07-18T11:54:00Z",
                "perp_timestamp": "2026-07-18T11:54:01.500000+00:00",
            },
            max_source_skew_seconds=2.0,
            horizon_label="mean_reversion",
        )

        self.assertTrue(audit["eligible"])
        self.assertEqual(audit["reason"], "eligible")
        self.assertEqual(audit["source_alignment_status"], "aligned")
        self.assertTrue(audit["source_alignment_eligible"])
        self.assertAlmostEqual(audit["source_skew_seconds"], 1.5, places=6)
        self.assertEqual(audit["horizon_alignment"], "aligned")

    def test_reports_source_misalignment_from_precomputed_skew(self):
        audit = paper_only_signal_freshness_audit(
            {
                "data_age_seconds": 20,
                "holding_period_seconds": 180,
                "spot_perp_skew_seconds": 5.5,
            },
            max_source_skew_seconds=2.0,
        )

        self.assertTrue(audit["eligible"])
        self.assertEqual(audit["reason"], "eligible")
        self.assertEqual(audit["source_alignment_status"], "misaligned")
        self.assertFalse(audit["source_alignment_eligible"])
        self.assertAlmostEqual(audit["source_skew_seconds"], 5.5, places=6)

    def test_invalid_payload_keeps_new_alignment_fields(self):
        audit = paper_only_signal_freshness_audit(None)

        self.assertFalse(audit["eligible"])
        self.assertEqual(audit["reason"], "invalid_payload")
        self.assertEqual(audit["source_alignment_status"], "unknown")
        self.assertIsNone(audit["source_skew_seconds"])


if __name__ == "__main__":
    unittest.main()
