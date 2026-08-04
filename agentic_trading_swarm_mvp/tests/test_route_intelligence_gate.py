import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


try:
    from src.route_resolver import evaluate_route_intelligence
except Exception:  # pragma: no cover
    evaluate_route_intelligence = None


class RouteIntelligenceGateTests(unittest.TestCase):
    def setUp(self):
        if evaluate_route_intelligence is None:
            self.skipTest("route intelligence evaluator unavailable")

    def test_executable_standard_route_passes(self):
        candidate = {
            "route_id": "spot_usdt",
            "venue": "okx",
            "market_key": "btc-usdt",
            "route_requirements": {"borrow_required": False, "proxy_allowed": False},
        }
        verdict = evaluate_route_intelligence(candidate)
        self.assertEqual(verdict["route_decision"], "executable_standard")
        self.assertFalse(verdict.get("suppressed", False))

    def test_borrow_blocked_without_proxy_is_suppressed(self):
        candidate = {
            "route_id": "spot_short",
            "venue": "okx",
            "market_key": "eth-usdt",
            "route_requirements": {"borrow_required": True, "proxy_allowed": False},
        }
        verdict = evaluate_route_intelligence(candidate)
        self.assertEqual(verdict["route_decision"], "blocked_hard")
        self.assertTrue(verdict.get("suppressed", False))
        self.assertIn("spot_borrow_missing", verdict.get("blocker_reasons", []))

    def test_borrow_blocked_with_proxy_is_allowed_as_proxy(self):
        candidate = {
            "route_id": "spot_short",
            "venue": "okx",
            "market_key": "eth-usdt",
            "route_requirements": {"borrow_required": True, "proxy_allowed": True, "paper_proxy_id": "okx_derivatives_paper"},
            "venue_capabilities": {"paper_route_feasible": True},
        }
        verdict = evaluate_route_intelligence(candidate)
        self.assertEqual(verdict["route_decision"], "executable_proxy")
        self.assertTrue(verdict.get("proxy_used", False))
        self.assertFalse(verdict.get("suppressed", False))

    def test_explicit_capability_veto_quarantines_candidate(self):
        verdict = evaluate_route_intelligence(
            {
                "surface": "spot",
                "direction": "short",
                "paper_short_simulation_allowed": True,
                "borrowable": True,
                "borrow_cost_bps": 4.0,
                "margin_eligible": True,
                "score": 80.0,
                "venue_capabilities": {
                    "supports_spot_short": False,
                    "supports_margin_spot": True,
                    "supports_borrow_check": True,
                },
            }
        )

        self.assertEqual("unsupported", verdict["route_status"])
        self.assertEqual(
            "quarantined_route_unavailable", verdict["candidate_status"]
        )
        self.assertEqual(0.0, verdict["rank_contribution_cap"])
        self.assertEqual(0.0, verdict["rank_contribution"])
        self.assertEqual(
            "venue_spot_short_capability_unconfirmed",
            verdict["blocking_reason"],
        )

    def test_unknown_capability_requires_confirmation_and_caps_rank(self):
        verdict = evaluate_route_intelligence(
            {
                "surface": "spot",
                "direction": "short",
                "paper_short_simulation_allowed": True,
                "borrowable": True,
                "borrow_cost_bps": 4.0,
                "margin_eligible": True,
                "score": 80.0,
                "venue_capabilities": {
                    "supports_spot_short": True,
                    "supports_margin_spot": None,
                    "supports_borrow_check": True,
                },
            }
        )

        self.assertEqual("unknown", verdict["route_status"])
        self.assertEqual("route_needs_confirmation", verdict["candidate_status"])
        self.assertEqual(0.2, verdict["rank_contribution_cap"])
        self.assertEqual(16.0, verdict["rank_contribution"])

    def test_supported_basis_transfer_route_keeps_full_rank(self):
        verdict = evaluate_route_intelligence(
            {
                "trade_type": "perp_funding_basis",
                "direction": "short_perp_long_spot",
                "route_type": "cross_venue_basis",
                "hedge_venue": "OKX_SPOT",
                "hedge_instrument": "BTC-USDT",
                "fee_model": "paper_conservative_v1",
                "paper_leg_mapping_valid": True,
                "score": 75.0,
                "venue_capabilities": {
                    "supports_basis_path": True,
                    "supports_perpetuals": True,
                    "supports_spot_long": True,
                    "supports_transfers": True,
                },
            }
        )

        self.assertTrue(verdict["transfer_required"])
        self.assertEqual("supported", verdict["route_status"])
        self.assertEqual("route_supported", verdict["candidate_status"])
        self.assertEqual(1.0, verdict["rank_contribution_cap"])
        self.assertEqual(75.0, verdict["rank_contribution"])
        states = {
            check["capability"]: check["state"]
            for check in verdict["capability_checks"]
        }
        self.assertEqual("supported", states["supports_transfers"])


if __name__ == "__main__":
    unittest.main()
