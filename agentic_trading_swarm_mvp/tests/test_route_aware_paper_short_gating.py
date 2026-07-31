import unittest

from src.route_resolver import assess_paper_short_route_gate


class RouteAwarePaperShortGatingTests(unittest.TestCase):
    def test_confirmed_spot_short_route_is_allowed_direct(self) -> None:
        candidate = {
            "venue": "OKX",
            "surface": "spot",
            "direction": "short",
            "route": {
                "route_id": "okx_spot_margin_paper",
                "route_status": "standard",
                "requirements": [
                    {
                        "requirement_id": "spot_borrow",
                        "status": "confirmed",
                        "blocking_level": "hard",
                    }
                ],
            },
        }

        assessment = assess_paper_short_route_gate(candidate)

        self.assertTrue(assessment["applies"])
        self.assertTrue(assessment["paper_trade_allowed"])
        self.assertEqual(assessment["gate_status"], "allowed_direct")
        self.assertEqual(assessment["selected_route_id"], "okx_spot_margin_paper")
        self.assertEqual(assessment["execution_semantics"], "direct_live_equivalent")

    def test_conditional_spot_short_without_proxy_is_suppressed(self) -> None:
        candidate = {
            "venue": "OKX",
            "surface": "spot",
            "direction": "short",
            "route": {
                "route_id": "okx_spot_paper",
                "route_status": "conditional",
                "requirements": [
                    {
                        "requirement_id": "spot_borrow",
                        "status": "unknown",
                        "blocking_level": "hard",
                    }
                ],
            },
        }

        assessment = assess_paper_short_route_gate(candidate)

        self.assertTrue(assessment["applies"])
        self.assertFalse(assessment["paper_trade_allowed"])
        self.assertEqual(assessment["gate_status"], "suppressed_no_proxy")
        self.assertEqual(
            assessment["suppression_reason"],
            "spot_short_route_requirements_unconfirmed",
        )
        self.assertEqual(assessment["execution_semantics"], "paper_trade_suppressed")

    def test_non_spot_short_route_is_not_affected(self) -> None:
        candidate = {
            "venue": "OKX",
            "surface": "perp",
            "direction": "short",
            "route": {"route_id": "okx_derivatives_paper", "route_status": "standard"},
        }

        assessment = assess_paper_short_route_gate(candidate)

        self.assertFalse(assessment["applies"])
        self.assertTrue(assessment["paper_trade_allowed"])
        self.assertEqual(assessment["gate_status"], "not_applicable")
        self.assertEqual(assessment["selected_route_id"], "okx_derivatives_paper")
        self.assertEqual(assessment["execution_semantics"], "direct_live_equivalent")
