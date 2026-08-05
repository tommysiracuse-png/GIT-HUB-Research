from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src import storage
from src.frontier_data_quality import (
    paper_only_yahoo_proxy_crypto_lineage_quarantine,
    paper_only_yahoo_proxy_cross_surface_alignment_guard,
)
from src.paper_order_router import apply_frontier_paper_guard
from src.settings import DEFAULT_SETTINGS


def cross_surface_candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "venue": "OKX",
        "inst_id": "OKX:BTC-USDT-SWAP",
        "asset_class": "crypto_derivatives",
        "market_surface": "perp",
        "trade_type": "frontier_crypto_venue_map",
        "direction": "long_frontier_perp",
        "source_family": "yahoo_proxy",
        "signal_family": "global_proxy_momentum",
        "source_signal_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
        "local_short_horizon_trend_bps": 3.0,
        "spread_bps": 4.0,
        "liquidity_score": 0.8,
        "native_yahoo_proxy_regime": {
            "momentum_bps": 8.0,
            "regime_stable": True,
            "regime_state": "stable_positive",
        },
        "execution_mode": "paper",
        "last": 100.0,
        "score": 70.0,
        "thesis": "paper-only cross-surface confirmation test",
        "edge_bps_estimate": 20.0,
        "gross_edge_bps_estimate": 40.0,
        "estimated_round_trip_cost_bps": 15.0,
        "quality_action": "normal",
        "anomaly_flags": [],
    }
    candidate.update(overrides)
    return candidate


