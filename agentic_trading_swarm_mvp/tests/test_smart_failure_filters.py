from __future__ import annotations

import copy
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agent_review
import contextual_failure_filters as filters
import llm_bridge
import self_improvement
import storage
from settings import DEFAULT_SETTINGS


def settings() -> dict:
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    cfg["contextual_failure_filters"]["max_new_policies_per_loop"] = 5
    return cfg


def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    return conn


def group(
    *,
    signal_key: str = "OKX|perp_funding_basis|demo|standard",
    dimension: str,
    value: str,
    status: str,
    domain: str = "signal_quality",
    closed_count: int = 8,
    avg_pnl_bps: float = -50.0,
    win_rate: float = 0.25,
    worst_bps: float = -120.0,
) -> dict:
    return {
        "signal_key": signal_key,
        "dimension": dimension,
        "value": value,
        "context_filter": {} if dimension == "signal_family" else {dimension: value},
        "status": status,
        "failure_domain": domain,
        "closed_count": closed_count,
        "wins": int(closed_count * win_rate),
        "win_rate": win_rate,
        "recent_closed_count": min(10, closed_count),
        "recent_win_rate": win_rate,
        "avg_pnl_bps": avg_pnl_bps,
        "recent_avg_pnl_bps": avg_pnl_bps,
        "recent_delta_bps": 0.0,
        "best_bps": 40.0,
        "worst_bps": worst_bps,
        "failure_score": abs(avg_pnl_bps),
        "recovery_score": max(0.0, avg_pnl_bps),
    }


