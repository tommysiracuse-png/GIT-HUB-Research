from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_expansion_campaign as campaign  # noqa: E402
import paper_admission_queue as queue  # noqa: E402
import cost_router  # noqa: E402
from execution_engine import execute_order  # noqa: E402
from settings import load_settings  # noqa: E402
from storage import connect, save_opportunity  # noqa: E402


class PaperExpansionCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.process_env = mock.patch.dict(
            os.environ,
            {
                "RADAR_PROCESS_ROLE": "bounded_paper_radar",
                "RADAR_BOUNDED_SUPERVISOR_COUNT": "1",
                "RADAR_BOUNDED_CHILD_COUNT": "1",
                "RADAR_FORBIDDEN_WORKER_COUNT": "0",
            },
        )
        self.process_env.start()
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.db_path = root / "campaign.sqlite"
        self.settings = load_settings(ROOT / "config" / "settings.bounded_crypto_paper.json")
        self.settings = copy.deepcopy(self.settings)
        self.settings["paper_expansion"]["deferred_cost_path"] = str(root / "deferred.jsonl")
        self.settings["paper_expansion"]["autonomous_ledger_path"] = str(root / "attempts.sqlite")

    def tearDown(self) -> None:
        self.temp.cleanup()
        self.process_env.stop()

    def connection(self):
        return connect(self.db_path)

    def healthy(self, **updates) -> dict:
        metrics = {
            "cycle_success": True,
            "exit_code": 0,
            "runtime_seconds": 100.0,
            "peak_rss_mb": 500.0,
            "supervisor_count": 1,
            "child_count": 1,
            "forbidden_worker_count": 0,
            "terminal_opportunity_rate": 1.0,
            "frontier_observation_count": 6000,
            "reachable_venue_count": 16,
            "db_growth_bytes": 0,
            "db_footprint_start_bytes": 0,
            "db_footprint_bytes": 0,
            "artifact_sizes": {},
        }
        metrics.update(updates)
        return metrics

    def set_phase(self, conn, phase: str, *, elapsed_hours: float = 0.0) -> None:
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        phase_now = dt.datetime.now(dt.timezone.utc)
        row = conn.execute(
            "select state_json from paper_expansion_campaign_state where campaign_id=?",
            (campaign_id,),
        ).fetchone()
        state = json.loads(row["state_json"])
        state["phase"] = phase
        state["run_status"] = "running"
        state["healthy_streak"] = 0
        state["phase_healthy_cycles"] = 0
        state["phase_started_at"] = (
            phase_now - dt.timedelta(hours=elapsed_hours)
        ).isoformat()
        state["phase_healthy_running_seconds"] = elapsed_hours * 3600.0
        state["phase_clock_checkpoint_at"] = phase_now.isoformat()
        state["accumulated"] = {}
        state.pop("inflight_cycle", None)
        conn.execute(
            """
            update paper_expansion_campaign_state
            set phase=?,run_status='running',healthy_streak=0,phase_started_at=?,state_json=?
            where campaign_id=?
            """,
            (phase, state["phase_started_at"], json.dumps(state), campaign_id),
        )
        conn.commit()

    def initialize(self, conn) -> None:
        _effective, token = campaign.apply_campaign_controls(conn, self.settings)
        campaign.record_campaign_cycle(conn, token, self.healthy())

    def deferred_payload(self, **updates: object) -> dict:
        payload = {
            "agent_name": "legacy-deferred-agent",
            "model_tier": "fast",
            "model_name": "legacy/deferred-model",
            "provider": "openai",
            "api": "responses",
            "operation": "bounded_crypto_paid_research",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "estimated_cost_usd": 0.01,
            "status": "model_call:responses",
        }
        payload.update(updates)
        return payload

    def write_deferred_payloads(self, *payloads: dict) -> tuple[pathlib.Path, bytes, str]:
        path = pathlib.Path(self.settings["paper_expansion"]["deferred_cost_path"])
        raw = "".join(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            for payload in payloads
        ).encode("utf-8")
        path.write_bytes(raw)
        return path, raw, hashlib.sha256(raw).hexdigest()

    def campaign_state(self, conn) -> dict:
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        row = conn.execute(
            "select state_json from paper_expansion_campaign_state where campaign_id=?",
            (campaign_id,),
        ).fetchone()
        return json.loads(row["state_json"])

    def persist_campaign_state(self, conn, state: dict) -> None:
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        conn.execute(
            """
            update paper_expansion_campaign_state
            set phase=?,run_status=?,healthy_streak=?,state_json=?
            where campaign_id=?
            """,
            (
                state["phase"],
                state["run_status"],
                int(state.get("healthy_streak", 0) or 0),
                json.dumps(state),
                campaign_id,
            ),
        )
        conn.commit()

    def prepare_deferred_maintenance(
        self,
        conn,
    ) -> tuple[pathlib.Path, bytes, str]:
        self.initialize(conn)
        path, raw, digest = self.write_deferred_payloads(self.deferred_payload())
        state = self.campaign_state(conn)
        state["phase"] = "burn_in"
        state["run_status"] = "soft_paused"
        state["healthy_streak"] = 2
        state["stop_reason"] = "operator_maintenance_pause"
        state.pop("inflight_cycle", None)
        state.pop("paid_research_inflight", None)
        self.persist_campaign_state(conn, state)
        return path, raw, digest

    def adopt_deferred(self, conn, path: pathlib.Path, digest: str, **updates):
        kwargs = {
            "campaign_id": self.settings["paper_expansion"]["campaign_id"],
            "operator_reason": "reconcile legacy deferred cost ledger",
            "expected_source_path": path,
            "expected_source_sha256": digest,
            "expected_line_count": 1,
            "active_runtime_processes": [],
        }
        kwargs.update(updates)
        return campaign.adopt_deferred_cost_ledger(conn, self.settings, **kwargs)

    def add_paid_autonomous_attempt(
        self,
        *,
        lease_id: str,
        attempt_id: str,
        created_at: dt.datetime,
        model_name: str = "test",
        model_tier: str = "frontier",
        provider: str = "openai",
        api: str = "responses",
    ) -> None:
        path = pathlib.Path(self.settings["paper_expansion"]["autonomous_ledger_path"])
        ledger = sqlite3.connect(path)
        try:
            ledger.execute(
                """
                create table if not exists autonomous_paid_attempts (
                    attempt_id text primary key,
                    created_at text not null,
                    day_utc text not null,
                    scope_id text not null,
                    source text not null,
                    agent_name text,
                    operation text not null,
                    metadata_json text not null
                )
                """
            )
            ledger.execute(
                """
                insert into autonomous_paid_attempts(
                    attempt_id,created_at,day_utc,scope_id,source,agent_name,
                    operation,metadata_json
                ) values(?,?,?,?,?,?,?,?)
                """,
                (
                    attempt_id,
                    created_at.isoformat(),
                    created_at.date().isoformat(),
                    lease_id,
                    "paid_research_once",
                    "global_research_worker",
                    "bounded_crypto_paid_research",
                    json.dumps(
                        {
                            "model_name": model_name,
                            "model_tier": model_tier,
                            "provider": provider,
                            "api": api,
                            "prompt_tokens": 100,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            ledger.commit()
        finally:
            ledger.close()

    def measurement_metrics(self, *, exact_keys: int, closes: int = 0) -> dict:
        return self.healthy(
            phase_distinct_exact_attributed_admission_keys_paper_evaluated=exact_keys,
            new_direct_closes=closes,
            new_reliable_direct_closes=closes,
            new_timely_direct_closes=closes,
            phase_due_direct_closes=closes,
            phase_reliable_direct_closes=closes,
            phase_timely_direct_closes=closes,
            new_horizon_outcomes=closes,
            new_timely_horizon_outcomes=closes,
            phase_due_horizon_outcomes=closes,
            phase_timely_horizon_outcomes=closes,
            new_opportunity_lineage_records=closes,
            new_opportunity_lineage_complete=closes,
            new_order_lineage_records=closes,
            new_order_lineage_complete=closes,
            new_trade_lineage_records=closes,
            new_trade_lineage_complete=closes,
            new_synthetic_proxy_primary=0,
            lineage_corruption_count=0,
        )

    def test_authoritative_phase_cohorts_replace_additive_event_counts(self) -> None:
        state = {
            "accumulated": {
                "direct_closes": 99,
                "reliable_direct_closes": 98,
                "timely_direct_closes": 97,
                "horizon_outcomes": 96,
                "timely_horizon_outcomes": 95,
                "canary_reliable_direct_labels": 94,
            }
        }
        campaign._accumulate(
            state,
            {
                "new_direct_closes": 50,
                "new_reliable_direct_closes": 50,
                "new_timely_direct_closes": 50,
                "new_horizon_outcomes": 50,
                "new_timely_horizon_outcomes": 50,
                "new_canary_reliable_direct_labels": 50,
                "phase_due_direct_closes": 6,
                "phase_reliable_direct_closes": 3,
                "phase_timely_direct_closes": 3,
                "phase_due_horizon_outcomes": 6,
                "phase_timely_horizon_outcomes": 2,
                "phase_canary_reliable_direct_labels": 1,
            },
        )

        self.assertEqual(
            {
                "direct_closes": 6,
                "reliable_direct_closes": 3,
                "timely_direct_closes": 3,
                "horizon_outcomes": 6,
                "timely_horizon_outcomes": 2,
                "canary_reliable_direct_labels": 1,
                "opportunity_lineage_records": 0,
                "opportunity_lineage_complete": 0,
                "order_lineage_records": 0,
                "order_lineage_complete": 0,
                "trade_lineage_records": 0,
                "trade_lineage_complete": 0,
                "synthetic_proxy_primary": 0,
            },
            state["accumulated"],
        )

    def test_locked_phase_controls_and_canary_is_exactly_isolated(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "canary", elapsed_hours=48)
            effective, _token = campaign.apply_campaign_controls(conn, self.settings)
            lab = effective["strategy_lab"]
            self.assertEqual(1, effective["scanner"]["max_new_paper_trades"])
            self.assertEqual(
                1,
                effective["market_admission"]["paper_queue"]["max_select_per_cycle"],
            )
            self.assertEqual(0, effective["scanner"]["max_new_paper_observations"])
            self.assertEqual(100, effective["risk"]["max_open_paper_trades"])
            self.assertEqual(1, lab["max_candidates_per_loop"])
            self.assertEqual(1, lab["runtime_review_reserved_slots"])
            self.assertTrue(lab["bootstrap_recovery_canary_enabled"])
            self.assertEqual(
                ["recovery_okx_short_perp_long_spot_v1"],
                lab["experiment_root_allowlist"],
            )
            self.assertTrue(lab["snapshot_warmup_enabled"])
            self.assertEqual(200, lab["snapshot_max_inputs_per_loop"])
            self.assertEqual(50, lab["snapshot_max_instruments_per_loop"])
            self.assertEqual(288, lab["feature_history_max_points"])
            self.assertTrue(lab["runtime_generation_enabled"])
            self.assertTrue(lab["evaluation_enabled"])
            self.assertFalse(lab["lifecycle_mutations_enabled"])
            self.assertFalse(lab["recommendation_emission_enabled"])
            self.assertFalse(lab["promotion_enabled"])
            self.assertFalse(lab["promoted_signal_plugins_enabled"])
            self.assertFalse(effective["okx_signal_research"]["enabled"])
            self.assertFalse(effective["paper_exploration"]["enabled"])
            self.assertEqual("strategy_lab_canary", effective["paper_expansion"]["runtime_phase"])

    def test_canary_fill_guard_cannot_persist_a_synthetic_shadow(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "canary", elapsed_hours=48)
            effective, _token = campaign.apply_campaign_controls(conn, self.settings)
            candidate = {
                "venue": "GATE",
                "inst_id": "GATE:ARC_USDT",
                "direction": "short_frontier_spot",
                "trade_type": "frontier_crypto_venue_map",
                "market_surface": "frontier_crypto_venue_map",
                "frontier_paper_admission_guard_applies": True,
                "last": 1.0,
                "score": 80.0,
                "edge_bps_estimate": 24.0,
                "quality_status": "verified",
                "quality_action": "normal",
                "execution_route": {
                    "route_id": "conditional_crypto_route_paper",
                    "route_status": "conditional",
                    "missing_permissions": ["spot_borrow"],
                    "route_blockers": ["spot_borrow"],
                    "borrow_status": "required_unconfirmed",
                },
            }
            review = {
                "decision": "approve_conditional_paper_trade",
                "confidence": 0.8,
                "net_edge_bps_estimate": 24.0,
                "feasibility_status": "conditional",
                "route_status": "conditional",
                "missing_requirements": ["spot_borrow"],
                "paper_allocation_multiplier": 1.0,
            }

            self.assertEqual(0, effective["scanner"]["max_new_paper_observations"])
            result = execute_order(
                conn,
                candidate,
                review,
                effective,
                record_shadow_observation=(
                    effective["scanner"]["max_new_paper_observations"] > 0
                ),
            )

            self.assertFalse(result.get("paper_filled", False))
            self.assertFalse(result.get("shadow_observation_recorded", False))
            self.assertEqual(
                0,
                conn.execute(
                    "select count(*) from frontier_paper_shadow_observations"
                ).fetchone()[0],
            )

    def test_measurement_gate_requires_exact_counts_quality_and_lineage(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "measurement", elapsed_hours=168)
            _effective, token = campaign.apply_campaign_controls(conn, self.settings)
            metrics = self.measurement_metrics(exact_keys=100, closes=250)
            metrics["new_timely_direct_closes"] = 225
            metrics["new_horizon_outcomes"] = 100
            metrics["new_timely_horizon_outcomes"] = 90
            metrics["phase_timely_direct_closes"] = 225
            metrics["phase_due_horizon_outcomes"] = 100
            metrics["phase_timely_horizon_outcomes"] = 90
            report = campaign.record_campaign_cycle(conn, token, metrics)
            self.assertTrue(report["transitioned"])
            self.assertEqual("canary", report["phase_after"])
            self.assertEqual(100, report["gate_evidence"]["actual"]["exact_attributed_admission_keys"])
            self.assertEqual(1.0, report["gate_evidence"]["actual"]["lineage_rates"]["trade"])

    def test_measurement_gate_soft_pauses_when_due_phase_snapshot_is_missing(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "measurement", elapsed_hours=168)
            _effective, token = campaign.apply_campaign_controls(conn, self.settings)
            metrics = self.measurement_metrics(exact_keys=100, closes=250)
            metrics.pop("phase_due_horizon_outcomes")

            report = campaign.record_campaign_cycle(conn, token, metrics)

            self.assertFalse(report["transitioned"])
            self.assertEqual("soft_paused", report["status"])
            self.assertIn(
                "missing_metric:phase_due_horizon_outcomes", report["soft_reasons"]
            )

    def test_distinct_exact_key_count_is_maxed_not_summed(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "measurement", elapsed_hours=168)
            for _ in range(2):
                _effective, token = campaign.apply_campaign_controls(conn, self.settings)
                report = campaign.record_campaign_cycle(
                    conn,
                    token,
                    self.measurement_metrics(exact_keys=1, closes=1),
                )
            self.assertEqual(1, report["state"]["accumulated"]["exact_attributed_admission_keys"])
            self.assertFalse(report["transitioned"])

    def test_measurement_and_canary_duration_gates_use_healthy_time(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        cfg = self.settings["paper_expansion"]
        measurement = {
            "phase": "measurement",
            "phase_started_at": (now - dt.timedelta(days=30)).isoformat(),
            "phase_healthy_running_seconds": 167 * 3600.0,
            "accumulated": {
                "direct_closes": 250,
                "reliable_direct_closes": 250,
                "timely_direct_closes": 250,
                "horizon_outcomes": 250,
                "timely_horizon_outcomes": 250,
                "exact_attributed_admission_keys": 100,
                "opportunity_lineage_records": 250,
                "opportunity_lineage_complete": 250,
                "order_lineage_records": 250,
                "order_lineage_complete": 250,
                "trade_lineage_records": 250,
                "trade_lineage_complete": 250,
                "synthetic_proxy_primary": 0,
            },
        }
        passed, reasons, evidence = campaign._phase_gate_passed(
            measurement, cfg, {}, now
        )
        self.assertFalse(passed)
        self.assertIn("measurement_elapsed_hours", reasons)
        self.assertEqual(167.0, evidence["healthy_elapsed_hours"])
        self.assertGreaterEqual(evidence["wall_elapsed_hours"], 720.0)
        measurement["phase_healthy_running_seconds"] = 168 * 3600.0
        passed, reasons, _evidence = campaign._phase_gate_passed(
            measurement, cfg, {}, now
        )
        self.assertTrue(passed, reasons)

        canary = {
            "phase": "canary",
            "phase_started_at": (now - dt.timedelta(days=10)).isoformat(),
            "phase_healthy_running_seconds": 47 * 3600.0,
            "accumulated": {"canary_reliable_direct_labels": 30},
        }
        passed, reasons, evidence = campaign._phase_gate_passed(
            canary, cfg, {"active_canary_count": 1}, now
        )
        self.assertFalse(passed)
        self.assertIn("canary_elapsed_hours", reasons)
        self.assertEqual(47.0, evidence["healthy_elapsed_hours"])
        canary["phase_healthy_running_seconds"] = 48 * 3600.0
        passed, reasons, _evidence = campaign._phase_gate_passed(
            canary, cfg, {"active_canary_count": 1}, now
        )
        self.assertTrue(passed, reasons)

    def test_persisted_phase_clock_excludes_pause_resume_and_caps_downtime(self) -> None:
        base = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.settings["paper_expansion"]["phases"]["measurement"][
            "min_elapsed_hours"
        ] = 0.5
        with self.connection() as conn:
            with mock.patch.object(campaign, "_utc_now", return_value=base):
                self.initialize(conn)
            campaign_id = self.settings["paper_expansion"]["campaign_id"]
            row = conn.execute(
                "select state_json from paper_expansion_campaign_state where campaign_id=?",
                (campaign_id,),
            ).fetchone()
            state = json.loads(row["state_json"])
            state.update(
                {
                    "phase": "measurement",
                    "run_status": "running",
                    "healthy_streak": 0,
                    "phase_healthy_cycles": 0,
                    "phase_started_at": (base - dt.timedelta(days=10)).isoformat(),
                    "phase_healthy_running_seconds": 0.0,
                    "phase_clock_checkpoint_at": base.isoformat(),
                    "accumulated": {},
                }
            )
            state.pop("inflight_cycle", None)
            conn.execute(
                """
                update paper_expansion_campaign_state
                set phase='measurement',run_status='running',healthy_streak=0,
                    phase_started_at=?,state_json=?
                where campaign_id=?
                """,
                (state["phase_started_at"], json.dumps(state), campaign_id),
            )
            conn.commit()

            def run_cycle(at: dt.datetime, metrics: dict) -> tuple[dict, dict]:
                with mock.patch.object(campaign, "_utc_now", return_value=at):
                    _effective, token = campaign.apply_campaign_controls(conn, self.settings)
                    report = campaign.record_campaign_cycle(conn, token, metrics)
                return token, report

            _token, failed = run_cycle(base + dt.timedelta(hours=10), {})
            self.assertEqual("soft_paused", failed["status"])
            self.assertEqual(0.0, failed["metrics"]["phase_healthy_seconds_credited"])

            report = failed
            for hours in (11, 12, 13):
                _token, report = run_cycle(
                    base + dt.timedelta(hours=hours),
                    self.measurement_metrics(exact_keys=0),
                )
                self.assertEqual(0.0, report["metrics"]["phase_healthy_seconds_credited"])
            self.assertEqual("healthy_resumed", report["health_status"])
            self.assertEqual(0.0, report["state"]["phase_healthy_running_seconds"])
            self.assertEqual(0, report["state"]["phase_healthy_cycles"])

            first_at = base + dt.timedelta(hours=13, minutes=15)
            first_token, first = run_cycle(
                first_at,
                self.measurement_metrics(exact_keys=100, closes=250),
            )
            self.assertEqual(900.0, first["metrics"]["phase_healthy_seconds_credited"])
            self.assertEqual(1, first["state"]["phase_healthy_cycles"])
            self.assertFalse(first["transitioned"])
            with mock.patch.object(campaign, "_utc_now", return_value=first_at):
                duplicate = campaign.record_campaign_cycle(
                    conn,
                    first_token,
                    self.measurement_metrics(exact_keys=100, closes=250),
                )
            self.assertEqual("already_recorded", duplicate["status"])
            persisted = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()["state_json"]
            )
            self.assertEqual(900.0, persisted["phase_healthy_running_seconds"])

            _token, second = run_cycle(
                base + dt.timedelta(hours=30),
                self.measurement_metrics(exact_keys=100, closes=250),
            )
            self.assertEqual(900.0, second["metrics"]["phase_healthy_seconds_credited"])
            self.assertEqual(1800.0, second["metrics"]["phase_healthy_running_seconds"])
            self.assertTrue(second["transitioned"])
            self.assertEqual("canary", second["phase_after"])
            self.assertEqual(0.0, second["state"]["phase_healthy_running_seconds"])
            self.assertEqual(
                (base + dt.timedelta(hours=30)).isoformat(),
                second["state"]["phase_clock_checkpoint_at"],
            )

    def test_legacy_phase_clock_state_migrates_fail_closed(self) -> None:
        base = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        with self.connection() as conn:
            with mock.patch.object(campaign, "_utc_now", return_value=base):
                self.initialize(conn)
            campaign_id = self.settings["paper_expansion"]["campaign_id"]
            row = conn.execute(
                "select state_json from paper_expansion_campaign_state where campaign_id=?",
                (campaign_id,),
            ).fetchone()
            state = json.loads(row["state_json"])
            state.update(
                {
                    "phase": "measurement",
                    "run_status": "running",
                    "phase_started_at": (base - dt.timedelta(days=30)).isoformat(),
                    "accumulated": {},
                }
            )
            state.pop("phase_healthy_running_seconds", None)
            state.pop("phase_clock_checkpoint_at", None)
            state.pop("inflight_cycle", None)
            conn.execute(
                """
                update paper_expansion_campaign_state
                set phase='measurement',run_status='running',phase_started_at=?,state_json=?
                where campaign_id=?
                """,
                (state["phase_started_at"], json.dumps(state), campaign_id),
            )
            conn.commit()

            migrated_at = base + dt.timedelta(days=1)
            with mock.patch.object(campaign, "_utc_now", return_value=migrated_at):
                _effective, token = campaign.apply_campaign_controls(conn, self.settings)
                report = campaign.record_campaign_cycle(
                    conn,
                    token,
                    self.measurement_metrics(exact_keys=100, closes=250),
                )
            self.assertEqual(0.0, report["metrics"]["phase_healthy_seconds_credited"])
            self.assertEqual(0.0, report["state"]["phase_healthy_running_seconds"])
            self.assertIn("measurement_elapsed_hours", report["gate_reasons"])

    def test_lineage_corruption_hard_halts(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "measurement", elapsed_hours=168)
            _effective, token = campaign.apply_campaign_controls(conn, self.settings)
            metrics = self.measurement_metrics(exact_keys=1, closes=1)
            metrics["lineage_corruption_count"] = 1
            report = campaign.record_campaign_cycle(conn, token, metrics)
            self.assertEqual("hard_halted", report["status"])
            self.assertIn("lineage_corruption", report["hard_reasons"])

    def test_unknown_cost_ledger_hard_halts_before_new_work(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                insert into llm_cost_events(
                    created_at,agent_name,model_tier,model_name,prompt_tokens,
                    completion_tokens,estimated_cost_usd,status
                ) values(?,?,?,?,?,?,?,?)
                """,
                (
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    "unknown-cost-test",
                    "fast",
                    "test",
                    1,
                    1,
                    "unknown",
                    "model_call",
                ),
            )
            conn.commit()
            effective, token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual("hard_halted", effective["paper_expansion_runtime"]["run_status"])
            self.assertEqual(0, effective["scanner"]["scan_universe"])
            self.assertEqual(0, effective["scanner"]["max_new_paper_trades"])
            self.assertEqual(
                "cost_ledger_invalid_costs_at_cycle_start",
                token["hard_halt_reason"],
            )

    def test_between_cycle_model_event_hard_halts_before_new_work(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            conn.execute(
                """
                insert into llm_cost_events(
                    created_at,agent_name,model_tier,model_name,operation,
                    structured_json,prompt_tokens,completion_tokens,
                    estimated_cost_usd,status,event_id
                ) values(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    "unexpected_between_cycle_agent",
                    "fast",
                    "test",
                    "unexpected_operation",
                    1,
                    1,
                    1,
                    0.01,
                    "model_call:responses",
                    "unexpected-between-cycle-event",
                ),
            )
            conn.commit()

            effective, token = campaign.apply_campaign_controls(conn, self.settings)

            self.assertEqual("hard_halted", token["run_status"])
            self.assertEqual(0, effective["scanner"]["scan_universe"])
            self.assertEqual(0, effective["scanner"]["review_top"])
            self.assertEqual(0, effective["scanner"]["max_new_paper_trades"])
            self.assertTrue(token["intercycle_safety_check"]["hard_halt"])
            self.assertIn(
                "intercycle_unattributed_model_activity",
                token["intercycle_safety_check"]["reasons"],
            )

    def test_between_cycle_live_order_attempt_hard_halts_before_new_work(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            conn.execute(
                """
                insert into execution_orders(
                    created_at,mode,route_id,venue,inst_id,direction,trade_type,
                    status,notional_usd,order_json,candidate_json,review_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    "live",
                    "forbidden-live-attempt",
                    "OKX",
                    "BTC-USDT-SWAP",
                    "short_perp_long_spot",
                    "perp_funding_basis",
                    "blocked_live_trading_not_implemented",
                    100.0,
                    "{}",
                    "{}",
                    "{}",
                ),
            )
            conn.commit()

            effective, token = campaign.apply_campaign_controls(conn, self.settings)

            self.assertEqual("hard_halted", token["run_status"])
            self.assertEqual(0, effective["scanner"]["scan_universe"])
            self.assertEqual(0, effective["scanner"]["max_new_paper_trades"])
            self.assertIn(
                "intercycle_forbidden_activity:live_orders",
                token["intercycle_safety_check"]["reasons"],
            )

    def test_between_cycle_agent_run_hard_halts_before_new_work(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            conn.execute(
                """
                insert into agent_runs(
                    run_id,agent_id,cycle_id,started_at,duration_ms,status,
                    trigger_match_json,memory_ids_json,model_json,
                    recommendation_json,outcome_json
                ) values(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "forbidden-between-cycle-agent-run",
                    "forbidden-agent",
                    "outside-bounded-radar",
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    1,
                    "completed",
                    "{}",
                    "[]",
                    "{}",
                    "{}",
                    "{}",
                ),
            )
            conn.commit()

            effective, token = campaign.apply_campaign_controls(conn, self.settings)

            self.assertEqual("hard_halted", token["run_status"])
            self.assertEqual(0, effective["scanner"]["scan_universe"])
            self.assertIn(
                "intercycle_forbidden_activity:agent_runs",
                token["intercycle_safety_check"]["reasons"],
            )

    def test_between_cycle_authorized_paid_research_result_is_accepted_once(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "research")
            campaign_id = self.settings["paper_expansion"]["campaign_id"]
            row = conn.execute(
                "select state_json from paper_expansion_campaign_state where campaign_id=?",
                (campaign_id,),
            ).fetchone()
            state = json.loads(row["state_json"])
            watermark = state["last_completed_safety_watermark"]
            captured_at = dt.datetime.fromisoformat(watermark["captured_at"])
            lease_started_at = captured_at + dt.timedelta(seconds=1)
            autonomous_attempt_at = lease_started_at + dt.timedelta(milliseconds=500)
            event_created_at = lease_started_at + dt.timedelta(seconds=1)
            lease_completed_at = event_created_at + dt.timedelta(seconds=1)
            apply_at = lease_completed_at + dt.timedelta(seconds=1)
            conn.execute(
                """
                insert into llm_cost_events(
                    created_at,agent_name,model_tier,model_name,provider,api,operation,
                    structured_json,prompt_tokens,completion_tokens,
                    estimated_cost_usd,status,event_id
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_created_at.isoformat(),
                    "global_research_worker",
                    "frontier",
                    "test",
                    "openai",
                    "responses",
                    "bounded_crypto_paid_research",
                    1,
                    100,
                    100,
                    0.25,
                    "model_call:responses",
                    "authorized-paid-research-event",
                ),
            )
            self.add_paid_autonomous_attempt(
                lease_id="authorized-paid-research-lease",
                attempt_id="authorized-paid-research-attempt",
                created_at=autonomous_attempt_at,
            )
            state["last_paid_research_lease"] = {
                "lease_id": "authorized-paid-research-lease",
                "campaign_id": campaign_id,
                "started_at": lease_started_at.isoformat(),
                "lease_expires_at": (
                    lease_started_at + dt.timedelta(minutes=10)
                ).isoformat(),
                "pid": 4242,
                "config_hash": campaign._config_hash(self.settings),
                "completed_at": lease_completed_at.isoformat(),
                "outcome": "model_call:responses",
                "provider_outcome": "model_call:responses",
                "provider_event_id": "authorized-paid-research-event",
                "provider_estimated_cost_usd": 0.25,
                "operation_outcome": "completed",
                "failure_category": None,
            }
            state["updated_at"] = lease_completed_at.isoformat()
            conn.execute(
                """
                update paper_expansion_campaign_state
                set updated_at=?,state_json=?
                where campaign_id=?
                """,
                (state["updated_at"], json.dumps(state), campaign_id),
            )
            conn.commit()

            with mock.patch.object(campaign, "_utc_now", return_value=apply_at):
                effective, token = campaign.apply_campaign_controls(conn, self.settings)

            self.assertEqual("running", token["run_status"])
            self.assertEqual(10, effective["scanner"]["max_new_paper_trades"])
            self.assertEqual(20, effective["scanner"]["max_new_paper_observations"])
            self.assertEqual(100, effective["risk"]["max_open_paper_trades"])
            intercycle = token["intercycle_safety_check"]
            self.assertEqual("authorized_paid_research", intercycle["status"])
            self.assertFalse(intercycle["hard_halt"])
            self.assertEqual(
                "authorized-paid-research-event",
                intercycle["authorized_paid_research"]["event_id"],
            )
            with mock.patch.object(
                campaign,
                "_utc_now",
                return_value=apply_at + dt.timedelta(seconds=1),
            ):
                report = campaign.record_campaign_cycle(conn, token, self.healthy())
            persisted_watermark = report["state"]["last_completed_safety_watermark"]
            self.assertEqual(
                "authorized-paid-research-lease",
                persisted_watermark["last_paid_research_lease_id"],
            )
            self.assertEqual(
                "authorized-paid-research-event",
                persisted_watermark["authorized_paid_research"]["event_id"],
            )

            # The same exact one-shot lease/result shape is never exempted
            # outside the research phase.
            self.set_phase(conn, "measurement", elapsed_hours=1)
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()["state_json"]
            )
            captured_at = dt.datetime.fromisoformat(
                state["last_completed_safety_watermark"]["captured_at"]
            )
            second_started_at = captured_at + dt.timedelta(seconds=1)
            second_attempt_at = second_started_at + dt.timedelta(milliseconds=500)
            second_event_at = second_started_at + dt.timedelta(seconds=1)
            second_completed_at = second_event_at + dt.timedelta(seconds=1)
            conn.execute(
                """
                insert into llm_cost_events(
                    created_at,agent_name,model_tier,model_name,provider,api,operation,
                    structured_json,prompt_tokens,completion_tokens,
                    estimated_cost_usd,status,event_id
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    second_event_at.isoformat(),
                    "global_research_worker",
                    "frontier",
                    "test",
                    "openai",
                    "responses",
                    "bounded_crypto_paid_research",
                    1,
                    100,
                    100,
                    0.25,
                    "model_call:responses",
                    "paid-research-event-outside-research",
                ),
            )
            self.add_paid_autonomous_attempt(
                lease_id="paid-research-lease-outside-research",
                attempt_id="paid-research-attempt-outside-research",
                created_at=second_attempt_at,
            )
            state["last_paid_research_lease"] = {
                "lease_id": "paid-research-lease-outside-research",
                "campaign_id": campaign_id,
                "started_at": second_started_at.isoformat(),
                "lease_expires_at": (
                    second_started_at + dt.timedelta(minutes=10)
                ).isoformat(),
                "pid": 4243,
                "config_hash": campaign._config_hash(self.settings),
                "completed_at": second_completed_at.isoformat(),
                "outcome": "model_call:responses",
                "provider_outcome": "model_call:responses",
                "provider_event_id": "paid-research-event-outside-research",
                "provider_estimated_cost_usd": 0.25,
                "operation_outcome": "completed",
                "failure_category": None,
            }
            state["updated_at"] = second_completed_at.isoformat()
            conn.execute(
                """
                update paper_expansion_campaign_state
                set updated_at=?,state_json=?
                where campaign_id=?
                """,
                (state["updated_at"], json.dumps(state), campaign_id),
            )
            conn.commit()
            with mock.patch.object(
                campaign,
                "_utc_now",
                return_value=second_completed_at + dt.timedelta(seconds=1),
            ):
                rejected_controls, rejected_token = campaign.apply_campaign_controls(
                    conn,
                    self.settings,
                )
            self.assertEqual("hard_halted", rejected_token["run_status"])
            self.assertEqual(0, rejected_controls["scanner"]["max_new_paper_trades"])
            self.assertIn(
                "intercycle_paid_research_attribution:phase_not_research",
                rejected_token["intercycle_safety_check"]["reasons"],
            )

    def test_attributed_paid_provider_failure_soft_pauses_then_resumes(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "research")
            campaign_id = self.settings["paper_expansion"]["campaign_id"]
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()["state_json"]
            )
            captured_at = dt.datetime.fromisoformat(
                state["last_completed_safety_watermark"]["captured_at"]
            )
            lease_started_at = captured_at + dt.timedelta(seconds=1)
            attempt_at = lease_started_at + dt.timedelta(milliseconds=500)
            event_at = lease_started_at + dt.timedelta(seconds=1)
            completed_at = event_at + dt.timedelta(seconds=1)
            outcome = "fallback_error:TimeoutError"
            conn.execute(
                """
                insert into llm_cost_events(
                    created_at,agent_name,model_tier,model_name,provider,api,operation,
                    structured_json,prompt_tokens,completion_tokens,
                    estimated_cost_usd,status,event_id
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_at.isoformat(),
                    "global_research_worker",
                    "frontier",
                    "test",
                    "openai",
                    "responses",
                    "bounded_crypto_paid_research",
                    1,
                    100,
                    0,
                    0.25,
                    outcome,
                    "known-paid-provider-failure-event",
                ),
            )
            self.add_paid_autonomous_attempt(
                lease_id="known-paid-provider-failure-lease",
                attempt_id="known-paid-provider-failure-attempt",
                created_at=attempt_at,
            )
            state["last_paid_research_lease"] = {
                "lease_id": "known-paid-provider-failure-lease",
                "campaign_id": campaign_id,
                "started_at": lease_started_at.isoformat(),
                "lease_expires_at": (
                    lease_started_at + dt.timedelta(minutes=10)
                ).isoformat(),
                "pid": 4244,
                "config_hash": campaign._config_hash(self.settings),
                "completed_at": completed_at.isoformat(),
                "outcome": outcome,
                "provider_outcome": outcome,
                "provider_event_id": "known-paid-provider-failure-event",
                "provider_estimated_cost_usd": 0.25,
                "operation_outcome": "downstream_failure",
                "failure_category": "provider_failure",
            }
            state["updated_at"] = completed_at.isoformat()
            conn.execute(
                """
                update paper_expansion_campaign_state
                set updated_at=?,state_json=?
                where campaign_id=?
                """,
                (state["updated_at"], json.dumps(state), campaign_id),
            )
            conn.commit()

            clock = completed_at + dt.timedelta(seconds=1)
            with mock.patch.object(campaign, "_utc_now", return_value=clock):
                paused_controls, token = campaign.apply_campaign_controls(
                    conn,
                    self.settings,
                )
            self.assertEqual("soft_paused", token["run_status"])
            self.assertEqual(0, paused_controls["scanner"]["max_new_paper_trades"])
            self.assertTrue(paused_controls["paper_expansion"]["reconciliation_only"])
            intercycle = token["intercycle_safety_check"]
            self.assertEqual("attributed_paid_research_failure", intercycle["status"])
            self.assertTrue(intercycle["soft_pause"])
            self.assertEqual(
                "known_provider_failure",
                intercycle["authorized_paid_research"]["result_type"],
            )

            reports = []
            for probe in range(3):
                record_at = clock + dt.timedelta(seconds=(probe * 2) + 1)
                with mock.patch.object(campaign, "_utc_now", return_value=record_at):
                    reports.append(
                        campaign.record_campaign_cycle(conn, token, self.healthy())
                    )
                if probe < 2:
                    apply_at = record_at + dt.timedelta(seconds=1)
                    with mock.patch.object(campaign, "_utc_now", return_value=apply_at):
                        probe_controls, token = campaign.apply_campaign_controls(
                            conn,
                            self.settings,
                        )
                    self.assertEqual(
                        0,
                        probe_controls["scanner"]["max_new_paper_trades"],
                    )
            self.assertEqual("healthy_resume_probe", reports[0]["health_status"])
            self.assertEqual("healthy_resume_probe", reports[1]["health_status"])
            self.assertEqual("healthy_resumed", reports[2]["health_status"])
            self.assertEqual("running", reports[2]["status"])
            with mock.patch.object(
                campaign,
                "_utc_now",
                return_value=clock + dt.timedelta(seconds=10),
            ):
                resumed_controls, _resumed_token = campaign.apply_campaign_controls(
                    conn,
                    self.settings,
                )
            self.assertEqual(10, resumed_controls["scanner"]["max_new_paper_trades"])

    def test_attributed_paid_downstream_failure_preserves_provider_identity(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "research")
            campaign_id = self.settings["paper_expansion"]["campaign_id"]
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()["state_json"]
            )
            captured_at = dt.datetime.fromisoformat(
                state["last_completed_safety_watermark"]["captured_at"]
            )
            started_at = captured_at + dt.timedelta(seconds=1)
            attempt_at = started_at + dt.timedelta(milliseconds=500)
            event_at = started_at + dt.timedelta(seconds=1)
            completed_at = event_at + dt.timedelta(seconds=1)
            event_id = "paid-downstream-failure-event"
            lease_id = "paid-downstream-failure-lease"
            conn.execute(
                """
                insert into llm_cost_events(
                    created_at,agent_name,model_tier,model_name,provider,api,operation,
                    structured_json,prompt_tokens,completion_tokens,
                    estimated_cost_usd,status,event_id
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_at.isoformat(),
                    "global_research_worker",
                    "frontier",
                    "test",
                    "openai",
                    "responses",
                    "bounded_crypto_paid_research",
                    1,
                    100,
                    100,
                    0.25,
                    "model_call:responses",
                    event_id,
                ),
            )
            self.add_paid_autonomous_attempt(
                lease_id=lease_id,
                attempt_id="paid-downstream-failure-attempt",
                created_at=attempt_at,
            )
            state["last_paid_research_lease"] = {
                "lease_id": lease_id,
                "campaign_id": campaign_id,
                "started_at": started_at.isoformat(),
                "lease_expires_at": (started_at + dt.timedelta(minutes=10)).isoformat(),
                "pid": 4247,
                "config_hash": campaign._config_hash(self.settings),
                "completed_at": completed_at.isoformat(),
                "outcome": "model_call:responses",
                "provider_outcome": "model_call:responses",
                "provider_event_id": event_id,
                "provider_estimated_cost_usd": 0.25,
                "operation_outcome": "downstream_failure",
                "failure_category": "downstream_parse_and_ingest_failed",
            }
            state["updated_at"] = completed_at.isoformat()
            conn.execute(
                """
                update paper_expansion_campaign_state
                set updated_at=?,state_json=?
                where campaign_id=?
                """,
                (state["updated_at"], json.dumps(state), campaign_id),
            )
            conn.commit()

            clock = completed_at + dt.timedelta(seconds=1)
            with mock.patch.object(campaign, "_utc_now", return_value=clock):
                controls, token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual("soft_paused", token["run_status"])
            self.assertEqual(0, controls["scanner"]["max_new_paper_trades"])
            check = token["intercycle_safety_check"]
            self.assertEqual("attributed_paid_research_failure", check["status"])
            attribution = check["authorized_paid_research"]
            self.assertEqual("downstream_failure", attribution["result_type"])
            self.assertEqual(event_id, attribution["event_id"])
            self.assertEqual(
                "downstream_parse_and_ingest_failed",
                attribution["failure_category"],
            )

    def test_known_zero_delta_paid_gate_soft_pauses_without_model_activity(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "research")
            campaign_id = self.settings["paper_expansion"]["campaign_id"]
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()["state_json"]
            )
            captured_at = dt.datetime.fromisoformat(
                state["last_completed_safety_watermark"]["captured_at"]
            )
            started_at = captured_at + dt.timedelta(seconds=1)
            completed_at = started_at + dt.timedelta(seconds=1)
            state["last_paid_research_lease"] = {
                "lease_id": "known-zero-delta-evidence-gate",
                "campaign_id": campaign_id,
                "started_at": started_at.isoformat(),
                "lease_expires_at": (started_at + dt.timedelta(minutes=10)).isoformat(),
                "pid": 4245,
                "config_hash": campaign._config_hash(self.settings),
                "completed_at": completed_at.isoformat(),
                "outcome": "evidence_denied",
                "operation_outcome": "evidence_denied",
                "failure_category": "reliable_direct_evidence_unavailable",
                "provider_outcome": None,
                "provider_event_id": None,
                "provider_estimated_cost_usd": None,
            }
            state["updated_at"] = completed_at.isoformat()
            conn.execute(
                """
                update paper_expansion_campaign_state
                set updated_at=?,state_json=?
                where campaign_id=?
                """,
                (state["updated_at"], json.dumps(state), campaign_id),
            )
            conn.commit()

            clock = completed_at + dt.timedelta(seconds=1)
            with mock.patch.object(campaign, "_utc_now", return_value=clock):
                controls, token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual("soft_paused", token["run_status"])
            self.assertEqual(0, controls["scanner"]["max_new_paper_trades"])
            self.assertTrue(controls["paper_expansion"]["reconciliation_only"])
            check = token["intercycle_safety_check"]
            self.assertEqual("attributed_paid_research_gate", check["status"])
            self.assertEqual(
                "soft_gate_blocked",
                check["authorized_paid_research"]["result_type"],
            )
            self.assertEqual(0, check["deltas"]["llm_cost_events"])
            self.assertEqual(0, check["deltas"]["autonomous_attempts_today"])
            for probe in range(3):
                record_at = clock + dt.timedelta(seconds=(probe * 2) + 1)
                with mock.patch.object(campaign, "_utc_now", return_value=record_at):
                    report = campaign.record_campaign_cycle(conn, token, self.healthy())
                self.assertEqual("soft_paused", report["status"])
                self.assertEqual("soft_paused", report["health_status"])
                self.assertEqual(0, report["state"]["healthy_streak"])
                self.assertIn(
                    "recovery_gate_unhealthy:research_evidence",
                    report["soft_reasons"],
                )
                if probe < 2:
                    with mock.patch.object(
                        campaign,
                        "_utc_now",
                        return_value=record_at + dt.timedelta(seconds=1),
                    ):
                        _probe_controls, token = campaign.apply_campaign_controls(
                            conn,
                            self.settings,
                        )
                    self.assertEqual(
                        0,
                        _probe_controls["scanner"]["max_new_paper_trades"],
                    )
            self.assertIn("recovery_pause_gate", report["state"])

    def test_unknown_zero_delta_paid_gate_hard_halts(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "research")
            campaign_id = self.settings["paper_expansion"]["campaign_id"]
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()["state_json"]
            )
            captured_at = dt.datetime.fromisoformat(
                state["last_completed_safety_watermark"]["captured_at"]
            )
            started_at = captured_at + dt.timedelta(seconds=1)
            completed_at = started_at + dt.timedelta(seconds=1)
            state["last_paid_research_lease"] = {
                "lease_id": "unknown-zero-delta-cost-gate",
                "campaign_id": campaign_id,
                "started_at": started_at.isoformat(),
                "lease_expires_at": (started_at + dt.timedelta(minutes=10)).isoformat(),
                "pid": 4246,
                "config_hash": campaign._config_hash(self.settings),
                "completed_at": completed_at.isoformat(),
                "outcome": "budget_denied",
                "operation_outcome": "budget_denied",
                "failure_category": "cost_ledger_unknown",
                "provider_outcome": None,
                "provider_event_id": None,
                "provider_estimated_cost_usd": None,
            }
            state["updated_at"] = completed_at.isoformat()
            conn.execute(
                """
                update paper_expansion_campaign_state
                set updated_at=?,state_json=?
                where campaign_id=?
                """,
                (state["updated_at"], json.dumps(state), campaign_id),
            )
            conn.commit()

            with mock.patch.object(
                campaign,
                "_utc_now",
                return_value=completed_at + dt.timedelta(seconds=1),
            ):
                controls, token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual("hard_halted", token["run_status"])
            self.assertEqual(0, controls["scanner"]["max_new_paper_trades"])
            self.assertIn(
                "intercycle_unattributed_paid_research_lease",
                token["intercycle_safety_check"]["reasons"],
            )
            self.assertIn(
                (
                    "intercycle_paid_research_attribution:"
                    "soft_gate_failure_category_unrecognized"
                ),
                token["intercycle_safety_check"]["reasons"],
            )

    def test_exhausted_paid_budget_cannot_resume_or_admit(self) -> None:
        with self.connection() as conn:
            event_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
            # The research agent's locked $4 cap can be exhausted while the
            # global $25/10-call ceiling still has ample room.
            for index in range(1):
                conn.execute(
                    """
                    insert into llm_cost_events(
                        created_at,agent_name,model_tier,model_name,provider,api,
                        operation,structured_json,prompt_tokens,completion_tokens,
                        estimated_cost_usd,status,event_id
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event_at.isoformat(),
                        "global_research_worker",
                        "frontier",
                        "historical-budget-use",
                        "openai",
                        "responses",
                        "bounded_crypto_paid_research",
                        1,
                        1,
                        1,
                        4.0,
                        "model_call:responses",
                        f"historical-budget-event-{index}",
                    ),
                )
            conn.commit()
            self.initialize(conn)
            self.set_phase(conn, "research")
            campaign_id = self.settings["paper_expansion"]["campaign_id"]
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()["state_json"]
            )
            captured_at = dt.datetime.fromisoformat(
                state["last_completed_safety_watermark"]["captured_at"]
            )
            started_at = captured_at + dt.timedelta(seconds=1)
            completed_at = started_at + dt.timedelta(seconds=1)
            state["last_paid_research_lease"] = {
                "lease_id": "known-exhausted-budget-gate",
                "campaign_id": campaign_id,
                "started_at": started_at.isoformat(),
                "lease_expires_at": (started_at + dt.timedelta(minutes=10)).isoformat(),
                "pid": 4248,
                "config_hash": campaign._config_hash(self.settings),
                "completed_at": completed_at.isoformat(),
                "outcome": "budget_denied",
                "operation_outcome": "budget_denied",
                "failure_category": "cost_ceiling_or_call_limit",
                "provider_outcome": None,
                "provider_event_id": None,
                "provider_estimated_cost_usd": None,
            }
            state["updated_at"] = completed_at.isoformat()
            conn.execute(
                """
                update paper_expansion_campaign_state
                set updated_at=?,state_json=?
                where campaign_id=?
                """,
                (state["updated_at"], json.dumps(state), campaign_id),
            )
            conn.commit()

            clock = completed_at + dt.timedelta(seconds=1)
            with mock.patch.object(campaign, "_utc_now", return_value=clock):
                controls, token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual("soft_paused", token["run_status"])
            self.assertEqual(0, controls["scanner"]["max_new_paper_trades"])
            self.assertEqual(
                "cost_capacity_exhausted",
                token["pause_gate_revalidation"]["reason"],
            )
            cost_windows = token["pause_gate_revalidation"]["detail"]["windows"]
            self.assertTrue(cost_windows["global_utc_day"]["healthy"])
            self.assertFalse(cost_windows["paid_research_agent_utc_day"]["healthy"])
            for probe in range(3):
                record_at = clock + dt.timedelta(seconds=(probe * 2) + 1)
                with mock.patch.object(campaign, "_utc_now", return_value=record_at):
                    report = campaign.record_campaign_cycle(conn, token, self.healthy())
                self.assertEqual("soft_paused", report["status"])
                self.assertEqual(0, report["state"]["healthy_streak"])
                self.assertIn(
                    "recovery_gate_unhealthy:paid_cost_capacity",
                    report["soft_reasons"],
                )
                if probe < 2:
                    with mock.patch.object(
                        campaign,
                        "_utc_now",
                        return_value=record_at + dt.timedelta(seconds=1),
                    ):
                        controls, token = campaign.apply_campaign_controls(
                            conn,
                            self.settings,
                        )
                    self.assertEqual(0, controls["scanner"]["max_new_paper_trades"])
            self.assertIn("recovery_pause_gate", report["state"])

    def test_canary_gate_is_only_48_hours_and_30_reliable_direct_labels(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "canary", elapsed_hours=48)
            _effective, token = campaign.apply_campaign_controls(conn, self.settings)
            report = campaign.record_campaign_cycle(
                conn,
                token,
                self.healthy(
                    active_canary_count=1,
                    new_canary_reliable_direct_labels=30,
                    phase_canary_reliable_direct_labels=30,
                ),
            )
            self.assertTrue(report["transitioned"])
            self.assertEqual("research", report["phase_after"])
            self.assertTrue(report["research_ready"])

    def test_research_keeps_measurement_capacity_but_never_promotes(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "research")
            effective, _token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual(10, effective["scanner"]["max_new_paper_trades"])
            self.assertEqual(20, effective["scanner"]["max_new_paper_observations"])
            self.assertEqual(100, effective["risk"]["max_open_paper_trades"])
            self.assertEqual(6, effective["strategy_lab"]["max_active_strategy_roots"])
            self.assertTrue(effective["strategy_lab"]["enabled"])
            self.assertTrue(effective["strategy_lab"]["lifecycle_mutations_enabled"])
            self.assertFalse(effective["strategy_lab"]["region_splits_enabled"])
            self.assertFalse(effective["strategy_lab"]["recommendation_emission_enabled"])
            self.assertFalse(effective["strategy_lab"]["promotion_enabled"])
            self.assertEqual(100, effective["strategy_lab"]["promote_min_labels"])
            self.assertEqual(100, effective["strategy_lab"]["promote_min_training_labels"])
            self.assertEqual(50, effective["strategy_lab"]["promote_min_holdout_labels"])
            self.assertEqual(50, effective["strategy_lab"]["holdout_min_labels"])
            self.assertEqual(50, effective["strategy_lab"]["promote_holdout_min_labels"])
            self.assertEqual(168, effective["strategy_lab"]["promote_min_active_hours"])
            self.assertEqual(10.0, effective["strategy_lab"]["promote_min_avg_pnl_bps"])
            self.assertEqual(0.53, effective["strategy_lab"]["promote_min_win_rate"])
            self.assertEqual(-45.0, effective["strategy_lab"]["promote_worst_decile_floor_bps"])
            self.assertEqual(0.90, effective["strategy_lab"]["promote_min_valid_label_rate"])
            self.assertEqual(2, effective["strategy_lab"]["consecutive_passes_to_promote"])
            self.assertEqual("paid_research", effective["paper_expansion"]["runtime_phase"])

    def test_missing_metrics_soft_pause_and_three_healthy_probes_resume(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "measurement", elapsed_hours=1)
            _effective, token = campaign.apply_campaign_controls(conn, self.settings)
            failed = campaign.record_campaign_cycle(conn, token, {})
            self.assertEqual("soft_paused", failed["status"])
            self.assertTrue(any(reason.startswith("missing_metric:") for reason in failed["soft_reasons"]))
            paused, _token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual(0, paused["scanner"]["scan_universe"])
            self.assertEqual(0, paused["scanner"]["review_top"])
            self.assertEqual(0, paused["scanner"]["max_new_paper_trades"])
            self.assertEqual(0, paused["scanner"]["max_new_paper_observations"])
            self.assertEqual(0, paused["scanner"]["frontier_crypto_review_top"])
            # A soft pause retains read-only coverage probes so three healthy
            # cycles can prove recovery; all review/opening limits remain zero.
            self.assertTrue(paused["scanner"]["enable_crypto_venue_health_scan"])
            self.assertTrue(paused["scanner"]["enable_frontier_crypto_adapter_scan"])
            self.assertTrue(paused["market_admission"]["enabled"])
            self.assertTrue(paused["market_admission"]["paper_queue_enabled"])
            self.assertTrue(paused["paper_expansion"]["reconciliation_only"])
            self.assertEqual(
                0,
                paused["market_admission"]["paper_queue"]["max_select_per_cycle"],
            )
            self.assertEqual(
                0,
                paused["market_admission"]["paper_queue"]["max_enqueue_per_cycle"],
            )
            self.assertFalse(paused["paper_expansion"]["measurement_probe_enabled"])
            # Finish the token returned by the controls check, then two more
            # clean probes.  The third clean probe resumes automatically.
            report = campaign.record_campaign_cycle(conn, _token, self.measurement_metrics(exact_keys=0))
            for _ in range(2):
                _effective, next_token = campaign.apply_campaign_controls(conn, self.settings)
                report = campaign.record_campaign_cycle(
                    conn,
                    next_token,
                    self.measurement_metrics(exact_keys=0),
                )
            self.assertEqual("running", report["status"])
            resumed, _token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual(10, resumed["scanner"]["max_new_paper_trades"])

    def test_overlapping_direct_cycle_latches_hard_halt(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "measurement", elapsed_hours=1)
            _effective, _abandoned_token = campaign.apply_campaign_controls(conn, self.settings)
            with self.assertRaisesRegex(campaign.CampaignError, "overlapping_bounded_workers"):
                campaign.apply_campaign_controls(conn, self.settings)
            row = conn.execute(
                "select run_status,state_json from paper_expansion_campaign_state"
            ).fetchone()
            state = json.loads(row["state_json"])
            self.assertEqual("hard_halted", row["run_status"])
            self.assertEqual("overlapping_bounded_workers", state["hard_halt_reason"])
            self.assertEqual(
                _abandoned_token["cycle_id"],
                state["inflight_cycle"]["cycle_id"],
            )
            with self.assertRaisesRegex(
                campaign.CampaignError, "active_runtime_lease_blocks_reset"
            ):
                campaign.reset_hard_halt(
                    conn,
                    self.settings,
                    campaign_id=self.settings["paper_expansion"]["campaign_id"],
                    operator_reason="must not clear a fresh owner",
                )

            state["inflight_cycle"]["cycle_started_at"] = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=13)
            ).isoformat()
            conn.execute(
                "update paper_expansion_campaign_state set state_json=?",
                (json.dumps(state),),
            )
            conn.commit()
            with self.assertRaisesRegex(
                campaign.CampaignError, "stale_runtime_lease_requires_explicit_clear"
            ):
                campaign.reset_hard_halt(
                    conn,
                    self.settings,
                    campaign_id=self.settings["paper_expansion"]["campaign_id"],
                    operator_reason="inspect stale owner first",
                )
            reset = campaign.reset_hard_halt(
                conn,
                self.settings,
                campaign_id=self.settings["paper_expansion"]["campaign_id"],
                operator_reason="confirmed stale owner is stopped",
                clear_stale_runtime_leases=True,
            )
            self.assertEqual("reset_to_soft_pause", reset["status"])
            self.assertNotIn("inflight_cycle", reset["state"])
            self.assertEqual(1, len(reset["state"]["cleared_runtime_leases"]))

    def test_simultaneous_direct_claims_have_one_winner_and_hard_halt(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "measurement", elapsed_hours=1)
        barrier = threading.Barrier(2)
        original = campaign._bounded_strategy_root_allowlist
        successes: list[str] = []
        failures: list[str] = []

        def synchronized_allowlist(*args, **kwargs):
            result = original(*args, **kwargs)
            barrier.wait(timeout=5)
            return result

        def invoke() -> None:
            try:
                with self.connection() as conn:
                    _effective, token = campaign.apply_campaign_controls(conn, self.settings)
                    successes.append(token["cycle_id"])
            except campaign.CampaignError as exc:
                failures.append(str(exc))

        with mock.patch.object(
            campaign,
            "_bounded_strategy_root_allowlist",
            side_effect=synchronized_allowlist,
        ):
            threads = [threading.Thread(target=invoke) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(1, len(successes))
        self.assertEqual(["overlapping_bounded_workers"], failures)
        with self.connection() as conn:
            row = conn.execute(
                "select run_status,state_json from paper_expansion_campaign_state"
            ).fetchone()
            state = json.loads(row["state_json"])
            self.assertEqual("hard_halted", row["run_status"])
            self.assertEqual(successes[0], state["inflight_cycle"]["cycle_id"])

    def test_paid_research_lease_blocks_and_hard_halts_radar_claim(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            row = conn.execute(
                "select state_json from paper_expansion_campaign_state"
            ).fetchone()
            state = json.loads(row["state_json"])
            state["paid_research_inflight"] = {
                "lease_id": "paid-active",
                "lease_expires_at": (
                    dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
                ).isoformat(),
            }
            conn.execute(
                "update paper_expansion_campaign_state set state_json=?",
                (json.dumps(state),),
            )
            conn.commit()
            with self.assertRaisesRegex(
                campaign.CampaignError, "paid_research_overlap_or_stale_lease"
            ):
                campaign.apply_campaign_controls(conn, self.settings)
            persisted = conn.execute(
                "select run_status,state_json from paper_expansion_campaign_state"
            ).fetchone()
            self.assertEqual("hard_halted", persisted["run_status"])
            self.assertIn("paid_research_inflight", json.loads(persisted["state_json"]))

    def test_supervisor_timeout_hard_halts_and_disables_next_cycle(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            self.set_phase(conn, "measurement", elapsed_hours=1)
            _effective, _token = campaign.apply_campaign_controls(conn, self.settings)
            report = campaign.record_inflight_failure(
                conn,
                self.settings,
                metrics=self.healthy(
                    cycle_success=False,
                    exit_code=124,
                    runtime_seconds=720,
                    timed_out=True,
                ),
            )
            self.assertEqual("hard_halted", report["status"])
            halted, _next_token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual(0, halted["scanner"]["scan_universe"])
            self.assertEqual(0, halted["scanner"]["review_top"])
            self.assertEqual(0, halted["scanner"]["max_new_paper_trades"])
            self.assertEqual(0, halted["scanner"]["max_new_paper_observations"])
            self.assertEqual(0, halted["scanner"]["frontier_crypto_review_top"])
            self.assertFalse(halted["scanner"]["enable_crypto_venue_health_scan"])
            self.assertFalse(halted["scanner"]["enable_frontier_crypto_adapter_scan"])
            self.assertTrue(halted["market_admission"]["enabled"])
            self.assertTrue(halted["market_admission"]["paper_queue_enabled"])
            self.assertTrue(halted["paper_expansion"]["reconciliation_only"])
            self.assertEqual(
                0,
                halted["market_admission"]["paper_queue"]["max_select_per_cycle"],
            )

    def test_pause_and_halt_keep_existing_queue_reconciliation_active(self) -> None:
        candidate = {
            "venue": "OKX",
            "inst_id": "BTC-USDT-SWAP",
            "asset_class": "crypto",
            "market_type": "perp",
            "trade_type": "perp_funding_basis",
            "direction": "funding_capture_short_perp",
            "quality_status": "verified",
            "freshness_state": "fresh",
            "data_status": "reachable",
            "route_status": "standard",
            "execution_feasibility": {"status": "standard"},
            "last": 100.0,
            "score": 70.0,
            "signal_lineage_key": "pause-reconciliation-lineage",
        }
        second_candidate = {
            **candidate,
            "inst_id": "ETH-USDT-SWAP",
            "signal_lineage_key": "halt-reconciliation-lineage",
        }
        review = {"decision": "approve_paper_trade", "learned_score": 70.0, "hard_blocks": []}
        with self.connection() as conn:
            self.initialize(conn)
            running, token = campaign.apply_campaign_controls(conn, self.settings)
            enqueued = queue.enqueue_paper_admission_candidates(
                conn, running, [candidate, second_candidate]
            )
            self.assertEqual(2, enqueued["enqueued"])
            claim_settings = copy.deepcopy(running)
            claim_settings["market_admission"]["paper_queue"][
                "max_select_per_cycle"
            ] = 2
            rows = {
                item["inst_id"]: item
                for item in queue.select_paper_admission_candidates(
                    conn,
                    claim_settings,
                    limit=2,
                )
            }
            queued_candidate = rows["BTC-USDT-SWAP"]
            save_opportunity(conn, queued_candidate, review)
            campaign.record_campaign_cycle(conn, token, {})

            paused, _paused_token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual(0, paused["market_admission"]["paper_queue"]["max_select_per_cycle"])
            reconciled = queue.reconcile_paper_admission_queue(conn, paused)
            self.assertGreaterEqual(reconciled["transitions"], 1)
            statuses = {
                row["inst_id"]: row["status"]
                for row in conn.execute("select inst_id,status from paper_admission_queue")
            }
            self.assertEqual("approved_waiting_capacity", statuses["BTC-USDT-SWAP"])
            self.assertEqual("queued_review", statuses["ETH-USDT-SWAP"])

            save_opportunity(conn, rows["ETH-USDT-SWAP"], review)
            campaign.record_inflight_failure(
                conn,
                self.settings,
                metrics=self.healthy(
                    cycle_success=False,
                    exit_code=124,
                    runtime_seconds=720,
                    timed_out=True,
                ),
            )
            halted, _halted_token = campaign.apply_campaign_controls(conn, self.settings)
            self.assertEqual("hard_halted", halted["paper_expansion_runtime"]["run_status"])
            self.assertEqual(0, halted["market_admission"]["paper_queue"]["max_select_per_cycle"])
            queue.reconcile_paper_admission_queue(conn, halted)
            statuses = {
                row["inst_id"]: row["status"]
                for row in conn.execute("select inst_id,status from paper_admission_queue")
            }
            self.assertEqual("approved_waiting_capacity", statuses["ETH-USDT-SWAP"])

    def test_burn_in_monitors_and_enqueues_but_selects_no_queue_work(self) -> None:
        with self.connection() as conn:
            effective, _token = campaign.apply_campaign_controls(conn, self.settings)
            admission = effective["market_admission"]
            self.assertTrue(admission["enabled"])
            self.assertTrue(admission["monitor_enabled"])
            self.assertTrue(admission["paper_queue_enabled"])
            self.assertEqual(0, admission["paper_queue"]["max_select_per_cycle"])

    def test_direct_invocation_without_bounded_process_proof_gets_no_new_work(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RADAR_PROCESS_ROLE": "manual_radar",
                "RADAR_BOUNDED_SUPERVISOR_COUNT": "1",
                "RADAR_BOUNDED_CHILD_COUNT": "1",
            },
        ):
            with self.connection() as conn:
                effective, token = campaign.apply_campaign_controls(conn, self.settings)
                self.assertEqual("hard_halted", effective["paper_expansion_runtime"]["run_status"])
                self.assertFalse(token["effective_controls"]["bounded_process_guard"]["authorized"])
                self.assertEqual(0, effective["scanner"]["scan_universe"])
                self.assertEqual(0, effective["scanner"]["review_top"])
                self.assertEqual(0, effective["scanner"]["max_new_paper_trades"])
                self.assertEqual(0, effective["scanner"]["max_new_paper_observations"])
                self.assertFalse(effective["scanner"]["enable_frontier_crypto_adapter_scan"])

    def test_operator_reset_adopts_changed_config_hash_and_allows_probe(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            changed = copy.deepcopy(self.settings)
            # Drift outside the paper_expansion subsection is equally safety
            # relevant and must require explicit operator adoption.
            changed["scanner"]["frontier_crypto_review_top"] = 39
            halted, halted_token = campaign.apply_campaign_controls(conn, changed)
            self.assertEqual("hard_halted", halted["paper_expansion_runtime"]["run_status"])
            campaign.record_campaign_cycle(conn, halted_token, self.healthy())

            reset = campaign.reset_hard_halt(
                conn,
                changed,
                campaign_id=changed["paper_expansion"]["campaign_id"],
                operator_reason="approved bounded threshold update",
            )
            self.assertEqual("reset_to_soft_pause", reset["status"])
            adoption = reset["state"]["last_config_hash_adoption"]
            self.assertNotEqual(adoption["previous_config_hash"], adoption["adopted_config_hash"])
            self.assertEqual(reset["state"]["config_hash"], adoption["adopted_config_hash"])

            _probe_controls, probe_token = campaign.apply_campaign_controls(conn, changed)
            probe = campaign.record_campaign_cycle(conn, probe_token, self.healthy())
            self.assertEqual("soft_paused", probe["status"])
            self.assertEqual("healthy_resume_probe", probe["health_status"])
            probe_controls, _probe_token = campaign.apply_campaign_controls(conn, changed)
            self.assertEqual("soft_paused", probe_controls["paper_expansion_runtime"]["run_status"])

    def test_non_phase_safety_setting_drift_hard_halts(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            changed = copy.deepcopy(self.settings)
            changed.setdefault("paper_admission_queue", {})[
                "max_freshness_age_seconds"
            ] = 91.0

            effective, token = campaign.apply_campaign_controls(conn, changed)

            self.assertEqual(
                "hard_halted",
                effective["paper_expansion_runtime"]["run_status"],
            )
            self.assertEqual("config_hash_changed", token["stop_reason"])
            self.assertEqual(0, effective["scanner"]["max_new_paper_trades"])

    def test_persisted_state_contains_audit_fields(self) -> None:
        with self.connection() as conn:
            _effective, token = campaign.apply_campaign_controls(conn, self.settings)
            report = campaign.record_campaign_cycle(conn, token, self.healthy())
            state = report["state"]
            self.assertEqual(64, len(state["config_hash"]))
            self.assertIn("gate_evidence", state)
            self.assertEqual("burn_in", state["last_good_phase"])
            self.assertIn("stop_reason", state)
            watermark = state["last_completed_safety_watermark"]
            self.assertEqual(campaign.SAFETY_WATERMARK_VERSION, watermark["version"])
            self.assertEqual(token["cycle_id"], watermark["cycle_id"])
            self.assertEqual(
                report["metrics"]["safety_snapshot_at_completion"],
                watermark["safety_snapshot"],
            )
            self.assertTrue(report["metrics"]["db_finalization_accounted"])
            self.assertEqual(
                campaign._sqlite_logical_footprint_bytes(conn),
                report["metrics"]["db_footprint_bytes"],
            )

    def test_burn_in_gate_uses_rolling_p95_and_daily_database_growth(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        cfg = self.settings["paper_expansion"]
        state = {
            "phase": "burn_in",
            "phase_started_at": (now - dt.timedelta(hours=25)).isoformat(),
            "phase_healthy_cycles": 90,
            "accumulated": {},
            "operational_history": {
                "runtime_seconds": [100.0] * 19 + [500.0],
                "peak_rss_mb": [500.0] * 19 + [1000.0],
                "db_growth": [{"at": now.isoformat(), "bytes": 262144000}],
            },
        }
        passed, reasons, evidence = campaign._phase_gate_passed(
            state,
            cfg,
            {"supervisor_count": 1, "child_count": 1, "new_deferred_cost_lines": 0},
            now,
        )
        self.assertTrue(passed, reasons)
        self.assertLessEqual(evidence["actual"]["runtime_p95_seconds"], 480)
        state["operational_history"]["runtime_seconds"][-2] = 500.0
        state["operational_history"]["db_growth"][0]["bytes"] += 1
        passed, reasons, _evidence = campaign._phase_gate_passed(
            state,
            cfg,
            {"supervisor_count": 1, "child_count": 1, "new_deferred_cost_lines": 0},
            now,
        )
        self.assertFalse(passed)
        self.assertIn("burn_in_runtime_p95", reasons)
        self.assertIn("burn_in_database_growth_24h", reasons)

    def test_database_footprint_uses_net_and_peak_not_checkpoint_delta_sum(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        state = {
            "operational_history": {
                "db_footprint": [
                    {"at": (now - dt.timedelta(hours=24)).isoformat(), "bytes": 100},
                    {"at": (now - dt.timedelta(hours=12)).isoformat(), "bytes": 200},
                    {"at": now.isoformat(), "bytes": 200},
                ],
                # These deltas mimic WAL bytes moving into the main file. They
                # must be ignored when absolute footprint samples are present.
                "db_growth": [
                    {"at": (now - dt.timedelta(hours=12)).isoformat(), "bytes": 100},
                    {"at": now.isoformat(), "bytes": 100},
                ],
            }
        }
        metrics: dict = {}
        campaign._attach_operational_rollups(state, metrics, now)
        self.assertEqual(100, metrics["rolling_db_net_growth_bytes_24h"])
        self.assertEqual(100, metrics["rolling_db_peak_growth_bytes_24h"])
        self.assertEqual(100, metrics["rolling_db_growth_bytes_24h"])

    def test_first_cycle_database_growth_keeps_the_prework_baseline(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        state: dict = {}
        metrics = {
            "db_growth_bytes": 100,
            "db_footprint_start_bytes": 100,
            "db_footprint_start_at": (now - dt.timedelta(minutes=1)).isoformat(),
            "db_footprint_bytes": 200,
        }

        campaign._append_operational_history(state, metrics, now)
        campaign._attach_operational_rollups(state, metrics, now)

        self.assertEqual(100, metrics["rolling_db_net_growth_bytes_24h"])
        self.assertEqual(100, metrics["rolling_db_peak_growth_bytes_24h"])

    def test_final_database_writes_reverse_unsafe_transition_and_retry_is_idempotent(
        self,
    ) -> None:
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        with self.connection() as conn:
            self.initialize(conn)
            campaign_id = self.settings["paper_expansion"]["campaign_id"]
            row = conn.execute(
                "select state_json from paper_expansion_campaign_state where campaign_id=?",
                (campaign_id,),
            ).fetchone()
            current = json.loads(row["state_json"])
            current.update(
                {
                    "phase": "burn_in",
                    "run_status": "running",
                    "healthy_streak": 0,
                    "phase_healthy_cycles": 89,
                    "phase_started_at": (now - dt.timedelta(hours=25)).isoformat(),
                    "phase_clock_checkpoint_at": now.isoformat(),
                    "operational_history": {
                        "runtime_seconds": [100.0] * 89,
                        "peak_rss_mb": [500.0] * 89,
                        "db_footprint": [],
                        "db_growth": [],
                    },
                }
            )
            current.pop("inflight_cycle", None)
            conn.execute(
                """
                update paper_expansion_campaign_state
                set phase='burn_in',run_status='running',healthy_streak=0,
                    phase_started_at=?,state_json=?
                where campaign_id=?
                """,
                (current["phase_started_at"], json.dumps(current), campaign_id),
            )
            conn.commit()

            with mock.patch.object(campaign, "_utc_now", return_value=now):
                _effective, token = campaign.apply_campaign_controls(conn, self.settings)
            cfg = token["campaign_config"]
            growth_limit = min(
                int(cfg["health"]["max_db_growth_bytes_per_day"]),
                int(cfg["phases"]["burn_in"]["max_db_growth_bytes_per_day"]),
            )
            prefinal_metrics = self.healthy(
                db_footprint_start_bytes=100,
                db_footprint_bytes=100 + growth_limit,
                db_growth_bytes=growth_limit,
            )

            # The caller-visible footprint is exactly at the cap and would
            # otherwise promote this 90th healthy burn-in cycle.
            base_current = campaign._decode_state(
                conn.execute(
                    "select * from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone(),
                now,
            )
            prefinal_merged = campaign._merged_metrics(
                conn,
                token,
                prefinal_metrics,
                cfg,
                now,
            )
            prefinal_merged["db_finalization_accounted"] = True
            would_promote = campaign._evaluate_campaign_cycle(
                base_current,
                token,
                prefinal_merged,
                cfg,
                now,
                token["config_hash"],
            )
            self.assertTrue(would_promote["transitioned"])
            self.assertEqual("measurement", would_promote["current"]["phase"])

            postwrite_footprint = 101 + growth_limit
            with (
                mock.patch.object(campaign, "_utc_now", return_value=now),
                mock.patch.object(
                    campaign,
                    "_sqlite_logical_footprint_bytes",
                    side_effect=[postwrite_footprint, postwrite_footprint],
                ) as footprint,
            ):
                report = campaign.record_campaign_cycle(conn, token, prefinal_metrics)
                total_after_first = report["state"]["total_cycle_count"]
                duplicate = campaign.record_campaign_cycle(conn, token, prefinal_metrics)

            self.assertFalse(report["transitioned"])
            self.assertEqual("burn_in", report["phase_after"])
            self.assertEqual("soft_paused", report["status"])
            self.assertIn("database_growth", report["soft_reasons"])
            self.assertIn("burn_in_database_growth_24h", report["gate_reasons"])
            self.assertTrue(report["metrics"]["db_finalization_accounted"])
            self.assertEqual(postwrite_footprint, report["metrics"]["db_footprint_bytes"])
            self.assertEqual(growth_limit + 1, report["metrics"]["db_growth_bytes"])
            self.assertEqual(2, footprint.call_count)

            persisted_cycle = conn.execute(
                "select metrics_json from paper_expansion_campaign_cycles where cycle_id=?",
                (token["cycle_id"],),
            ).fetchone()
            persisted_metrics = json.loads(persisted_cycle["metrics_json"])
            self.assertTrue(persisted_metrics["db_finalization_accounted"])
            self.assertEqual(postwrite_footprint, persisted_metrics["db_footprint_bytes"])
            self.assertEqual("already_recorded", duplicate["status"])
            self.assertEqual(persisted_metrics, duplicate["metrics"])
            persisted_state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()["state_json"]
            )
            self.assertEqual(total_after_first, persisted_state["total_cycle_count"])

    def test_unstable_database_finalization_rolls_back_and_keeps_token_retryable(self) -> None:
        with self.connection() as conn:
            _effective, token = campaign.apply_campaign_controls(conn, self.settings)
            campaign_id = token["campaign_id"]
            with mock.patch.object(
                campaign,
                "_sqlite_logical_footprint_bytes",
                side_effect=[101, 102, 103, 104],
            ):
                with self.assertRaisesRegex(
                    campaign.CampaignError,
                    "campaign_db_footprint_finalization_unstable",
                ):
                    campaign.record_campaign_cycle(conn, token, self.healthy())

            rolled_back = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()["state_json"]
            )
            self.assertEqual(
                token["cycle_id"],
                rolled_back["inflight_cycle"]["cycle_id"],
            )
            self.assertEqual(
                0,
                conn.execute(
                    "select count(*) from paper_expansion_campaign_cycles where cycle_id=?",
                    (token["cycle_id"],),
                ).fetchone()[0],
            )

            with mock.patch.object(
                campaign,
                "_sqlite_logical_footprint_bytes",
                side_effect=[105, 105],
            ):
                retry = campaign.record_campaign_cycle(conn, token, self.healthy())
            self.assertTrue(retry["metrics"]["db_finalization_accounted"])
            self.assertNotIn("inflight_cycle", retry["state"])
            self.assertEqual(1, retry["state"]["total_cycle_count"])

    def test_deferred_maintenance_adopts_append_only_source_and_audits_every_step(
        self,
    ) -> None:
        with self.connection() as conn:
            path, source_before, digest = self.prepare_deferred_maintenance(conn)
            total_changes_before = conn.total_changes

            result = self.adopt_deferred(conn, path, digest)

            self.assertEqual("deferred_cost_ledger_adopted", result["status"])
            self.assertFalse(result["resumed"])
            self.assertEqual(source_before, path.read_bytes())
            self.assertEqual(digest, result["source_sha256"])
            self.assertEqual(1, result["line_count"])
            self.assertAlmostEqual(0.01, result["cost_delta_usd"])
            state = result["state"]
            self.assertEqual("soft_paused", state["run_status"])
            self.assertEqual(0, state["healthy_streak"])
            self.assertIsNone(state["hard_halt_reason"])
            watermark = state["last_completed_safety_watermark"]
            self.assertEqual(
                result["maintenance_id"], watermark["maintenance_adoption_id"]
            )
            self.assertEqual(
                f"maintenance:{result['maintenance_id']}", watermark["cycle_id"]
            )
            self.assertTrue(
                watermark["safety_snapshot"][
                    "deferred_cost_reconciliation_complete"
                ]
            )
            self.assertEqual(
                digest,
                watermark["safety_snapshot"]["deferred_cost_source_digest"],
            )
            self.assertEqual(
                1, watermark["safety_snapshot"]["reconciled_deferred_cost_lines"]
            )
            events = conn.execute(
                """
                select event_type,details_json
                from paper_expansion_campaign_maintenance_events
                where maintenance_id=? order by id
                """,
                (result["maintenance_id"],),
            ).fetchall()
            self.assertEqual(
                ["started", "replayed", "adopted"],
                [row["event_type"] for row in events],
            )
            for event in events:
                details = json.loads(event["details_json"])
                self.assertEqual(digest, details.get("source_sha256", digest))
            self.assertEqual(
                1,
                conn.execute("select count(*) from llm_cost_events").fetchone()[0],
            )
            self.assertGreater(conn.total_changes, total_changes_before)

    def test_fully_reconciled_nonempty_ledger_passes_paid_capacity_read_only(
        self,
    ) -> None:
        with self.connection() as conn:
            path, source_before, digest = self.prepare_deferred_maintenance(conn)
            self.adopt_deferred(conn, path, digest)
            rows_before = conn.execute(
                "select count(*),coalesce(max(id),0) from llm_cost_events"
            ).fetchone()
            total_changes_before = conn.total_changes

            detail = campaign._paid_cost_capacity_revalidation(
                conn,
                dt.datetime.now(dt.timezone.utc),
                self.settings,
            )

            self.assertTrue(detail["healthy"], detail)
            self.assertEqual("cost_capacity_available", detail["reason"])
            self.assertEqual(1, detail["deferred_cost_lines"])
            self.assertTrue(detail["deferred_cost_reconciliation_complete"])
            self.assertEqual(0, detail["deferred_cost_pending"])
            self.assertEqual(1, detail["deferred_cost_reconciled"])
            self.assertEqual(digest, detail["deferred_cost_source_digest"])
            self.assertEqual(source_before, path.read_bytes())
            self.assertEqual(total_changes_before, conn.total_changes)
            self.assertEqual(
                tuple(rows_before),
                tuple(
                    conn.execute(
                        "select count(*),coalesce(max(id),0) from llm_cost_events"
                    ).fetchone()
                ),
            )

    def test_deferred_maintenance_rejects_unsafe_or_mismatched_preconditions(
        self,
    ) -> None:
        with self.connection() as conn:
            path, _source_before, digest = self.prepare_deferred_maintenance(conn)

            with self.assertRaisesRegex(
                campaign.CampaignError,
                "active_runtime_processes_block_maintenance",
            ):
                self.adopt_deferred(
                    conn,
                    path,
                    digest,
                    active_runtime_processes=[
                        {"pid": 4242, "role": "bounded_paper_radar"}
                    ],
                )
            with self.assertRaisesRegex(
                campaign.CampaignError,
                "deferred_maintenance_source_path_mismatch",
            ):
                self.adopt_deferred(
                    conn,
                    path,
                    digest,
                    expected_source_path=path.with_name("wrong-ledger.jsonl"),
                )
            with self.assertRaisesRegex(
                campaign.CampaignError,
                "deferred_maintenance_source_digest_mismatch",
            ):
                self.adopt_deferred(
                    conn,
                    path,
                    digest,
                    expected_source_sha256="0" * 64,
                )
            with self.assertRaisesRegex(
                campaign.CampaignError,
                "deferred_maintenance_source_line_count_mismatch",
            ):
                self.adopt_deferred(
                    conn,
                    path,
                    digest,
                    expected_line_count=2,
                )

            state = self.campaign_state(conn)
            state["phase"] = "measurement"
            self.persist_campaign_state(conn, state)
            with self.assertRaisesRegex(
                campaign.CampaignError,
                "deferred_maintenance_requires_burn_in",
            ):
                self.adopt_deferred(conn, path, digest)
            state["phase"] = "burn_in"
            state["run_status"] = "hard_halted"
            state["hard_halt_reason"] = "live_order_attempt"
            self.persist_campaign_state(conn, state)
            with self.assertRaisesRegex(
                campaign.CampaignError,
                "deferred_maintenance_requires_soft_paused_campaign",
            ):
                self.adopt_deferred(conn, path, digest)
            state["run_status"] = "soft_paused"
            state["hard_halt_reason"] = None
            state["inflight_cycle"] = {"cycle_id": "still-running"}
            self.persist_campaign_state(conn, state)
            with self.assertRaisesRegex(
                campaign.CampaignError,
                "inflight_cycle_blocks_deferred_maintenance",
            ):
                self.adopt_deferred(conn, path, digest)
            state.pop("inflight_cycle")
            state["paid_research_inflight"] = {"lease_id": "active-paid-lease"}
            self.persist_campaign_state(conn, state)
            with self.assertRaisesRegex(
                campaign.CampaignError,
                "paid_research_lease_blocks_deferred_maintenance",
            ):
                self.adopt_deferred(conn, path, digest)
            state.pop("paid_research_inflight")
            self.persist_campaign_state(conn, state)

            conn.execute(
                """
                insert into agent_runs(
                    run_id,agent_id,cycle_id,started_at,duration_ms,status,
                    trigger_match_json,memory_ids_json,model_json,
                    recommendation_json,outcome_json
                ) values(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "unrelated-maintenance-delta",
                    "unexpected-agent",
                    "outside-maintenance",
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    1,
                    "completed",
                    "{}",
                    "[]",
                    "{}",
                    "{}",
                    "{}",
                ),
            )
            conn.commit()
            with self.assertRaisesRegex(
                campaign.CampaignError,
                "deferred_maintenance_unrelated_delta:agent_runs",
            ):
                self.adopt_deferred(conn, path, digest)
            self.assertEqual(
                0,
                conn.execute(
                    "select count(*) from paper_expansion_campaign_maintenance_events"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                conn.execute("select count(*) from llm_cost_events").fetchone()[0],
            )

    def test_deferred_maintenance_recovers_after_replay_before_adoption_crash(
        self,
    ) -> None:
        with self.connection() as conn:
            path, source_before, digest = self.prepare_deferred_maintenance(conn)
            real_replay = cost_router.replay_deferred_cost_events
            calls = 0

            def replay_then_crash(*args, **kwargs):
                nonlocal calls
                calls += 1
                report = real_replay(*args, **kwargs)
                if calls == 1:
                    raise RuntimeError("simulated crash after durable replay")
                return report

            with mock.patch.object(
                cost_router,
                "replay_deferred_cost_events",
                side_effect=replay_then_crash,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash after durable replay",
                ):
                    self.adopt_deferred(conn, path, digest)

            self.assertEqual(source_before, path.read_bytes())
            reconciliation = cost_router.deferred_cost_reconciliation_status(
                conn,
                path,
            )
            self.assertTrue(reconciliation["complete"], reconciliation)
            pending_events = conn.execute(
                """
                select maintenance_id,event_type
                from paper_expansion_campaign_maintenance_events order by id
                """
            ).fetchall()
            self.assertEqual(["started"], [row["event_type"] for row in pending_events])

            resumed = self.adopt_deferred(conn, path, digest)

            self.assertTrue(resumed["resumed"])
            self.assertEqual(
                pending_events[0]["maintenance_id"], resumed["maintenance_id"]
            )
            self.assertEqual(source_before, path.read_bytes())
            self.assertEqual("soft_paused", resumed["state"]["run_status"])
            self.assertEqual(0, resumed["state"]["healthy_streak"])
            self.assertEqual(
                ["started", "replayed", "adopted"],
                [
                    row["event_type"]
                    for row in conn.execute(
                        """
                        select event_type
                        from paper_expansion_campaign_maintenance_events
                        where maintenance_id=? order by id
                        """,
                        (resumed["maintenance_id"],),
                    ).fetchall()
                ],
            )
            self.assertEqual(
                1,
                conn.execute("select count(*) from llm_cost_events").fetchone()[0],
            )

    def test_appended_pending_deferred_line_hard_halts_before_admission(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            path, first_source, _digest = self.write_deferred_payloads(
                self.deferred_payload(status="fallback_error:provider_failure")
            )
            queue_before = conn.execute(
                "select count(*) from paper_admission_queue"
            ).fetchone()[0]
            cost_rows_before = conn.execute(
                "select count(*) from llm_cost_events"
            ).fetchone()[0]

            effective, token = campaign.apply_campaign_controls(conn, self.settings)

            self.assertEqual("hard_halted", token["run_status"])
            self.assertEqual(0, effective["scanner"]["scan_universe"])
            self.assertEqual(0, effective["scanner"]["review_top"])
            self.assertEqual(0, effective["scanner"]["max_new_paper_trades"])
            self.assertEqual(
                0,
                effective["market_admission"]["paper_queue"][
                    "max_select_per_cycle"
                ],
            )
            self.assertTrue(effective["paper_expansion"]["reconciliation_only"])
            self.assertTrue(token["intercycle_safety_check"]["hard_halt"])
            self.assertIn(
                "intercycle_deferred_cost_source_changed",
                token["intercycle_safety_check"]["reasons"],
            )
            self.assertIn(
                "intercycle_deferred_cost_reconciliation_incomplete",
                token["intercycle_safety_check"]["reasons"],
            )
            self.assertEqual(first_source, path.read_bytes())
            self.assertEqual(
                queue_before,
                conn.execute("select count(*) from paper_admission_queue").fetchone()[0],
            )
            self.assertEqual(
                cost_rows_before,
                conn.execute("select count(*) from llm_cost_events").fetchone()[0],
            )

    def test_campaign_id_change_cannot_trigger_implicit_deferred_replay(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            path, source_before, _digest = self.write_deferred_payloads(
                self.deferred_payload()
            )
            renamed = copy.deepcopy(self.settings)
            renamed["paper_expansion"]["campaign_id"] = "renamed-campaign"

            effective, token = campaign.apply_campaign_controls(conn, renamed)

            self.assertEqual("hard_halted", token["run_status"])
            self.assertEqual(0, effective["scanner"]["scan_universe"])
            self.assertEqual(source_before, path.read_bytes())
            self.assertEqual(
                0,
                conn.execute("select count(*) from llm_cost_events").fetchone()[0],
            )
            self.assertEqual(
                2,
                conn.execute(
                    "select count(*) from paper_expansion_campaign_state"
                ).fetchone()[0],
            )

    def test_deferred_maintenance_deduplicates_identical_explicit_event_ids(self) -> None:
        with self.connection() as conn:
            self.initialize(conn)
            payload = self.deferred_payload(
                event_id="same-explicit-event",
                created_at="2026-08-07T00:00:00+00:00",
            )
            path, source_before, digest = self.write_deferred_payloads(payload, payload)
            state = self.campaign_state(conn)
            state["run_status"] = "soft_paused"
            state["healthy_streak"] = 0
            state.pop("inflight_cycle", None)
            self.persist_campaign_state(conn, state)

            result = self.adopt_deferred(
                conn,
                path,
                digest,
                expected_line_count=2,
            )

            self.assertEqual("deferred_cost_ledger_adopted", result["status"])
            self.assertEqual(source_before, path.read_bytes())
            self.assertEqual(2, result["line_count"])
            self.assertAlmostEqual(0.01, result["cost_delta_usd"])
            self.assertEqual(
                1,
                conn.execute("select count(*) from llm_cost_events").fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