class YahooProxyCrossSurfaceAlignmentGuardTests(unittest.TestCase):
    def target_proof(self, **overrides: object) -> dict[str, object]:
        proof: dict[str, object] = {
            "paper_only": True,
            "target_surface": "OKX_PERP",
            "closed_count": 24,
            "expectancy_net_bps": 2.5,
            "quality_pass_rate": 0.58,
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "realized_paper_outcomes": True,
            "transfer_mapping_key": (
                "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard->OKX:BTC-USDT-SWAP"
            ),
            "transfer_window_count": 3,
            "positive_transfer_windows": 3,
        }
        proof.update(overrides)
        return proof

    def test_explicit_family_root_okx_remap_is_shadow_quarantined_with_diagnostics(self) -> None:
        candidate = cross_surface_candidate(
            family_root="YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
            target_surface="OKX",
            native_surface={"closed_count": 176, "avg_pnl_bps": -3.9},
            remapped_okx={"closed_count": 48, "avg_pnl_bps": -7.1},
        )

        review = paper_only_yahoo_proxy_crypto_lineage_quarantine(candidate)
        guarded = apply_frontier_paper_guard(candidate)

        self.assertTrue(review["applies"])
        self.assertTrue(review["quarantined"])
        self.assertEqual("shadow_quarantined", review["status"])
        self.assertIn("native_surface_positive", review["failed_checks"])
        self.assertTrue(
            paper_only_yahoo_proxy_cross_surface_alignment_guard(candidate)["emit_recommendation"]
        )
        self.assertTrue(guarded["paper_fill_allowed"])
        self.assertFalse(guarded["promotion_eligible"])
        self.assertEqual("shadow_quarantined", guarded["paper_quarantine_status"])
        self.assertEqual("synthetic_paper", guarded["paper_execution_mode"])

    def test_explicit_family_root_releases_only_after_comparative_positive_samples(self) -> None:
        review = paper_only_yahoo_proxy_crypto_lineage_quarantine(
            {
                "execution_mode": "paper",
                "family_root": "YAHOO_PROXY|global_proxy_momentum|recovered",
                "target_surface": "OKX_SPOT",
                "native_surface": {"closed_count": 24, "avg_pnl_bps": 1.5},
                "remapped_okx_spot": {"closed_count": 22, "avg_pnl_bps": 1.5},
            }
        )

        self.assertTrue(review["applies"])
        self.assertFalse(review["quarantined"])
        self.assertTrue(review["eligible"])
        self.assertEqual([], review["failed_checks"])

    def test_local_quality_is_diagnostic_but_cannot_release_route(self) -> None:
        aligned = paper_only_yahoo_proxy_cross_surface_alignment_guard(cross_surface_candidate())
        adverse_trend = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(local_short_horizon_trend_bps=-1.0)
        )
        adverse_spread = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(spread_bps=8.1)
        )
        missing_trend = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(local_short_horizon_trend_bps=None)
        )

        self.assertTrue(all(row["blocked"] for row in (aligned, adverse_trend, adverse_spread, missing_trend)))
        self.assertEqual("yahoo_proxy_cross_surface_quarantined", aligned["reason"])
        self.assertEqual("local_alignment_confirmed", aligned["alignment_reason"])
        self.assertEqual("local_short_horizon_trend_adverse", adverse_trend["alignment_reason"])
        self.assertEqual("destination_spread_adverse", adverse_spread["alignment_reason"])
        self.assertEqual("missing_local_short_horizon_trend", missing_trend["alignment_reason"])

    def test_negative_local_trend_confirms_a_short_destination(self) -> None:
        review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                direction="short_frontier_perp",
                local_short_horizon_trend_bps=-2.5,
            )
        )

        self.assertFalse(review["eligible"])
        self.assertEqual("aligned", review["local_trend_state"])
        self.assertEqual("local_alignment_confirmed", review["alignment_reason"])

    def test_fresh_exact_surface_paper_proof_releases_paper_promotion(self) -> None:
        review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(target_surface_paper_evidence=self.target_proof())
        )

        self.assertTrue(review["eligible"])
        self.assertFalse(review["blocked"])
        self.assertTrue(review["promotion_eligible"])
        self.assertEqual("paper_promotion", review["maximum_stage"])
        self.assertEqual(
            "fresh_target_surface_paper_evidence_validated",
            review["reason"],
        )

    def test_surface_aggregate_without_same_realized_mapping_stays_quarantined(self) -> None:
        proof = self.target_proof()
        proof.pop("transfer_mapping_key")
        proof.pop("realized_paper_outcomes")

        review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(target_surface_paper_evidence=proof)
        )

        self.assertFalse(review["eligible"])
        self.assertTrue(review["blocked"])
        self.assertIn(
            "same_source_target_mapping",
            review["target_surface_paper_evidence_review"]["failed_checks"],
        )
        self.assertIn(
            "realized_transfer_outcomes",
            review["target_surface_paper_evidence_review"]["failed_checks"],
        )

    def test_negative_native_and_transferred_outcomes_mark_mapping_incompatible(self) -> None:
        proof = self.target_proof(expectancy_net_bps=-2.0)
        review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                target_surface_paper_evidence=proof,
                native_yahoo_proxy_paper_evidence={"expectancy_net_bps": -3.0},
                native_yahoo_proxy_regime={"momentum_bps": -4.0, "regime_stable": True},
            )
        )

        self.assertFalse(review["eligible"])
        self.assertTrue(review["target_surface_paper_evidence_review"]["source_target_incompatible"])
        self.assertEqual("yahoo_proxy_frontier_source_target_incompatible", review["reason"])

    def test_source_regime_must_be_decisively_positive_and_stable(self) -> None:
        non_positive = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                native_yahoo_proxy_regime={"momentum_bps": 0.0, "regime_stable": True},
                target_surface_paper_evidence=self.target_proof(),
            )
        )
        unstable = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                native_yahoo_proxy_regime={
                    "momentum_bps": 9.0,
                    "regime_stable": False,
                    "regime_state": "stable_positive",
                },
                target_surface_paper_evidence=self.target_proof(),
            )
        )

        self.assertFalse(non_positive["eligible"])
        self.assertEqual("native_yahoo_proxy_regime_non_positive", non_positive["reason"])
        self.assertFalse(unstable["eligible"])
        self.assertEqual("native_yahoo_proxy_regime_unstable", unstable["reason"])
        self.assertFalse(unstable["paper_rank_eligible"])
        self.assertFalse(unstable["activation_allowed"])

    def test_local_spread_liquidity_and_trend_are_all_required_for_release(self) -> None:
        low_liquidity = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                liquidity_score=0.64,
                target_surface_paper_evidence=self.target_proof(),
            )
        )
        adverse_spread = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                spread_bps=8.1,
                target_surface_paper_evidence=self.target_proof(),
            )
        )
        adverse_trend = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                local_short_horizon_trend_bps=-1.0,
                target_surface_paper_evidence=self.target_proof(),
            )
        )

        self.assertTrue(all(row["blocked"] for row in (low_liquidity, adverse_spread, adverse_trend)))
        self.assertEqual("destination_liquidity_below_floor", low_liquidity["alignment_reason"])
        self.assertEqual(
            {"short_horizon_trend": True, "spread": True, "liquidity": False},
            low_liquidity["local_confirmation_checks"],
        )
        self.assertTrue(all(not row["paper_rank_eligible"] for row in (low_liquidity, adverse_spread, adverse_trend)))

    def test_stale_wrong_surface_or_low_quality_proof_stays_in_sandbox(self) -> None:
        stale = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                target_surface_paper_evidence=self.target_proof(
                    observed_at="2020-01-01T00:00:00+00:00"
                )
            )
        )
        wrong_surface = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                target_surface_paper_evidence=self.target_proof(target_surface="OKX_SPOT")
            )
        )
        low_quality = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                target_surface_paper_evidence=self.target_proof(quality_pass_rate=0.49)
            )
        )

        self.assertTrue(all(row["blocked"] for row in (stale, wrong_surface, low_quality)))
        self.assertTrue(all(row["sandbox_rank_eligible"] for row in (stale, wrong_surface, low_quality)))
        self.assertTrue(all(row["maximum_stage"] == "sandbox_ranking" for row in (stale, wrong_surface, low_quality)))
        self.assertTrue(all(not row["promotion_eligible"] for row in (stale, wrong_surface, low_quality)))
        self.assertIn(
            "fresh_observations",
            stale["target_surface_paper_evidence_review"]["failed_checks"],
        )
        self.assertIn(
            "exact_target_surface",
            wrong_surface["target_surface_paper_evidence_review"]["failed_checks"],
        )
        self.assertIn(
            "quality_rate",
            low_quality["target_surface_paper_evidence_review"]["failed_checks"],
        )

    def test_scope_excludes_native_yahoo_and_live_contexts(self) -> None:
        native = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(venue="YAHOO_PROXY", asset_class="equity", market_surface="proxy")
        )
        live = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(execution_mode="live")
        )

        self.assertFalse(native["applies"])
        self.assertFalse(live["applies"])
        self.assertTrue(native["eligible"])
        self.assertTrue(live["eligible"])
        self.assertTrue(native["allow_native_proxy_monitoring"])
        self.assertEqual(["OKX_SPOT", "OKX_PERP"], native["quarantined_target_surfaces"])

    def test_quarantine_covers_frontier_crypto_spot_and_perp(self) -> None:
        spot = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                venue="OKX",
                inst_id="OKX:BTC-USDT",
                asset_class="crypto_spot",
                market_surface="spot",
                direction="long_frontier_spot",
            )
        )
        perp = paper_only_yahoo_proxy_cross_surface_alignment_guard(cross_surface_candidate())
        route_family_spot = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                inst_id=None,
                asset_class=None,
                market_surface=None,
                direction="long_frontier_spot",
            )
        )
        other_crypto = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                venue="BITGET",
                inst_id="BITGET:BTCUSDT",
                asset_class="crypto_derivatives",
                market_surface="perp",
            )
        )

        self.assertEqual("OKX_SPOT", spot["target_surface"])
        self.assertEqual("OKX_PERP", perp["target_surface"])
        self.assertEqual("OKX_SPOT", route_family_spot["target_surface"])
        self.assertTrue(spot["blocked"])
        self.assertTrue(perp["blocked"])
        self.assertTrue(route_family_spot["blocked"])
        self.assertTrue(other_crypto["applies"])
        self.assertTrue(other_crypto["blocked"])
        self.assertFalse(other_crypto["emit_route"])
        self.assertEqual("BITGET_PERP", other_crypto["target_surface"])
        self.assertEqual(
            "stable_positive_realized_paper_outcomes_for_same_source_target_mapping_and_native_proxy_regime_and_local_frontier_confirmation",
            spot["reenable_condition"],
        )

    def test_scope_excludes_non_momentum_yahoo_lineage(self) -> None:
        reversal = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(signal_family="global_proxy_shock_reversal")
        )

        self.assertFalse(reversal["applies"])
        self.assertTrue(reversal["eligible"])
        self.assertTrue(reversal["emit_route"])

    def test_generic_proxy_lineage_cannot_bypass_cross_surface_quarantine(self) -> None:
        review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                source_family="vendor_proxy_derived",
                signal_family="proxy_momentum",
            )
        )

        self.assertTrue(review["applies"])
        self.assertTrue(review["blocked"])
        self.assertEqual("proxy_derived", review["source_family"])
        self.assertEqual("sandbox_ranking", review["maximum_stage"])

        routed = apply_frontier_paper_guard(
            cross_surface_candidate(
                source_family="vendor_proxy_derived",
                signal_family="proxy_momentum",
            )
        )
        self.assertTrue(routed["shadow_filtered"])
        self.assertFalse(routed["paper_fill_allowed"])
        self.assertFalse(routed["promotion_eligible"])

    def test_default_policy_publishes_target_surface_proof_thresholds(self) -> None:
        policy = DEFAULT_SETTINGS["yahoo_proxy_cross_surface_alignment_guard"]

        self.assertEqual(20, policy["min_target_surface_closed_count"])
        self.assertEqual(0.0, policy["min_target_surface_expectancy_net_bps"])
        self.assertEqual(0.5, policy["min_target_surface_quality_rate"])
        self.assertEqual(168.0, policy["max_target_surface_evidence_age_hours"])
        self.assertEqual(3, policy["min_realized_transfer_windows"])
        self.assertEqual(0.67, policy["min_realized_transfer_positive_window_rate"])
        self.assertEqual(5.0, policy["min_native_proxy_momentum_bps"])
        self.assertEqual(0.65, policy["min_destination_liquidity_score"])
        self.assertEqual(
            "stable_positive_realized_paper_outcomes_for_same_source_target_mapping_and_native_proxy_regime_and_local_frontier_confirmation",
            policy["reenable_condition"],
        )

    def test_destination_context_is_not_masked_by_yahoo_source_venue(self) -> None:
        review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            {
                "source_context": {
                    "venue": "YAHOO_PROXY",
                    "source_family": "yahoo_proxy",
                    "signal_family": "global_proxy_momentum",
                },
                "destination_context": {
                    "venue": "OKX",
                    "asset_class": "crypto_spot",
                    "market_surface": "spot",
                    "local_short_horizon_trend_bps": 2.0,
                    "spread_bps": 3.0,
                },
                "direction": "long_frontier_spot",
                "execution_mode": "paper",
            }
        )

        self.assertTrue(review["applies"])
        self.assertFalse(review["eligible"])
        self.assertEqual("okx", review["destination_venue"])

    def test_crypto_scope_uses_destination_fields_not_substring_collisions(self) -> None:
        review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            cross_surface_candidate(
                venue="DELEGATE",
                inst_id="DELEGATE:ABC",
                asset_class="equity",
                market_surface="spotlight_research",
                trade_type="research_candidate",
            )
        )

        self.assertFalse(review["applies"])
        self.assertTrue(review["eligible"])

    def test_paper_order_guard_shadow_filters_unconfirmed_transfer(self) -> None:
        guarded = apply_frontier_paper_guard(
            cross_surface_candidate(local_short_horizon_trend_bps=-2.0)
        )

        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["paper_fill_allowed"])
        self.assertEqual(
            "paper_yahoo_proxy_cross_surface_quarantined",
            guarded["candidate_reject_reason"],
        )

    def test_confirmed_transfer_is_quarantined_even_when_family_gate_is_disabled(self) -> None:
        guarded = apply_frontier_paper_guard(
            cross_surface_candidate(),
            {"paper_family_quarantine_enabled": False, "mode": "paper"},
        )

        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["yahoo_proxy_cross_surface_alignment_guard"]["eligible"])
        self.assertFalse(guarded["emit_route"])
        self.assertEqual(0.0, guarded["paper_allocation_multiplier"])

    def test_legacy_confirmed_entry_exits_under_quarantine(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        candidate = cross_surface_candidate()
        candidate["yahoo_proxy_cross_surface_alignment_guard"] = {
            **paper_only_yahoo_proxy_cross_surface_alignment_guard(candidate),
            "eligible": True,
            "blocked": False,
            "entry_allowed": True,
        }
        trade_id = storage.open_paper_trade(
            conn,
            candidate,
            {"learned_score": 70.0, "route_status": "standard"},
            settings={"scanner": {"hold_minutes": 60}},
        )

        closed = storage.close_due_trades(
            conn,
            {
                "OKX:BTC-USDT-SWAP": {
                    "inst_id": "OKX:BTC-USDT-SWAP",
                    "venue": "OKX",
                    "last": 99.5,
                    "local_short_horizon_trend_bps": -2.0,
                    "spread_bps": 3.0,
                    "observed_at": storage.utc_now(),
                    "price_source": "OKX public REST",
                }
            },
            60,
            settings={"risk": {}},
        )

        self.assertEqual(1, len(closed))
        self.assertTrue(closed[0]["forced_exit"])
        row = conn.execute(
            "select status, close_measurement_status, close_reason from paper_trades where id = ?",
            (trade_id,),
        ).fetchone()
        self.assertEqual("closed", row["status"])
        self.assertEqual(
            "forced_yahoo_proxy_cross_surface_quarantine",
            row["close_measurement_status"],
        )
        self.assertEqual("yahoo_proxy_cross_surface_quarantined", row["close_reason"])

    def test_legacy_entry_exit_does_not_require_refreshed_local_trend(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        candidate = cross_surface_candidate()
        candidate["yahoo_proxy_cross_surface_alignment_guard"] = {
            **paper_only_yahoo_proxy_cross_surface_alignment_guard(candidate),
            "eligible": True,
            "blocked": False,
            "entry_allowed": True,
        }
        trade_id = storage.open_paper_trade(
            conn,
            candidate,
            {"learned_score": 70.0, "route_status": "standard"},
            settings={"scanner": {"hold_minutes": 60}},
        )

        closed = storage.close_due_trades(
            conn,
            {
                "OKX:BTC-USDT-SWAP": {
                    "inst_id": "OKX:BTC-USDT-SWAP",
                    "venue": "OKX",
                    "last": 99.5,
                    "observed_at": storage.utc_now(),
                    "price_source": "OKX public REST",
                    "candidate": {
                        "return_1m_bps": -2.0,
                        "microstructure_history_ready": 1.0,
                        "spread_bps": 3.0,
                    },
                }
            },
            60,
            settings={"risk": {}},
        )

        self.assertEqual(1, len(closed))
        self.assertTrue(closed[0]["forced_exit"])
        row = conn.execute(
            "select status, close_reason from paper_trades where id = ?",
            (trade_id,),
        ).fetchone()
        self.assertEqual("closed", row["status"])
        self.assertEqual("yahoo_proxy_cross_surface_quarantined", row["close_reason"])


if __name__ == "__main__":
    unittest.main()
