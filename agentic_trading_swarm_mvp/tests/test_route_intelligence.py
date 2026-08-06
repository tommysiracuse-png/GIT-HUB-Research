
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
    build_candidate_route_requirement_summary,
    build_conditional_short_route_intelligence,
    build_conditional_short_route_diagnostics,
    extract_route_requirements,
    build_frontier_short_spot_route_intelligence,
    build_paper_route_requirement_report,
    build_route_friction_summary,
    build_route_requirements_annotation,
    build_route_requirements_matrix,
    build_route_requirements_report,
    build_short_frontier_spot_route_outcome_diagnostics,
    render_route_requirements_markdown,
    route_requirements_json,
)


class RouteIntelligenceTests(unittest.TestCase):
    def test_route_friction_summary_attributes_conditional_route_costs_without_blocking(self) -> None:
        candidate = {
            "venue": "TEST_VENUE",
            "inst_id": "TEST_VENUE:ARC-USDT",
            "direction": "long_perp_short_spot",
            "route_status": "conditional",
            "route_blockers": ["spot_borrow"],
            "required_permissions": ["crypto_spot", "margin_spot", "spot_borrow"],
            "borrow_available": "unknown",
            "indicative_borrow_fee_bps_range": {"lower_bps": 8.0, "upper_bps": 16.0},
            "margin_mode": "isolated",
            "fee_model": "maker_taker_estimate",
            "maker_fee_bps": 1.0,
            "taker_fee_bps": 3.0,
            "api_access_status": "public_data_only",
            "freshness_state": "stale",
            "liquidity_score": 0.1,
            "available_depth_usd": 1000.0,
            "min_liquidity_usd": 5000.0,
        }

        friction = build_route_friction_summary(candidate)
        report = build_route_requirements_report([candidate])

        self.assertTrue(friction["paper_only"])
        self.assertTrue(friction["read_only"])
        self.assertEqual(
            ["crypto_spot", "margin_spot", "spot_borrow"],
            friction["required_broker_permissions"],
        )
        self.assertEqual("unconfirmed", friction["borrow_availability"])
        self.assertEqual(
            {"status": "indicative", "lower_bps": 8.0, "upper_bps": 16.0,
             "source": "maintained_paper_route_metadata_or_candidate_diagnostic"},
            friction["indicative_borrow_fee_bps_range"],
        )
        self.assertEqual("isolated", friction["margin_type"])
        self.assertEqual("public_data_only", friction["api_coverage"]["status"])
        self.assertEqual("conditional", friction["venue_route_status"]["resolved_status"])
        self.assertEqual("maker_taker_estimate", friction["fee_model"]["model"])
        self.assertEqual("stale_and_illiquid", friction["stale_illiquid_diagnostics"]["status"])
        self.assertLess(friction["paper_rank_multiplier"], 1.0)
        self.assertEqual("down_rank_and_size_only", friction["ranking_action"])
        self.assertFalse(friction["entry_blocked"])
        self.assertFalse(friction["routing_decision_changed"])
        self.assertEqual("retained_for_paper_exploration", friction["paper_candidate_emission"])
        self.assertEqual(1, report["route_friction_summary"]["applicable_candidate_count"])
        self.assertEqual(1, report["route_friction_summary"]["stale_diagnostic_count"])
        self.assertEqual(1, report["route_friction_summary"]["illiquid_diagnostic_count"])

    def test_weak_short_frontier_outcomes_become_route_diagnostics_not_entry_blocks(self) -> None:
        diagnostics = build_short_frontier_spot_route_outcome_diagnostics(
            [
                {
                    "signal_key": "SYNTHETIC_RESEARCH|MEXC|frontier_crypto_venue_map|short_frontier_spot|conditional",
                    "closed_count": 19,
                    "avg_pnl_bps": -41.539,
                    "win_rate": 0.263,
                    "score_adjustment": 0.0,
                },
                {
                    "signal_key": "SYNTHETIC_RESEARCH|VALR|frontier_crypto_venue_map|short_frontier_spot|conditional",
                    "closed_count": 19,
                    "avg_pnl_bps": -41.539,
                    "win_rate": 0.263,
                    "score_adjustment": 0.0,
                },
            ]
        )

        self.assertTrue(diagnostics["paper_only"])
        self.assertTrue(diagnostics["read_only"])
        self.assertEqual(2, diagnostics["route_count"])
        self.assertEqual({"MEXC", "VALR"}, {row["venue"] for row in diagnostics["routes"]})
        for row in diagnostics["routes"]:
            self.assertEqual("weak_paper_outcome", row["outcome_status"])
            self.assertEqual(
                {
                    "borrow_permissions",
                    "fees",
                    "margin_constraints",
                    "api_reliability",
                    "spread_liquidity",
                    "carry",
                },
                set(row["route_diagnostic_dimensions"]),
            )
            self.assertEqual("diagnose_and_down_rank_only", row["ranking_input"]["ranking_action"])
            self.assertEqual("retained_for_paper_exploration", row["paper_candidate_emission"])
            self.assertFalse(row["hard_blocking"])
            self.assertFalse(row["entry_blocked"])

    def test_requirement_extractor_gates_unresolved_direct_route_but_retains_paper_candidate(self) -> None:
        extraction = extract_route_requirements(
            {
                "venue": "OKX",
                "direction": "long_perp_short_spot",
                "borrow_available": "unknown",
                "margin_mode": "required_unconfirmed",
                "api_access_status": "public_data_only",
            },
            route={
                "required_permissions": ["crypto_derivatives", "crypto_spot", "spot_borrow"],
                "missing_permissions": ["spot_borrow"],
                "requirements": [
                    {"requirement_id": "crypto_derivatives", "status": "confirmed"},
                    {"requirement_id": "crypto_spot", "status": "confirmed"},
                    {"requirement_id": "spot_borrow", "status": "missing"},
                ],
                "borrow_required": True,
                "margin_required": True,
                "api_access_status": "public_data_only",
            },
        )

        self.assertTrue(extraction["paper_only"])
        self.assertTrue(extraction["read_only"])
        self.assertEqual("gated", extraction["route_recommendation_status"])
        self.assertEqual("gated", extraction["route_actionability"])
        self.assertIn("borrow_availability", extraction["unresolved_requirements"])
        self.assertIn("api_constraints", extraction["unresolved_requirements"])
        self.assertFalse(extraction["direct_route_actionable"])
        self.assertEqual("retained_for_paper_exploration", extraction["paper_candidate_emission"])
        self.assertFalse(extraction["entry_blocked"])

    def test_requirement_extractor_marks_confirmed_direct_route_actionable(self) -> None:
        extraction = extract_route_requirements(
            {
                "venue": "OKX",
                "direction": "long_spot",
                "maker_fee_bps": 1.0,
                "taker_fee_bps": 2.0,
                "api_access_status": "confirmed",
            },
            route={
                "required_permissions": ["crypto_spot"],
                "requirements": [
                    {"requirement_id": "crypto_spot", "status": "confirmed"},
                ],
                "api_access_status": "confirmed",
            },
        )

        self.assertEqual("actionable", extraction["route_recommendation_status"])
        self.assertTrue(extraction["direct_route_actionable"])
        self.assertEqual([], extraction["unresolved_requirements"])

    def test_frontier_short_spot_pass_attaches_route_validation_and_quote_notes(self) -> None:
        intelligence = build_frontier_short_spot_route_intelligence(
            {
                "venue": "BITSO",
                "inst_id": "BITSO:BTC-MXN",
                "direction": "short_frontier_spot",
                "route_status": "conditional",
                "route_feasibility_reason": "conditional_short_support_unknown",
                "required_permissions": ["crypto_spot", "margin_spot", "spot_borrow"],
                "route_blockers": ["spot_borrow"],
                "borrow_available": "unknown",
                "maker_fee_bps": 2.0,
                "taker_fee_bps": 8.0,
                "margin_mode": "required_unconfirmed",
                "api_access_status": "public_data_only",
                "freshness_state": "fresh",
                "freshness_age_seconds": 4.0,
                "latency_ms": 31.5,
                "depth_latency_ms": 44.0,
            }
        )

        self.assertTrue(intelligence["paper_only"])
        self.assertTrue(intelligence["read_only"])
        self.assertTrue(intelligence["applies"])
        self.assertEqual(
            ["crypto_spot", "margin_spot", "spot_borrow"],
            intelligence["broker_permissions"],
        )
        self.assertEqual("unconfirmed", intelligence["borrow_availability"])
        self.assertEqual(8.0, intelligence["fee_estimates"]["taker_fee_bps"])
        self.assertEqual("required_unconfirmed", intelligence["margin_mode"])
        self.assertEqual("public_data_only", intelligence["api_route_status"])
        self.assertEqual("needs route validation", intelligence["route_validation_status"])
        self.assertEqual(
            "conditional_short_support_unknown",
            intelligence["route_feasibility_reason"],
        )
        self.assertIn("latency_ms:31.5", intelligence["freshness_latency_notes"])
        self.assertIn("depth_latency_ms:44.0", intelligence["freshness_latency_notes"])
        self.assertFalse(intelligence["hard_blocking"])
        self.assertFalse(intelligence["entry_blocked"])

    def test_frontier_short_spot_telemetry_collects_economics_before_ranking(self) -> None:
        intelligence = build_frontier_short_spot_route_intelligence(
            {
                "venue": "BITSO",
                "inst_id": "BITSO:BTC-MXN",
                "direction": "short_frontier_spot",
                "borrow_available": True,
                "borrow_fee_bps_estimate": 7.5,
                "maker_fee_bps": 1.0,
                "taker_fee_bps": 3.0,
                "margin_mode": "isolated",
                "is_shortable": True,
                "api_access_status": "public_data_only",
                "freshness_state": "fresh",
                "freshness_age_seconds": 2.0,
                "spread_bps": 4.0,
                "available_depth_usd": 40000.0,
                "min_depth_usd": 20000.0,
                "estimated_slippage_bps": 1.5,
            }
        )

        telemetry = intelligence["route_economics_telemetry"]
        self.assertTrue(telemetry["paper_only"])
        self.assertTrue(telemetry["prepared_before_ranking"])
        self.assertEqual("available", telemetry["borrow"]["availability"])
        self.assertEqual(7.5, telemetry["borrow"]["estimated_fee_bps"])
        self.assertEqual(1.0, telemetry["fees"]["maker_bps"])
        self.assertEqual(3.0, telemetry["fees"]["taker_bps"])
        self.assertEqual("isolated", telemetry["margin"]["mode"])
        self.assertEqual("available", telemetry["shortability_status"])
        self.assertEqual("public_data_only", telemetry["shortability_api_status"])
        self.assertEqual("public_data_only", telemetry["api_permission"]["status"])
        self.assertEqual("observed", telemetry["quote_freshness"]["status"])
        self.assertEqual(2.0, telemetry["quote_freshness"]["age_seconds"])
        self.assertEqual(4.0, telemetry["market_impact_proxies"]["spread_bps"])
        self.assertEqual(40000.0, telemetry["market_impact_proxies"]["depth_usd"])
        self.assertEqual("observed", telemetry["market_impact_proxies"]["depth_status"])
        self.assertEqual(1.5, telemetry["market_impact_proxies"]["slippage_bps_per_side"])
        self.assertEqual(0.0, telemetry["ranking_hook"]["score_adjustment"])
        self.assertLess(telemetry["sizing_hook"]["recommended_paper_allocation_multiplier"], 1.0)
        self.assertFalse(telemetry["entry_blocked"])

    def test_route_economics_depth_adjusts_paper_size_without_changing_entry_status(self) -> None:
        report = build_paper_route_requirement_report(
            {
                "venue": "BITSO",
                "inst_id": "BITSO:THIN-USDT",
                "direction": "short_frontier_spot",
                "route_status": "conditional",
                "borrow_available": True,
                "maker_fee_bps": 1.0,
                "taker_fee_bps": 2.0,
                "margin_mode": "isolated",
                "api_access_status": "public_data_only",
                "freshness_state": "fresh",
                "available_depth_usd": 5000.0,
                "min_depth_usd": 20000.0,
                "spread_bps": 3.0,
                "estimated_slippage_bps": 1.0,
            }
        )

        telemetry = report["route_economics_telemetry"]
        self.assertEqual(
            "thin_relative_to_declared_minimum",
            telemetry["market_impact_proxies"]["depth_status"],
        )
        self.assertLess(
            report["paper_allocation_multiplier"],
            report["paper_rank_multiplier"],
        )
        self.assertEqual(
            report["paper_allocation_multiplier"],
            report["paper_sizing_guidance"]["recommended_paper_allocation_multiplier"],
        )
        self.assertFalse(report["entry_blocked"])
        self.assertFalse(report["routing_decision_changed"])

    def test_frontier_short_spot_validation_is_visible_in_the_paper_matrix(self) -> None:
        candidate = {
            "venue": "BITSO",
            "inst_id": "BITSO:BTC-MXN",
            "direction": "short_frontier_spot",
            "route_status": "conditional",
            "route_blockers": ["spot_borrow"],
            "required_permissions": ["crypto_spot", "spot_borrow"],
            "api_access_status": "public_data_only",
            "freshness_state": "fresh",
            "latency_ms": 19.0,
        }

        row = build_route_requirements_matrix([candidate])[0]
        markdown = render_route_requirements_markdown([candidate])

        self.assertEqual("needs route validation", row["route_validation_status"])
        self.assertEqual("unknown", row["route_feasibility_reason"])
        self.assertIn("borrow_availability", row["frontier_short_spot_route_intelligence"]["missing_route_metadata"])
        self.assertIn("latency_ms:19.0", row["freshness_latency_notes"])
        self.assertIn("route_validation_status", markdown)
        self.assertIn("needs route validation", markdown)

    def test_raw_frontier_short_matrix_uses_maintained_venue_status_without_a_route_probe(self) -> None:
        rows = build_route_requirements_matrix(
            [
                {
                    "venue": venue,
                    "inst_id": f"{venue}:BTC-USDT",
                    "trade_type": "frontier_crypto_venue_map",
                    "direction": "short_frontier_spot",
                }
                for venue in ("MEXC", "VALR")
            ]
        )

        self.assertEqual(2, len(rows))
        for row in rows:
            report = row["frontier_short_spot_route_requirements_report"]
            self.assertTrue(report["paper_only"])
            self.assertTrue(report["prepared_before_ranking_and_sizing"])
            self.assertEqual("unsupported", report["per_venue_status"]["status"])
            self.assertFalse(report["entry_blocked"])

    def test_short_proxy_report_is_non_blocking_and_supplies_rank_and_size(self) -> None:
        report = build_paper_route_requirement_report(
            {
                "venue": "CME_GROUP",
                "trade_type": "global_market_discovery_proxy",
                "direction": "short_proxy",
                "route_status": "standard",
                "api_access_status": "public_data_only",
            }
        )

        self.assertTrue(report["paper_only"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["applies"])
        self.assertEqual("short_proxy", report["candidate_kind"])
        self.assertLess(report["paper_rank_multiplier"], 1.0)
        self.assertEqual(
            report["paper_rank_multiplier"], report["paper_allocation_multiplier"]
        )
        self.assertFalse(report["hard_blocking"])
        self.assertFalse(report["entry_blocked"])

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

    def test_candidate_summary_exposes_all_required_route_fields_without_suppression(self) -> None:
        opportunity = {
            "venue": "OKX",
            "inst_id": "OKX:ARC-USDT",
            "direction": "long_perp_short_spot",
            "route_status": "conditional",
            "route_blockers": ["spot_borrow"],
            "required_permissions": ["crypto_derivatives", "spot_borrow"],
            "borrow_available": "unknown",
            "borrow_fee_bps_estimate": 12.5,
            "maker_fee_bps": 1.0,
            "taker_fee_bps": 3.0,
            "margin_required": True,
            "margin_mode": "isolated",
            "api_access_status": "public_data_only",
            "freshness_state": "fresh",
            "freshness_age_seconds": 4.0,
        }

        row = build_route_requirements_matrix([opportunity])[0]
        summary = build_candidate_route_requirement_summary(opportunity, row=row)

        self.assertTrue(summary["paper_only"])
        self.assertTrue(summary["read_only"])
        self.assertTrue(summary["candidate_remains_priceable"])
        self.assertFalse(summary["routing_decision_changed"])
        self.assertEqual("OKX", summary["candidate"]["venue"])
        self.assertIn(
            "spot_borrow",
            summary["broker_venue_eligibility"]["required_permissions"],
        )
        self.assertEqual(
            "unconfirmed",
            summary["short_borrow_availability"]["availability_status"],
        )
        self.assertEqual("isolated", summary["margin_mode"]["mode"])
        self.assertEqual(3.0, summary["fee_estimate"]["taker_bps"])
        self.assertEqual(
            "public_data_only",
            summary["api_entitlement"]["entitlement_status"],
        )
        self.assertEqual("fresh", summary["freshness"]["state"])
        self.assertEqual(4.0, summary["freshness"]["age_seconds"])

    def test_candidate_report_exposes_short_fee_api_and_order_type_friction_metadata(self) -> None:
        """Every paper candidate carries route facts without becoming an entry gate."""

        opportunity = {
            "venue": "TEST_VENUE",
            "inst_id": "TEST_VENUE:ARC-USDT",
            "direction": "long_perp_short_spot",
            "route_status": "conditional",
            "route_blockers": ["spot_borrow"],
            "required_permissions": ["crypto_spot", "margin_spot", "spot_borrow"],
            "shortability_status": True,
            "borrow_available": True,
            "margin_required": True,
            "margin_mode": "isolated",
            "fee_tier": "vip_1",
            "maker_fee_bps": 1.0,
            "taker_fee_bps": 3.0,
            "api_permission_status": "trade_permission_unconfirmed",
            "required_order_types": ["limit", "ioc"],
            "supported_order_types": ["market", "limit", "ioc"],
        }

        row = build_route_requirements_matrix([opportunity])[0]
        summary = build_candidate_route_requirement_summary(opportunity, row=row)

        self.assertEqual("available", row["shortability_status"])
        self.assertEqual("available", row["borrow_availability_status"])
        self.assertEqual("isolated", row["margin_spot_constraints"]["margin_mode"])
        self.assertEqual("vip_1", row["fee_tier"])
        self.assertEqual("trade_permission_unconfirmed", row["api_permission_status"])
        self.assertEqual("supported", row["order_type_support"]["status"])
        self.assertEqual(["limit", "ioc"], row["order_type_support"]["required_order_types"])
        self.assertEqual("available", summary["short_borrow_availability"]["shortability_status"])
        self.assertEqual("vip_1", summary["fee_estimate"]["fee_tier"])
        self.assertEqual("trade_permission_unconfirmed", summary["api_entitlement"]["entitlement_status"])
        self.assertEqual("supported", summary["order_type_support"]["status"])
        self.assertTrue(summary["candidate_remains_priceable"])
        self.assertFalse(summary["routing_decision_changed"])

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
