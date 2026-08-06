from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_engine import execute_order
from paper_order_router import apply_frontier_paper_guard
from paper_decay_quarantine import apply_quarantine, quarantine_record, runtime_report, target_signal
from paper_exploration_report import build_paper_exploration_report
from settings import DEFAULT_SETTINGS
from storage import connect, open_paper_trade, save_execution_order
from strategy_reliability import apply_strategy_reliability


class OkxBasisDecayQuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.settings["mode"] = "paper"
        self.settings["allow_live_trading"] = False

    @staticmethod
    def candidate(**overrides: object) -> dict:
        candidate = {
            "venue": "OKX",
            "inst_id": "BTC-USDT-SWAP",
            "trade_type": "perp_funding_basis",
            "direction": "basis_mean_reversion_short_perp",
            "last": 100.0,
            "score": 80.0,
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
            "execution_route": {"route_status": "standard"},
            "thesis": "priceable basis observation",
        }
        candidate.update(overrides)
        status = str(
            candidate.get("feasibility_status")
            or (candidate.get("execution_feasibility") or {}).get("status")
            or "standard"
        ).strip().lower()
        explicit_signal_key = str(
            candidate.get("signal_key")
            or candidate.get("market_key")
            or ""
        ).strip()
        default_signal_key = (
            f"{candidate.get('venue') or 'OKX'}|{candidate.get('trade_type') or 'perp_funding_basis'}"
            f"|{candidate.get('direction') or 'basis_mean_reversion_short_perp'}|{status}"
        )
        candidate.setdefault(
            "paper_okx_basis_decay_signal_stats",
            {
                "signal_key": explicit_signal_key or default_signal_key,
                "closed_count": 21,
                "avg_pnl_bps": -352.202,
                "score_adjustment": -15.0,
                "win_rate": 0.476,
                "updated_at": "2026-08-06T00:00:00+00:00",
            },
        )
        return candidate

    def add_closed_shadow_labels(
        self,
        conn,
        *,
        candidate: dict | None = None,
        settings: dict | None = None,
        count: int = 30,
        pnl_bps: float = 6.0,
    ) -> None:
        runtime_settings = settings or self.settings
        guarded = apply_quarantine(candidate or self.candidate(), runtime_settings, conn=conn)
        closed_base = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
        signal_key = guarded["paper_okx_basis_decay_quarantine"]["target"]["signal_key"]
        for index in range(count):
            shadow_candidate = copy.deepcopy(guarded)
            shadow_candidate["inst_id"] = f"BTC-{index}-USDT-SWAP"
            closed_at = (closed_base + dt.timedelta(seconds=index)).isoformat()
            conn.execute(
                """
                insert into paper_trades (
                    opened_at, closed_at, venue, inst_id, direction, trade_type,
                    signal_key, base_score, learned_score, entry, exit, pnl_bps,
                    status, thesis, candidate_json, review_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)
                """,
                (
                    closed_at,
                    closed_at,
                    shadow_candidate["venue"],
                    shadow_candidate["inst_id"],
                    shadow_candidate["direction"],
                    shadow_candidate["trade_type"],
                    signal_key,
                    80.0,
                    80.0,
                    100.0,
                    101.0,
                    pnl_bps,
                    "shadow label",
                    json.dumps(shadow_candidate),
                    "{}",
                ),
            )
        conn.commit()

    def test_only_named_okx_families_are_shadow_filtered_in_exploration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            decayed = self.candidate()
            funding_capture = self.candidate(direction="funding_capture_long_perp")
            healthy_carry = self.candidate(direction="short_perp_long_spot")

            rows, report = apply_strategy_reliability(
                [decayed, funding_capture, healthy_carry], self.settings, conn=conn
            )

            by_direction = {row["direction"]: row for row in rows}
            blocked = by_direction["basis_mean_reversion_short_perp"]
            self.assertTrue(blocked["paper_okx_basis_decay_quarantine"]["diagnostic_only"])
            self.assertIn("decayed_basis_mean_reversion_quarantine", blocked["paper_exploration_would_block_reasons"])
            self.assertEqual("shadow_filtered", blocked.get("paper_action"))
            self.assertEqual(
                "zero_cap",
                blocked["okx_basis_decay_quarantine_score_policy"]["mode"],
            )
            self.assertEqual(0.0, blocked["okx_basis_decay_quarantine_score_policy"]["post_quarantine_score"])
            self.assertEqual("quarantined_basis_mr", blocked["candidate_status"])
            self.assertTrue(blocked.get("paper_entry_blocked", False))
            self.assertFalse(blocked.get("paper_fill_allowed", True))
            self.assertEqual("shadow_filtered", blocked.get("quality_action"))
            self.assertNotIn("paper_okx_basis_decay_quarantine", by_direction["funding_capture_long_perp"])
            self.assertNotIn("paper_okx_basis_decay_quarantine", by_direction["short_perp_long_spot"])
            self.assertEqual(1, report["summary"]["okx_basis_decay_quarantine_count"])
            conn.close()

    def test_diagnostic_quarantine_clears_stale_block_state(self) -> None:
        candidate = self.candidate(
            paper_entry_blocked=True,
            shadow_filtered=True,
            candidate_reject_reason="legacy_guard",
            candidate_reject_detail={"reason": "legacy_guard"},
        )

        rows, _ = apply_strategy_reliability([candidate], self.settings)
        guarded = rows[0]

        self.assertEqual("quarantined_basis_mr", guarded["candidate_status"])
        self.assertTrue(guarded.get("paper_entry_blocked", False))
        self.assertTrue(guarded.get("shadow_filtered", False))
        self.assertFalse(guarded.get("paper_eligible", True))
        self.assertEqual("decayed_basis_mean_reversion_quarantine", guarded["candidate_reject_reason"])
        self.assertEqual("shadow_filtered", guarded["quality_action"])

    def test_exact_target_directions_quarantine_only_mean_reversion_variants(self) -> None:
        conditional = self.candidate(
            direction="basis_mean_reversion_long_perp",
            execution_feasibility={"status": "conditional", "route_status": "conditional"},
        )
        reverse_basis_conditional = self.candidate(
            direction="long_perp_short_spot",
            execution_feasibility={"status": "conditional", "route_status": "conditional"},
        )
        reverse_basis_standard = self.candidate(
            direction="long_perp_short_spot",
            execution_feasibility={"status": "standard", "route_status": "standard"},
        )
        protected = self.candidate(
            direction="short_perp_long_spot",
            execution_feasibility={"status": "conditional", "route_status": "conditional"},
        )

        self.assertTrue(quarantine_record(conditional, self.settings)["active"])
        self.assertIsNone(quarantine_record(reverse_basis_conditional, self.settings))
        self.assertIsNone(quarantine_record(reverse_basis_standard, self.settings))
        self.assertIsNone(quarantine_record(protected, self.settings))

    def test_explicit_feasibility_status_is_reflected_in_target_signal_key(self) -> None:
        conditional = self.candidate(
            direction="basis_mean_reversion_long_perp",
            feasibility_status="conditional",
            execution_feasibility={"status": "standard", "route_status": "standard"},
        )

        record = quarantine_record(conditional, self.settings)

        self.assertTrue(record["active"])
        self.assertEqual(
            "OKX|perp_funding_basis|basis_mean_reversion_long_perp|conditional",
            record["target"]["signal_key"],
        )

    def test_explicit_non_target_signal_key_is_not_quarantined(self) -> None:
        protected = self.candidate(
            signal_key="OKX|perp_funding_basis|funding_capture_short_perp|standard",
        )

        self.assertIsNone(quarantine_record(protected, self.settings))

    def test_market_key_target_is_quarantined_without_signal_key(self) -> None:
        candidate = self.candidate(
            signal_key=None,
            market_key="OKX|perp_funding_basis|basis_mean_reversion_long_perp|conditional",
            venue=None,
            trade_type=None,
            direction=None,
        )

        record = quarantine_record(candidate, self.settings)

        self.assertTrue(record["active"])
        self.assertEqual(
            "OKX|perp_funding_basis|basis_mean_reversion_long_perp|conditional",
            record["target"]["signal_key"],
        )

    def test_market_key_positive_control_is_not_quarantined(self) -> None:
        protected = self.candidate(
            signal_key=None,
            market_key="OKX|perp_funding_basis|funding_capture_short_perp|standard",
            venue=None,
            trade_type=None,
            direction=None,
        )

        self.assertIsNone(quarantine_record(protected, self.settings))

    def test_strategy_lab_lineage_is_not_reclassified_as_native_decay_signal(self) -> None:
        translated = self.candidate(
            strategy_lab_id="okx_basis_lab_v2",
            strategy_lab_version=2,
        )

        self.assertIsNone(quarantine_record(translated, self.settings))

    def test_exploration_emits_a_shadow_filtered_observation_with_quarantine_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            review = {"paper_allocation_multiplier": 1.0, "decision": "approve_paper_trade"}

            execution = execute_order(conn, self.candidate(), review, self.settings)

            self.assertFalse(execution["paper_filled"])
            self.assertTrue(execution["paper_observation_ready"])
            self.assertEqual("shadow_filtered", execution["order"]["status"])
            self.assertEqual("synthetic_research", execution["order"]["signal_stats_scope"])
            self.assertTrue(execution["candidate"]["paper_okx_basis_decay_quarantine"]["diagnostic_only"])
            self.assertIn(
                "decayed_basis_mean_reversion_quarantine",
                execution["candidate"]["paper_exploration_would_block_reasons"],
            )
            conn.close()

    def test_router_marks_target_shadow_filtered_in_exploration(self) -> None:
        guarded = apply_frontier_paper_guard(
            self.candidate(paper_filled=True, status="paper_filled"), self.settings
        )

        self.assertTrue(guarded.get("shadow_filtered", False))
        self.assertEqual("shadow_filtered", guarded["paper_action"])
        self.assertEqual("quarantined_basis_mr", guarded["candidate_status"])
        self.assertFalse(guarded["paper_filled"])

    def test_non_exploration_mode_still_uses_shadow_filtered_quarantine(self) -> None:
        settings = copy.deepcopy(self.settings)
        settings["paper_exploration"]["enabled"] = False
        guarded = apply_frontier_paper_guard(
            self.candidate(paper_filled=True, status="paper_filled"), settings
        )

        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["paper_filled"])
        self.assertFalse(guarded["paper_eligible"])
        self.assertEqual("shadow_filtered", guarded["paper_action"])

    def test_reused_quarantine_candidate_keeps_zero_cap_policy(self) -> None:
        settings = copy.deepcopy(self.settings)
        settings["paper_exploration"]["enabled"] = False
        candidate = apply_quarantine(self.candidate(), settings)

        self.assertEqual(0.0, candidate["score"])
        self.assertEqual(
            "zero_cap",
            candidate["okx_basis_decay_quarantine_score_policy"]["mode"],
        )

        recovered = apply_quarantine(candidate, self.settings)

        self.assertEqual(0.0, recovered["score"])
        self.assertEqual(
            "zero_cap",
            recovered["okx_basis_decay_quarantine_score_policy"]["mode"],
        )
        self.assertTrue(recovered.get("shadow_filtered", False))
        self.assertTrue(recovered.get("paper_entry_blocked", False))
        self.assertFalse(recovered.get("paper_fill_allowed", True))

    def test_released_reused_candidate_restores_pre_quarantine_score_after_shadow_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            settings = copy.deepcopy(self.settings)
            settings["paper_exploration"]["enabled"] = False
            candidate = apply_quarantine(self.candidate(), settings, conn=conn)

            self.assertEqual(0.0, candidate["score"])
            self.add_closed_shadow_labels(conn, candidate=self.candidate(), settings=settings, count=30, pnl_bps=6.0)

            released = apply_quarantine(candidate, settings, conn=conn)

            self.assertEqual("released", released["paper_okx_basis_decay_quarantine"]["status"])
            self.assertEqual(80.0, released["score"])
            self.assertNotIn("okx_basis_decay_quarantine_score_policy", released)
            self.assertFalse(released.get("shadow_filtered", False))
            self.assertFalse(released.get("paper_entry_blocked", False))
            self.assertTrue(released.get("paper_fill_allowed", True))
            conn.close()

    def test_proxy_lineage_is_not_reclassified_as_the_direct_decayed_family(self) -> None:
        proxy = self.candidate(
            direction="long_perp_short_spot",
            signal_key="PAPER_PROXY|okx_derivatives_paper|OKX|perp_funding_basis|long_perp_short_spot|conditional",
            signal_stats_scope="paper_proxy",
            paper_proxy_activated=True,
            paper_proxy_not_live_equivalent=True,
            paper_execution_semantics="proxy_not_live_equivalent",
        )

        self.assertIsNone(target_signal(proxy))
        self.assertIsNone(quarantine_record(proxy, self.settings))

    def test_runtime_report_releases_after_thirty_profitable_shadow_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            candidate = self.candidate()
            first = quarantine_record(candidate, self.settings, conn=conn)
            self.assertTrue(first["active"])
            self.add_closed_shadow_labels(conn, candidate=candidate, count=30, pnl_bps=6.0)

            report = runtime_report(conn, self.settings)
            paper_report = build_paper_exploration_report(conn, self.settings)

            self.assertEqual("released", report["status"])
            self.assertEqual("rolling_shadow_recovery_confirmed", report["release_reason"])
            self.assertEqual(30, report["closed_label_count"])
            self.assertEqual(6.0, report["avg_pnl_bps"])
            self.assertEqual(1.0, report["win_rate"])
            self.assertEqual(
                "decayed_basis_mean_reversion_quarantine",
                paper_report["okx_basis_decay_quarantine"]["reason"],
            )
            self.assertEqual(30, paper_report["summary"]["okx_basis_decay_quarantine_closed_labels"])
            conn.close()

    def test_runtime_report_exposes_quarantine_counts_and_shadow_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            candidate = self.candidate()
            rows, _ = apply_strategy_reliability([candidate], self.settings, conn=conn)
            candidate = rows[0]
            review = {
                "decision": "approve_paper_trade",
                "learned_score": candidate["score"],
                "paper_allocation_multiplier": 1.0,
            }
            execution = execute_order(conn, candidate, review, self.settings)
            trade_id = open_paper_trade(conn, candidate, review, execution=execution, settings=self.settings)
            observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
            conn.execute(
                """
                insert into paper_trade_outcomes (
                    trade_id, horizon_minutes, measured_at, price, pnl_bps, context_json,
                    target_at, observed_at, delay_seconds, measurement_status, price_source
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    60,
                    observed_at,
                    101.0,
                    12.5,
                    "{}",
                    observed_at,
                    observed_at,
                    0.0,
                    "valid",
                    "unit_test",
                ),
            )
            conn.commit()

            paper_report = build_paper_exploration_report(conn, self.settings)

            self.assertEqual(1, paper_report["okx_basis_decay_quarantine"]["quarantined_count"])
            self.assertEqual(0, paper_report["okx_basis_decay_quarantine"]["would_have_filled_count"])
            self.assertEqual(1, paper_report["okx_basis_decay_quarantine"]["shadow_valid_outcome_count"])
            self.assertEqual(12.5, paper_report["okx_basis_decay_quarantine"]["shadow_pnl_bps"])
            self.assertEqual(
                0,
                paper_report["summary"]["okx_basis_decay_quarantine_would_have_filled_count"],
            )
            conn.close()

    def test_runtime_report_cycle_counters_ignore_positive_control_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            decayed_rows, _ = apply_strategy_reliability([self.candidate()], self.settings, conn=conn)
            decayed = decayed_rows[0]
            protected = self.candidate(
                direction="funding_capture_long_perp",
                signal_key="OKX|perp_funding_basis|funding_capture_long_perp|conditional",
            )
            protected["paper_okx_basis_decay_quarantine"] = dict(decayed["paper_okx_basis_decay_quarantine"])

            paper_report = build_paper_exploration_report(
                conn,
                self.settings,
                reviewed=[
                    {"candidate": decayed, "review": {"decision": "approve_paper_trade"}},
                    {"candidate": protected, "review": {"decision": "approve_paper_trade"}},
                ],
            )

            self.assertEqual(1, paper_report["okx_basis_decay_quarantine"]["current_cycle_quarantined_count"])
            self.assertEqual(0, paper_report["okx_basis_decay_quarantine"]["current_cycle_would_have_filled_count"])
            self.assertEqual(
                ["OKX|perp_funding_basis|basis_mean_reversion_short_perp|standard"],
                paper_report["okx_basis_decay_quarantine"]["current_cycle_signal_keys"],
            )
            self.assertEqual(
                1,
                paper_report["summary"]["okx_basis_decay_quarantine_current_cycle_quarantined_count"],
            )
            self.assertEqual(
                0,
                paper_report["summary"]["okx_basis_decay_quarantine_current_cycle_would_have_filled_count"],
            )
            conn.close()

    def test_paper_report_distinguishes_preserved_and_quarantined_okx_basis_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            quarantined = apply_strategy_reliability([self.candidate()], self.settings, conn=conn)[0][0]
            preserved_candidate = self.candidate(
                direction="short_perp_long_spot",
                signal_key="OKX|perp_funding_basis|short_perp_long_spot|standard",
                execution_feasibility={"status": "standard", "route_status": "standard"},
                paper_okx_basis_context_signal_stats={
                    "signal_key": "OKX|perp_funding_basis|short_perp_long_spot|standard",
                    "closed_count": 274,
                    "avg_pnl_bps": 20.742,
                    "score_adjustment": 7.391,
                    "win_rate": 0.511,
                },
            )
            preserved = apply_strategy_reliability([preserved_candidate], self.settings, conn=conn)[0][0]

            paper_report = build_paper_exploration_report(
                conn,
                self.settings,
                reviewed=[
                    {"candidate": quarantined, "review": {"decision": "approve_paper_trade"}},
                    {"candidate": preserved, "review": {"decision": "approve_paper_trade"}},
                ],
            )

            self.assertEqual(
                1,
                paper_report["okx_basis_context_overlays"]["current_cycle_reason_counts"][
                    "decayed_basis_mean_reversion_quarantine"
                ],
            )
            self.assertEqual(
                1,
                paper_report["okx_basis_context_overlays"]["current_cycle_reason_counts"][
                    "okx_standard_short_perp_long_spot_preserved"
                ],
            )
            conn.close()

    def test_runtime_report_cycle_would_have_filled_count_ignores_hard_quarantine_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            settings = copy.deepcopy(self.settings)
            settings["paper_exploration"]["enabled"] = False
            rows, _ = apply_strategy_reliability([self.candidate()], settings, conn=conn)
            blocked = rows[0]

            self.assertFalse(blocked["paper_okx_basis_decay_quarantine"]["paper_fill_allowed"])

            paper_report = build_paper_exploration_report(
                conn,
                settings,
                reviewed=[
                    {"candidate": blocked, "review": {"decision": "approve_paper_trade"}},
                ],
            )

            self.assertEqual(1, paper_report["okx_basis_decay_quarantine"]["current_cycle_quarantined_count"])
            self.assertEqual(0, paper_report["okx_basis_decay_quarantine"]["current_cycle_would_have_filled_count"])
            self.assertEqual(
                0,
                paper_report["summary"]["okx_basis_decay_quarantine_current_cycle_would_have_filled_count"],
            )
            conn.close()

    def test_runtime_report_cycle_counts_market_key_only_reviewed_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            candidate = self.candidate(
                signal_key=None,
                market_key="OKX|perp_funding_basis|basis_mean_reversion_short_perp|standard",
                venue=None,
                trade_type=None,
                direction=None,
            )
            candidate["paper_okx_basis_decay_quarantine"] = quarantine_record(
                candidate, self.settings, conn=conn
            )

            paper_report = build_paper_exploration_report(
                conn,
                self.settings,
                reviewed=[
                    {"candidate": candidate, "review": {"decision": "approve_paper_trade"}},
                ],
            )

            self.assertEqual(1, paper_report["okx_basis_decay_quarantine"]["current_cycle_quarantined_count"])
            self.assertEqual(0, paper_report["okx_basis_decay_quarantine"]["current_cycle_would_have_filled_count"])
            conn.close()

    def test_runtime_report_cycle_counts_guard_only_reviewed_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            candidate = self.candidate(
                signal_key=None,
                market_key="OKX|perp_funding_basis|basis_mean_reversion_short_perp|standard",
            )
            record = quarantine_record(candidate, self.settings, conn=conn)
            candidate["paper_guard_would_block"] = {
                "reason": record["reason"],
                "guard": record["guard"],
                "record": dict(record),
            }

            paper_report = build_paper_exploration_report(
                conn,
                self.settings,
                reviewed=[
                    {"candidate": candidate, "review": {"decision": "approve_paper_trade"}},
                ],
            )

            self.assertEqual(1, paper_report["okx_basis_decay_quarantine"]["current_cycle_quarantined_count"])
            self.assertEqual(0, paper_report["okx_basis_decay_quarantine"]["current_cycle_would_have_filled_count"])
            conn.close()

    def test_runtime_report_counts_guard_only_persisted_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            candidate = self.candidate(
                signal_key=None,
                market_key="OKX|perp_funding_basis|basis_mean_reversion_short_perp|standard",
            )
            record = quarantine_record(candidate, self.settings, conn=conn)
            candidate["paper_guard_would_block"] = {
                "reason": record["reason"],
                "guard": record["guard"],
                "record": dict(record),
            }
            order = {
                "mode": "paper",
                "route_id": "okx_derivatives_paper",
                "status": "paper_filled",
                "notional_usd": 1000.0,
            }
            review = {
                "decision": "approve_paper_trade",
                "learned_score": candidate["score"],
                "paper_allocation_multiplier": 1.0,
            }
            order_id = save_execution_order(conn, order, candidate, review)
            trade_id = open_paper_trade(
                conn,
                candidate,
                review,
                execution={"candidate": candidate, "order": order, "order_id": order_id, "fills": []},
                settings=self.settings,
            )
            observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
            conn.execute(
                """
                insert into paper_trade_outcomes (
                    trade_id, horizon_minutes, measured_at, price, pnl_bps, context_json,
                    target_at, observed_at, delay_seconds, measurement_status, price_source
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    60,
                    observed_at,
                    101.0,
                    12.5,
                    "{}",
                    observed_at,
                    observed_at,
                    0.0,
                    "valid",
                    "unit_test",
                ),
            )
            conn.commit()

            report = runtime_report(conn, self.settings)

            self.assertEqual(1, report["quarantined_count"])
            self.assertEqual(1, report["would_have_filled_count"])
            self.assertEqual(1, report["shadow_valid_outcome_count"])
            self.assertEqual(12.5, report["shadow_pnl_bps"])
            conn.close()

    def test_runtime_report_does_not_release_on_deadline_without_shadow_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            quarantine_record(self.candidate(), self.settings, conn=conn)
            conn.execute(
                "update paper_decay_quarantines set expires_at = ?",
                ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat(),),
            )
            conn.commit()

            report = runtime_report(conn, self.settings)

            self.assertEqual("active", report["status"])
            self.assertIsNone(report["release_reason"])
            conn.close()

    def test_released_quarantine_allows_a_new_paper_fill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            settings = copy.deepcopy(self.settings)
            settings["paper_exploration"]["enabled"] = False
            settings["paper_context_cost_floor"]["enabled"] = False
            quarantine_record(self.candidate(), self.settings, conn=conn)
            self.add_closed_shadow_labels(conn, settings=settings, count=30, pnl_bps=6.0)

            try:
                execution = execute_order(
                    conn,
                    self.candidate(),
                    {"paper_allocation_multiplier": 1.0, "decision": "approve_paper_trade"},
                    settings,
                )

                self.assertTrue(execution["paper_filled"])
                self.assertEqual("paper_filled", execution["order"]["status"])
                self.assertEqual("released", execution["candidate"]["paper_okx_basis_decay_quarantine"]["status"])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
