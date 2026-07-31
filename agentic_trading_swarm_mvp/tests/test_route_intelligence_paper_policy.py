import unittest

from src.route_intelligence import build_route_feasibility_summary, build_route_requirements_matrix


class RouteIntelligencePaperPolicyTests(unittest.TestCase):
    def test_conditional_candidate_is_annotated_with_route_cost_and_requirements(self):
        rows = build_route_requirements_matrix(
            [
                {
                    "venue": "OKX",
                    "inst_id": "OKX:BTC-USDT",
                    "market_key": "OKX|perp_funding_basis|long_perp_short_spot|conditional",
                    "strategy_profile": "perp_funding_basis long_perp_short_spot conditional",
                    "direction": "long_perp_short_spot",
                    "route_blockers": ["spot_borrow"],
                    "proxy_supported": True,
                    "fee_bps_per_side": 2.0,
                    "slippage_bps_per_side": 1.0,
                    "borrow_fee_bps_estimate": 8.0,
                }
            ]
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["route_type"], "long_perp_short_spot_conditional")
        self.assertTrue(row["route_feasible_paper"])
        self.assertEqual(row["api_surface_required"], "public_plus_margin_and_borrow")
        self.assertTrue(row["requires_spot_borrow"])
        self.assertTrue(row["requires_margin_permission"])
        self.assertEqual(row["route_cost_bps_paper"], 26.5)
        self.assertIn("borrow_cost", row["route_cost_reason_codes"])
        self.assertIn("paper_proxy_basis_risk", row["route_cost_reason_codes"])

    def test_feasibility_summary_includes_route_type_and_cost_rollup(self):
        summary = build_route_feasibility_summary(
            [
                {
                    "venue": "OKX",
                    "inst_id": "OKX:BTC-USDT",
                    "market_key": "OKX|perp_funding_basis|long_perp_short_spot|conditional",
                    "strategy_profile": "perp_funding_basis long_perp_short_spot conditional",
                    "direction": "long_perp_short_spot",
                    "route_blockers": ["spot_borrow"],
                    "proxy_supported": True,
                    "fee_bps_per_side": 2.0,
                    "slippage_bps_per_side": 1.0,
                    "borrow_fee_bps_estimate": 8.0,
                },
                {
                    "venue": "OKX",
                    "inst_id": "OKX:ETH-USDT",
                    "market_key": "OKX|perp_funding_basis|long_perp_short_spot|conditional",
                    "strategy_profile": "perp_funding_basis long_perp_short_spot conditional",
                    "direction": "long_perp_short_spot",
                    "route_blockers": ["spot_borrow"],
                    "proxy_supported": False,
                },
            ]
        )
        self.assertEqual(summary["counts_by_feasibility"]["blocked"], 1)
        self.assertEqual(summary["counts_by_feasibility"]["proxy_only"], 1)
        self.assertEqual(summary["counts_by_route_type"]["long_perp_short_spot_conditional"], 2)
        self.assertEqual(summary["estimated_route_cost_bps"]["count"], 1)
        self.assertEqual(summary["estimated_route_cost_bps"]["average"], 26.5)


if __name__ == "__main__":
    unittest.main()
