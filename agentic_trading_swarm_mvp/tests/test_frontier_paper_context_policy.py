import unittest

from src.frontier_data_quality import paper_only_route_requirement_profile


class PaperOnlyRouteRequirementProfileContextTests(unittest.TestCase):
    def test_direction_is_part_of_context_key(self):
        long_profile = paper_only_route_requirement_profile(
            {
                "market_key": "OKX_SPOT",
                "venue": "OKX_SPOT",
                "market_surface": "spot",
                "direction": "long",
                "signal_key": "OKX_SPOT|frontier_spot|long|confirmed",
            }
        )
        short_profile = paper_only_route_requirement_profile(
            {
                "market_key": "OKX_SPOT",
                "venue": "OKX_SPOT",
                "market_surface": "spot",
                "direction": "short",
                "signal_key": "OKX_SPOT|frontier_spot|short|confirmed",
            }
        )

        self.assertNotEqual(long_profile["paper_context_key"], short_profile["paper_context_key"])
        self.assertIn("|long|", long_profile["paper_context_key"])
        self.assertIn("|short|", short_profile["paper_context_key"])

    def test_funding_capture_basis_mode_is_explicit_in_profile(self):
        profile = paper_only_route_requirement_profile(
            {
                "venue": "OKX",
                "market_surface": "perp",
                "direction": "long",
                "variant": "funding_capture",
                "strategy_family": "perp_funding_basis",
            }
        )

        self.assertEqual(profile["basis_mode"], "funding_capture")
        self.assertTrue(profile["funding_capture_requires_perp_access"])
        self.assertIn("basis_mode=funding_capture", profile["summary"])
        self.assertTrue(profile["paper_context_key"].endswith("|funding_capture"))

    def test_ambiguous_basis_context_stays_neutral_without_explicit_mode(self):
        profile = paper_only_route_requirement_profile(
            {
                "venue": "OKX",
                "market_surface": "perp",
                "direction": "long",
                "strategy_family": "basis",
                "variant": "experimental",
            }
        )

        self.assertIsNone(profile["basis_mode"])
        self.assertTrue(profile["paper_context_key"].endswith("|neutral_basis_mode"))


if __name__ == "__main__":
    unittest.main()
