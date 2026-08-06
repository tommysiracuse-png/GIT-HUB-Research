from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import llm_bridge
import self_improvement
import strategy_reliability
import storage


def base_candidate(**overrides: object) -> dict:
    candidate = {
        "seen_at": "2026-06-24T00:00:00+00:00",
        "venue": "GATE",
        "inst_id": "GATE:ABC_USDT",
        "direction": "short_frontier_spot",
        "trade_type": "frontier_crypto_venue_map",
        "score": 70.0,
        "last": 100.0,
        "funding_bps": 0.0,
        "basis_bps": 0.0,
        "spread_bps": 2.0,
        "freshness_age_seconds": 30.0,
        "change_24h_pct": 0.0,
        "liquidity_score": 0.8,
        "edge_bps_estimate": 12.0,
        "estimated_round_trip_cost_bps": 30.0,
        "frontier_cost_source": "public_order_book",
        "quality_score": 80.0,
        "quality_status": "verified",
        "source_venue_count": 4,
        "quote_normalization_status": "usd_like",
        "execution_feasibility": {"status": "standard", "route_status": "standard"},
    }
    candidate.update(overrides)
    return candidate


class StrategyReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_json = strategy_reliability.REPORT_JSON
        self.old_md = strategy_reliability.REPORT_MD
        tmp_path = pathlib.Path(self.tmp.name)
        strategy_reliability.REPORT_JSON = tmp_path / "strategy_reliability.json"
        strategy_reliability.REPORT_MD = tmp_path / "strategy_reliability.md"

    def tearDown(self) -> None:
        strategy_reliability.REPORT_JSON = self.old_json
        strategy_reliability.REPORT_MD = self.old_md
        self.tmp.cleanup()

    def test_frontier_long_bad_venue_moves_to_shadow(self) -> None:
        candidate = base_candidate(
            venue="MEXC",
            inst_id="MEXC:ABC_USDT",
            direction="long_frontier_spot",
            quality_score=40.0,
            source_venue_count=2,
            edge_bps_estimate=4.0,
        )

        rows, report = strategy_reliability.apply_strategy_reliability([candidate])

        self.assertTrue(rows[0]["paper_entry_blocked"])
        self.assertFalse(rows[0]["promotion_eligible"])
        self.assertEqual(rows[0]["strategy_reliability_action"], "shadow_only_long_strict_gate")
        self.assertEqual(report["summary"]["shadow_or_blocked_count"], 1)

    def test_frontier_long_requires_minimum_liquidity_and_freshness(self) -> None:
        candidate = base_candidate(
            venue="MEXC",
            inst_id="MEXC:THIN_USDT",
            direction="long_frontier_spot",
            liquidity_score=0.3,
            freshness_age_seconds=120.0,
        )

        rows, _ = strategy_reliability.apply_strategy_reliability([candidate])

        self.assertTrue(rows[0]["paper_entry_blocked"])
        self.assertFalse(rows[0]["promotion_eligible"])
        self.assertIn("liquidity_score<0.45", rows[0]["strategy_reliability_reasons"])
        self.assertIn("freshness_age>90s", rows[0]["strategy_reliability_reasons"])

    def test_gate_short_good_quality_gets_probation_not_blanket_block(self) -> None:
        candidate = base_candidate(venue="GATE", direction="short_frontier_spot")

        rows, _ = strategy_reliability.apply_strategy_reliability([candidate])

        self.assertFalse(rows[0].get("paper_entry_blocked", False))
        self.assertEqual(rows[0]["strategy_reliability_action"], "probation_short_expansion")
        self.assertEqual(rows[0]["strategy_reliability_allocation_multiplier"], 0.25)

    def test_frontier_short_route_feasibility_penalizes_unverified_conditional_and_preserves_verified_exception(self) -> None:
        route_checklist = {
            "route_requirement_checklist": {
                "shortable_inventory_declared": True,
                "borrow_cost_model_present": True,
                "venue_supports_margin_or_equivalent": True,
                "fees_modeled": True,
            }
        }
        unverified = base_candidate(
            venue="GATE",
            inst_id="GATE:UNVERIFIED_USDT",
            execution_feasibility={"status": "conditional", "route_status": "conditional"},
            margin_eligible=True,
            fees_modeled=True,
            symbol_supported=True,
            supports_conditional_orders=True,
            paper_route_requirement_report={"route_requirements": route_checklist},
        )
        verified = base_candidate(
            venue="GATE",
            inst_id="GATE:VERIFIED_USDT",
            execution_feasibility={"status": "conditional", "route_status": "conditional"},
            paper_route_registry={"support_status": "supported"},
            margin_eligible=True,
            fees_modeled=True,
            symbol_supported=True,
            supports_conditional_orders=True,
            paper_route_requirement_report={"route_requirements": route_checklist},
        )

        rows, report = strategy_reliability.apply_strategy_reliability(
            [unverified, verified],
            {"mode": "paper", "allow_live_trading": False},
        )

        by_inst = {row["inst_id"]: row for row in rows}
        unverified_row = by_inst["GATE:UNVERIFIED_USDT"]
        verified_row = by_inst["GATE:VERIFIED_USDT"]

        self.assertEqual(
            "conditional_short_unverified_route",
            unverified_row["route_feasibility_reason"],
        )
        self.assertFalse(unverified_row["paper_active_scoring_eligible"])
        self.assertTrue(unverified_row["paper_route_feasibility_shadow_label"])
        self.assertLess(
            unverified_row["frontier_route_feasibility_score_multiplier"],
            0.2,
        )
        self.assertLess(unverified_row["score"], verified_row["score"])

        self.assertEqual(
            "verified_standard_short_route",
            verified_row["route_feasibility_reason"],
        )
        self.assertTrue(verified_row["paper_active_scoring_eligible"])
        self.assertFalse(verified_row["paper_route_feasibility_shadow_label"])
        self.assertGreater(
            verified_row["frontier_route_feasibility_score_multiplier"],
            unverified_row["frontier_route_feasibility_score_multiplier"],
        )

        summary = report["summary"]
        self.assertEqual(
            1,
            summary["route_feasibility_reason_counts"]["conditional_short_unverified_route"],
        )
        self.assertEqual(
            1,
            summary["route_feasibility_reason_counts"]["verified_standard_short_route"],
        )
        self.assertEqual(1, summary["route_feasibility_shadow_count"])
        self.assertEqual(1, summary["route_feasibility_verified_exception_count"])

    def test_runtime_applies_context_prior_before_candidate_sorting(self) -> None:
        candidate = {
            "seen_at": "2026-06-24T00:00:00+00:00",
            "venue": "BYBIT_SPOT",
            "inst_id": "BYBIT_SPOT:BTC_USDT",
            "direction": "long_frontier_spot",
            "trade_type": "spot_carry",
            "score": 60.0,
            "last": 100.0,
            "liquidity_score": 0.8,
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
        }

        rows, report = strategy_reliability.apply_strategy_reliability(
            [candidate], {"mode": "paper", "allow_live_trading": False}
        )

        self.assertEqual(rows[0]["score"], 75.0)
        self.assertEqual(rows[0]["final_paper_score"], 75.0)
        self.assertEqual(rows[0]["paper_context_prior"]["context_slice_key"], "BYBIT_SPOT|long|standard")
        self.assertEqual(rows[0]["paper_context_prior"]["context_slice_prior"], 6.0)
        self.assertFalse(rows[0]["promotion_eligible"])
        self.assertFalse(rows[0]["paper_context_top_rank_eligible"])
        self.assertEqual(rows[0]["paper_context_prior_status"], "ranked_hard_gated")
        self.assertEqual(report["paper_context_prior_adjustments"][0]["final_paper_score"], 75.0)

    def test_runtime_hydrates_realized_context_by_venue_direction_and_feasibility(self) -> None:
        standard = {
            "seen_at": "2026-08-06T00:00:00+00:00",
            "venue": "BYBIT_SPOT",
            "inst_id": "BYBIT_SPOT:BTC_USDT",
            "direction": "long_frontier_spot",
            "trade_type": "spot_carry",
            "score": 60.0,
            "last": 100.0,
            "liquidity_score": 0.8,
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
        }
        conditional = {
            **standard,
            "inst_id": "BYBIT_SPOT:ETH_USDT",
            "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
        }
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        try:
            for index in range(8):
                for inst_id, status, pnl in (
                    ("BYBIT_SPOT:BTC_USDT", "standard", 20.0),
                    ("BYBIT_SPOT:ETH_USDT", "conditional", -20.0),
                ):
                    payload = {
                        "venue": "BYBIT_SPOT",
                        "inst_id": inst_id,
                        "direction": "long_frontier_spot",
                        "trade_type": "spot_carry",
                        "execution_feasibility": {"status": status, "route_status": status},
                    }
                    conn.execute(
                        """
                        insert into paper_trades (
                            opened_at, closed_at, venue, inst_id, direction, trade_type,
                            signal_key, base_score, learned_score, entry, exit, pnl_bps,
                            status, thesis, candidate_json, review_json
                        ) values (?, ?, 'BYBIT_SPOT', ?, 'long_frontier_spot',
                                  'spot_carry', ?, 80, 80, 100, 101, ?, 'closed', 'test', ?, '{}')
                        """,
                        (
                            f"2026-08-05T00:{index:02d}:00+00:00",
                            f"2026-08-05T01:{index:02d}:00+00:00",
                            inst_id,
                            f"{inst_id}|{status}",
                            pnl,
                            json.dumps(payload),
                        ),
                    )
            conn.commit()
            rows, report = strategy_reliability.apply_strategy_reliability(
                [standard, conditional],
                {"mode": "paper", "allow_live_trading": False},
                conn=conn,
            )
        finally:
            conn.close()

        by_inst = {row["inst_id"]: row for row in rows}
        standard_row = by_inst["BYBIT_SPOT:BTC_USDT"]
        conditional_row = by_inst["BYBIT_SPOT:ETH_USDT"]

        self.assertEqual(standard_row["paper_context_prior"]["realized_context_key"], "BYBIT_SPOT|long|standard")
        self.assertEqual(conditional_row["paper_context_prior"]["realized_context_key"], "BYBIT_SPOT|long|conditional")
        self.assertEqual(standard_row["paper_context_prior"]["context_slice_key"], "BYBIT_SPOT|long|standard")
        self.assertEqual(conditional_row["paper_context_prior"]["context_slice_key"], "BYBIT_SPOT|long|conditional")
        self.assertEqual(standard_row["paper_context_prior"]["realized_context_prior"], 4.0)
        self.assertEqual(conditional_row["paper_context_prior"]["realized_context_prior"], -15.75)
        self.assertGreater(standard_row["score"], conditional_row["score"])
        report_keys = {item["realized_context_key"] for item in report["paper_context_prior_adjustments"]}
        self.assertIn("BYBIT_SPOT|long|standard", report_keys)
        self.assertIn("BYBIT_SPOT|long|conditional", report_keys)

    def test_runtime_hydrates_realized_context_from_review_metadata_when_candidate_payload_is_sparse(self) -> None:
        candidate = {
            "seen_at": "2026-08-06T00:00:00+00:00",
            "venue": "BYBIT_SPOT",
            "inst_id": "BYBIT_SPOT:ETH_USDT",
            "direction": "long_frontier_spot",
            "trade_type": "spot_carry",
            "score": 60.0,
            "last": 100.0,
            "liquidity_score": 0.8,
            "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
        }
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        try:
            for index in range(8):
                payload = {
                    "venue": "BYBIT_SPOT",
                    "inst_id": "BYBIT_SPOT:ETH_USDT",
                    "direction": "long_frontier_spot",
                    "trade_type": "spot_carry",
                }
                review = {
                    "feasibility_status": "conditional",
                    "route_status": "conditional",
                }
                conn.execute(
                    """
                    insert into paper_trades (
                        opened_at, closed_at, venue, inst_id, direction, trade_type,
                        signal_key, base_score, learned_score, entry, exit, pnl_bps,
                        status, thesis, candidate_json, review_json, context_json
                    ) values (?, ?, 'BYBIT_SPOT', 'BYBIT_SPOT:ETH_USDT', 'long_frontier_spot',
                              'spot_carry', ?, 80, 80, 100, 101, ?, 'closed', 'test', ?, ?, ?)
                    """,
                    (
                        f"2026-08-05T00:{index:02d}:00+00:00",
                        f"2026-08-05T01:{index:02d}:00+00:00",
                        f"BYBIT_SPOT:ETH_USDT|conditional|{index}",
                        -20.0,
                        json.dumps(payload),
                        json.dumps(review),
                        json.dumps({"feasibility_status": "conditional", "route_status": "conditional"}),
                    ),
                )
            conn.commit()
            rows, _ = strategy_reliability.apply_strategy_reliability(
                [candidate],
                {"mode": "paper", "allow_live_trading": False},
                conn=conn,
            )
        finally:
            conn.close()

        detail = rows[0]["paper_context_prior"]
        self.assertEqual(detail["realized_context_key"], "BYBIT_SPOT|long|conditional")
        self.assertEqual(detail["realized_context_closed_count"], 8)
        self.assertEqual(detail["realized_context_prior"], -15.75)

    def test_runtime_hydrates_realized_context_from_sparse_okx_surface_metadata(self) -> None:
        candidate = {
            "seen_at": "2026-08-06T00:00:00+00:00",
            "venue": "OKX",
            "inst_id": "BTC-USDT",
            "market_surface": "spot",
            "direction": "long_frontier_spot",
            "trade_type": "spot_carry",
            "score": 60.0,
            "last": 100.0,
            "liquidity_score": 0.8,
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
        }
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        try:
            for index in range(8):
                payload = {
                    "venue": "OKX",
                    "inst_id": "BTC-USDT",
                    "market_surface": "spot",
                    "direction": "long_frontier_spot",
                    "trade_type": "spot_carry",
                }
                review = {
                    "feasibility_status": "standard",
                    "route_status": "standard",
                }
                conn.execute(
                    """
                    insert into paper_trades (
                        opened_at, closed_at, venue, inst_id, direction, trade_type,
                        signal_key, base_score, learned_score, entry, exit, pnl_bps,
                        status, thesis, candidate_json, review_json, context_json
                    ) values (?, ?, 'OKX', 'BTC-USDT', 'long_frontier_spot',
                              'spot_carry', ?, 80, 80, 100, 101, ?, 'closed', 'test', ?, ?, ?)
                    """,
                    (
                        f"2026-08-05T00:{index:02d}:00+00:00",
                        f"2026-08-05T01:{index:02d}:00+00:00",
                        f"OKX:BTC-USDT|standard|{index}",
                        20.0,
                        json.dumps(payload),
                        json.dumps(review),
                        json.dumps({"feasibility_status": "standard", "route_status": "standard"}),
                    ),
                )
            conn.commit()
            rows, _ = strategy_reliability.apply_strategy_reliability(
                [candidate],
                {"mode": "paper", "allow_live_trading": False},
                conn=conn,
            )
        finally:
            conn.close()

        detail = rows[0]["paper_context_prior"]
        self.assertEqual(detail["context_slice_key"], "OKX_SPOT|long|standard")
        self.assertEqual(detail["realized_context_key"], "OKX_SPOT|long|standard")
        self.assertEqual(detail["realized_context_closed_count"], 8)
        self.assertEqual(detail["realized_context_prior"], 4.0)

    def test_bybit_long_quality_slice_gets_probation_not_blanket_expand(self) -> None:
        candidate = base_candidate(
            venue="BYBIT_SPOT",
            inst_id="BYBIT_SPOT:ABC_USDT",
            direction="long_frontier_spot",
            quality_score=82.0,
            source_venue_count=3,
            edge_bps_estimate=14.0,
            estimated_round_trip_cost_bps=28.0,
            recent_decay_status="stable",
        )

        rows, report = strategy_reliability.apply_strategy_reliability([candidate])

        self.assertFalse(rows[0].get("paper_entry_blocked", False))
        self.assertEqual(rows[0]["strategy_reliability_action"], "bybit_probation_quality_expansion")
        self.assertEqual(rows[0]["strategy_reliability_allocation_multiplier"], 0.25)
        focus = report["summary"]["manual_repair_focus"]["bybit_quality_decay_expansion_pack"]
        self.assertEqual(focus["classification"], "conditional_expand_quality_slice_only")

    def test_bybit_long_decayed_slice_stays_shadow(self) -> None:
        candidate = base_candidate(
            venue="BYBIT_SPOT",
            inst_id="BYBIT_SPOT:ABC_USDT",
            direction="long_frontier_spot",
            quality_score=82.0,
            source_venue_count=3,
            edge_bps_estimate=14.0,
            estimated_round_trip_cost_bps=28.0,
            recent_decay_status="deteriorating",
        )

        rows, _ = strategy_reliability.apply_strategy_reliability([candidate])

        self.assertTrue(rows[0]["paper_entry_blocked"])
        self.assertEqual(rows[0]["strategy_reliability_action"], "bybit_shadow_until_quality_decay_gate")
        self.assertIn("recent_decay_status=deteriorating", rows[0]["strategy_reliability_reasons"])

    def test_kucoin_long_repair_requires_strict_recovery_evidence(self) -> None:
        weak = base_candidate(
            venue="KUCOIN",
            inst_id="KUCOIN:ABC-USDT",
            direction="long_frontier_spot",
            quality_score=70.0,
            source_venue_count=3,
            edge_bps_estimate=8.0,
            estimated_round_trip_cost_bps=35.0,
        )
        strong = base_candidate(
            venue="KUCOIN",
            inst_id="KUCOIN:XYZ-USDT",
            direction="long_frontier_spot",
            quality_score=86.0,
            source_venue_count=4,
            edge_bps_estimate=18.0,
            estimated_round_trip_cost_bps=24.0,
        )

        rows, report = strategy_reliability.apply_strategy_reliability([weak, strong])
        by_inst = {row["inst_id"]: row for row in rows}

        self.assertTrue(by_inst["KUCOIN:ABC-USDT"]["paper_entry_blocked"])
        self.assertEqual(
            by_inst["KUCOIN:ABC-USDT"]["strategy_reliability_action"],
            "kucoin_shadow_diagnostic_gate",
        )
        self.assertEqual(
            by_inst["KUCOIN:XYZ-USDT"]["strategy_reliability_action"],
            "kucoin_small_recovery_probe",
        )
        self.assertEqual(by_inst["KUCOIN:XYZ-USDT"]["strategy_reliability_allocation_multiplier"], 0.1)
        focus = report["summary"]["manual_repair_focus"]["kucoin_long_repair_diagnostics"]
        self.assertIn(focus["classification"], {"rare_strict_recovery_probe_available", "rare_winners_vs_noisy_entries_shadow_diagnostics"})

    def test_okx_funding_capture_is_protected(self) -> None:
        candidate = base_candidate(
            venue="OKX",
            inst_id="OKX:BTC-USDT-SWAP",
            trade_type="perp_funding_basis",
            direction="funding_capture_short_perp",
            funding_bps=8.0,
            basis_bps=12.0,
            quality_score=None,
        )

        rows, _ = strategy_reliability.apply_strategy_reliability([candidate])

        self.assertFalse(rows[0].get("paper_entry_blocked", False))
        self.assertEqual(rows[0]["strategy_reliability_action"], "protect_working_funding_slice")
        self.assertTrue(rows[0]["strategy_reliability"]["protect_working_slice"])

    def test_okx_basis_mean_reversion_is_decay_quarantined_before_regime_confirmation(self) -> None:
        candidate = base_candidate(
            venue="OKX",
            inst_id="OKX:BTC-USDT-SWAP",
            trade_type="perp_funding_basis",
            direction="basis_mean_reversion_short_perp",
            funding_bps=8.0,
            basis_bps=40.0,
            change_24h_pct=35.0,
            quality_score=None,
        )

        rows, _ = strategy_reliability.apply_strategy_reliability([candidate])

        self.assertFalse(rows[0].get("paper_entry_blocked", False))
        self.assertEqual(rows[0]["strategy_reliability_action"], "decay_quarantine_shadow_trial")
        self.assertEqual(rows[0]["candidate_status"], "shadow_quarantined")
        self.assertEqual(rows[0]["candidate_reject_reason"], "decayed_basis_mean_reversion_quarantine")
        self.assertEqual(rows[0]["score"], 0.0)

    def test_yahoo_proxy_direction_family_is_quarantined_on_both_sides(self) -> None:
        short = base_candidate(
            venue="YAHOO_PROXY",
            inst_id="YAHOO_PROXY:EWZ",
            trade_type="global_proxy_momentum",
            direction="short_proxy",
            change_24h_pct=-0.4,
            edge_bps_estimate=4.0,
            quality_score=None,
        )
        long = base_candidate(
            venue="YAHOO_PROXY",
            inst_id="YAHOO_PROXY:EWT",
            trade_type="global_proxy_momentum",
            direction="long_proxy",
            change_24h_pct=1.5,
            edge_bps_estimate=8.0,
            quality_score=None,
        )

        rows, _ = strategy_reliability.apply_strategy_reliability([short, long])
        by_direction = {row["direction"]: row for row in rows}

        for direction in ("short_proxy", "long_proxy"):
            candidate = by_direction[direction]
            self.assertTrue(candidate["paper_entry_blocked"])
            self.assertFalse(candidate["paper_score_eligible"])
            self.assertEqual(candidate["score"], 0.0)
            self.assertEqual(candidate["strategy_reliability_action"], "family_quarantine_shadow_only")

    def test_proxy_short_quality_failure_is_preserved_through_family_quarantine(self) -> None:
        candidate = base_candidate(
            venue="YAHOO_PROXY",
            inst_id="YAHOO_PROXY:EWZ",
            trade_type="global_proxy_momentum",
            direction="short_proxy",
            change_24h_pct=-3.0,
            short_return_pct=-2.0,
            edge_bps_estimate=12.0,
            spread_bps=3.0,
            liquidity_score=0.8,
            stale_minutes=None,
            freshness_age_seconds=None,
            quality_score=None,
        )

        rows, report = strategy_reliability.apply_strategy_reliability([candidate])

        self.assertTrue(rows[0]["paper_entry_blocked"])
        self.assertEqual("proxy_short_quality_missing_freshness", rows[0]["quality_failure_reason"])
        self.assertIn(
            "proxy_short_quality_missing_freshness",
            rows[0]["strategy_reliability"]["quality_failure_reasons"],
        )
        self.assertIn(
            "proxy_short_quality_missing_depth",
            rows[0]["strategy_reliability"]["quality_failure_reasons"],
        )
        self.assertIn(
            "proxy_short_quality_missing_venue_health",
            rows[0]["strategy_reliability"]["quality_failure_reasons"],
        )
        self.assertEqual(
            1,
            report["summary"]["by_quality_failure"]["proxy_short_quality_missing_freshness"],
        )

    def test_yahoo_proxy_freshness_gate_shadow_only_suppresses_fill_but_preserves_counterfactual_scope(self) -> None:
        candidate = base_candidate(
            seen_at="2026-08-06T14:19:00+00:00",
            venue="YAHOO_PROXY",
            inst_id="YAHOO_PROXY:EWZ",
            trade_type="global_proxy_momentum",
            direction="long_proxy",
            score=88.0,
            source_quote_timestamp="2026-08-06T14:00:00+00:00",
            source_session_status="closed",
            source_session_open=False,
            source_quote_age_seconds=1260.0,
            last_trade_timestamp="2026-08-06T14:00:00+00:00",
            last_trade_age_seconds=1260.0,
            pre_entry_tick_returns_bps=[-18.0, -10.0, 5.0, -6.0],
            proxy_reuse_gate={
                "quote_age_seconds": 1260.0,
                "source_session_status": "closed",
                "reasons": ["opening_gap_without_live_followthrough"],
            },
            quality_score=None,
        )

        rows, _ = strategy_reliability.apply_strategy_reliability([candidate], {"mode": "paper"})

        row = rows[0]
        self.assertTrue(row["paper_entry_blocked"])
        self.assertFalse(row["paper_fill_allowed"])
        self.assertTrue(row["paper_observation_only"])
        self.assertEqual("synthetic_research", row["signal_stats_scope"])
        self.assertEqual("yahoo_proxy_freshness_shadow_only", row["strategy_reliability_action"])
        self.assertIn("proxy_quote_age_exceeded", row["strategy_reliability_reasons"])
        self.assertIn("source_session_closed", row["strategy_reliability_reasons"])
        self.assertIn("cross_tick_direction_inconsistent", row["strategy_reliability_reasons"])
        self.assertEqual(88.0, row["score"])

    def test_yahoo_proxy_with_fresh_session_telemetry_skips_family_quarantine(self) -> None:
        candidate = base_candidate(
            seen_at="2026-08-06T14:15:00+00:00",
            venue="YAHOO_PROXY",
            inst_id="YAHOO_PROXY:EWT",
            trade_type="global_proxy_momentum",
            direction="long_proxy",
            score=81.0,
            edge_bps_estimate=9.0,
            spread_bps=2.0,
            stale_minutes=1.0,
            source_quote_timestamp="2026-08-06T14:05:00+00:00",
            source_session_status="open",
            source_session_open=True,
            source_quote_age_seconds=600.0,
            last_trade_timestamp="2026-08-06T14:05:00+00:00",
            last_trade_age_seconds=600.0,
            pre_entry_tick_returns_bps=[7.0, 9.0, 6.0, 4.0],
            proxy_reuse_gate={
                "quote_age_seconds": 600.0,
                "source_session_status": "open",
                "reasons": [],
            },
            quality_score=None,
        )

        rows, report = strategy_reliability.apply_strategy_reliability([candidate], {"mode": "paper"})

        row = rows[0]
        self.assertFalse(row.get("paper_entry_blocked", False))
        self.assertNotEqual("family_quarantine_shadow_only", row["strategy_reliability_action"])
        self.assertEqual("long_proxy_context_tracking", row["strategy_reliability_action"])
        self.assertEqual(0, report["summary"]["family_quarantine_count"])
        self.assertTrue(row["paper_yahoo_proxy_freshness_gate"]["eligible"])

    def test_yahoo_proxy_recovery_snapshot_releases_family_quarantine(self) -> None:
        candidate = base_candidate(
            venue="YAHOO_PROXY",
            inst_id="YAHOO_PROXY:EWT",
            trade_type="global_proxy_momentum",
            direction="long_proxy",
            score=81.0,
            edge_bps_estimate=9.0,
            spread_bps=2.0,
            stale_minutes=1.0,
            quality_score=None,
            latest_family_paper={
                "long_proxy_standard": {"closed_count": 30, "avg_pnl_bps": 1.25, "win_rate": 0.52},
                "short_proxy_conditional": {"closed_count": 28, "avg_pnl_bps": -3.0, "win_rate": 0.41},
            },
        )

        rows, report = strategy_reliability.apply_strategy_reliability([candidate], {"mode": "paper"})

        row = rows[0]
        self.assertFalse(row.get("paper_entry_blocked", False))
        self.assertEqual("long_proxy_context_tracking", row["strategy_reliability_action"])
        self.assertEqual(0, report["summary"]["family_quarantine_count"])

    def test_yahoo_proxy_transfer_diagnostic_tags_translated_okx_route(self) -> None:
        candidate = base_candidate(
            seen_at="2026-08-06T14:05:00+00:00",
            venue="OKX",
            inst_id="OKX:BTC-USDT-SWAP",
            trade_type="frontier_crypto_venue_map",
            direction="long_frontier_perp",
            score=79.0,
            score_before_proxy_momentum_context=83.0,
            source_signal_key="YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
            signal_family="global_proxy_momentum",
            source_family="yahoo_proxy",
            source_quote_timestamp="2026-08-06T13:55:00+00:00",
            spread_bps=7.0,
            liquidity_score=0.58,
            basis_bps=12.0,
        )
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        try:
            conn.execute(
                """
                insert into signal_stats(
                    signal_key, closed_count, wins, avg_pnl_bps,
                    win_rate, score_adjustment, updated_at
                ) values
                    ('YAHOO_PROXY|global_proxy_momentum|long_proxy|standard', 18, 8, -1.5, 0.444, -2.0, '2026-08-06T13:50:00+00:00'),
                    (?, 11, 3, -6.0, 0.273, -3.0, '2026-08-06T13:59:00+00:00')
                """,
                (storage.signal_key(candidate),),
            )
            conn.commit()

            rows, _ = strategy_reliability.apply_strategy_reliability([candidate], {"mode": "paper"}, conn=conn)
        finally:
            conn.close()

        diagnostic = rows[0]["yahoo_proxy_transfer_diagnostic"]
        self.assertTrue(diagnostic["applies"])
        self.assertEqual("OKX_PERP", diagnostic["mapped_okx_route"])
        self.assertEqual("matched", diagnostic["mapping_status"])
        self.assertEqual(600.0, diagnostic["transfer_delay_seconds"])
        self.assertEqual("5m_to_15m", diagnostic["delay_bucket"])
        self.assertEqual("mid", diagnostic["liquidity_tier"])
        self.assertEqual("normal", diagnostic["spread_regime"])
        self.assertEqual(83.0, diagnostic["native_proxy_score"])
        self.assertEqual(-1.5, diagnostic["native_surface_paper_pnl_bps"])
        self.assertEqual(-6.0, diagnostic["mapped_route_paper_pnl_bps"])
        self.assertEqual(-4.5, diagnostic["route_vs_native_pnl_delta_bps"])

    def test_yahoo_proxy_transfer_report_segments_native_vs_okx_routes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        source_key = "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard"
        try:
            def insert_trade(
                *,
                opened_at: str,
                venue: str,
                inst_id: str,
                direction: str,
                signal_key_value: str,
                pnl_bps: float,
                candidate_payload: dict,
            ) -> None:
                conn.execute(
                    """
                    insert into paper_trades (
                        opened_at, closed_at, venue, inst_id, direction, trade_type,
                        signal_key, base_score, learned_score, entry, exit, pnl_bps,
                        status, thesis, candidate_json, review_json
                    ) values (?, ?, ?, ?, ?, ?, ?, 80, 80, 100, 101, ?, 'closed', 'test', ?, '{}')
                    """,
                    (
                        opened_at,
                        "2026-08-05T15:00:00+00:00",
                        venue,
                        inst_id,
                        direction,
                        candidate_payload["trade_type"],
                        signal_key_value,
                        pnl_bps,
                        json.dumps(candidate_payload),
                    ),
                )

            insert_trade(
                opened_at="2026-08-05T14:00:00+00:00",
                venue="YAHOO_PROXY",
                inst_id="YAHOO_PROXY:EWZ",
                direction="long_proxy",
                signal_key_value=source_key,
                pnl_bps=4.0,
                candidate_payload={
                    "venue": "YAHOO_PROXY",
                    "inst_id": "YAHOO_PROXY:EWZ",
                    "trade_type": "global_proxy_momentum",
                    "direction": "long_proxy",
                    "signal_key": source_key,
                    "score": 84.0,
                    "source_quote_timestamp": "2026-08-05T14:00:00+00:00",
                    "spread_bps": 2.0,
                    "liquidity_score": 0.9,
                    "basis_bps": 0.0,
                },
            )
            insert_trade(
                opened_at="2026-08-05T14:10:00+00:00",
                venue="OKX_SPOT",
                inst_id="OKX_SPOT:BTC-USDT",
                direction="long_frontier_spot",
                signal_key_value="OKX_SPOT|frontier_crypto_venue_map|long_frontier_spot|standard",
                pnl_bps=-6.0,
                candidate_payload={
                    "venue": "OKX_SPOT",
                    "inst_id": "OKX_SPOT:BTC-USDT",
                    "trade_type": "frontier_crypto_venue_map",
                    "direction": "long_frontier_spot",
                    "score": 78.0,
                    "source_signal_key": source_key,
                    "source_quote_timestamp": "2026-08-05T14:00:00+00:00",
                    "spread_bps": 7.0,
                    "liquidity_score": 0.55,
                    "basis_bps": 0.0,
                },
            )
            insert_trade(
                opened_at="2026-08-05T14:20:00+00:00",
                venue="OKX",
                inst_id="OKX:BTC-USDT-SWAP",
                direction="long_frontier_perp",
                signal_key_value="OKX|frontier_crypto_venue_map|long_frontier_perp|standard",
                pnl_bps=-10.0,
                candidate_payload={
                    "venue": "OKX",
                    "inst_id": "OKX:BTC-USDT-SWAP",
                    "trade_type": "frontier_crypto_venue_map",
                    "direction": "long_frontier_perp",
                    "score": 75.0,
                    "source_signal_key": source_key,
                    "source_quote_timestamp": "2026-08-05T14:00:00+00:00",
                    "spread_bps": 11.0,
                    "liquidity_score": 0.25,
                    "basis_bps": 18.0,
                },
            )
            conn.commit()

            _, report = strategy_reliability.apply_strategy_reliability([], {"mode": "paper"}, conn=conn)
        finally:
            conn.close()

        diagnostic = report["yahoo_proxy_transfer_friction_diagnostic"]
        self.assertEqual(3, diagnostic["closed_trade_count"])
        self.assertEqual(4.0, diagnostic["native_surface"]["avg_pnl_bps"])
        self.assertEqual(-6.0, diagnostic["transferred_routes"]["OKX_SPOT"]["avg_pnl_bps"])
        self.assertEqual(-10.0, diagnostic["transferred_routes"]["OKX_PERP"]["avg_pnl_bps"])
        self.assertEqual(-10.0, diagnostic["route_vs_native_pnl_delta_bps"]["OKX_SPOT"])
        self.assertEqual(-14.0, diagnostic["route_vs_native_pnl_delta_bps"]["OKX_PERP"])
        self.assertEqual(-6.0, diagnostic["segments"]["delay_bucket"]["5m_to_15m"]["avg_pnl_bps"])
        self.assertEqual(-10.0, diagnostic["segments"]["delay_bucket"]["over_15m"]["avg_pnl_bps"])
        self.assertEqual(1, diagnostic["segments"]["liquidity_tier"]["low"]["routes"]["OKX_PERP"])
        self.assertEqual(-10.0, diagnostic["segments"]["spread_regime"]["wide"]["avg_pnl_bps"])
        self.assertIn("## Yahoo Proxy Transfer Friction", strategy_reliability.REPORT_MD.read_text(encoding="utf-8"))

    def test_distinct_proxy_shock_reversal_uses_its_own_quality_profile(self) -> None:
        candidate = base_candidate(
            venue="YAHOO_PROXY",
            inst_id="YAHOO_PROXY:EWZ",
            trade_type="global_proxy_shock_reversal",
            direction="short_proxy",
            edge_bps_estimate=12.0,
            spread_bps=3.0,
            liquidity_score=0.8,
            stale_minutes=1.0,
            freshness_age_seconds=60.0,
            proxy_depth_notional_usd=2_000_000.0,
            proxy_venue_health_status="reachable",
        )

        rows, report = strategy_reliability.apply_strategy_reliability([candidate])

        self.assertFalse(rows[0].get("paper_entry_blocked", False))
        self.assertEqual("shock_reversal_confirmation_probe", rows[0]["strategy_reliability_action"])
        self.assertEqual("yahoo_proxy_shock_reversal", rows[0]["strategy_reliability"]["profile"])
        self.assertEqual(0, report["summary"]["family_quarantine_count"])

        missing_depth = {**candidate, "proxy_depth_notional_usd": None}
        blocked, _ = strategy_reliability.apply_strategy_reliability([missing_depth])
        self.assertTrue(blocked[0]["paper_entry_blocked"])
        self.assertEqual("shock_reversal_shadow_confirmation", blocked[0]["strategy_reliability_action"])
        self.assertIn("proxy_short_quality_missing_depth", blocked[0]["strategy_reliability_reasons"])

    def test_duplicate_suppression_recognizes_strategy_pack_themes(self) -> None:
        self.assertEqual(
            llm_bridge._implemented_manual_category("Investigate microstructure and liquidity impact on frontier spot"),
            "strategy_reliability_pack",
        )
        self.assertTrue(
            self_improvement._duplicate_strategy_reliability_pack_payload(
                {"title": "Yahoo proxy short weak win-rate", "proposed_change": "tighten confirmation"}
            )
        )

    def test_task_cleanup_marks_bybit_and_kucoin_items_implemented(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        try:
            for task_id in (134818, 137407, 136386, 136387):
                conn.execute(
                    "insert into improvement_tasks(id, created_at, priority, title, rationale, status) values (?, '2026-07-01T00:00:00+00:00', 90, ?, 'test', 'open')",
                    (task_id, f"Task {task_id}"),
                )
            conn.commit()

            _, report = strategy_reliability.apply_strategy_reliability([], conn=conn)
            rows = {
                row["id"]: row["status"]
                for row in conn.execute("select id, status from improvement_tasks").fetchall()
            }
        finally:
            conn.close()

        self.assertEqual(report["task_cleanup"]["updated"], 4)
        self.assertEqual(rows[134818], "implemented_bybit_quality_decay_expansion_pack")
        self.assertEqual(rows[137407], "implemented_bybit_quality_decay_expansion_pack")
        self.assertEqual(rows[136386], "implemented_kucoin_long_repair_diagnostics")
        self.assertEqual(rows[136387], "implemented_kucoin_long_repair_diagnostics")


if __name__ == "__main__":
    unittest.main()
