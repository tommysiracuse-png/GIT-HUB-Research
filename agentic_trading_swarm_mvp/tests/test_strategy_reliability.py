from __future__ import annotations

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

        self.assertEqual(rows[0]["score"], 84.0)
        self.assertEqual(rows[0]["final_paper_score"], 84.0)
        self.assertEqual(rows[0]["paper_context_prior"]["venue_direction_prior"], 6.0)
        self.assertEqual(report["paper_context_prior_adjustments"][0]["final_paper_score"], 84.0)

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
        self.assertEqual(rows[0]["candidate_reject_reason"], "decay_quarantine")

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
