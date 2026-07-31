import unittest
from unittest.mock import patch

from src import route_intelligence


class PaperOnlyRouteFeasibilityStateTests(unittest.TestCase):
    def test_matrix_annotates_unsupported_route_state_and_friction(self):
        opportunity = {"inst_id": "BTC-USDT", "direction": "short"}
        with patch.object(
            route_intelligence,
            "_build_route_requirement_row",
            return_value={"inst_id": "BTC-USDT", "venue": "OKX", "direction": "short"},
        ), patch.object(route_intelligence, "_route_priority_key", return_value=0), patch.object(
            route_intelligence,
            "_route_blockers",
            return_value=["spot_borrow"],
        ), patch.object(
            route_intelligence,
            "_paper_proxy_route",
            return_value="not_applicable",
        ), patch.object(
            route_intelligence,
            "_paper_feasibility",
            return_value="blocked",
        ), patch.object(
            route_intelligence,
            "_route_feasible_paper",
            return_value=False,
        ), patch.object(
            route_intelligence,
            "_paper_route_cost_bps",
            return_value=(33.25, ["blocked_route_penalty"]),
        ), patch.object(
            route_intelligence,
            "_requires_spot_borrow",
            return_value=True,
        ), patch.object(
            route_intelligence,
            "_requires_margin_permission",
            return_value=True,
        ), patch.object(
            route_intelligence,
            "_frontier_spot_short_capability_confirmed",
            return_value=False,
        ):
            row = route_intelligence.build_route_requirements_matrix([opportunity])[0]
        self.assertEqual(row["feasibility_state"], "unsupported")
        self.assertEqual(row["route_friction_bps"], 33.25)

    def test_summary_counts_requires_borrow_when_support_is_unconfirmed(self):
        opportunity = {"inst_id": "ETH-USDT", "direction": "short"}
        with patch.object(route_intelligence, "_route_blockers", return_value=["spot_borrow"]), patch.object(
            route_intelligence,
            "_paper_proxy_route",
            return_value="not_applicable",
        ), patch.object(
            route_intelligence,
            "_paper_feasibility",
            return_value="direct_feasible",
        ), patch.object(
            route_intelligence,
            "_route_type",
            return_value="long_perp_short_spot_conditional",
        ), patch.object(
            route_intelligence,
            "_route_feasible_paper",
            return_value=True,
        ), patch.object(
            route_intelligence,
            "_paper_route_cost_bps",
            return_value=(14.5, ["borrow_cost"]),
        ), patch.object(
            route_intelligence,
            "_requires_spot_borrow",
            return_value=True,
        ), patch.object(
            route_intelligence,
            "_requires_margin_permission",
            return_value=True,
        ), patch.object(
            route_intelligence,
            "_frontier_spot_short_capability_confirmed",
            return_value=False,
        ):
            summary = route_intelligence.build_route_feasibility_summary([opportunity])
        self.assertEqual(summary["counts_by_feasibility"], {"direct_feasible": 1})
        self.assertEqual(summary["counts_by_feasibility_state"], {"requires_borrow": 1})
        self.assertEqual(summary["estimated_route_cost_bps"]["count"], 1)
        self.assertEqual(summary["estimated_route_cost_bps"]["average"], 14.5)

    def test_quality_gate_adds_unsupported_route_reason(self):
        opportunity = {"inst_id": "SOL-USDT", "direction": "short", "edge_bps_estimate": 8.0}
        with patch.object(route_intelligence, "_conditional_gate_reasons", return_value=[]), patch.object(
            route_intelligence,
            "_paper_route_status",
            return_value="blocked",
        ), patch.object(route_intelligence, "_route_blockers", return_value=["spot_borrow"]), patch.object(
            route_intelligence,
            "_paper_proxy_route",
            return_value="not_applicable",
        ), patch.object(
            route_intelligence,
            "_paper_feasibility",
            return_value="blocked",
        ), patch.object(
            route_intelligence,
            "_route_feasible_paper",
            return_value=False,
        ), patch.object(
            route_intelligence,
            "_paper_route_cost_bps",
            return_value=(28.0, ["blocked_route_penalty"]),
        ), patch.object(
            route_intelligence,
            "_requires_spot_borrow",
            return_value=True,
        ), patch.object(
            route_intelligence,
            "_requires_margin_permission",
            return_value=True,
        ), patch.object(
            route_intelligence,
            "_frontier_spot_short_capability_confirmed",
            return_value=False,
        ), patch.object(route_intelligence, "_venue", return_value="OKX"):
            gate = route_intelligence.build_conditional_paper_quality_gate([opportunity])
        self.assertEqual(gate["gate_count"], 1)
        self.assertEqual(gate["reason_counts"]["unsupported_route"], 1)
        self.assertEqual(gate["top_examples"][0]["feasibility_state"], "unsupported")
        self.assertEqual(gate["top_examples"][0]["route_friction_bps"], 28.0)
