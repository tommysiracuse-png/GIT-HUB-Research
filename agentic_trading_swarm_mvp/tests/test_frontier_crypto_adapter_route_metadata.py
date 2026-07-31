import unittest

from src import frontier_crypto_adapter


class PaperOnlyRouteMetadataPacketTests(unittest.TestCase):
    def setUp(self):
        self._original_profile = frontier_crypto_adapter.paper_only_route_requirement_profile

    def tearDown(self):
        frontier_crypto_adapter.paper_only_route_requirement_profile = self._original_profile

    def test_supported_basis_route_builds_actionable_route_packet(self):
        def fake_profile(_route_status):
            return {
                "summary": "Funding basis capture using spot and perp legs",
                "route_requirement_status": "supported",
                "api_surface": "public_market_data",
                "broker_surface": "okx",
                "perp_support": "supported",
                "basis_support": "supported",
                "fee_reference": "public_taker_fee_schedule",
                "required_permissions": ["spot", "perp"],
            }

        frontier_crypto_adapter.paper_only_route_requirement_profile = fake_profile
        route_status = {"venue": "OKX"}

        annotated = frontier_crypto_adapter._paper_only_annotate_route_intelligence(route_status)
        packet = annotated.get("route_requirements_packet") or {}

        self.assertEqual(packet.get("required_side"), "spot_plus_perp")
        self.assertGreaterEqual(packet.get("route_confidence", 0.0), 0.75)
        self.assertEqual(packet.get("borrow_confidence"), 1.0)
        self.assertGreaterEqual(packet.get("fee_confidence", 0.0), 0.75)
        self.assertGreaterEqual(packet.get("api_surface_confidence", 0.0), 0.75)
        self.assertEqual(packet.get("route_actionability"), "actionable_paper")
        self.assertEqual(packet.get("route_priority_cap"), "high")
        self.assertTrue(packet.get("venue_capabilities", {}).get("perp_available"))

    def test_spot_short_without_explicit_support_stays_low_priority_research(self):
        def fake_profile(_route_status):
            return {
                "summary": "Conditional frontier spot short candidate",
                "route_requirement_status": "unknown",
                "api_surface": "public_market_data",
                "broker_surface": "frontier_spot",
                "spot_short_support": "unknown",
                "required_permissions": ["margin", "borrow"],
            }

        frontier_crypto_adapter.paper_only_route_requirement_profile = fake_profile
        route_status = {"venue": "FrontierX"}

        annotated = frontier_crypto_adapter._paper_only_annotate_route_intelligence(route_status)
        packet = annotated.get("route_requirements_packet") or {}

        self.assertEqual(packet.get("required_side"), "spot_short")
        self.assertLessEqual(packet.get("borrow_confidence", 1.0), 0.25)
        self.assertLessEqual(packet.get("route_viability_score", 1.0), 0.34)
        self.assertEqual(packet.get("route_actionability"), "low_priority_research")
        self.assertEqual(packet.get("route_priority_cap"), "low")
        self.assertIn("shortability", packet.get("critical_missing_fields", []))


if __name__ == "__main__":
    unittest.main()
