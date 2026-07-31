import unittest

try:
    from src.frontier_data_quality import _PAPER_ONLY_ROUTE_INTELLIGENCE
except ImportError:  # pragma: no cover
    from frontier_data_quality import _PAPER_ONLY_ROUTE_INTELLIGENCE


class PaperOnlyRouteMetadataCapabilityTests(unittest.TestCase):
    def test_okx_basis_route_remains_supported_while_spot_short_is_blocked(self):
        profile = _PAPER_ONLY_ROUTE_INTELLIGENCE["OKX"]

        self.assertEqual(profile["spot_short_route_status"], "blocked_for_paper_route")
        self.assertEqual(profile["basis_route_status"], "supported")
        self.assertEqual(profile["basis_required_side"], "spot_plus_perp")
        self.assertIn("spot_short", profile["blocked_route_sides"])
        self.assertIn("spot_plus_perp", profile["supported_route_sides"])

        spot_short_requirements = profile["spot_short_requirements"]
        self.assertEqual(spot_short_requirements["route_status"], "blocked_for_paper_route")
        self.assertIn("margin", spot_short_requirements["required_permissions"])
        self.assertIn("borrow_inventory", spot_short_requirements["required_permissions"])

        basis_requirements = profile["basis_requirements"]
        self.assertEqual(basis_requirements["route_status"], "supported")
        self.assertEqual(basis_requirements["required_side"], "spot_plus_perp")
        self.assertIn("perp", basis_requirements["required_permissions"])

    def test_unknown_gate_basis_and_short_routes_are_explicitly_downgraded(self):
        profile = _PAPER_ONLY_ROUTE_INTELLIGENCE["GATE"]

        self.assertEqual(profile["spot_short_route_status"], "blocked_for_paper_route")
        self.assertEqual(profile["basis_route_status"], "blocked_for_paper_route")
        self.assertIn("spot_short", profile["blocked_route_sides"])
        self.assertIn("spot_plus_perp", profile["blocked_route_sides"])

        basis_requirements = profile["basis_requirements"]
        self.assertEqual(basis_requirements["route_status"], "blocked_for_paper_route")
        self.assertLess(basis_requirements["route_confidence"], 1.0)
        self.assertIn("perp", basis_requirements["required_permissions"])

    def test_route_metadata_stays_public_market_data_only(self):
        disallowed_tokens = {"private", "account", "order", "orders", "trade", "credential", "write"}

        for venue, profile in _PAPER_ONLY_ROUTE_INTELLIGENCE.items():
            with self.subTest(venue=venue):
                surfaces = [str(profile.get("api_surface", ""))]
                surfaces.extend(
                    str(profile.get(key, ""))
                    for key in (
                        "spot_short_api_capability",
                        "basis_api_capability",
                    )
                )
                for nested_key in ("spot_short_requirements", "basis_requirements"):
                    nested = profile.get(nested_key, {})
                    if isinstance(nested, dict):
                        surfaces.append(str(nested.get("api_capability", "")))

                normalized = " ".join(surfaces).lower()
                self.assertIn("public", normalized)
                for token in disallowed_tokens:
                    self.assertNotIn(token, normalized)

    def test_spot_only_surface_blocks_basis_route_without_perp_leg(self):
        profile = _PAPER_ONLY_ROUTE_INTELLIGENCE["OKX_SPOT"]

        self.assertEqual(profile["basis_route_status"], "blocked_for_paper_route")
        self.assertEqual(profile["basis_requirements"]["route_status"], "blocked_for_paper_route")
        self.assertEqual(profile["basis_requirements"]["perp_support"], "unsupported")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
