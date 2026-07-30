import unittest

from src.route_intelligence import (
    build_conditional_paper_quality_gate,
    build_route_requirements_matrix,
)


class PaperOnlyFrontierRouteCapabilityGateTests(unittest.TestCase):
    def test_marks_frontier_spot_short_without_explicit_support_as_unsupported_or_unknown(self):
        opportunity = {
            "market_key": "frontier_crypto_routes",
            "inst_id": "GATE:ARC_USDT",
            "direction": "short",
        }

        row = build_route_requirements_matrix([opportunity])[0]

        self.assertEqual(row["route_status"], "unsupported_or_unknown")
        self.assertIn("spot_borrow", row["route_blockers"])
        self.assertTrue(row["borrow_required"])

    def test_frontier_spot_short_gate_emits_severe_penalty_action_until_supported(self):
        opportunity = {
            "market_key": "frontier_crypto_routes",
            "inst_id": "GATE:ARC_USDT",
            "direction": "short",
            "edge_bps_estimate": 12.5,
        }

        gate = build_conditional_paper_quality_gate([opportunity])

        self.assertEqual(gate["gate_count"], 1)
        top_example = gate["top_examples"][0]
        self.assertEqual(top_example["route_status"], "unsupported_or_unknown")
        self.assertIn("unsupported_or_unknown_frontier_spot_short_route", top_example["reasons"])
        self.assertEqual(top_example["paper_policy_action"], "apply_severe_ranking_penalty_paper_only")

    def test_supported_frontier_spot_short_route_keeps_requirement_metadata_without_gate(self):
        opportunity = {
            "market_key": "frontier_crypto_routes",
            "inst_id": "GATE:ARC_USDT",
            "direction": "short",
            "margin_supported": True,
            "borrow_supported": True,
        }

        row = build_route_requirements_matrix([opportunity])[0]
        gate = build_conditional_paper_quality_gate([opportunity])

        self.assertEqual(row["route_status"], "paper_observation_only")
        self.assertNotIn("spot_borrow", row["route_blockers"])
        self.assertTrue(row["borrow_required"])
        self.assertEqual(gate["gate_count"], 0)
