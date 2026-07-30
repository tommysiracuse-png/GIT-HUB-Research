import unittest


try:
    from src.opportunity_ranker import _apply_route_intelligence_penalty
except Exception:  # pragma: no cover
    _apply_route_intelligence_penalty = None


class OpportunityRankerRoutePenaltyTests(unittest.TestCase):
    def setUp(self):
        if _apply_route_intelligence_penalty is None:
            self.skipTest("route intelligence penalty hook unavailable")

    def test_standard_executable_route_unchanged(self):
        candidate = {
            "score": 1.25,
            "route_requirements": {"route_decision": "executable_standard", "proxy_used": False},
        }
        adjusted = _apply_route_intelligence_penalty(candidate)
        self.assertEqual(adjusted["score"], 1.25)
        self.assertEqual(adjusted["route_requirements"]["route_decision"], "executable_standard")

    def test_blocked_route_is_suppressed_without_proxy(self):
        candidate = {
            "score": 1.25,
            "route_requirements": {
                "route_decision": "blocked_hard",
                "blocker_reasons": ["spot_borrow_missing"],
                "proxy_used": False,
            },
        }
        adjusted = _apply_route_intelligence_penalty(candidate)
        self.assertTrue(adjusted.get("suppressed", False))
        self.assertIn("spot_borrow_missing", adjusted["route_requirements"]["blocker_reasons"])


if __name__ == "__main__":
    unittest.main()
