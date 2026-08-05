import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from strategy_reliability import evaluate_paper_cell_policy, paper_signal_cell, paper_signal_cell_key


class TestSelfImprovementCellScope(unittest.TestCase):
    def test_signal_cell_key_distinguishes_sibling_cells(self) -> None:
        base = {
            "signal_family": "frontier_crypto_venue_map",
            "signal_key": "frontier_crypto_venue_map|alpha",
            "strategy": "frontier_crypto_venue_map_alpha",
            "variant": "v2",
            "venue": "GATE",
            "paper_route_status": "paper_testable_proxy",
        }
        long_cell = paper_signal_cell({**base, "direction": "long"})
        short_cell = paper_signal_cell({**base, "direction": "short"})

        self.assertNotEqual(long_cell["cell_key"], short_cell["cell_key"])
        self.assertEqual(long_cell["venue"], "GATE")
        self.assertEqual(long_cell["direction"], "long")
        self.assertEqual(long_cell["paper_route_status"], "paper_testable_proxy")

    def test_mixed_cells_are_decided_independently(self) -> None:
        promoted = evaluate_paper_cell_policy(
            {
                "signal_key": "frontier_crypto_venue_map|alpha",
                "strategy": "frontier_alpha",
                "variant": "v2",
                "venue": "OKX_SPOT",
                "direction": "long",
                "paper_route_status": "executable",
                "closed_count": 6,
                "avg_pnl_bps": 8.25,
                "win_rate": 0.67,
            }
        )
        reverted = evaluate_paper_cell_policy(
            {
                "signal_key": "frontier_crypto_venue_map|alpha",
                "strategy": "frontier_alpha",
                "variant": "v2",
                "venue": "OKX_SPOT",
                "direction": "short",
                "paper_route_status": "blocked",
                "closed_count": 6,
                "avg_pnl_bps": -8.75,
                "win_rate": 0.33,
            }
        )

        self.assertEqual(promoted["decision"], "promoted")
        self.assertEqual(promoted["action"], "promote_cell")
        self.assertEqual(reverted["decision"], "reverted")
        self.assertEqual(reverted["action"], "rollback_cell")
        self.assertNotEqual(promoted["cell_key"], reverted["cell_key"])

    def test_probation_expiry_can_revert_a_losing_cell(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "signal_key": "frontier_crypto_venue_map|alpha",
                "strategy": "frontier_alpha",
                "variant": "v2",
                "venue": "GATE",
                "direction": "short",
                "paper_route_status": "paper_testable_proxy",
                "closed_count": 2,
                "avg_pnl_bps": -1.5,
                "prior_state": "probation",
                "probation_started_at": "2026-01-01T00:00:00+00:00",
            },
            config={"paper_cell_policy": {"min_closed_trades": 3, "probation_ttl_days": 7}},
            now="2026-01-10T00:00:00+00:00",
        )

        self.assertEqual(result["decision"], "reverted")
        self.assertEqual(result["action"], "rollback_cell")
        self.assertTrue(result["probation_expired"])

    def test_short_discovery_proxy_cannot_promote_on_small_negative_sample(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "signal_key": "CME_GROUP|global_market_discovery_proxy|short_proxy|standard",
                "signal_family": "global_market_discovery_proxy",
                "venue": "CME_GROUP",
                "direction": "short_proxy",
                "paper_route_status": "standard",
                "closed_count": 11,
                "avg_pnl_bps": -6.613,
                "win_rate": 0.545,
            },
            config={
                "paper_cell_policy": {
                    "min_closed_trades": 3,
                    "promote_min_avg_pnl_bps": -10.0,
                    "revert_avg_pnl_bps": -20.0,
                }
            },
        )

        self.assertEqual(result["decision"], "probation")
        self.assertEqual(result["action"], "retain_cell_probation")
        gate = result["promotion_gate"]
        self.assertTrue(gate["paper_only"])
        self.assertTrue(gate["direction_asymmetric"])
        self.assertEqual(gate["min_closed_trades"], 20)
        self.assertEqual(gate["min_avg_pnl_bps"], 1.0)
        self.assertIn("insufficient_direction_specific_closed_trades", gate["blockers"])
        self.assertIn("direction_specific_realized_edge_below_floor", gate["blockers"])

    def test_sparse_cme_negative_evidence_defers_rollback_and_records_audit(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "signal_key": "CME_GROUP|global_market_discovery_proxy|short_proxy|standard",
                "signal_family": "global_market_discovery_proxy",
                "venue": "CME_GROUP",
                "direction": "short_proxy",
                "paper_route_status": "standard",
                "closed_count": 11,
                "avg_pnl_bps": -6.613,
                "win_rate": 0.545,
            }
        )

        self.assertEqual(result["decision"], "probation")
        self.assertEqual(result["action"], "retain_cell_probation")
        audit = result["promotion_gate"]["negative_retention_audit"]
        self.assertTrue(audit["negative_retention_signal"])
        self.assertFalse(audit["negative_adjustment_evidence_floor_met"])
        self.assertTrue(audit["negative_adjustment_deferred"])
        self.assertEqual(audit["confidence_status"], "evidence_limited")
        score = result["promotion_score_components"]
        self.assertEqual(score["pre_sample_size_adjustment_bps"], -6.613)
        self.assertEqual(score["post_sample_size_adjustment_bps"], -6.613)
        self.assertEqual(score["confidence_status"], "evidence_limited")

    def test_short_discovery_proxy_promotes_after_stable_positive_edge(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "signal_key": "CME_GROUP|global_market_discovery_proxy|short_proxy|standard",
                "signal_family": "global_market_discovery_proxy",
                "venue": "CME_GROUP",
                "direction": "short_proxy",
                "paper_route_status": "standard",
                "closed_count": 20,
                "avg_pnl_bps": 3.25,
                "avg_pnl_cost_basis": "after_modeled_context_cost",
                "win_rate": 0.55,
                "liquidity_score": 0.8,
                "freshness_age_seconds": 60.0,
            }
        )

        self.assertEqual(result["decision"], "promoted")
        self.assertEqual(result["promotion_gate"]["blockers"], [])

    def test_conditional_frontier_short_stays_in_probation_with_sparse_positive_sample(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "signal_key": "BITGET|frontier_crypto_venue_map|short_frontier_spot|conditional",
                "signal_family": "frontier_crypto_venue_map",
                "trade_type": "conditional",
                "venue": "BITGET",
                "direction": "short_frontier_spot",
                "paper_route_status": "executable",
                "closed_count": 12,
                "avg_pnl_bps": 2.0,
                "avg_pnl_cost_basis": "after_cost",
                "win_rate": 0.60,
            }
        )

        self.assertEqual(result["decision"], "probation")
        self.assertEqual(result["action"], "retain_cell_probation")
        confidence = result["promotion_gate"]["promotion_confidence"]
        self.assertTrue(confidence["applies"])
        self.assertEqual(confidence["sample_confidence"], 0.6)
        self.assertGreater(confidence["confidence_penalty_bps"], 0.0)
        self.assertEqual(confidence["raw_confidence_penalty_bps"], confidence["confidence_penalty_bps"])
        self.assertIn(
            "conditional_frontier_short_promotion_confidence_below_floor",
            result["promotion_gate"]["blockers"],
        )

    def test_sparse_conditional_sample_downgrades_confidence_without_penalty_or_hard_failure(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "signal_key": "BITGET|frontier_crypto_venue_map|short_frontier_spot|conditional",
                "signal_family": "frontier_crypto_venue_map",
                "trade_type": "conditional",
                "venue": "BITGET",
                "direction": "short_frontier_spot",
                "paper_route_status": "executable",
                "closed_count": 5,
                "avg_pnl_bps": -8.0,
                "avg_pnl_cost_basis": "after_cost",
                "win_rate": 0.40,
            }
        )

        self.assertEqual(result["decision"], "probation")
        confidence = result["promotion_gate"]["promotion_confidence"]
        self.assertEqual(confidence["confidence_status"], "evidence_limited")
        self.assertGreater(confidence["raw_confidence_penalty_bps"], 0.0)
        self.assertEqual(confidence["confidence_penalty_bps"], 0.0)
        self.assertTrue(confidence["confidence_penalty_deferred"])
        self.assertNotIn(
            "conditional_frontier_short_promotion_confidence_below_floor",
            result["promotion_gate"]["blockers"],
        )

    def test_conditional_frontier_short_promotes_only_after_stable_sample(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "signal_key": "BITGET|frontier_crypto_venue_map|short_frontier_spot|conditional",
                "signal_family": "frontier_crypto_venue_map",
                "trade_type": "conditional",
                "venue": "BITGET",
                "direction": "short_frontier_spot",
                "paper_route_status": "executable",
                "closed_count": 20,
                "avg_pnl_bps": 2.0,
                "avg_pnl_cost_basis": "after_cost",
                "win_rate": 0.60,
            }
        )

        self.assertEqual(result["decision"], "promoted")
        confidence = result["promotion_gate"]["promotion_confidence"]
        self.assertEqual(confidence["sample_confidence"], 1.0)
        self.assertEqual(confidence["confidence_penalty_bps"], 0.0)

    def test_short_proxy_gross_edge_does_not_promote_after_cost_backfill(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "signal_key": "CME_GROUP|global_market_discovery_proxy|short_proxy|standard",
                "signal_family": "global_market_discovery_proxy",
                "trade_type": "global_market_discovery_proxy",
                "venue": "CME_GROUP",
                "direction": "short_proxy",
                "paper_route_status": "standard",
                "closed_count": 24,
                "avg_pnl_bps": 4.0,
                "gross_avg_pnl_bps": 4.0,
                "avg_pnl_cost_basis": "price_only",
                "realized_cost_bps": 1.0,
                "estimated_round_trip_cost_bps": 6.0,
                "gross_edge_bps_estimate": 20.0,
                "spread_bps": 3.0,
                "liquidity_score": 0.8,
                "freshness_age_seconds": 60.0,
                "win_rate": 0.58,
            }
        )

        self.assertEqual("probation", result["decision"])
        audit = result["promotion_gate"]["realized_cost_audit"]
        self.assertTrue(audit["backfill_applied"])
        self.assertLess(result["avg_pnl_bps"], 1.0)
        self.assertIn(
            "direction_specific_realized_edge_below_floor",
            result["promotion_gate"]["blockers"],
        )

    def test_long_discovery_cell_keeps_generic_promotion_threshold(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "signal_key": "CME_GROUP|global_market_discovery_proxy|long_proxy|standard",
                "signal_family": "global_market_discovery_proxy",
                "venue": "CME_GROUP",
                "direction": "long_proxy",
                "paper_route_status": "standard",
                "closed_count": 6,
                "avg_pnl_bps": 2.0,
                "win_rate": 0.55,
            }
        )

        self.assertEqual(result["decision"], "promoted")
        self.assertFalse(result["promotion_gate"]["direction_asymmetric"])
        self.assertEqual(result["promotion_gate"]["min_closed_trades"], 3)

    def test_legacy_record_without_explicit_cell_fields_gets_safe_fallback_scope(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "strategy": "legacy_strategy",
                "variant": "legacy_variant",
                "closed_count": 4,
                "avg_pnl_bps": 1.25,
            }
        )

        self.assertEqual(result["decision"], "promoted")
        self.assertTrue(result["cell_key"])
        self.assertEqual(result["cell"]["scope"], "paper_signal_cell_v1")
        self.assertEqual(result["cell"]["strategy"], "legacy_strategy")

    def test_public_key_helper_matches_cell_record(self) -> None:
        candidate = {
            "signal_key": "frontier_crypto_venue_map|alpha",
            "strategy": "frontier_alpha",
            "variant": "v2",
            "venue": "GATE",
            "direction": "long",
            "paper_route_status": "executable",
        }
        self.assertEqual(paper_signal_cell_key(candidate), paper_signal_cell(candidate)["cell_key"])


if __name__ == "__main__":
    unittest.main()
