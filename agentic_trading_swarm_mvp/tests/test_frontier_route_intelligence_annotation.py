import unittest

from src.route_intelligence import build_route_feasibility_summary, build_route_requirements_matrix


class PaperOnlyRouteIntelligenceAnnotationTests(unittest.TestCase):
    def test_conditional_spot_short_profile_reports_margin_and_borrow_requirements(self):
        candidate = {
            "inst_id": "OKX:BTC-USDT",
            "direction": "short",
            "market_key": "STRATEGY_LAB|route_rich_frontier_long_filter_2942c975|OKX_SPOT|short_frontier_spot|conditional",
            "strategy_profile": "conditional_spot_short",
            "requires_spot_borrow": True,
            "requires_margin_permission": True,
            "fee_bps_per_side_or_unknown": 8.5,
            "api_surface_required": "margin_api",
        }

        row = build_route_requirements_matrix([candidate])[0]

        self.assertEqual(row["feasibility_state"], "requires_borrow")
        self.assertEqual(row["venue_supports_margin_or_equivalent"], "unknown")
        self.assertEqual(row["shortable_inventory_declared"], "unknown")
        self.assertEqual(row["borrow_cost_model_present"], "unknown")
        self.assertEqual(row["fees_modeled"], "satisfied")
        self.assertEqual(row["order_api_surface_mapped"], "satisfied")
        self.assertEqual(row["paper_recommendation_action"], "downgrade_confidence_and_label_unverified_route")
        self.assertEqual(row["paper_recommendation_reason"], "venue_supports_margin_or_equivalent_unverified")
        self.assertTrue(row["route_requirement_checklist_complete"])
        self.assertEqual(
            set(row["route_requirement_checklist"]),
            {"broker_permissions", "borrow_availability", "fees", "margin", "api_coverage"},
        )
        self.assertTrue(row["route_requirement_checklist"]["broker_permissions"]["read_only"])

    def test_funding_capture_profile_reports_perp_and_collateral_requirements(self):
        candidate = {
            "inst_id": "OKX:ETH-USDT-SWAP",
            "direction": "long",
            "market_key": "frontier_crypto_routes",
            "route_type": "perp_carry",
        }

        row = build_route_requirements_matrix([candidate])[0]

        self.assertEqual(row["paper_recommendation_action"], "allow_paper_evaluation")
        self.assertEqual(row["venue_supports_margin_or_equivalent"], "not_applicable")
        self.assertEqual(row["shortable_inventory_declared"], "not_applicable")
        self.assertEqual(row["borrow_cost_model_present"], "not_applicable")
        self.assertEqual(row["fees_modeled"], "not_applicable")
        self.assertEqual(row["order_api_surface_mapped"], "not_applicable")

    def test_route_lookup_annotates_route_status_in_place(self):
        unverified_short = {
            "inst_id": "OKX:XRP-USDT",
            "direction": "short",
            "market_key": "STRATEGY_LAB|route_rich_frontier_long_filter_2942c975|OKX_SPOT|short_frontier_spot|conditional",
            "strategy_profile": "conditional_spot_short",
            "requires_spot_borrow": True,
            "requires_margin_permission": True,
            "fee_bps_per_side_or_unknown": 6.0,
            "api_surface_required": "margin_api",
        }
        explicit_missing = {
            "inst_id": "OKX:ARB-USDT",
            "direction": "short",
            "market_key": "STRATEGY_LAB|route_rich_frontier_long_filter_2942c975|OKX_SPOT|short_frontier_spot|conditional",
            "strategy_profile": "conditional_spot_short",
            "requires_spot_borrow": True,
            "requires_margin_permission": True,
            "shortable_inventory_declared": False,
            "borrow_cost_model_present": False,
            "fees_modeled": False,
            "order_api_surface_mapped": False,
        }
        direct_long = {
            "inst_id": "BINANCE:SOL-USDT",
            "direction": "long",
            "market_key": "frontier_crypto_routes",
            "route_type": "direct_market_access",
        }

        summary = build_route_feasibility_summary([unverified_short, explicit_missing, direct_long])

        self.assertEqual(summary["counts_by_paper_recommendation_action"]["allow_paper_evaluation"], 1)
        self.assertEqual(summary["counts_by_paper_recommendation_action"]["downgrade_confidence_and_label_unverified_route"], 1)
        self.assertEqual(summary["counts_by_paper_recommendation_action"]["suppress_from_paper_recommendations"], 1)
import unittest

from src.frontier_crypto_adapter import _paper_only_route_review_lookup
from src.frontier_data_quality import paper_only_route_requirement_profile


class PaperOnlyRouteIntelligenceAnnotationTests(unittest.TestCase):
    def test_conditional_spot_short_profile_reports_margin_and_borrow_requirements(self):
        route_status = {
            "route_id": "OKX_SPOT|frontier_crypto_venue_map|short_frontier_spot|conditional",
            "venue": "OKX_SPOT",
            "market_surface": "spot",
            "direction": "short",
        }
        profile = paper_only_route_requirement_profile(route_status)
        self.assertEqual(profile.get("broker_surface"), "okx:spot")
        self.assertTrue(profile.get("spot_short_requires_margin_and_borrow"))
        self.assertEqual(profile.get("api_surface"), "public_market_data_only")
        self.assertIn("margin_enabled", profile.get("required_permissions", []))
        self.assertIn("borrow_inventory_access", profile.get("required_permissions", []))
        self.assertEqual(
            profile.get("route_requirement_status"),
            "supported_with_margin_and_borrow_requirements",
        )

    def test_funding_capture_profile_reports_perp_and_collateral_requirements(self):
        route_status = {
            "route_id": "OKX|perp_funding_basis|funding_capture_long_perp|conditional",
            "venue": "OKX",
            "market_surface": "perp",
            "direction": "long",
        }
        profile = paper_only_route_requirement_profile(route_status)
        self.assertEqual(profile.get("broker_surface"), "okx:perp")
        self.assertTrue(profile.get("funding_capture_requires_perp_access"))
        self.assertTrue(profile.get("collateral_transfer_required"))
        self.assertIn("perpetuals_enabled", profile.get("required_permissions", []))
        self.assertIn("collateral_transfer_capability", profile.get("required_permissions", []))
        self.assertEqual(
            profile.get("public_fee_assumptions"),
            "public_perp_taker_fee_and_funding_rate_estimate",
        )

    def test_route_lookup_annotates_route_status_in_place(self):
        route_status = {
            "route_id": "OKX_SPOT|frontier_crypto_venue_map|short_frontier_spot|conditional",
            "venue": "OKX_SPOT",
            "market_surface": "spot",
            "direction": "short",
        }
        summary = _paper_only_route_review_lookup(route_status, "route_requirement_summary")
        self.assertIsInstance(summary, str)
        self.assertIn("spot_short_requires_margin_and_borrow", summary)
        self.assertIn("route_requirements", route_status)
        self.assertEqual(
            route_status.get("route_requirement_status"),
            "supported_with_margin_and_borrow_requirements",
        )


if __name__ == "__main__":
    unittest.main()
