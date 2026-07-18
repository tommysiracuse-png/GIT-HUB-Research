import unittest

from src.frontier_data_quality import paper_only_timestamp_alignment_diagnostic


class PaperOnlyTimestampAlignmentDiagnosticTests(unittest.TestCase):
    def test_eligible_when_spot_and_perp_are_tightly_aligned(self):
        diagnostic = paper_only_timestamp_alignment_diagnostic(
            {
                "spot_timestamp": "2026-07-18T11:54:00Z",
                "perp_timestamp": "2026-07-18T11:54:01+00:00",
            },
            max_skew_seconds=2.0,
        )

        self.assertTrue(diagnostic["eligible"])
        self.assertEqual(diagnostic["reason"], "eligible")
        self.assertEqual(diagnostic["alignment_status"], "aligned")
        self.assertAlmostEqual(diagnostic["skew_seconds"], 1.0, places=6)

    def test_rejects_large_spot_perp_skew(self):
        diagnostic = paper_only_timestamp_alignment_diagnostic(
            {
                "spot_timestamp": "2026-07-18T11:54:00Z",
                "perp_timestamp": "2026-07-18T11:54:06Z",
            },
            max_skew_seconds=2.0,
        )

        self.assertFalse(diagnostic["eligible"])
        self.assertEqual(diagnostic["reason"], "skew_above_threshold")
        self.assertEqual(diagnostic["alignment_status"], "misaligned")
        self.assertAlmostEqual(diagnostic["skew_seconds"], 6.0, places=6)

    def test_uses_precomputed_skew_when_present(self):
        diagnostic = paper_only_timestamp_alignment_diagnostic(
            {"spot_perp_skew_seconds": 1.25},
            max_skew_seconds=2.0,
        )

        self.assertTrue(diagnostic["eligible"])
        self.assertEqual(diagnostic["reason"], "eligible")
        self.assertAlmostEqual(diagnostic["skew_seconds"], 1.25, places=6)


if __name__ == "__main__":
    unittest.main()
