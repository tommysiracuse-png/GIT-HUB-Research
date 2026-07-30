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
