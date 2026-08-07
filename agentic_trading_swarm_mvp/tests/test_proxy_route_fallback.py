import unittest
from unittest.mock import patch

from src.llm_state_packet import build_route_intelligence_packet_fragment
from src.route_resolver import assess_paper_short_route_gate


class ProxyRouteFallbackTests(unittest.TestCase):
    def test_conditional_spot_short_reroutes_to_paper_proxy(self) -> None:
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
                        "status": "missing",
                        "blocking_level": "hard",
                    }
                ],
                "paper_route_alternatives": [
                    {
                        "alternative_id": "crypto_perp_proxy_for_spot_borrow",
                        "status": "paper_testable_proxy",
                        "route_id": "okx_derivatives_paper",
                        "paper_allocation_multiplier": 0.25,
                        "execution_semantics": "proxy_not_live_equivalent",
                    }
                ],
            },
        }

        assessment = assess_paper_short_route_gate(candidate)

        self.assertTrue(assessment["paper_trade_allowed"])
        self.assertEqual(assessment["gate_status"], "rerouted_to_proxy")
        self.assertEqual(assessment["selected_route_id"], "okx_derivatives_paper")
        self.assertEqual(assessment["execution_semantics"], "proxy_not_live_equivalent")
        self.assertEqual(assessment["allocation_multiplier"], 0.25)
        self.assertEqual(assessment["missing_requirements"], ["spot_borrow"])

    def test_explicitly_disabled_structural_proxy_stays_disabled(self) -> None:
        candidate = {
            "venue": "OKX",
            "surface": "spot",
            "direction": "short",
            "route": {
                "route_status": "conditional",
                "requirements": [{"requirement_id": "spot_borrow", "status": "missing"}],
                "paper_route_alternatives": [
                    {
                        "status": "paper_testable_proxy",
                        "route_id": "okx_derivatives_paper",
                        "activated": False,
                    }
                ],
            },
        }

        assessment = assess_paper_short_route_gate(candidate)

        self.assertFalse(assessment["paper_trade_allowed"])
        self.assertEqual("suppressed_no_proxy", assessment["gate_status"])

    @patch("src.llm_state_packet.build_route_requirements_report", return_value={"summary": {}})
    def test_llm_packet_exposes_proxy_gate_summary(self, _mock_report) -> None:
        opportunities = [
            {
                "venue": "OKX",
                "surface": "spot",
                "direction": "short",
                "route": {
                    "route_id": "okx_spot_paper",
                    "route_status": "conditional",
                    "requirements": [
                        {"requirement_id": "spot_borrow", "status": "missing"},
                    ],
                    "paper_route_alternatives": [
                        {
                            "status": "paper_testable_proxy",
                            "route_id": "okx_derivatives_paper",
                            "paper_allocation_multiplier": 0.25,
                            "execution_semantics": "proxy_not_live_equivalent",
                        }
                    ],
                },
            },
            {
                "venue": "OKX",
                "surface": "spot",
                "direction": "long",
            },
        ]

        fragment = build_route_intelligence_packet_fragment(opportunities)
        gate = fragment["paper_short_route_gate"]

        self.assertTrue(gate["enabled"])
        self.assertEqual(gate["candidate_count"], 1)
        self.assertEqual(gate["status_counts"], {"rerouted_to_proxy": 1})
        self.assertEqual(gate["execution_semantics_counts"], {"proxy_not_live_equivalent": 1})

