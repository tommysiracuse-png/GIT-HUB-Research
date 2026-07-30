import unittest

try:
    from llm_state_packet import assess_strategy_lab_promotion_guard
except ImportError:  # pragma: no cover - package import fallback
    from src.llm_state_packet import assess_strategy_lab_promotion_guard


class StrategyLabSegmentGuardTests(unittest.TestCase):
    def test_exact_segment_is_promotable_without_extra_evidence(self):
        result = assess_strategy_lab_promotion_guard(
            {
                "strategy_lab_id": "lab-exact",
                "venue": "binance",
                "execution_surface": "perp",
                "direction": "long",
            }
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["guard_status"], "promotable_exact_segment")
        self.assertEqual(result["promotion_scope"], "exact_segment")
        self.assertEqual(result["recommended_action"], "promote")
        self.assertEqual(result["blocker_reasons"], [])

    def test_mismatched_target_segment_blocks_without_matching_evidence(self):
        result = assess_strategy_lab_promotion_guard(
            {
                "strategy_lab_id": "lab-blocked",
                "source_segment": {
                    "venue": "BINANCE",
                    "surface": "perp",
                    "direction": "long",
                },
                "target_segment": {
                    "venue": "KRAKEN",
                    "surface": "spot",
                    "direction": "short",
                },
            }
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["guard_status"], "blocked_pending_segment_evidence")
        self.assertEqual(result["promotion_scope"], "local_only")
        self.assertEqual(result["recommended_action"], "keep_local")
        self.assertIn("venue_mismatch", result["blocker_reasons"])
        self.assertIn("surface_mismatch", result["blocker_reasons"])
        self.assertIn("direction_mismatch", result["blocker_reasons"])
        self.assertIn("missing_target_segment_evidence", result["blocker_reasons"])

    def test_explicit_target_segment_evidence_allows_cross_segment_promotion(self):
        result = assess_strategy_lab_promotion_guard(
            {
                "strategy_lab_id": "lab-evidence",
                "source_segment": {
                    "venue": "BINANCE",
                    "surface": "perp",
                    "direction": "long",
                },
                "target_segment": {
                    "venue": "KRAKEN",
                    "surface": "perp",
                    "direction": "long",
                },
                "paper_evidence_segments": [
                    {
                        "venue": "KRAKEN",
                        "execution_surface": "perp",
                        "direction": "long",
                    }
                ],
            }
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["guard_status"], "promotable_with_segment_evidence")
        self.assertEqual(result["promotion_scope"], "validated_target_segment")
        self.assertEqual(result["recommended_action"], "promote")
        self.assertTrue(result["target_segment_evidence_found"])
        self.assertEqual(result["blocker_reasons"], [])

    def test_non_strategy_lab_items_remain_unannotated(self):
        result = assess_strategy_lab_promotion_guard(
            {
                "venue": "BINANCE",
                "execution_surface": "perp",
                "direction": "long",
                "candidate_source": "radar",
            }
        )

        self.assertIsNone(result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
