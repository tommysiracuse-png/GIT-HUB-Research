
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
    _compact_self_improvement_open_pack,
    _compact_frontier_crypto,
    _compact_frontier_gap_summary,
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

    def test_frontier_gap_summary_prioritizes_quote_and_health_infrastructure_gaps(self) -> None:
        packet = _compact_frontier_gap_summary(
            {
                "summary": {
                    "candidate_count": 40,
                    "active_paper_review_candidate_count": 9,
                    "candidate_activity": {
                        "active_paper_review_candidates": 9,
                        "regional_admitted_candidates": 0,
                        "route_feasibility_shadow_candidates": 3,
                        "marketability_conservative_route_candidates": 2,
                    },
                    "by_quote_normalization": {
                        "unsupported_quote": 4,
                        "external_fx_reference": 47,
                        "missing_same_venue_stablecoin_reference": 162,
                    },
                    "blocked_venues": ["BITSO"],
                    "degraded_venues": ["MEXC", "OKX_SPOT"],
                    "expansion_map": {
                        "depth_enriched_rate": 0.3009,
                        "unknown_quality_count": 11,
                        "starved_venue_coverage": {"LUNO": {"status": "starved"}},
                    },
                }
            }
        )

        self.assertTrue(packet["paper_only"])
        self.assertTrue(packet["read_only"])
        self.assertEqual(40, packet["frontier_candidates"])
        self.assertEqual(9, packet["active_paper_review_candidates"])
        self.assertEqual(0, packet["regional_admissions"])
        self.assertEqual(30.09, packet["depth_enrichment_rate_pct"])
        self.assertEqual(4, packet["quote_gap_counts"]["unsupported_quote_paths"])
        self.assertEqual(47, packet["quote_gap_counts"]["needs_external_fx_reference"])
        self.assertEqual(162, packet["quote_gap_counts"]["needs_same_venue_stablecoin_reference"])
        self.assertEqual(1, packet["venue_health_gap_counts"]["blocked_venues"])
        self.assertEqual(2, packet["venue_health_gap_counts"]["degraded_venues"])
        self.assertEqual(11, packet["venue_health_gap_counts"]["unknown_quality_observations"])
        self.assertEqual("quote_adapter", packet["priority_gaps"][0]["gap_type"])
        self.assertEqual(
            "missing_same_venue_stablecoin_reference",
            packet["priority_gaps"][0]["reason"],
        )
        self.assertEqual(
            "request_venue_health_check",
            next(
                item["recommended_request"]
                for item in packet["priority_gaps"]
                if item["gap_type"] == "venue_health_check"
            ),
        )
        self.assertEqual(
            "request_directive_cleanup",
            next(
                item["recommended_request"]
                for item in packet["priority_gaps"]
                if item["gap_type"] == "directive_cleanup"
            ),
        )

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

    def test_frontier_short_packet_surfaces_route_feasibility_reasons_and_verified_exceptions(self) -> None:
        packet = build_route_intelligence_packet_fragment(
            [
                {
                    "venue": "BITSO",
                    "inst_id": "BITSO:PEPE-USDT",
                    "direction": "short_frontier_spot",
                    "trade_type": "frontier_crypto_venue_map",
                    "route_status": "conditional",
                    "route_blockers": ["spot_borrow"],
                    "route_feasibility_reason": "conditional_short_paper_metadata_missing",
                    "paper_active_scoring_eligible": False,
                    "paper_route_feasibility_shadow_label": True,
                },
                {
                    "venue": "GATE",
                    "inst_id": "GATE:ABC-USDT",
                    "direction": "short_frontier_spot",
                    "trade_type": "frontier_crypto_venue_map",
                    "route_status": "conditional",
                    "route_blockers": ["spot_borrow"],
                    "route_feasibility_reason": "verified_standard_short_route",
                    "paper_active_scoring_eligible": True,
                    "paper_route_feasibility_shadow_label": False,
                },
            ]
        )

        gate = packet["paper_short_route_gate"]
        self.assertTrue(gate["enabled"])
        self.assertEqual(2, gate["candidate_count"])
        self.assertEqual(1, gate["status_counts"]["allowed_verified_exception"])
        self.assertEqual(1, gate["status_counts"]["shadow_only_route_feasibility"])
        self.assertEqual(
            1,
            gate["route_feasibility_reason_counts"]["conditional_short_paper_metadata_missing"],
        )
        self.assertEqual(
            1,
            gate["route_feasibility_reason_counts"]["verified_standard_short_route"],
        )
        self.assertEqual(1, len(gate["gated_candidates"]))
        gated = gate["gated_candidates"][0]
        self.assertEqual("BITSO:PEPE-USDT", gated["inst_id"])
        self.assertEqual(
            "conditional_short_paper_metadata_missing",
            gated["route_feasibility_reason"],
        )
        self.assertEqual(
            "conditional_short_paper_metadata_missing",
            gated["suppression_reason"],
        )

    def test_open_pack_compaction_keeps_yahoo_decay_window_summary(self) -> None:
        compact = _compact_self_improvement_open_pack(
            {
                "generated_at": "2026-08-06T00:00:00+00:00",
                "paper_only": True,
                "signal_repair_diagnostics": {
                    "yahoo_proxy_decay_analysis": {
                        "primary_horizon_minutes": 60,
                        "leading_counterfactual_hypothesis": "horizon_or_sign_mismatch",
                        "localization_summary": {
                            "localized_decay_detected": True,
                            "likely_decay_sources": ["route_surface_mismatch"],
                        },
                        "bounded_hypothesis_labels": {
                            "tracked_windows": ["5m", "15m", "60m", "realized_post_entry"],
                            "windows": {
                                "5m": {"overall": {"count": 12, "avg_pnl_bps": -3.0}},
                                "15m": {"overall": {"count": 12, "avg_pnl_bps": -6.0}},
                                "60m": {"overall": {"count": 12, "avg_pnl_bps": -11.0}},
                                "realized_post_entry": {"overall": {"count": 12, "avg_pnl_bps": -7.5}},
                            },
                        },
                    }
                },
            }
        )

        yahoo_decay = compact["signal_repair_diagnostics"]["yahoo_decay"]
        self.assertEqual(60, yahoo_decay["primary_horizon_minutes"])
        self.assertEqual("horizon_or_sign_mismatch", yahoo_decay["leading_counterfactual_hypothesis"])
        self.assertTrue(yahoo_decay["localized_decay_detected"])
        self.assertEqual(["route_surface_mismatch"], yahoo_decay["likely_decay_sources"])
        self.assertEqual(-7.5, yahoo_decay["bounded_windows"]["realized_post_entry"]["avg_pnl_bps"])


if __name__ == "__main__":
    unittest.main()
