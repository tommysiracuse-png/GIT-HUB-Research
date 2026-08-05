from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_review import review_candidate  # noqa: E402
from execution_engine import execute_order  # noqa: E402
from paper_context_cost import (  # noqa: E402
    annotate_paper_context_cost,
    paper_context_cost_gate,
    paper_context_cost_report,
    paper_context_attribution_score,
    paper_context_transfer_score,
    rank_paper_candidates_by_context,
    realized_paper_cost_audit,
)
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db, open_paper_trade, record_due_horizon_outcomes  # noqa: E402


def frontier_candidate(**overrides: object) -> dict:
    candidate = {
        "venue": "COINBASE",
        "inst_id": "COINBASE:BTC-USD",
        "trade_type": "frontier_crypto_venue_map",
        "direction": "long_frontier_spot",
        "score": 80.0,
        "last": 100.0,
        "funding_bps": 0.0,
        "basis_bps": 0.0,
        "gross_edge_bps_estimate": 40.0,
        "edge_bps_estimate": 18.0,
        "estimated_round_trip_cost_bps": 20.0,
        "liquidity_score": 0.9,
        "spread_bps": 3.0,
        "freshness_age_seconds": 10.0,
        "recent_volatility_bps": 10.0,
        "change_24h_pct": 1.0,
        "execution_feasibility": {"status": "standard", "legs": ["paper spot leg"]},
    }
    candidate.update(overrides)
    return candidate