class SmartFailureFilterTests(unittest.TestCase):
    def test_low_sample_signals_are_observed_not_filtered(self) -> None:
        status = filters._classify({"closed_count": 2, "avg_pnl_bps": -200.0, "win_rate": 0.0}, settings(), "hour_utc")

        self.assertEqual(status, "low_sample_observe")

    def test_severe_repeat_loser_is_structural_failure_at_signal_level(self) -> None:
        metrics = {
            "closed_count": 12,
            "avg_pnl_bps": -90.0,
            "win_rate": 0.25,
            "worst_bps": -900.0,
        }

        status = filters._classify(metrics, settings(), "signal_family")

        self.assertEqual(status, "structural_failure")

    def test_contextual_policy_is_created_without_blanket_signal_policy(self) -> None:
        conn = memory_conn()
        groups = [
            group(dimension="signal_family", value="all", status="structural_failure", domain="signal_family"),
            group(dimension="hour_utc", value="01", status="contextual_failure"),
            group(dimension="hour_utc", value="02", status="working_slice", avg_pnl_bps=45.0, win_rate=0.7),
        ]

        created, skipped = filters._create_policies(conn, groups, settings())

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["group"]["context_filter"], {"hour_utc": "01"})
        self.assertEqual(skipped, [])

    def test_working_slices_inside_bad_families_are_protected(self) -> None:
        groups = [
            group(dimension="signal_family", value="all", status="structural_failure", domain="signal_family"),
            group(dimension="base_asset", value="ABC", status="working_slice", avg_pnl_bps=55.0, win_rate=0.75),
            group(dimension="base_asset", value="XYZ", status="contextual_failure"),
        ]

        protected = filters._protected_working_slices(groups)

        self.assertEqual(len(protected), 1)
        self.assertEqual(protected[0]["value"], "ABC")

    def test_recovery_candidate_does_not_create_new_strict_filter(self) -> None:
        conn = memory_conn()
        groups = [
            group(
                dimension="spread_bucket",
                value="tight",
                status="recovery_candidate",
                avg_pnl_bps=-8.0,
                win_rate=0.45,
            )
        ]

        created, skipped = filters._create_policies(conn, groups, settings())

        self.assertEqual(created, [])
        self.assertEqual(skipped, [])

    def test_route_and_data_quality_failures_are_diagnostic_only(self) -> None:
        conn = memory_conn()
        groups = [
            group(
                dimension="route_blocker",
                value="spot_borrow",
                status="contextual_failure",
                domain="route_or_feasibility",
            ),
            group(
                dimension="data_status",
                value="degraded",
                status="contextual_failure",
                domain="data_quality",
            ),
        ]

        created, skipped = filters._create_policies(conn, groups, settings())

        self.assertEqual(created, [])
        self.assertEqual({item["skip_reason"] for item in skipped}, {"diagnostic_only_route_or_data_issue"})

    def test_context_features_include_route_blocker_and_frontier_market_fields(self) -> None:
        candidate = {
            "venue": "MEXC",
            "inst_id": "MEXC:ABCUSDT",
            "direction": "short_frontier_spot",
            "base": "ABC",
            "quote": "USDT",
            "data_status": "reachable",
            "venue_deviation_bps": 42.0,
            "source_venue_count": 3,
            "execution_feasibility": {
                "status": "conditional",
                "route_blockers": ["spot_borrow"],
            },
        }

        features = filters.build_context_features(candidate)

        self.assertEqual(features["venue"], "MEXC")
        self.assertEqual(features["base_asset"], "ABC")
        self.assertEqual(features["quote_asset"], "USDT")
        self.assertEqual(features["route_blocker"], "spot_borrow")
        self.assertEqual(features["source_venue_count_bucket"], "few_venues")
        self.assertEqual(features["dislocation_bucket"], "large")

    def test_llm_failure_filter_policy_has_recovery_probe_contract(self) -> None:
        policy = self_improvement._policy_for_signal(
            {"avg_pnl_bps": -120.0, "win_rate": 0.2, "closed_count": 8},
            settings(),
        )

        self.assertTrue(policy["pause_entries"])
        self.assertTrue(policy["allow_recovery_probes"])
        self.assertEqual(policy["recovery_probe_every_n_reviews"], 25)
        self.assertIn("release_criteria", policy)

    def test_paused_policy_allows_tiny_recovery_probe_when_due(self) -> None:
        cfg = settings()
        candidate = {
            "venue": "OKX",
            "inst_id": "BTC-USDT-SWAP",
            "direction": "short_perp_long_spot",
            "trade_type": "perp_funding_basis",
            "score": 60.0,
            "funding_bps": 10.0,
            "basis_bps": 20.0,
            "edge_bps_estimate": 20.0,
            "liquidity_score": 0.9,
            "spread_bps": 1.0,
            "change_24h_pct": 0.0,
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
        }
        policy = {
            "policy_id": "p1",
            "signal_key": "OKX|perp_funding_basis|short_perp_long_spot|standard",
            "market_key": "OKX|perp_funding_basis",
            "policy_type": "failure_filter",
            "allocation_multiplier": 0.0,
            "pause_entries": True,
            "min_score_delta": 12.0,
            "min_net_edge_bps": 8.0,
            "max_spread_bps": 4.0,
            "applied_count": 24,
            "policy": {
                "allow_recovery_probes": True,
                "recovery_probe_every_n_reviews": 25,
                "recovery_probe_allocation_multiplier": 0.1,
            },
        }

        review = agent_review.review_candidate(candidate, cfg, {}, [policy])

        self.assertEqual(review["decision"], "approve_paper_trade")
        self.assertEqual(review["paper_allocation_multiplier"], 0.1)
        self.assertTrue(review["applied_policies"][0]["recovery_probe"])

    def test_implemented_manual_categories_do_not_reopen_duplicate_tasks(self) -> None:
        conn = memory_conn()
        conn.execute(
            """
            insert into improvement_tasks (created_at, priority, title, rationale, status)
            values ('now', 85, 'done route requirements', 'done', 'implemented_route_requirements')
            """
        )
        conn.commit()

        llm_bridge._apply_recommendation(
            conn,
            "propose_build_task",
            "Define Execution Route Requirements for Conditional Opportunities",
            "Document route requirements for conditional opportunities.",
            85,
            {"proposed_change": "Document route requirements for conditional opportunities."},
        )
        rows = conn.execute("select title from improvement_tasks where status = 'open'").fetchall()
        self.assertEqual(rows, [])

        result = self_improvement._execute_route_resolver(
            conn,
            {
                "recommendation_id": "r1",
                "title": "Define Execution Route Requirements",
                "payload": {
                    "title": "Define Execution Route Requirements for Conditional Opportunities",
                    "market_key": "execution_routes",
                    "signal_key": "conditional",
                    "rationale": "Route requirements for conditional opportunities.",
                },
            },
        )
        self.assertEqual(result[0]["action_status"], "skipped")
        self.assertEqual(result[0]["skip_reason"], "route_requirements_already_implemented")


if __name__ == "__main__":
    unittest.main()
