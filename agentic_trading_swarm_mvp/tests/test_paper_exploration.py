from __future__ import annotations

import copy
import datetime as dt
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_review import review_candidate
from execution_engine import execute_order
from paper_admission_replay import compare_replays, replay_candidates
from paper_exploration import (
    SYNTHETIC_ROUTE_ID,
    fair_lineage_order,
    prepare_candidate_for_exploration,
)
from paper_exploration_report import (
    _reason_category,
    build_paper_exploration_report,
    compact_paper_exploration_report,
)
from route_resolver import _hedged_structure_dependency
from settings import DEFAULT_SETTINGS
from storage import connect, open_paper_trade, performance_summary, save_opportunity, signal_key


class PaperExplorationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.settings["mode"] = "paper"
        self.settings["allow_live_trading"] = False

    @staticmethod
    def candidate(**overrides: object) -> dict:
        base = {
            "seen_at": "2026-08-04T12:00:00+00:00",
            "venue": "TEST",
            "inst_id": "ABC-USD",
            "trade_type": "frontier_crypto_venue_map",
            "direction": "long_frontier_spot",
            "last": 100.0,
            "score": 1.0,
            "funding_bps": 0.0,
            "basis_bps": 0.0,
            "edge_bps_estimate": -20.0,
            "liquidity_score": 0.01,
            "spread_bps": 80.0,
            "change_24h_pct": 0.0,
            "stale_minutes": 0.0,
            "paper_entry_blocked": True,
            "execution_feasibility": {
                "status": "blocked",
                "route_status": "blocked",
                "route_id": "missing_route",
                "missing_requirements": ["account", "jurisdiction"],
            },
            "execution_route": {
                "route_id": "missing_route",
                "route_status": "blocked",
                "missing_permissions": ["account", "jurisdiction"],
            },
            "thesis": "test exploration candidate",
        }
        base.update(overrides)
        return base

    def test_route_blocked_low_quality_candidate_becomes_isolated_synthetic_trade(self) -> None:
        candidate = prepare_candidate_for_exploration(self.candidate(), self.settings)
        review = review_candidate(
            candidate,
            self.settings,
            {},
            policies=[
                {
                    "policy_id": "family",
                    "priority": 90,
                    "signal_key": signal_key(candidate),
                    "pause_entries": True,
                    "allocation_multiplier": 0.0,
                    "min_score_delta": 50.0,
                    "policy": {},
                }
            ],
        )
        self.assertEqual(SYNTHETIC_ROUTE_ID, candidate["route_id"])
        self.assertEqual("synthetic_research", candidate["signal_stats_scope"])
        self.assertEqual("approve_conditional_paper_trade", review["decision"])
        self.assertFalse(review["hard_blocks"])
        self.assertTrue(review["would_block_reasons"])
        self.assertGreater(review["paper_allocation_multiplier"], 0.0)
        self.assertTrue(signal_key(candidate).startswith("SYNTHETIC_RESEARCH|"))

    def test_cost_swallowed_frontier_candidate_is_shadow_observed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            candidate = prepare_candidate_for_exploration(self.candidate(), self.settings)
            review = review_candidate(candidate, self.settings, {}, policies=[])
            execution = execute_order(conn, candidate, review, self.settings)
            self.assertFalse(execution["paper_filled"])
            self.assertEqual("shadow_only", execution["order"]["status"])
            self.assertIsNone(execution["order_id"])
            self.assertEqual(0, conn.execute("select count(*) from paper_trades").fetchone()[0])
            self.assertEqual(
                "paper_net_edge_guard",
                conn.execute("select reject_reason from frontier_paper_shadow_observations").fetchone()[0],
            )
            conn.close()

    def test_invalid_price_and_dangerous_staleness_remain_hard_rejections(self) -> None:
        candidate = prepare_candidate_for_exploration(
            self.candidate(last=0.0, stale_minutes=180.0),
            self.settings,
        )
        review = review_candidate(candidate, self.settings, {}, policies=[])
        self.assertEqual("reject", review["decision"])
        self.assertIn("missing or invalid price", review["hard_blocks"])
        self.assertTrue(any("dangerously stale" in item for item in review["hard_blocks"]))

    def test_explicit_capacity_deferral_keeps_candidate_visible_but_prevents_fill(self) -> None:
        candidate = prepare_candidate_for_exploration(
            self.candidate(paper_experiment_capacity_deferred="resolution window is too near"),
            self.settings,
        )

        self.assertTrue(candidate["paper_entry_blocked"])
        self.assertTrue(candidate["shadow_filtered"])
        self.assertFalse(candidate["paper_fill_allowed"])
        self.assertEqual("paper_experiment_capacity_deferred", candidate["candidate_reject_reason"])

        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            review = review_candidate(candidate, self.settings, {}, policies=[])
            execution = execute_order(conn, candidate, review, self.settings)
            self.assertFalse(execution["paper_filled"])
            self.assertEqual("shadow_only", execution["order"]["status"])
            conn.close()

    def test_real_two_leg_candidate_requires_prices_or_explicit_proxy(self) -> None:
        candidate = prepare_candidate_for_exploration(
            self.candidate(
                venue="OKX",
                trade_type="perp_funding_basis",
                direction="short_perp_long_spot",
                execution_feasibility={"status": "standard", "route_status": "standard"},
                execution_route={"route_id": "okx_derivatives_paper", "route_status": "standard"},
            ),
            self.settings,
        )
        review = review_candidate(candidate, self.settings, {}, policies=[])
        self.assertEqual("reject", review["decision"])
        self.assertIn(
            "multi-leg strategy has unavailable leg prices and no explicit proxy",
            review["hard_blocks"],
        )

    def test_single_perpetual_funding_does_not_infer_hedge_from_words(self) -> None:
        candidate = self.candidate(
            venue="OKX",
            trade_type="perp_funding_basis",
            direction="funding_capture_long_perp",
            strategy="funding carry",
        )
        self.assertFalse(_hedged_structure_dependency(candidate))
        candidate["requires_hedge"] = True
        self.assertTrue(_hedged_structure_dependency(candidate))

    def test_policy_stack_is_limited_to_one_family_and_one_context(self) -> None:
        candidate = prepare_candidate_for_exploration(self.candidate(), self.settings)
        key = signal_key(candidate)
        policies = []
        for index in range(3):
            policies.append(
                {
                    "policy_id": f"family-{index}",
                    "priority": 80 + index,
                    "signal_key": key,
                    "pause_entries": True,
                    "allocation_multiplier": 0.0,
                    "policy": {},
                }
            )
            policies.append(
                {
                    "policy_id": f"context-{index}",
                    "priority": 80 + index,
                    "signal_key": key,
                    "pause_entries": True,
                    "allocation_multiplier": 0.0,
                    "policy": {"context_filter": {"venue": "TEST"}},
                }
            )
        review = review_candidate(candidate, self.settings, {}, policies=policies)
        self.assertEqual(2, len(review["applied_policies"]))
        self.assertTrue(all(not item["filtered"] for item in review["applied_policies"]))
        self.assertTrue(all(item["would_block"] for item in review["applied_policies"]))

    def test_lineage_order_rotates_and_interleaves(self) -> None:
        candidates = [
            self.candidate(inst_id=f"A-{index}", venue="A", score=100 - index)
            for index in range(4)
        ] + [
            self.candidate(inst_id=f"B-{index}", venue="B", score=80 - index)
            for index in range(4)
        ]
        ordered = fair_lineage_order(candidates, 0, self.settings)
        self.assertNotEqual(ordered[0]["venue"], ordered[1]["venue"])
        rotated = fair_lineage_order(candidates, 1, self.settings)
        self.assertNotEqual(ordered[0]["venue"], rotated[0]["venue"])

    def test_replay_detects_family_collapse(self) -> None:
        before = replay_candidates([self.candidate()], self.settings)
        after_settings = copy.deepcopy(self.settings)
        after_settings["paper_exploration"]["enabled"] = False
        after = replay_candidates([self.candidate()], after_settings)
        comparison = compare_replays(before, after)
        self.assertFalse(comparison["passed"])
        self.assertTrue(comparison["collapsed_lineages"])

    def test_report_canonicalizes_guard_values_and_excludes_legacy_rejections(self) -> None:
        self.assertEqual(
            "paper context cost floor not cleared",
            _reason_category("paper context cost floor not cleared: gross edge 12.2 bps must exceed 50.7 bps"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            try:
                seen_at = dt.datetime.now(dt.timezone.utc).isoformat()
                current = prepare_candidate_for_exploration(
                    self.candidate(seen_at=seen_at, last=0.0),
                    self.settings,
                )
                current_review = review_candidate(current, self.settings, {}, policies=[])
                legacy = self.candidate(seen_at=seen_at)
                legacy_review = dict(current_review, decision="reject", hard_blocks=["legacy score block"])
                save_opportunity(conn, legacy, legacy_review)
                save_opportunity(conn, current, current_review)
                report = build_paper_exploration_report(conn, self.settings)
                self.assertEqual(1, report["summary"]["true_invalid_data_rejections_24h"])
                self.assertNotIn("legacy score block", report["true_rejection_reasons_24h"])
                compact = compact_paper_exploration_report(
                    dict(report, guard_value=[{"guard_reason": str(index)} for index in range(20)])
                )
                self.assertEqual(10, len(compact["top_guard_value"]))
                self.assertNotIn("guard_value", compact)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
