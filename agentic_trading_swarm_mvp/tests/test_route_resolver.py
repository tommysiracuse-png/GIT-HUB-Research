from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import route_resolver
from settings import DEFAULT_SETTINGS


def settings() -> dict:
    return copy.deepcopy(DEFAULT_SETTINGS)


class RouteResolverTests(unittest.TestCase):
    def test_standard_short_proxy_receives_refreshed_route_report_and_paper_sizing(self) -> None:
        enriched = route_resolver.enrich_candidate_with_route(
            {
                "venue": "CME_GROUP",
                "trade_type": "global_market_discovery_proxy",
                "direction": "short_proxy",
                "asset_class": "equity_or_proxy",
                "score": 100.0,
            },
            settings(),
        )

        report = enriched["paper_route_requirement_report"]
        self.assertTrue(report["applies"])
        self.assertEqual("short_proxy", report["candidate_kind"])
        self.assertEqual(report, enriched["execution_route"]["paper_route_requirement_report"])
        self.assertLess(enriched["score"], 100.0)
        self.assertGreater(enriched["paper_allocation_multiplier"], 0.0)
        self.assertLess(enriched["paper_allocation_multiplier"], 1.0)
        self.assertFalse(enriched.get("paper_entry_blocked", False))

    def test_unmapped_conditional_short_is_down_ranked_without_a_new_paper_block(self) -> None:
        enriched = route_resolver.enrich_candidate_with_route(
            {
                "venue": "UNMAPPED",
                "trade_type": "research",
                "direction": "short_signal",
                "asset_class": "research",
                "score": 100.0,
            },
            settings(),
        )

        diagnostics = enriched["conditional_short_route_diagnostics"]
        self.assertTrue(diagnostics["applies"])
        self.assertLess(enriched["score"], 100.0)
        self.assertFalse(enriched["paper_route_eligibility"]["suppressed"])
        self.assertFalse(enriched.get("paper_entry_blocked", False))
        self.assertTrue(enriched["conditional_short_execution_risk_downrank_applied"])

    def test_conditional_short_is_enriched_with_per_venue_route_intelligence(self) -> None:
        enriched = route_resolver.enrich_candidate_with_route(
            {
                "venue": "MEXC",
                "trade_type": "frontier_crypto_venue_map",
                "direction": "short_frontier_spot",
                "asset_class": "crypto_spot",
                "score": 80.0,
                "data_status": "reachable",
            },
            settings(),
        )

        packet = enriched["conditional_short_route_intelligence"]
        self.assertTrue(packet["paper_only"])
        self.assertTrue(packet["read_only"])
        self.assertEqual("MEXC", packet["venue"])
        self.assertIn("spot_short", packet["shorting_requirements"])
        self.assertEqual("unavailable", packet["borrow_availability"])
        self.assertEqual("unsupported", packet["margin_mode"])
        self.assertEqual("maintained_paper_route_registry", packet["fee_class"])
        self.assertEqual(2, packet["maker_fee_bps"])
        self.assertEqual(10, packet["taker_fee_bps"])
        self.assertEqual("public_data_only", packet["api_permission_status"])
        self.assertEqual(packet, enriched["execution_route"]["conditional_short_route_intelligence"])
        self.assertEqual("down_rank_only", packet["ranking_action"])
        self.assertFalse(packet["hard_blocking"])
        self.assertEqual("MEXC", packet["venue_capability_profile"])
        self.assertFalse(enriched["paper_route_eligibility"]["suppressed"])
        self.assertEqual("paper_observation", enriched["paper_route_eligibility"]["route_decision"])
        self.assertIn(
            "venue_spot_short_capability_unconfirmed",
            enriched["paper_route_eligibility"]["route_diagnostic_reasons"],
        )
        self.assertGreater(enriched["score"], 0.0)
        self.assertLess(enriched["score"], 80.0)
        self.assertFalse(enriched.get("paper_entry_blocked", False))
        frontier_intelligence = enriched["frontier_short_spot_route_intelligence"]
        self.assertEqual("needs route validation", frontier_intelligence["route_validation_status"])
        self.assertFalse(frontier_intelligence["entry_blocked"])
        self.assertEqual(10, frontier_intelligence["fee_estimates"]["taker_fee_bps"])
        self.assertEqual(
            frontier_intelligence,
            enriched["execution_route"]["frontier_short_spot_route_intelligence"],
        )

    def test_enrichment_tags_requirements_gaps_without_changing_route_decision(self) -> None:
        candidate = {
            "venue": "OKX",
            "trade_type": "perp_funding_basis",
            "direction": "long_perp_short_spot",
            "asset_class": "crypto_derivatives",
            "score": 73.0,
            "freshness_state": "stale",
        }
        resolved = route_resolver.resolve_candidate_route(candidate, settings())
        enriched = route_resolver.enrich_candidate_with_route(candidate, settings())

        panel = enriched["route_requirements_panel"]
        self.assertEqual(resolved["route_status"], enriched["route_status"])
        self.assertEqual(panel, enriched["execution_route"]["route_requirements_panel"])
        self.assertIn("borrow_availability", enriched["route_requirement_gaps"])
        self.assertIn("stale_data", enriched["route_requirement_gaps"])
        self.assertEqual("stale", panel["stale_data_status"])
        self.assertTrue(enriched["paper_route_sizing_guidance"]["non_blocking"])
        self.assertFalse(enriched["paper_route_sizing_guidance"]["routing_decision_changed"])
        self.assertTrue(enriched["paper_route_guard_value_measurement"]["enabled"])
        self.assertFalse(enriched.get("paper_entry_blocked", False))

    def test_okx_short_perp_route_is_standard(self) -> None:
        candidate = {
            "venue": "OKX",
            "trade_type": "perp_funding_basis",
            "direction": "short_perp_long_spot",
            "asset_class": "crypto_derivatives",
            "score": 66.0,
        }
        route = route_resolver.resolve_candidate_route(candidate, settings())

        self.assertEqual(route["route_id"], "okx_derivatives_paper")
        self.assertEqual(route["route_status"], "standard")
        self.assertEqual(route["missing_permissions"], [])
        self.assertTrue(route["requirements"])

    def test_okx_long_perp_short_spot_requires_spot_borrow(self) -> None:
        candidate = {
            "venue": "OKX",
            "trade_type": "perp_funding_basis",
            "direction": "long_perp_short_spot",
            "asset_class": "crypto_derivatives",
            "score": 73.0,
        }
        route = route_resolver.resolve_candidate_route(candidate, settings())

        self.assertEqual(route["route_id"], "conditional_crypto_route_paper")
        self.assertEqual(route["route_status"], "conditional")
        self.assertIn("spot_borrow", route["missing_permissions"])
        self.assertEqual(route["best_route_alternative"]["status"], "paper_testable_proxy")
        self.assertEqual(route["best_route_alternative"]["route_id"], "okx_derivatives_paper")
        self.assertTrue(route["route_next_actions"])
        self.assertGreaterEqual(route["route_probe_priority"], 70)

    def test_polymarket_route_is_conditional_with_account_api_and_jurisdiction(self) -> None:
        candidate = {
            "venue": "POLYMARKET",
            "trade_type": "prediction_market",
            "direction": "buy_yes_event",
            "asset_class": "prediction_markets",
            "score": 72.0,
        }
        route = route_resolver.resolve_candidate_route(candidate, settings())

        self.assertEqual(route["route_id"], "polymarket_events")
        self.assertEqual(route["route_status"], "conditional")
        self.assertEqual(
            set(route["missing_permissions"]),
            {"prediction_markets_account", "venue_api_access", "jurisdiction_eligibility"},
        )
        self.assertEqual(route["best_route_alternative"]["status"], "paper_testable_research")
        self.assertEqual(route["best_route_alternative"]["route_id"], "prediction_market_public_research_paper")

    def test_public_polymarket_adapter_cannot_be_promoted_to_direct_route(self) -> None:
        cfg = settings()
        cfg["account_capabilities"]["prediction_markets"] = True
        candidate = {
            "venue": "POLYMARKET",
            "trade_type": "prediction_market_probability",
            "direction": "buy_yes_event",
            "asset_class": "event_contract",
            "score": 72.0,
            "paper_only": True,
            "read_only": True,
            "execution_feasibility": {
                "public_data_only": True,
                "live_execution_supported": False,
                "order_routing_disabled": True,
            },
        }

        route = route_resolver.resolve_candidate_route(candidate, cfg)

        self.assertEqual(route["route_status"], "conditional")
        self.assertEqual(
            set(route["missing_permissions"]),
            {"prediction_markets_account", "venue_api_access", "jurisdiction_eligibility"},
        )
        self.assertEqual(
            route["best_route_alternative"]["route_id"],
            "prediction_market_public_research_paper",
        )
        self.assertTrue(any("order routing are disabled" in note for note in route["route_notes"]))

    def test_public_kalshi_adapter_cannot_be_promoted_to_direct_route(self) -> None:
        cfg = settings()
        cfg["account_capabilities"]["prediction_markets"] = True
        candidate = {
            "venue": "KALSHI",
            "trade_type": "prediction_market_probability",
            "direction": "buy_yes_event",
            "asset_class": "event_contract",
            "score": 72.0,
            "paper_only": True,
            "read_only": True,
            "execution_disabled": True,
            "order_routing_disabled": True,
            "execution_feasibility": {
                "public_data_only": True,
                "live_execution_supported": False,
            },
        }

        route = route_resolver.resolve_candidate_route(candidate, cfg)

        self.assertEqual(route["route_status"], "conditional")
        self.assertEqual(
            set(route["missing_permissions"]),
            {"prediction_markets_account", "venue_api_access", "jurisdiction_eligibility"},
        )
        self.assertEqual(
            route["best_route_alternative"]["route_id"],
            "prediction_market_public_research_paper",
        )
        self.assertTrue(any("Kalshi candidate" in note for note in route["route_notes"]))

    def test_watch_only_route_is_blocked(self) -> None:
        candidate = {
            "venue": "OKX",
            "trade_type": "perp_funding_basis",
            "direction": "watch_only",
            "asset_class": "crypto_derivatives",
            "score": 25.0,
        }
        route = route_resolver.resolve_candidate_route(candidate, settings())

        self.assertEqual(route["route_id"], "watch_only")
        self.assertEqual(route["route_status"], "blocked")

    def test_unknown_venue_route_is_route_unknown(self) -> None:
        candidate = {
            "venue": "UNKNOWN_EXCHANGE",
            "trade_type": "local_special",
            "direction": "buy_local_asset",
            "asset_class": "frontier_equity",
            "score": 81.0,
        }
        route = route_resolver.resolve_candidate_route(candidate, settings())

        self.assertEqual(route["route_id"], "local_or_specialist_broker")
        self.assertEqual(route["route_status"], "route_unknown")
        self.assertIn("broker_or_venue", route["missing_permissions"])
        self.assertIn("api_or_manual_workflow", route["missing_permissions"])

    def test_global_market_discovery_proxy_uses_existing_equity_paper_route(self) -> None:
        candidate = {
            "venue": "B3",
            "trade_type": "global_market_discovery_proxy",
            "direction": "long_proxy",
            "asset_class": "equity_or_proxy",
            "score": 78.0,
        }
        route = route_resolver.resolve_candidate_route(candidate, settings())

        self.assertEqual(route["route_id"], "equity_proxy_paper")
        self.assertEqual(route["route_status"], "standard")
        self.assertEqual(route["missing_permissions"], [])
        self.assertIn("Global discovery proxy exposure", route["route_notes"][0])

    def test_paper_enrichment_ranks_context_adjusted_net_edge_without_entry_block(self) -> None:
        common = {
            "venue": "B3",
            "trade_type": "global_market_discovery_proxy",
            "direction": "long_proxy",
            "asset_class": "equity_or_proxy",
            "gross_edge_bps_estimate": 80.0,
            "liquidity_score": 0.9,
            "spread_bps": 2.0,
            "freshness_age_seconds": 5.0,
            "regime_stability_score": 0.9,
        }
        candidates = route_resolver.enrich_candidates(
            [
                {
                    **common,
                    "inst_id": "B3:FRAGILE",
                    "score": 95.0,
                    "venue_quality": {"venue_quality_score": 30.0},
                    "liquidity_score": 0.1,
                    "spread_bps": 20.0,
                    "freshness_age_seconds": 800.0,
                    "regime_stability": "unstable",
                },
                {
                    **common,
                    "inst_id": "B3:TRANSPORTABLE",
                    "score": 60.0,
                    "venue_quality": {"venue_quality_score": 95.0},
                },
            ],
            settings(),
        )

        self.assertEqual("B3:TRANSPORTABLE", candidates[0]["inst_id"])
        fragile = candidates[1]
        self.assertIn("paper_context_attribution", fragile)
        self.assertIn("thin_liquidity_depth", fragile["paper_context_ranking_reasons"])
        self.assertNotIn("paper_entry_blocked", fragile)

    def test_global_proxy_shock_reversal_uses_only_the_existing_paper_equity_routes(self) -> None:
        long_route = route_resolver.resolve_candidate_route(
            {
                "venue": "YAHOO_PROXY",
                "trade_type": "global_proxy_shock_reversal",
                "direction": "long_proxy",
                "asset_class": "equity_proxy",
                "score": 78.0,
            },
            settings(),
        )
        short_route = route_resolver.resolve_candidate_route(
            {
                "venue": "YAHOO_PROXY",
                "trade_type": "global_proxy_shock_reversal",
                "direction": "short_proxy",
                "asset_class": "equity_proxy",
                "score": 78.0,
            },
            settings(),
        )

        self.assertEqual("equity_proxy_paper", long_route["route_id"])
        self.assertEqual("standard", long_route["route_status"])
        self.assertEqual("conditional_equity_route_paper", short_route["route_id"])
        self.assertIn(short_route["route_status"], {"conditional", "blocked"})
        self.assertEqual("public_data_only", short_route["api_access_status"])

    def test_enriched_candidate_preserves_legacy_feasibility_fields(self) -> None:
        candidate = {
            "venue": "OKX",
            "trade_type": "perp_funding_basis",
            "direction": "long_perp_short_spot",
            "asset_class": "crypto_derivatives",
            "score": 73.0,
        }
        enriched = route_resolver.enrich_candidate_with_route(candidate, settings())
        feasibility = enriched["execution_feasibility"]

        self.assertEqual(feasibility["status"], "conditional")
        self.assertEqual(feasibility["route_id"], "conditional_crypto_route_paper")
        self.assertEqual(feasibility["route_status"], "conditional")
        self.assertEqual(feasibility["missing_requirements"], enriched["execution_route"]["missing_permissions"])
        self.assertIn("requirements", feasibility)
        self.assertIn("route_next_actions", feasibility)

    def test_report_summary_ranks_manual_actions(self) -> None:
        candidates = route_resolver.enrich_candidates(
            [
                {
                    "venue": "OKX",
                    "trade_type": "perp_funding_basis",
                    "direction": "long_perp_short_spot",
                    "asset_class": "crypto_derivatives",
                    "score": 73.0,
                },
                {
                    "venue": "POLYMARKET",
                    "trade_type": "prediction_market",
                    "direction": "buy_yes_event",
                    "asset_class": "prediction_markets",
                    "score": 72.0,
                },
            ],
            settings(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            old_json = route_resolver.REPORT_JSON
            old_md = route_resolver.REPORT_MD
            old_intel_json = route_resolver.ROUTE_INTELLIGENCE_JSON
            old_intel_md = route_resolver.ROUTE_INTELLIGENCE_MD
            route_resolver.REPORT_JSON = pathlib.Path(tmp) / "route_report.json"
            route_resolver.REPORT_MD = pathlib.Path(tmp) / "route_report.md"
            route_resolver.ROUTE_INTELLIGENCE_JSON = pathlib.Path(tmp) / "route_intel.json"
            route_resolver.ROUTE_INTELLIGENCE_MD = pathlib.Path(tmp) / "route_intel.md"
            try:
                report = route_resolver.write_route_resolver_report(candidates, settings())
                primary_markdown = route_resolver.REPORT_MD.read_text(encoding="utf-8")
                intelligence_markdown = route_resolver.ROUTE_INTELLIGENCE_MD.read_text(encoding="utf-8")
                intelligence_sidecar = json.loads(
                    route_resolver.ROUTE_INTELLIGENCE_JSON.read_text(encoding="utf-8")
                )
            finally:
                route_resolver.REPORT_JSON = old_json
                route_resolver.REPORT_MD = old_md
                route_resolver.ROUTE_INTELLIGENCE_JSON = old_intel_json
                route_resolver.ROUTE_INTELLIGENCE_MD = old_intel_md

        summary = report["summary"]
        self.assertIn("by_requirement_category", summary)
        self.assertIn("by_requirement_id", summary)
        self.assertTrue(summary["top_manual_actions"])
        self.assertIn("route_intelligence", report)
        self.assertIn("paper_context_ranking", report)
        self.assertTrue(report["paper_context_ranking"]["paper_only"])
        self.assertIn("## Paper Context Ranking", primary_markdown)
        self.assertIn("spot_borrow", report["route_intelligence"]["blocker_counts"])
        self.assertGreaterEqual(summary["paper_proxy_available_count"], 1)
        self.assertGreaterEqual(summary["paper_research_available_count"], 1)
        self.assertTrue(report["route_intelligence"]["potentially_executable_soon"])
        self.assertIn("route_decision_pack", report["route_intelligence"])
        self.assertEqual(
            report["route_intelligence"]["route_decision_pack"]["spot_borrow"]["route_feasibility"],
            "potentially_executable_after_borrow_or_margin_route_confirmation",
        )
        requirements_intel = report["route_requirements_intel"]
        self.assertTrue(requirements_intel["paper_only"])
        self.assertTrue(requirements_intel["promotion_review"]["required_before_route_promotion"])
        self.assertEqual("report_only", requirements_intel["promotion_review"]["mode"])
        short_spot = next(
            row
            for row in requirements_intel["routes"]
            if row["direction"] == "long_perp_short_spot"
        )
        self.assertIn("spot_borrow", short_spot["route_blockers"])
        self.assertTrue(short_spot["borrow_required"])
        self.assertTrue(short_spot["margin_required"])
        self.assertEqual(
            "public_data_only_private_or_order_endpoint_unconfirmed",
            short_spot["endpoint_constraints"],
        )
        self.assertIn("Pre-Promotion Route Requirements Intel", primary_markdown)
        self.assertIn("Pre-Promotion Route Requirements Intel", intelligence_markdown)
        self.assertIn("api_path_readiness", primary_markdown)
        self.assertIn("guard_value_measurement", intelligence_markdown)
        self.assertIn("route_requirement_gaps", short_spot)
        self.assertIn("route_requirements_intel", intelligence_sidecar)
        self.assertEqual(
            requirements_intel["candidate_route_requirement_summaries"],
            [row["route_requirement_summary"] for row in requirements_intel["routes"]],
        )
        summary = short_spot["route_requirement_summary"]
        self.assertTrue(summary["paper_only"])
        self.assertTrue(summary["candidate_remains_priceable"])
        self.assertFalse(summary["routing_decision_changed"])
        self.assertIn("broker_venue_eligibility", summary)
        self.assertIn("short_borrow_availability", summary)
        self.assertIn("margin_mode", summary)
        self.assertIn("fee_estimate", summary)
        self.assertIn("api_entitlement", summary)
        self.assertIn("freshness", summary)
        self.assertIn("Candidate Route Requirement Summary", primary_markdown)
        self.assertIn("Candidate Route Requirement Summary", intelligence_markdown)

    def test_route_intelligence_is_read_only_and_ranks_blockers(self) -> None:
        candidates = route_resolver.enrich_candidates(
            [
                {
                    "venue": "GATE",
                    "trade_type": "frontier_crypto_venue_map",
                    "direction": "short_frontier_spot",
                    "asset_class": "crypto_spot",
                    "base": "DOGE",
                    "score": 88.0,
                    "data_status": "reachable",
                },
                {
                    "venue": "POLYMARKET",
                    "trade_type": "prediction_market_probability",
                    "direction": "buy_yes_event",
                    "asset_class": "event_contract",
                    "score": 91.0,
                },
            ],
            settings(),
        )

        report = route_resolver.summarize_route_intelligence(candidates)

        self.assertTrue(report["read_only"])
        self.assertEqual(report["spot_borrow_assets"]["DOGE"], 1)
        self.assertIn("prediction_markets_account", report["blocker_counts"])
        self.assertEqual(report["potentially_executable_soon_count"], 2)
        self.assertEqual(report["paper_proxy_available_count"], 1)
        self.assertEqual(report["paper_research_available_count"], 1)
        decision_pack = report["route_decision_pack"]
        self.assertTrue(decision_pack["spot_borrow"]["shadow_testing_can_continue"])
        self.assertEqual(decision_pack["prediction_markets_account"]["affected_opportunity_count"], 1)
        self.assertIn("No credentials are added.", decision_pack["venue_api_access"]["hard_limits"])


if __name__ == "__main__":
    unittest.main()
