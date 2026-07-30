import unittest
from unittest.mock import patch

try:
    import llm_state_packet as packet_module
except ImportError:  # pragma: no cover - package import fallback
    from src import llm_state_packet as packet_module


class StrategyLabPromotionPipelineTests(unittest.TestCase):
    def test_packet_surfaces_blocked_strategy_lab_candidate(self):
        opportunities = [
            {
                "strategy_lab_id": "lab-blocked",
                "candidate_source": "strategy_lab",
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
            },
            {
                "candidate_source": "radar",
                "venue": "BINANCE",
                "execution_surface": "spot",
                "direction": "long",
            },
        ]

        with patch.object(packet_module, "build_route_requirements_report", return_value={"ok": True}):
            fragment = packet_module.build_route_intelligence_packet_fragment(opportunities)

        self.assertTrue(fragment["paper_only"])
        self.assertEqual(fragment["route_intelligence_report"], {"ok": True})
        guard = fragment["strategy_lab_promotion_guard"]
        self.assertTrue(guard["enabled"])
        self.assertEqual(guard["candidate_count"], 1)
        self.assertEqual(guard["blocked_count"], 1)
        self.assertEqual(
            guard["status_counts"].get("blocked_pending_segment_evidence"),
            1,
        )
        self.assertEqual(guard["blocked_candidates"][0]["strategy_lab_id"], "lab-blocked")
        self.assertIn(
            "missing_target_segment_evidence",
            guard["blocked_candidates"][0]["blocker_reasons"],
        )

    def test_packet_counts_promotable_candidate_when_target_segment_evidence_exists(self):
        opportunities = [
            {
                "strategy_lab_id": "lab-promotable",
                "candidate_source": "strategy_lab",
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
                "strategy_lab_segment_evidence": [
                    {
                        "venue": "KRAKEN",
                        "surface": "perp",
                        "direction": "long",
                    }
                ],
            }
        ]

        with patch.object(packet_module, "build_route_requirements_report", return_value={"ok": True}):
            fragment = packet_module.build_route_intelligence_packet_fragment(opportunities)

        guard = fragment["strategy_lab_promotion_guard"]
        self.assertEqual(guard["candidate_count"], 1)
        self.assertEqual(guard["promotable_count"], 1)
        self.assertEqual(guard["blocked_count"], 0)
        self.assertEqual(
            guard["status_counts"].get("promotable_with_segment_evidence"),
            1,
        )
        self.assertEqual(guard["candidates"][0]["recommended_action"], "promote")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
