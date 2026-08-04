
import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_state_packet import build_route_intelligence_packet_fragment  # noqa: E402
from llm_bridge import _compact_frontier_crypto  # noqa: E402


class LLMStatePacketTests(unittest.TestCase):
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
        self.assertLessEqual(
            len(groups["venue_api_access"]["affected_instruments_top_10"]),
            summary["max_affected_instruments_per_group"],
        )
        json.dumps(packet, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
