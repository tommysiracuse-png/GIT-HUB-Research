import pathlib
import sys
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import frontier_crypto_adapter as fca


class ProxySignalFreshnessGateTests(unittest.TestCase):
    def test_stale_yahoo_quote_is_neutralized_before_crypto_propagation(self):
        gate = fca.paper_only_yahoo_proxy_crypto_momentum_gate(
            momentum_contribution=0.8,
            source_quote_timestamp="2026-08-04T14:00:00+00:00",
            evaluated_at="2026-08-04T14:02:00+00:00",
            source_session_status="open",
            destination_proxy_age_seconds=45.0,
        )

        self.assertTrue(gate["applies"])
        self.assertTrue(gate["blocked"])
        self.assertEqual("stale_source_quote", gate["gate_reason"])
        self.assertEqual("neutral", gate["momentum_state"])
        self.assertEqual(0.0, gate["propagated_momentum_contribution"])

    def test_delayed_mode_cannot_release_cross_surface_momentum(self):
        common = {
            "momentum_contribution": -0.55,
            "source_quote_timestamp": "2026-08-04T14:00:30+00:00",
            "evaluated_at": "2026-08-04T14:01:00+00:00",
            "source_session_status": "closed",
            "destination_proxy_age_seconds": 30.0,
        }

        blocked = fca.paper_only_yahoo_proxy_crypto_momentum_gate(**common)
        allowed = fca.paper_only_yahoo_proxy_crypto_momentum_gate(
            **common,
            allow_delayed_mode=True,
        )

        self.assertEqual("source_session_closed", blocked["gate_reason"])
        self.assertEqual(0.0, blocked["propagated_momentum_contribution"])
        self.assertFalse(allowed["eligible"])
        self.assertEqual("yahoo_proxy_cross_surface_quarantined", allowed["gate_reason"])
        self.assertEqual(0.0, allowed["propagated_momentum_contribution"])

    def test_stale_destination_proxy_is_neutral_even_with_fresh_open_source(self):
        gate = fca.paper_only_yahoo_proxy_crypto_momentum_gate(
            momentum_contribution=0.4,
            source_quote_timestamp="2026-08-04T14:00:45+00:00",
            evaluated_at="2026-08-04T14:01:00+00:00",
            source_session_open=True,
            destination_proxy_age_seconds=91.0,
        )

        self.assertEqual("stale_destination_proxy", gate["gate_reason"])
        self.assertEqual(0.0, gate["propagated_momentum_contribution"])

    def test_fresh_non_okx_crypto_route_still_requires_target_surface_proof(self):
        gate = fca.paper_only_yahoo_proxy_crypto_momentum_gate(
            momentum_contribution=0.4,
            source_quote_timestamp="2026-08-04T14:00:45+00:00",
            evaluated_at="2026-08-04T14:01:00+00:00",
            source_session_open=True,
            destination_proxy_age_seconds=15.0,
            destination_surface="perp",
            destination_venue="BITGET",
        )

        self.assertTrue(gate["applies"])
        self.assertFalse(gate["eligible"])
        self.assertFalse(gate["emit_route"])
        self.assertEqual(0.0, gate["propagated_momentum_contribution"])
        self.assertIn("yahoo_proxy_cross_surface_quarantined", gate["gate_reasons"])

    def test_fresh_non_okx_crypto_route_releases_with_exact_surface_paper_proof(self):
        gate = fca.paper_only_yahoo_proxy_crypto_momentum_gate(
            momentum_contribution=0.4,
            source_quote_timestamp="2026-08-04T14:00:45+00:00",
            evaluated_at="2026-08-04T14:01:00+00:00",
            source_session_open=True,
            destination_proxy_age_seconds=15.0,
            destination_surface="perp",
            destination_venue="BITGET",
            native_proxy_momentum_bps=8.0,
            native_proxy_regime_stable=True,
            native_proxy_regime_state="stable_positive",
            destination_direction="long_frontier_perp",
            local_short_horizon_trend_bps=3.0,
            destination_spread_bps=3.0,
            destination_liquidity_score=0.8,
            target_surface_paper_evidence={
                "paper_only": True,
                "target_surface": "BITGET_PERP",
                "closed_count": 24,
                "expectancy_net_bps": 2.0,
                "quality_pass_rate": 0.6,
                "observed_at": "2026-08-04T14:00:00+00:00",
            },
        )

        self.assertTrue(gate["eligible"])
        self.assertTrue(gate["emit_route"])
        self.assertEqual(0.4, gate["propagated_momentum_contribution"])

    def test_gate_is_not_applied_to_live_mode(self):
        gate = fca.paper_only_yahoo_proxy_crypto_momentum_gate(
            momentum_contribution=0.7,
            execution_mode="live",
            source_session_status="closed",
        )

        self.assertFalse(gate["applies"])
        self.assertFalse(gate["blocked"])
        self.assertEqual("non_paper_mode", gate["reason"])
        self.assertEqual(0.7, gate["propagated_momentum_contribution"])

    def test_crypto_extension_of_existing_gate_records_session_reason(self):
        gate = fca.paper_only_proxy_signal_freshness_gate(
            market_key="YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            latest_bar_timestamp="2026-08-04T14:00:30+00:00",
            previous_bar_timestamp="2026-08-04T13:59:30+00:00",
            scheduler_timestamp="2026-08-04T14:01:00+00:00",
            source_timestamp="2026-08-04T14:00:45+00:00",
            basis_deviation_bps=5.0,
            mapping_confidence=0.9,
            destination_market="OKX crypto perp",
            source_session_status="closed",
            destination_proxy_age_ms=30_000.0,
            momentum_contribution=0.6,
        )

        self.assertIn("source_session_closed", gate["gate_reasons"])
        self.assertEqual(0.0, gate["propagated_momentum_contribution"])
        self.assertEqual("neutral", gate["momentum_state"])

    def test_route_annotation_overwrites_stale_propagated_contribution(self):
        route = {
            "execution_mode": "paper",
            "source_family": "yahoo_proxy",
            "feature_family": "global_proxy_momentum",
            "target_surface": "frontier_perp",
            "venue": "OKX",
            "source_quote_timestamp": "2026-08-04T14:00:00+00:00",
            "evaluated_at": "2026-08-04T14:02:00+00:00",
            "source_session_status": "open",
            "destination_proxy_age_seconds": 120.0,
            "momentum_contribution": 0.9,
            "propagated_momentum_contribution": 0.9,
        }

        annotated = fca._paper_only_annotate_route_intelligence(route)

        self.assertEqual(0.0, annotated["propagated_momentum_contribution"])
        self.assertEqual("stale_source_quote", annotated["proxy_momentum_gate_reason"])
        self.assertTrue(annotated["paper_only_route_blocked"])

    def test_route_seed_review_blocks_unproven_non_okx_transfer(self):
        review = fca._paper_only_cross_surface_seed_guard_review(
            {
                "execution_mode": "paper",
                "source_family": "yahoo_proxy",
                "feature_family": "global_proxy_momentum",
                "target_surface": "perp",
                "venue": "BITGET",
                "source_quote_timestamp": "2026-08-04T14:00:45+00:00",
                "evaluated_at": "2026-08-04T14:01:00+00:00",
                "source_session_open": True,
                "destination_proxy_age_seconds": 15.0,
                "momentum_contribution": 0.9,
            }
        )

        self.assertTrue(review["applies"])
        self.assertFalse(review["eligible"])
        self.assertFalse(review["emit_route"])
        self.assertEqual("proxy_frontier_cross_surface_paper_quarantine", review["policy"])
        self.assertEqual(0.0, review["propagated_momentum_contribution"])

    def test_stale_proxy_bar_suppresses_yahoo_proxy_context(self):
        gate = fca.paper_only_proxy_signal_freshness_gate(
            market_key="YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            latest_bar_timestamp="2026-07-18T04:40:00+00:00",
            previous_bar_timestamp="2026-07-18T04:39:00+00:00",
            scheduler_timestamp="2026-07-18T04:48:00+00:00",
            source_timestamp="2026-07-18T04:47:00+00:00",
            basis_deviation_bps=12.0,
            mapping_confidence=0.85,
        )

        self.assertTrue(gate["applicable"])
        self.assertFalse(gate["eligible"])
        self.assertFalse(gate["emit_recommendation"])
        self.assertEqual(gate["fail_closed_reason"], "stale_proxy_bar")
        self.assertEqual(gate["suppressed_signal_count"], 1)
        self.assertGreater(gate["proxy_bar_age_ms"], gate["max_bar_age_ms"])

    def test_fresh_proxy_context_remains_eligible(self):
        gate = fca.paper_only_proxy_signal_freshness_gate(
            market_key="YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            latest_bar_timestamp="2026-07-18T04:47:10+00:00",
            previous_bar_timestamp="2026-07-18T04:46:10+00:00",
            scheduler_timestamp="2026-07-18T04:48:00+00:00",
            source_timestamp="2026-07-18T04:47:25+00:00",
            basis_deviation_bps=14.5,
            mapping_confidence=0.82,
        )

        self.assertTrue(gate["eligible"])
        self.assertTrue(gate["emit_recommendation"])
        self.assertIsNone(gate["fail_closed_reason"])
        self.assertEqual(gate["suppressed_signal_count"], 0)

    def test_fresh_proxy_momentum_is_suppressed_for_crypto_destination(self):
        gate = fca.paper_only_proxy_signal_freshness_gate(
            market_key="YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            latest_bar_timestamp="2026-07-18T04:47:10+00:00",
            previous_bar_timestamp="2026-07-18T04:46:10+00:00",
            scheduler_timestamp="2026-07-18T04:48:00+00:00",
            source_timestamp="2026-07-18T04:47:25+00:00",
            basis_deviation_bps=14.5,
            mapping_confidence=0.82,
            destination_market="OKX_SPOT",
            source_session_status="open",
            destination_proxy_age_ms=50_000.0,
            momentum_contribution=0.75,
        )

        self.assertFalse(gate["eligible"])
        self.assertFalse(gate["emit_recommendation"])
        self.assertTrue(gate["quarantined_cross_surface"])
        self.assertIn("yahoo_proxy_cross_surface_quarantined", gate["fail_closed_reasons"])
        self.assertEqual(0.0, gate["propagated_momentum_contribution"])
        self.assertEqual("OKX_SPOT", gate["target_surface"])

    def test_fresh_proxy_momentum_requires_proof_for_other_frontier_crypto_venue(self):
        gate = fca.paper_only_proxy_signal_freshness_gate(
            market_key="YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            latest_bar_timestamp="2026-07-18T04:47:10+00:00",
            previous_bar_timestamp="2026-07-18T04:46:10+00:00",
            scheduler_timestamp="2026-07-18T04:48:00+00:00",
            source_timestamp="2026-07-18T04:47:25+00:00",
            basis_deviation_bps=14.5,
            mapping_confidence=0.82,
            destination_market="BITGET_PERP",
            source_session_status="open",
            destination_proxy_age_ms=50_000.0,
            momentum_contribution=0.75,
        )

        self.assertFalse(gate["eligible"])
        self.assertFalse(gate["emit_recommendation"])
        self.assertTrue(gate["quarantined_cross_surface"])
        self.assertEqual(0.0, gate["propagated_momentum_contribution"])

    def test_signal_quality_gate_fail_closes_when_proxy_gate_fails(self):
        result = fca.paper_only_cross_market_signal_quality_gate(
            confidence=0.91,
            primary_trigger_present=True,
            related_market_confirmed=True,
            signal_age_ms=500.0,
            market_key="YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            proxy_signal_freshness_gate={
                "applicable": True,
                "eligible": False,
                "fail_closed_reason": "basis_deviation_exceeded",
                "suppressed_signal_count": 1,
                "state": "observe_only",
            },
        )

        self.assertFalse(result["promote"])
        self.assertTrue(result["observe_only"])
        self.assertEqual(result["fail_closed_reason"], "basis_deviation_exceeded")
        self.assertEqual(result["suppressed_signal_count"], 1)
        self.assertEqual(result["state"], "observe_only")

    def test_validate_payload_converts_failed_proxy_gate_to_hold(self):
        payload = {
            "action": "propose_code_change",
            "title": "Proxy momentum packet",
            "market_key": "YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            "priority": 93,
            "confidence": 0.9,
            "evidence": {
                "paper_only_proxy_signal_freshness_gate": {
                    "applicable": True,
                    "eligible": False,
                    "emit_recommendation": False,
                    "fail_closed_reason": "mapping_confidence_too_low",
                    "fail_closed_reasons": ["mapping_confidence_too_low"],
                    "proxy_bar_age_ms": 42000.0,
                    "source_timestamp_lag_ms": 35000.0,
                    "basis_deviation_bps": 10.0,
                    "mapping_confidence": 0.42,
                    "suppressed_signal_count": 1,
                }
            },
            "proposed_change": {"objective": "suppress stale proxy signals"},
            "rationale": "Fail closed when proxy mapping quality is too low.",
        }

        validated = fca.validate_paper_recommendation_payload(payload)

        self.assertEqual(validated["action"], "hold")
        self.assertEqual(validated["evidence"]["issue_type"], "proxy_signal_freshness_failed")
        self.assertEqual(validated["evidence"]["fail_closed_reason"], "mapping_confidence_too_low")
        self.assertEqual(validated["evidence"]["suppressed_signal_count"], 1)


if __name__ == "__main__":
    unittest.main()
