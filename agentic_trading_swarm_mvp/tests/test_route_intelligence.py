
import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from route_intelligence import (  # noqa: E402
    ROUTE_REQUIREMENT_FIELDS,
    build_conditional_short_route_intelligence,
    build_conditional_short_route_diagnostics,
    build_route_requirements_annotation,
    build_route_requirements_matrix,
    render_route_requirements_markdown,
    route_requirements_json,
)


class RouteIntelligenceTests(unittest.TestCase):
    def test_conditional_short_packet_keeps_venue_requirements_read_only(self) -> None:
        packet = build_conditional_short_route_intelligence(
            {
                "venue": "MEXC",
                "direction": "short_frontier_spot",
                "route_status": "conditional",
                "paper_route_required_permissions": [
                    "crypto_spot",
                    "margin_spot",
                    "spot_short",
                    "spot_borrow",
                ],
                "paper_route_required_account_modes": ["margin"],
                "paper_route_estimated_cost_bps": {"estimated_total": 30.0},
                "api_access_status": "public_data_only",
            },
            route={
                "venue": "MEXC",
                "direction": "short_frontier_spot",
                "route_status": "conditional",
                "required_permissions": ["crypto_spot", "spot_borrow"],
                "missing_permissions": ["spot_borrow"],
                "borrow_required": True,
                "margin_required": True,
                "borrow_status": "required_unconfirmed",
                "api_access_status": "public_data_only",
            },
        )

        self.assertTrue(packet["applies"])
        self.assertIn("spot_borrow", packet["shorting_requirements"])
        self.assertEqual("unconfirmed", packet["borrow_availability"])
        self.assertEqual("required_unconfirmed", packet["margin_mode"])
        self.assertEqual("maintained_paper_route_estimate", packet["fee_class"])
        self.assertEqual("public_data_only", packet["api_permission_status"])
        self.assertEqual("down_rank_only", packet["ranking_action"])
        self.assertFalse(packet["hard_blocking"])

    def test_conditional_short_diagnostics_expose_route_requirements_and_only_down_rank(self) -> None:
        opportunity = {
            "venue": "OKX",
            "inst_id": "OKX:ARC-USDT",
            "direction": "long_perp_short_spot",
            "route_status": "conditional",
            "route_blockers": ["spot_borrow"],
            "borrow_available": "unknown",
            "borrow_fee_bps_estimate": 12.5,
            "maker_fee_bps": 1.0,
            "taker_fee_bps": 3.0,
            "margin_mode": "isolated",
            "api_access_status": "public_data_only",
            "min_liquidity_usd": 50000,
        }

        diagnostics = build_conditional_short_route_diagnostics(opportunity)
        row = build_route_requirements_matrix([opportunity])[0]

        self.assertTrue(diagnostics["applies"])
        self.assertEqual("unconfirmed", diagnostics["borrow_availability"])
        self.assertEqual(12.5, diagnostics["estimated_borrow_fee_bps"])
        self.assertEqual(6.0, diagnostics["maker_taker_fee_stack_bps"]["estimated_round_trip_taker_bps"])
        self.assertEqual("isolated", diagnostics["margin_mode"])
        self.assertEqual("public_data_only", diagnostics["api_route_status"])
        self.assertEqual(50000, diagnostics["minimum_liquidity_usd"])
        self.assertLess(diagnostics["paper_rank_multiplier"], 1.0)
        self.assertEqual("down_rank_only", diagnostics["ranking_action"])
        self.assertFalse(diagnostics["hard_blocking"])
        self.assertEqual("isolated", row["margin_mode"])
        self.assertEqual(3.0, row["taker_fee_bps_or_unknown"])
        self.assertIn("conditional_short_route_diagnostics", row)

    def test_requirements_panel_reports_gaps_staleness_and_measurement_without_a_route_gate(self) -> None:
        opportunity = {
            "venue": "OKX",
            "inst_id": "OKX:ARC-USDT",
            "direction": "long_perp_short_spot",
            "route_status": "conditional",
            "route_blockers": ["spot_borrow"],
            "borrow_available": "unknown",
            "maker_fee_bps": 1.0,
            "taker_fee_bps": 3.0,
            "margin_mode": "isolated",
            "api_access_status": "public_data_only",
            "freshness_state": "stale",
        }

        row = build_route_requirements_matrix([opportunity])[0]
        annotation = build_route_requirements_annotation(opportunity)

        self.assertEqual("unknown", row["broker_permission_status"])
        self.assertEqual("unconfirmed", row["api_path_readiness"])
        self.assertEqual("stale", row["stale_data_status"])
        self.assertIn("freshness_state:stale", row["stale_data_flags"])
        self.assertIn("borrow_availability", row["route_requirement_gaps"])
        self.assertIn("stale_data", row["route_requirement_gaps"])
        self.assertTrue(row["paper_sizing_guidance"]["non_blocking"])
        self.assertFalse(row["paper_sizing_guidance"]["routing_decision_changed"])
        self.assertTrue(row["guard_value_measurement"]["enabled"])
        self.assertFalse(annotation["guard_value_measurement"]["routing_decision_changed"])

    def test_spot_borrow_routes_are_paper_only_prioritized_with_unknowns(self) -> None:
        rows = build_route_requirements_matrix(
            [
                {
                    "venue": "POLYMARKET",
                    "inst_id": "POLYMARKET:EXAMPLE",
                    "direction": "prediction_market",
                    "route_blockers": [
                        "jurisdiction_eligibility",
                        "prediction_markets_account",
                        "venue_api_access",
                    ],
                },
                {
                    "inst_id": "GATE:DEXE_USDT",
                    "direction": "short_frontier_spot",
                    "route_blockers": ["spot_borrow"],
                },
                {
                    "inst_id": "GATE:ARC_USDT",
                    "direction": "short_frontier_spot",
                    "route_blockers": ["spot_borrow"],
                },
                {
                    "inst_id": "COINBASE:XRP-USDT",
                    "direction": "short_frontier_spot",
                    "route_blockers": ["spot_borrow"],
                },
            ]
        )

        self.assertEqual(
            [row["inst_id"] for row in rows[:3]],
            ["GATE:ARC_USDT", "GATE:DEXE_USDT", "COINBASE:XRP-USDT"],
        )
        arc = rows[0]
        self.assertEqual(set(ROUTE_REQUIREMENT_FIELDS), set(arc))
        self.assertTrue(arc["paper_route_only"])
        self.assertTrue(arc["borrow_required"])
        self.assertEqual(arc["borrow_asset"], "ARC")
        self.assertEqual(arc["borrow_fee_bps_estimate_or_unknown"], "unknown")
        self.assertEqual(arc["fee_bps_per_side_or_unknown"], "unknown")
        self.assertEqual(arc["slippage_bps_per_side_or_unknown"], "unknown")
        self.assertEqual(arc["margin_required"], "unknown")
        self.assertEqual(arc["route_status"], "blocked_until_requirements_confirmed")

    def test_polymarket_requirements_remain_blocked_and_no_credentials(self) -> None:
        opportunity = {
            "venue": "POLYMARKET",
            "inst_id": "POLYMARKET:EVENT",
            "direction": "prediction_market",
            "route_blockers": [
                "jurisdiction_eligibility",
                "prediction_markets_account",
                "venue_api_access",
            ],
        }

        row = build_route_requirements_matrix([opportunity])[0]
        self.assertEqual(row["venue"], "POLYMARKET")
        self.assertTrue(row["paper_route_only"])
        self.assertIn("prediction_markets_account", row["route_blockers"])
        self.assertIn("prediction_markets", row["required_account_type"])
        self.assertIn("jurisdiction", row["jurisdiction_requirement"])
        self.assertIn("no_credentials", row["venue_api_requirement"])

        markdown = render_route_requirements_markdown([opportunity])
        self.assertIn("Paper-only read-only output", markdown)
        self.assertIn("No credentials", markdown)
        self.assertTrue(json.loads(route_requirements_json([opportunity]))["paper_only"])
