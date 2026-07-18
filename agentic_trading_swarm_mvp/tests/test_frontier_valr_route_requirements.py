import pathlib
import sys
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from frontier_crypto_adapter import (  # noqa: E402
    _paper_only_route_requirement_keys,
    paper_only_conditional_short_route_requirements,
)


class FrontierValrRouteRequirementTests(unittest.TestCase):
    def test_valr_route_id_reduces_to_base_venue_key(self):
        keys = _paper_only_route_requirement_keys("valr_spot_public")
        self.assertIn("VALR_SPOT_PUBLIC", keys)
        self.assertIn("VALR_SPOT", keys)
        self.assertIn("VALR", keys)

    def test_valr_route_id_resolves_existing_unsupported_short_policy(self):
        requirements = paper_only_conditional_short_route_requirements(
            venue="valr_spot_public",
        )
        self.assertEqual(requirements["venue_key"], "VALR")
        self.assertEqual(requirements["support_status"], "unsupported")
        self.assertFalse(requirements["supports_spot_short"])
        self.assertIn("spot_short_unsupported", requirements["notes"])

    def test_binance_us_route_id_keeps_base_alias_available(self):
        keys = _paper_only_route_requirement_keys("binance_us_spot_public")
        self.assertIn("BINANCE_US", keys)

    def test_gate_route_id_resolves_supported_margin_metadata(self):
        requirements = paper_only_conditional_short_route_requirements(
            venue="gate_spot_public",
        )
        self.assertEqual(requirements["venue_key"], "GATE")
        self.assertEqual(requirements["support_status"], "supported")
        self.assertTrue(requirements["requires_margin_permission"])
        self.assertTrue(requirements["requires_borrow_check"])


if __name__ == "__main__":
    unittest.main()
