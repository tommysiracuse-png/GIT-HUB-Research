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

    def test_explicit_paper_proxy_activation_confirms_borrow_dependent_basis_route(self) -> None:
        candidate = {
            "venue": "OKX",
            "trade_type": "perp_funding_basis",
            "direction": "long_perp_short_spot",
            "paper_proxy_activated": True,
            "paper_proxy_not_live_equivalent": True,
            "paper_execution_semantics": "proxy_not_live_equivalent",
            "paper_allocation_multiplier": 0.25,
            "paper_proxy_route": {
                "route_id": "okx_derivatives_paper",
                "execution_semantics": "proxy_not_live_equivalent",
            },
            "execution_feasibility": {
                "status": "conditional",
                "route_status": "conditional",
                "missing_requirements": ["spot_borrow"],
            },
            "execution_route": {
                "route_id": "conditional_crypto_route_paper",
                "route_status": "conditional",
                "missing_permissions": ["spot_borrow"],
                "requirements": [
                    {
                        "requirement_id": "spot_borrow",
                        "status": "missing",
                        "blocking_level": "hard",
                    }
                ],
            },
        }

        assessment = assess_paper_short_route_gate(candidate)

        self.assertTrue(assessment["applies"])
        self.assertTrue(assessment["paper_trade_allowed"])
        self.assertEqual(assessment["gate_status"], "rerouted_to_proxy")
        self.assertEqual(assessment["selected_route_id"], "okx_derivatives_paper")
        self.assertEqual(assessment["capability_confirmation_status"], "proxy_confirmed")

    def test_stale_borrow_confirmation_is_suppressed(self) -> None:
        candidate = {
            "venue": "OKX",
            "surface": "spot",
            "direction": "short",
            "route_feasibility_reason": "stale_confirmation",
            "route": {
                "route_id": "okx_spot_paper",
                "route_status": "conditional",
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
        self.assertFalse(assessment["paper_trade_allowed"])
        self.assertEqual(assessment["gate_status"], "shadow_only_route_feasibility")
        self.assertEqual(assessment["suppression_reason"], "stale_confirmation")
        self.assertEqual(assessment["capability_confirmation_status"], "stale")
