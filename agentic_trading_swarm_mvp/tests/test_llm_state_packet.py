
import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_state_packet import build_route_intelligence_packet_fragment  # noqa: E402
from llm_bridge import (  # noqa: E402
    _compact_frontier_crypto,
    build_paper_route_requirement_summaries,
)


class LLMStatePacketTests(unittest.TestCase):
    def test_route_requirement_summaries_keep_frontier_and_basis_candidates_priceable(self) -> None:
        packet = build_paper_route_requirement_summaries(
            [
                {
                    "venue": "GATE",
                    "inst_id": "GATE:ABC-USDT",
                    "trade_type": "frontier_crypto_venue_map",
                    "direction": "short_frontier_spot",
                    "score": 87.0,
                    "spread_bps": 6.0,
                    "depth_usd": 25000.0,
                    "minimum_size": 1.0,
                    "route_blockers": ["spot_borrow"],
                },
                {
                    "venue": "OKX",
                    "inst_id": "OKX:BTC-USDT-SWAP",
                    "trade_type": "perp_funding_basis",
                    "direction": "long_perp_short_spot",
                    "score": 73.0,
                    "spread_bps": 2.0,
                    "liquidity_usd": 500000.0,
                    "size_increment": 0.01,
                    "route_blockers": ["spot_borrow"],
                },
                {
                    "venue": "OTHER",
                    "inst_id": "OTHER:XYZ",
                    "trade_type": "global_proxy_momentum",
                    "direction": "long",
                },
            ]
        )

        self.assertTrue(packet["paper_only"])
        self.assertEqual(2, packet["candidate_count"])
        self.assertEqual(
            "diagnostic_only_no_eligibility_or_quarantine_change",
            packet["ranking_policy"],
        )
        frontier, basis = packet["candidates"]
        self.assertEqual("frontier_crypto_venue_map", frontier["candidate"]["trade_type"])
        self.assertEqual("perp_funding_basis", basis["candidate"]["trade_type"])
        self.assertIn("short_borrow_or_proxy", frontier["missing_data_flags"])
        self.assertIn("transfer_dependency", basis["missing_data_flags"])
        self.assertIn("score", frontier["route_friction"])
        self.assertGreaterEqual(frontier["normalized_feasibility_score"], 0.0)
        self.assertLessEqual(frontier["normalized_feasibility_score"], 100.0)
        self.assertEqual(0.0, basis["ranking_annotation"]["score_adjustment"])
        self.assertEqual(73.0, basis["ranking_annotation"]["raw_alpha_score"])
        self.assertFalse(frontier["entry_blocked"])
        self.assertFalse(basis["routing_decision_changed"])

    def test_frontier_packet_preserves_net_edge_gate_diagnostics(self) -> None:
        diagnostics = {
            "gross_edge_bps": 12.0,
            "modeled_cost_bps": 14.0,
            "net_edge_bps": -2.0,
            "freshness_minutes": 0.25,
            "gating_reason": "effective_cost_exceeds_edge",
        }
        packet = _compact_frontier_crypto(
            {
                "summary": {"candidate_count": 1},
                "candidates": [
                    {
                        "inst_id": "GATE:ABC-USDT",
                        "venue": "GATE",
                        "direction": "long_frontier_spot",
                        **diagnostics,
                    }
                ],
            }
        )

        self.assertEqual(diagnostics, {key: packet["candidates"][0][key] for key in diagnostics})

    def test_route_playbooks_are_nested_in_packet_without_credentials(self) -> None:
        opportunities = [
            {
                "venue": "POLYMARKET",
                "inst_id": "POLYMARKET:EVENT",
                "direction": "prediction_market",
                "route_blockers": [
                    "jurisdiction_eligibility",
                    "prediction_markets_account",
                    "venue_api_access",
                ],
            },
            {
                "inst_id": "NYSE:XYZ",
                "direction": "equity_short_proxy",
                "route_blockers": ["equity_short", "options_or_inverse_product"],
            },
        ]

        packet = build_route_intelligence_packet_fragment(opportunities)
        report = packet["route_intelligence_report"]
        summary = report["playbook_summary"]
        groups = {group["blocker"]: group for group in summary["top_blocker_groups"]}

        self.assertTrue(packet["paper_only"])
        self.assertIn("no_credentials", packet["safety_constraints"])
        self.assertTrue(summary["paper_only"])
        self.assertIn("venue_api_access", groups)
        self.assertEqual(
            groups["venue_api_access"]["playbook"]["route_family"],
            "prediction_market",
        )
        self.assertIn(
            "credential_collection",
            groups["venue_api_access"]["playbook"]["unavailable_in_paper"],
        )
        self.assertIn("equity_short", groups)
        self.assertEqual(
            groups["equity_short"]["playbook"]["route_family"],
            "equity_short_or_options_proxy",
        )
        route_row = next(
            row for row in report["routes"] if row["inst_id"] == "POLYMARKET:EVENT"
        )
        self.assertIn("broker_permission_status", route_row)
        self.assertIn("api_path_readiness", route_row)
        self.assertIn("stale_data_flags", route_row)
        self.assertIn("route_requirement_gaps", route_row)
        self.assertTrue(route_row["paper_sizing_guidance"]["non_blocking"])
        self.assertFalse(route_row["guard_value_measurement"]["routing_decision_changed"])
        self.assertLessEqual(
            len(groups["venue_api_access"]["affected_instruments_top_10"]),
            summary["max_affected_instruments_per_group"],
        )
        json.dumps(packet, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
