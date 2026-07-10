from __future__ import annotations

import copy
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
        self.assertIn("spot_borrow", report["route_intelligence"]["blocker_counts"])
        self.assertTrue(report["route_intelligence"]["potentially_executable_soon"])
        self.assertIn("route_decision_pack", report["route_intelligence"])
        self.assertEqual(
            report["route_intelligence"]["route_decision_pack"]["spot_borrow"]["route_feasibility"],
            "potentially_executable_after_borrow_or_margin_route_confirmation",
        )

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
        decision_pack = report["route_decision_pack"]
        self.assertTrue(decision_pack["spot_borrow"]["shadow_testing_can_continue"])
        self.assertEqual(decision_pack["prediction_markets_account"]["affected_opportunity_count"], 1)
        self.assertIn("No credentials are added.", decision_pack["venue_api_access"]["hard_limits"])


if __name__ == "__main__":
    unittest.main()