class PaperContextCostFloorTests(unittest.TestCase):
    def test_context_attribution_down_ranks_fragile_net_edge_without_blocking_exploration(self) -> None:
        healthy = frontier_candidate(
            gross_edge_bps_estimate=100.0,
            venue_quality={"venue_quality_score": 95.0},
            liquidity_score=0.9,
            spread_bps=2.0,
            freshness_age_seconds=5.0,
            regime_stability_score=0.95,
        )
        fragile = frontier_candidate(
            inst_id="COINBASE:ETH-USD",
            gross_edge_bps_estimate=100.0,
            venue_quality={"venue_quality_score": 35.0},
            liquidity_score=0.15,
            spread_bps=20.0,
            freshness_age_seconds=80.0,
            regime_stability="unstable",
            borrow_cost_bps_horizon=12.0,
        )

        healthy_attribution = paper_context_attribution_score(healthy, DEFAULT_SETTINGS)
        fragile_attribution = paper_context_attribution_score(fragile, DEFAULT_SETTINGS)

        self.assertTrue(healthy_attribution["paper_only"])
        self.assertTrue(healthy_attribution["ranking_only"])
        self.assertGreater(
            healthy_attribution["context_adjusted_expected_net_edge_bps"],
            fragile_attribution["context_adjusted_expected_net_edge_bps"],
        )
        self.assertIn("low_venue_quality", fragile_attribution["ranking_reasons"])
        self.assertIn("wide_spread_burden", fragile_attribution["ranking_reasons"])
        self.assertIn("thin_liquidity_depth", fragile_attribution["ranking_reasons"])
        self.assertIn("unstable_regime", fragile_attribution["ranking_reasons"])
        self.assertNotIn("paper_entry_blocked", fragile_attribution)

        ranked = rank_paper_candidates_by_context([fragile, healthy], DEFAULT_SETTINGS)
        self.assertEqual("COINBASE:BTC-USD", ranked[0]["inst_id"])
        self.assertEqual(
            healthy_attribution["context_adjusted_expected_net_edge_bps"],
            ranked[0]["paper_context_rank_score"],
        )

    def test_effective_cost_is_additive_and_buffer_comparison_is_strict(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["paper_context_cost_floor"].update(
            {
                "safety_multiplier": 1.0,
                "min_net_edge_buffer_bps": 2.0,
                "frontier_tail_buffer_bps": 0.0,
            }
        )
        candidate = frontier_candidate(
            predicted_edge_bps=17.0,
            gross_edge_bps_estimate=None,
            estimated_round_trip_cost_bps=None,
            half_spread_bps=2.0,
            slippage_bps=3.0,
            latency_decay_bps=1.0,
            carry_bps_horizon=4.0,
            volatility_tail_buffer_bps=5.0,
            liquidity_score=1.0,
            signal_age_seconds=1.0,
            freshness_age_seconds=None,
        )

        gate = paper_context_cost_gate(candidate, settings)

        self.assertEqual(15.0, gate["effective_cost_bps"])
        self.assertEqual(17.0, gate["required_gross_edge_bps"])
        self.assertEqual(0.0, gate["gate_margin_bps"])
        self.assertFalse(gate["paper_eligible"])
        self.assertEqual("effective_cost_exceeds_edge", gate["veto_reason"])
        self.assertEqual(
            gate["effective_cost_bps"],
            round(sum(gate["components_bps"][field] for field in (
                "half_spread_bps",
                "slippage_bps",
                "latency_decay_bps",
                "carry_bps_horizon",
                "volatility_tail_buffer_bps",
            )), 3),
        )

        candidate["predicted_edge_bps"] = 17.001
        self.assertTrue(paper_context_cost_gate(candidate, settings)["paper_eligible"])

    def test_signal_age_must_be_strictly_below_context_limit(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["paper_context_cost_floor"]["frontier_max_signal_age_seconds"] = 10.0

        gate = paper_context_cost_gate(
            frontier_candidate(predicted_edge_bps=100.0, freshness_age_seconds=10.0),
            settings,
        )

        self.assertFalse(gate["eligible"])
        self.assertEqual("signal_too_old", gate["veto_reason"])
        self.assertIn("signal_age_limit_exceeded", gate["reasons"])

        missing_age = paper_context_cost_gate(
            frontier_candidate(predicted_edge_bps=100.0, freshness_age_seconds=None),
            settings,
        )
        self.assertFalse(missing_age["eligible"])
        self.assertEqual("missing_signal_age", missing_age["veto_reason"])

    def test_thin_gap_prone_frontier_and_unstable_funding_raise_cost(self) -> None:
        healthy = paper_context_cost_gate(
            frontier_candidate(predicted_edge_bps=100.0, liquidity_score=1.0),
            DEFAULT_SETTINGS,
        )
        thin_gap = paper_context_cost_gate(
            frontier_candidate(
                predicted_edge_bps=100.0,
                liquidity_score=0.1,
                recent_gap_bps=80.0,
            ),
            DEFAULT_SETTINGS,
        )
        stable_carry = paper_context_cost_gate(
            frontier_candidate(
                trade_type="perp_funding_basis",
                direction="funding_capture_short_perp",
                predicted_edge_bps=100.0,
                liquidity_score=1.0,
                funding_history_min_bps=4.0,
                funding_history_max_bps=4.0,
            ),
            DEFAULT_SETTINGS,
        )
        unstable_carry = paper_context_cost_gate(
            frontier_candidate(
                trade_type="perp_funding_basis",
                direction="funding_capture_short_perp",
                predicted_edge_bps=100.0,
                liquidity_score=0.2,
                funding_history_min_bps=-8.0,
                funding_history_max_bps=12.0,
            ),
            DEFAULT_SETTINGS,
        )

        self.assertGreater(thin_gap["effective_cost_bps"], healthy["effective_cost_bps"])
        self.assertGreater(
            unstable_carry["effective_cost_bps"],
            stable_carry["effective_cost_bps"],
        )
        self.assertGreater(
            unstable_carry["inputs"]["funding_instability_bps"],
            stable_carry["inputs"]["funding_instability_bps"],
        )

    def test_annotation_emits_structured_effective_cost_log(self) -> None:
        annotated = annotate_paper_context_cost(
            frontier_candidate(predicted_edge_bps=1.0),
            DEFAULT_SETTINGS,
        )

        log = annotated["paper_effective_cost_log"]
        self.assertEqual(
            {
                "predicted_edge_bps",
                "effective_cost_bps",
                "gate_margin_bps",
                "signal_age_seconds",
                "max_signal_age_seconds",
                "carry_bps_horizon",
                "spread_proxy_bps",
                "veto_reason",
                "paper_eligible",
            },
            set(log),
        )
        self.assertFalse(log["paper_eligible"])
        self.assertEqual("effective_cost_exceeds_edge", log["veto_reason"])

    def test_surface_defaults_charge_round_trip_spread_slippage_fees_and_carry(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["paper_context_cost_floor"].update(
            {
                "safety_multiplier": 1.0,
                "min_net_edge_buffer_bps": 0.0,
                "default_volatility_tail_buffer_bps": 0.0,
                "frontier_tail_buffer_bps": 0.0,
            }
        )
        common = {
            "predicted_edge_bps": 100.0,
            "spread_bps": 4.0,
            "liquidity_score": 1.0,
            "freshness_age_seconds": 1.0,
            "latency_decay_bps": 0.0,
            "volatility_tail_buffer_bps": 0.0,
            "execution_feasibility": {"status": "standard"},
        }

        proxy = paper_context_cost_gate(
            {**common, "trade_type": "global_proxy_momentum", "direction": "long_proxy"},
            settings,
        )
        frontier = paper_context_cost_gate(
            {**common, "trade_type": "frontier_crypto_venue_map", "direction": "long_frontier_spot"},
            settings,
        )
        carry = paper_context_cost_gate(
            {
                **common,
                "trade_type": "perp_funding_basis",
                "direction": "long_perp_short_spot",
                "funding_bps": 4.0,
                "funding_interval_hours": 8.0,
            },
            settings,
        )

        self.assertEqual(4.0, proxy["components_bps"]["round_trip_spread_bps"])
        self.assertEqual(3.0, proxy["components_bps"]["slippage_bps"])
        self.assertEqual(1.0, proxy["components_bps"]["fees_bps"])
        self.assertEqual(6.0, frontier["components_bps"]["slippage_bps"])
        self.assertEqual(2.0, frontier["components_bps"]["fees_bps"])
        self.assertEqual(2, carry["inputs"]["leg_count"])
        self.assertEqual(12.0, carry["components_bps"]["slippage_bps"])
        self.assertEqual(4.0, carry["components_bps"]["fees_bps"])
        self.assertEqual(4.0, carry["components_bps"]["carry_bps_horizon"])

        settings["paper_context_cost_floor"]["surface_costs"]["frontier"] = {
            "slippage_bps_per_side": 7.0,
            "fee_bps_per_side": 2.0,
        }
        configured_frontier = paper_context_cost_gate(
            {**common, "trade_type": "frontier_crypto_venue_map", "direction": "long_frontier_spot"},
            settings,
        )
        self.assertEqual(14.0, configured_frontier["components_bps"]["slippage_bps"])
        self.assertEqual(4.0, configured_frontier["components_bps"]["fees_bps"])

    def test_annotation_exposes_canonical_runtime_diagnostics(self) -> None:
        annotated = annotate_paper_context_cost(
            frontier_candidate(
                gross_edge_bps_estimate=24.0,
                freshness_age_seconds=30.0,
            ),
            DEFAULT_SETTINGS,
        )

        self.assertEqual(24.0, annotated["gross_edge_bps"])
        self.assertEqual(0.5, annotated["freshness_minutes"])
        self.assertEqual(
            round(annotated["gross_edge_bps"] - annotated["modeled_cost_bps"], 3),
            annotated["net_edge_bps"],
        )
        self.assertEqual("effective_cost_exceeds_edge", annotated["gating_reason"])
        self.assertFalse(annotated["paper_eligible"])

    def test_cross_surface_runtime_report_prioritizes_gated_candidates(self) -> None:
        proxy = annotate_paper_context_cost(
            frontier_candidate(
                venue="YAHOO_PROXY",
                trade_type="global_proxy_momentum",
                direction="long_proxy",
                gross_edge_bps_estimate=5.0,
            ),
            DEFAULT_SETTINGS,
        )
        carry = annotate_paper_context_cost(
            frontier_candidate(
                venue="OKX",
                trade_type="perp_funding_basis",
                direction="short_perp_long_spot",
                gross_edge_bps_estimate=100.0,
            ),
            DEFAULT_SETTINGS,
        )

        report = paper_context_cost_report([carry, proxy])

        self.assertTrue(report["paper_only"])
        self.assertEqual(2, report["candidate_count"])
        self.assertEqual("YAHOO_PROXY", report["candidates"][0]["venue"])
        self.assertTrue(
            {
                "gross_edge_bps",
                "modeled_cost_bps",
                "net_edge_bps",
                "freshness_minutes",
                "gating_reason",
            }.issubset(report["candidates"][0])
        )

    def test_gross_edge_must_clear_safety_adjusted_context_floor(self) -> None:
        gate = paper_context_cost_gate(
            frontier_candidate(gross_edge_bps_estimate=24.0),
            DEFAULT_SETTINGS,
        )

        self.assertTrue(gate["applicable"])
        self.assertFalse(gate["eligible"])
        self.assertGreater(gate["required_gross_edge_bps"], 24.0)
        self.assertEqual(gate["safety_multiplier"], 1.25)
        self.assertIn("gross_edge_does_not_clear_context_cost_floor", gate["reasons"])

        stricter = copy.deepcopy(DEFAULT_SETTINGS)
        stricter["paper_context_cost_floor"]["safety_multiplier"] = 2.0
        self.assertGreater(
            paper_context_cost_gate(frontier_candidate(), stricter)["required_gross_edge_bps"],
            paper_context_cost_gate(frontier_candidate(), DEFAULT_SETTINGS)["required_gross_edge_bps"],
        )

    def test_freshness_liquidity_volatility_and_complexity_raise_floor(self) -> None:
        healthy = paper_context_cost_gate(frontier_candidate(), DEFAULT_SETTINGS)
        poor = paper_context_cost_gate(
            frontier_candidate(
                gross_edge_bps_estimate=100.0,
                liquidity_score=0.1,
                freshness_age_seconds=90.0,
                recent_volatility_bps=100.0,
                execution_leg_count=2,
            ),
            DEFAULT_SETTINGS,
        )

        self.assertGreater(poor["context_cost_floor_bps"], healthy["context_cost_floor_bps"])
        self.assertGreater(poor["components_bps"]["freshness"], 0.0)
        self.assertGreater(poor["components_bps"]["liquidity"], healthy["components_bps"]["liquidity"])
        self.assertGreater(poor["components_bps"]["volatility"], healthy["components_bps"]["volatility"])
        self.assertEqual(poor["components_bps"]["complexity"], 4.0)
        self.assertFalse(poor["eligible"])
        self.assertIn("liquidity_below_promotion_floor", poor["reasons"])
        self.assertLess(poor["score_multiplier"], healthy["score_multiplier"])

    def test_annotation_down_ranks_thin_stale_surface_without_mutation(self) -> None:
        candidate = frontier_candidate(
            gross_edge_bps_estimate=100.0,
            liquidity_score=0.1,
            freshness_age_seconds=90.0,
        )

        annotated = annotate_paper_context_cost(candidate, DEFAULT_SETTINGS)

        self.assertEqual(candidate["score"], 80.0)
        self.assertEqual(annotated["score_before_context_cost"], 80.0)
        self.assertLess(annotated["score"], 80.0)
        self.assertIn("paper_context_cost_gate", annotated)

    def test_context_transfer_discount_preserves_direct_same_surface_candidate(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["paper_context_cost_floor"]["enabled"] = False
        candidate = frontier_candidate(
            source_surface="spot",
            target_surface="spot",
            paper_route_type="direct",
            venue_tier="primary",
            paper_allocation_multiplier=0.8,
        )

        annotated = annotate_paper_context_cost(candidate, settings)
        transfer = annotated["paper_context_transfer_score"]

        self.assertEqual(1.0, transfer["confidence_multiplier"])
        self.assertEqual(80.0, annotated["score"])
        self.assertEqual(0.8, annotated["paper_allocation_multiplier"])
        self.assertTrue(annotated["paper_eligible"])
        self.assertNotIn("paper_entry_blocked", annotated)

    def test_context_transfer_discount_ranks_and_sizes_proxy_frontier_short(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["paper_context_cost_floor"]["enabled"] = False
        settings["paper_context_cost_floor"]["context_transfer_scoring"].update(
            {
                "distance_penalty": 0.10,
                "synthetic_route_multiplier": 0.95,
                "secondary_venue_multiplier": 0.95,
                "product_type_mismatch_multiplier": 0.95,
                "secondary_venue_short_multiplier": 0.95,
                "near_trade_threshold_score": 50.0,
                "near_threshold_band": 10.0,
                "near_threshold_allocation_multiplier": 0.4,
            }
        )
        candidate = frontier_candidate(
            direction="short_frontier_spot",
            source_surface="YAHOO_PROXY",
            target_surface="OKX_SPOT",
            source_target_distance=0.8,
            paper_route_type="synthetic_proxy",
            venue_tier="secondary",
            confidence=0.8,
        )

        annotated = annotate_paper_context_cost(candidate, settings)
        transfer = annotated["paper_context_transfer_score"]

        expected_multiplier = 0.92 * 0.95 * 0.95 * 0.95 * 0.95
        self.assertAlmostEqual(expected_multiplier, transfer["confidence_multiplier"], places=6)
        self.assertEqual(59.948, annotated["score"])
        self.assertEqual(0.4, annotated["paper_allocation_multiplier"])
        self.assertAlmostEqual(0.8 * expected_multiplier, annotated["confidence"], places=6)
        self.assertTrue(transfer["near_trade_threshold"])
        self.assertEqual(
            {
                "source_target_distance",
                "synthetic_route",
                "secondary_venue",
                "product_type_mismatch",
                "secondary_venue_short_asymmetry",
            },
            set(transfer["reasons"]),
        )
        self.assertTrue(annotated["paper_eligible"])
        self.assertNotIn("paper_entry_blocked", annotated)

    def test_context_transfer_scoring_is_inactive_outside_paper_mode(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["mode"] = "live"
        candidate = frontier_candidate(
            source_surface="YAHOO_PROXY",
            target_surface="OKX_SPOT",
            source_target_distance=1.0,
            paper_route_type="synthetic_proxy",
            venue_tier="secondary",
            direction="short_frontier_spot",
        )

        transfer = paper_context_transfer_score(candidate, settings)
        annotated = annotate_paper_context_cost(candidate, settings)

        self.assertFalse(transfer["enabled"])
        self.assertEqual(1.0, transfer["confidence_multiplier"])
        self.assertEqual(candidate["score"], annotated["score"])

    def test_review_tags_low_gross_edge_without_blocking_exploration(self) -> None:
        candidate = frontier_candidate(
            gross_edge_bps_estimate=24.0,
            edge_bps_estimate=18.0,
        )

        review = review_candidate(candidate, copy.deepcopy(DEFAULT_SETTINGS), {})

        self.assertEqual(review["decision"], "approve_paper_trade")
        self.assertTrue(
            any("paper context cost floor not cleared" in block for block in review["would_block_reasons"])
        )
        self.assertEqual([], review["hard_blocks"])
        self.assertFalse(review["paper_context_cost_gate"]["eligible"])

    def test_discovery_proxy_short_has_hard_liquidity_and_freshness_floor(self) -> None:
        candidate = frontier_candidate(
            venue="CME_GROUP",
            inst_id="ES=F",
            trade_type="global_market_discovery_proxy",
            direction="short_proxy",
            gross_edge_bps_estimate=80.0,
            liquidity_score=0.5,
            freshness_age_seconds=1200.0,
        )

        gate = paper_context_cost_gate(candidate, DEFAULT_SETTINGS)

        self.assertTrue(gate["applicable"])
        self.assertEqual("proxy", gate["family_kind"])
        self.assertFalse(gate["eligible"])
        self.assertIn("liquidity_below_promotion_floor", gate["reasons"])
        self.assertIn("freshness_above_promotion_ceiling", gate["reasons"])

    def test_route_friction_is_costed_and_missing_realized_cost_is_backfilled(self) -> None:
        candidate = frontier_candidate(
            gross_edge_bps_estimate=80.0,
            paper_route_eligibility={"assumed_route_cost_bps": 24.0},
            execution_feasibility={"status": "conditional"},
        )

        gate = paper_context_cost_gate(candidate, DEFAULT_SETTINGS)
        candidate["paper_context_cost_gate"] = gate
        audit = realized_paper_cost_audit(
            candidate,
            18.0,
            charged_cost_bps=10.0,
            settings=DEFAULT_SETTINGS,
        )

        self.assertGreaterEqual(gate["components_bps"]["route"], 4.0)
        self.assertTrue(audit["backfill_applied"])
        self.assertAlmostEqual(
            audit["adjusted_pnl_bps"],
            18.0 - audit["realized_cost_backfill_bps"],
        )

    def test_blocked_paper_route_cannot_clear_context_cost_gate(self) -> None:
        candidate = frontier_candidate(
            gross_edge_bps_estimate=200.0,
            execution_feasibility={"status": "blocked"},
        )

        gate = paper_context_cost_gate(candidate, DEFAULT_SETTINGS)
        annotated = annotate_paper_context_cost(candidate, DEFAULT_SETTINGS)

        self.assertFalse(gate["eligible"])
        self.assertEqual("route_status_not_paper_promotable", gate["gating_reason"])
        self.assertFalse(annotated["paper_eligible"])
        self.assertLess(annotated["score"], candidate["score"])

    def test_policy_is_paper_only_configurable_and_scope_limited(self) -> None:
        disabled = copy.deepcopy(DEFAULT_SETTINGS)
        disabled["paper_context_cost_floor"]["enabled"] = False
        candidate = frontier_candidate(gross_edge_bps_estimate=1.0)

        self.assertTrue(paper_context_cost_gate(candidate, disabled)["eligible"])
        carry = paper_context_cost_gate(
            {
                "trade_type": "perp_funding_basis",
                "predicted_edge_bps": 1.0,
                "signal_age_seconds": 1.0,
                "execution_feasibility": {"status": "standard"},
            },
            DEFAULT_SETTINGS,
        )
        self.assertTrue(carry["applicable"])
        self.assertEqual("carry", carry["family_kind"])
        unrelated = paper_context_cost_gate(
            {"trade_type": "prediction_market_dislocation", "gross_edge_bps_estimate": 1.0},
            DEFAULT_SETTINGS,
        )
        self.assertFalse(unrelated["applicable"])
        live_settings = copy.deepcopy(DEFAULT_SETTINGS)
        live_settings["mode"] = "live"
        live_gate = paper_context_cost_gate(candidate, live_settings)
        self.assertFalse(live_gate["enabled"])
        self.assertTrue(live_gate["eligible"])

    def test_fill_boundary_records_proxy_quarantine_but_allows_exploration(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "YAHOO_PROXY",
            "inst_id": "EWZ",
            "trade_type": "global_proxy_momentum",
            "direction": "long_proxy",
            "score": 70.0,
            "last": 25.0,
            "gross_edge_bps_estimate": 5.0,
            "edge_bps_estimate": 3.0,
            "spread_bps": 3.0,
            "liquidity_score": 0.9,
            "provider_age_seconds": 10.0,
            "execution_feasibility": {"status": "standard"},
        }
        approved_review = {
            "decision": "approve_paper_trade",
            "confidence": 0.8,
            "net_edge_bps_estimate": 3.0,
            "feasibility_status": "standard",
            "paper_allocation_multiplier": 1.0,
        }

        result = execute_order(conn, candidate, approved_review, DEFAULT_SETTINGS)

        self.assertTrue(result["paper_filled"])
        self.assertEqual(result["order"]["status"], "paper_filled")
        self.assertEqual(result["order"]["signal_stats_scope"], "direct")

    def test_new_outcome_persists_realized_cost_backfill_audit(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["learning"]["horizon_minutes"] = [0]
        candidate = {
            "venue": "CME_GROUP",
            "inst_id": "CME_GROUP:ES=F",
            "trade_type": "global_market_discovery_proxy",
            "direction": "short_proxy",
            "score": 70.0,
            "last": 100.0,
            "thesis": "paper-only cost audit fixture",
            "paper_context_cost_gate": {
                "paper_only": True,
                "applicable": True,
                "enabled": True,
                "eligible": True,
                "context_cost_floor_bps": 25.0,
                "inputs": {"route_status": "conditional"},
            },
        }
        review = {
            "learned_score": 70.0,
            "decision": "approve_conditional_paper_trade",
            "route_status": "conditional",
        }
        try:
            trade_id = open_paper_trade(conn, candidate, review, settings=settings)
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            recorded = record_due_horizon_outcomes(
                conn,
                {
                    candidate["inst_id"]: {
                        "last": 99.8,
                        "observed_at": now,
                        "venue": candidate["venue"],
                    }
                },
                settings,
            )
            row = conn.execute(
                "select pnl_bps, context_json from paper_trade_outcomes where trade_id = ?",
                (trade_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(1, len(recorded))
        audit = json.loads(row["context_json"])["paper_realized_cost_audit"]
        self.assertTrue(audit["backfill_applied"])
        self.assertEqual(row["pnl_bps"], audit["adjusted_pnl_bps"])


if __name__ == "__main__":
    unittest.main()
