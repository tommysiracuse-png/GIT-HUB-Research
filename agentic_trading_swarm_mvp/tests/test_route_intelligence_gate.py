import unittest


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


if __name__ == "__main__":
    unittest.main()
