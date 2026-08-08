from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cost_router  # noqa: E402
import paid_research_once as research  # noqa: E402
import storage  # noqa: E402
from autonomous_cost_guard import current_autonomous_scope  # noqa: E402
from settings import SettingsError, load_settings  # noqa: E402


class PaidResearchOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.db_path = self.root / "research.sqlite"
        self.settings = load_settings(ROOT / "config" / "settings.bounded_crypto_paper.json")
        with storage.connect(self.db_path):
            pass
        self.connect_patch = mock.patch.object(
            research,
            "connect",
            side_effect=lambda initialize=True: storage.connect(
                self.db_path,
                initialize=initialize,
            ),
        )
        self.runs_patch = mock.patch.object(research, "RUNS_DIR", self.root / "runs")
        self.reconciliation_patch = mock.patch.object(
            research,
            "deferred_cost_reconciliation_status",
            return_value={
                "status": "deferred_cost_reconciled",
                "source_digest": "unit-test-reconciled-ledger",
                "read": 3,
                "invalid": 0,
                "pending": 0,
                "reserved": 0,
                "conflicting": 0,
                "reconciled": 3,
                "complete": True,
            },
        )
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "RADAR_PROCESS_ROLE": "research_one_shot",
                "RADAR_RESEARCH_MODEL_OVERRIDE": "1",
                "RADAR_USE_LITELLM": "1",
                "OPENAI_API_KEY": "unit-test-placeholder",
            },
        )
        self.connect_patch.start()
        self.runs_patch.start()
        self.reconciliation_mock = self.reconciliation_patch.start()
        self.env_patch.start()
        self.seed_campaign(research._campaign_hash(self.settings))

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.reconciliation_patch.stop()
        self.runs_patch.stop()
        self.connect_patch.stop()
        self.temp.cleanup()

    def seed_campaign(self, config_hash: str) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        state = {
            "campaign_id": self.settings["paper_expansion"]["campaign_id"],
            "phase": "research",
            "run_status": "running",
            "config_hash": config_hash,
        }
        with storage.connect(self.db_path, initialize=False) as conn:
            conn.execute("delete from paper_expansion_campaign_cycles")
            conn.execute(
                "delete from paper_expansion_campaign_state"
            )
            conn.execute(
                """
                insert into paper_expansion_campaign_state(
                    campaign_id,phase,run_status,healthy_streak,phase_cycle_count,
                    total_cycle_count,phase_started_at,updated_at,state_json
                ) values(?,?,?,?,?,?,?,?,?)
                """,
                (
                    state["campaign_id"],
                    "research",
                    "running",
                    0,
                    0,
                    0,
                    now,
                    now,
                    json.dumps(state, sort_keys=True),
                ),
            )
            conn.execute(
                """
                insert into paper_expansion_campaign_cycles(
                    cycle_id,campaign_id,phase,started_at,completed_at,
                    health_status,metrics_json,reasons_json
                ) values(?,?,?,?,?,'healthy','{}','[]')
                """,
                (
                    "fresh-healthy-cycle",
                    state["campaign_id"],
                    "research",
                    now,
                    now,
                ),
            )
            conn.commit()

    def test_paid_lease_requires_a_recent_healthy_radar_cycle(self) -> None:
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as conn:
            conn.execute(
                "update paper_expansion_campaign_cycles set health_status='soft_paused'"
            )
            conn.commit()
            with self.assertRaisesRegex(SettingsError, "latest bounded radar cycle is not healthy"):
                research._claim_paid_research_lease(
                    conn,
                    self.settings,
                    campaign_id,
                )
            self.assertIsNone(
                json.loads(
                    conn.execute(
                        "select state_json from paper_expansion_campaign_state where campaign_id=?",
                        (campaign_id,),
                    ).fetchone()["state_json"]
                ).get("paid_research_inflight")
            )

            stale = (
                dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(
                    seconds=research.MAX_HEALTHY_RADAR_CYCLE_AGE_SECONDS + 1
                )
            ).isoformat()
            conn.execute(
                """
                update paper_expansion_campaign_cycles
                set health_status='healthy',completed_at=?
                """,
                (stale,),
            )
            conn.commit()
            with self.assertRaisesRegex(SettingsError, "latest healthy bounded radar cycle is stale"):
                research._claim_paid_research_lease(
                    conn,
                    self.settings,
                    campaign_id,
                )

    def contract(self, index: int) -> dict:
        return {
            "strategy_lab_id": f"paid_crypto_root_{index}",
            "version": 1,
            "experiment_type": "market_strategy",
            "hypothesis": f"Direct crypto basis hypothesis {index}",
            "source_surface": "perp_funding_basis",
            "permitted_target_surface": ["perp_funding_basis"],
            "strategy_logic": {
                "type": "candidate_filter",
                "venues": ["OKX"],
                "trade_types": ["perp_funding_basis"],
                "directions": ["short_perp_long_spot"],
                "asset_classes": ["crypto"],
                "required_fields": ["edge_bps_estimate", "funding_bps", "basis_bps"],
                "min_edge_bps": 2.0,
            },
            "data_requirements": {
                "paper_only": True,
                "route_status": "standard",
            },
            "risk_gates": {"min_edge_bps": 2.0},
            "promotion_rules": {},
        }

    def model_result(self, payload: object) -> cost_router.ModelResult:
        return cost_router.ModelResult(
            text=json.dumps(payload, sort_keys=True),
            model_name="openai/test",
            model_tier="fast",
            prompt_tokens=100,
            completion_tokens=100,
            estimated_cost_usd=0.01,
            status="model_call:responses",
            event_id="event-paid-research",
        )

    @staticmethod
    def evidence_context() -> dict:
        return {
            "signal_stats": [
                {
                    "signal_key": "OKX|perp_funding_basis|short_perp_long_spot",
                    "venue": "OKX",
                    "trade_type": "perp_funding_basis",
                    "direction": "short_perp_long_spot",
                    "closed_count": 30,
                    "wins": 18,
                    "avg_pnl_bps": 10.0,
                    "win_rate": 0.6,
                    "evidence_scope": (
                        "reliable_timely_direct_standard_route_only"
                    ),
                }
            ],
            "evidence_contracts": [
                {
                    "venue": "OKX",
                    "trade_type": "perp_funding_basis",
                    "direction": "short_perp_long_spot",
                    "reliable_label_count": 30,
                }
            ],
            "constraints": {
                "asset_scope": "crypto_only",
                "mode": "paper_only",
                "allowed_direct_crypto_venues": ["OKX"],
                "evidence_scope": (
                    "reliable_timely_direct_standard_route_only"
                ),
            },
        }

    def run_with_payload(self, payload: object) -> dict:
        with mock.patch.object(
            research,
            "completion_preflight_status",
            return_value={"ok": True, "status": "budget_ok"},
        ), mock.patch.object(
            research,
            "cost_budget_status",
            return_value={"allowed": True, "status": "cost_budget_available"},
        ), mock.patch.object(
            research,
            "complete",
            return_value=self.model_result(payload),
        ), mock.patch.object(
            research,
            "_research_context",
            return_value=self.evidence_context(),
        ):
            return research.run_once(ROOT / "config" / "settings.bounded_crypto_paper.json")

    def test_incomplete_deferred_cost_reconciliation_blocks_before_any_paid_state(self) -> None:
        self.reconciliation_mock.return_value = {
            "status": "deferred_cost_reconciliation_incomplete",
            "source_digest": "unit-test-pending-ledger",
            "read": 4,
            "invalid": 1,
            "pending": 1,
            "reserved": 1,
            "conflicting": 1,
            "reconciled": 0,
            "complete": False,
        }
        with mock.patch.object(
            research,
            "_claim_paid_research_lease",
        ) as lease_mock, mock.patch.object(
            research,
            "autonomous_paid_scope",
        ) as scope_mock, mock.patch.object(
            research,
            "completion_preflight_status",
        ) as preflight_mock, mock.patch.object(
            research,
            "cost_budget_status",
        ) as budget_mock, mock.patch.object(
            research,
            "complete",
        ) as complete_mock:
            with self.assertRaisesRegex(
                SettingsError,
                "deferred_cost_reconciliation_blocked",
            ):
                research.run_once(
                    ROOT / "config" / "settings.bounded_crypto_paper.json"
                )
        lease_mock.assert_not_called()
        scope_mock.assert_not_called()
        preflight_mock.assert_not_called()
        budget_mock.assert_not_called()
        complete_mock.assert_not_called()
        self.assertEqual(
            [],
            list((self.root / "runs" / "paid_research").glob("*.claim.json")),
        )
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as conn:
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0]
            )
        self.assertNotIn("paid_research_inflight", state)
        self.assertNotIn("last_paid_research_lease", state)

    def test_fully_reconciled_deferred_cost_ledger_allows_paid_path(self) -> None:
        report = self.run_with_payload({"strategy_contracts": []})
        self.assertEqual("model_call:responses", report["status"])
        self.reconciliation_mock.assert_called_once()
        reconciliation_args = self.reconciliation_mock.call_args.args
        self.assertEqual(2, len(reconciliation_args))
        self.assertEqual(
            research.COST_LOG_DEFERRED_PATH.resolve(),
            pathlib.Path(reconciliation_args[1]),
        )

    def seed_active_roots(
        self,
        count: int,
        *,
        source_agent: str = "paid_research_one_shot",
        prefix: str = "existing_root",
        strategy_ids: list[str] | None = None,
    ) -> None:
        now = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
        with storage.connect(self.db_path, initialize=False) as conn:
            for index in range(count):
                conn.execute(
                    """
                    insert into strategy_lab_experiments(
                        strategy_lab_id,version,parent_strategy_lab_id,experiment_type,
                        status,hypothesis,strategy_logic_json,data_requirements_json,
                        risk_gates_json,promotion_rules_json,source_surface,
                        permitted_target_surfaces_json,source_agent,
                        source_recommendation_id,created_at,updated_at
                    ) values(?,1,null,'market_strategy','active_testing',?,?,?,?,?,?,?,?,null,?,?)
                    """,
                    (
                        strategy_ids[index] if strategy_ids is not None else f"{prefix}_{index}",
                        f"existing {index}",
                        json.dumps(
                            {
                                "type": "candidate_filter",
                                "venues": ["OKX"],
                                "trade_types": ["perp_funding_basis"],
                                "directions": ["short_perp_long_spot"],
                                "asset_classes": ["crypto"],
                            }
                        ),
                        json.dumps({"paper_only": True, "route_status": "standard"}),
                        "{}",
                        "{}",
                        "perp_funding_basis",
                        json.dumps(["perp_funding_basis"]),
                        source_agent,
                        now,
                        now,
                    ),
                )
            conn.commit()

    def test_structured_output_ingests_at_most_two_new_roots(self) -> None:
        report = self.run_with_payload(
            {"strategy_contracts": [self.contract(1), self.contract(2), self.contract(3)]}
        )
        self.assertEqual(2, report["ingestion"]["new_root_count"])
        self.assertTrue(
            any(row["reason"] == "daily_new_root_output_cap" for row in report["ingestion"]["rejected"])
        )
        with storage.connect(self.db_path, initialize=False) as conn:
            rows = conn.execute(
                "select strategy_lab_id,parent_strategy_lab_id,source_agent from strategy_lab_experiments"
            ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["parent_strategy_lab_id"] is None for row in rows))
        self.assertTrue(all(row["source_agent"] == "paid_research_one_shot" for row in rows))

    def test_active_root_cap_allows_only_one_when_five_exist(self) -> None:
        self.seed_active_roots(5)
        report = self.run_with_payload(
            {"strategy_contracts": [self.contract(1), self.contract(2)]}
        )
        self.assertEqual(1, report["ingestion"]["new_root_count"])
        self.assertEqual(6, report["ingestion"]["active_root_count_after"])
        self.assertTrue(
            any(row["reason"] == "active_root_cap" for row in report["ingestion"]["rejected"])
        )

    def test_free_form_or_proxy_contract_is_never_ingested(self) -> None:
        free_form = self.run_with_payload({"hypothesis": "raw advisory text"})
        self.assertEqual(0, free_form["ingestion"]["new_root_count"])
        self.assertEqual("strategy_contracts_list_required", free_form["ingestion"]["rejected"][0]["reason"])
        proxy = self.contract(9)
        proxy["source_surface"] = "global_market_discovery_proxy"
        proxy["permitted_target_surface"] = ["global_market_discovery_proxy"]
        proxy["strategy_logic"]["trade_types"] = ["global_market_discovery_proxy"]
        proxy["strategy_logic"]["directions"] = ["long_proxy"]
        validated, reason = research._validate_paid_contract(proxy)
        self.assertIsNone(validated)
        self.assertIn("direct_crypto", str(reason))

    def test_non_crypto_venue_is_never_ingested(self) -> None:
        contract = self.contract(11)
        contract["strategy_logic"]["venues"] = ["NYSE"]
        validated, reason = research._validate_paid_contract(contract)
        self.assertIsNone(validated)
        self.assertEqual("unsupported_direct_crypto_venue", reason)

    def test_watch_only_and_evidence_absent_contracts_are_rejected(self) -> None:
        self.assertNotIn("YELLOW_CARD", research._direct_crypto_venue_allowlist())
        self.assertNotIn("BITNOB", research._direct_crypto_venue_allowlist())
        absent = self.contract(12)
        absent["strategy_logic"]["venues"] = ["GATE"]
        validated, reason = research._validate_paid_contract(
            absent,
            evidence_contracts=research._research_evidence_contracts(
                self.evidence_context()
            ),
        )
        self.assertIsNone(validated)
        self.assertEqual("contract_not_supported_by_research_evidence", reason)

    def test_legacy_strategy_roots_do_not_consume_bounded_paid_root_cap(self) -> None:
        self.seed_active_roots(9, source_agent="legacy_agent", prefix="legacy_root")
        report = self.run_with_payload(
            {"strategy_contracts": [self.contract(1), self.contract(2)]}
        )
        self.assertEqual(2, report["ingestion"]["new_root_count"])
        self.assertEqual(2, report["ingestion"]["active_root_count_after"])

    def test_spoofed_paid_source_with_unbounded_contract_does_not_consume_cap(self) -> None:
        self.seed_active_roots(5, source_agent="paid_research_one_shot")
        with storage.connect(self.db_path, initialize=False) as conn:
            conn.execute(
                """
                update strategy_lab_experiments
                set strategy_logic_json=?,source_surface=?,permitted_target_surfaces_json=?
                where strategy_lab_id like 'existing_root_%'
                """,
                (
                    json.dumps(
                        {
                            "type": "candidate_filter",
                            "venues": ["NYSE"],
                            "trade_types": ["global_market_discovery_proxy"],
                            "directions": ["long_proxy"],
                            "asset_classes": ["equity"],
                        }
                    ),
                    "global_market_discovery_proxy",
                    json.dumps(["global_market_discovery_proxy"]),
                ),
            )
            conn.commit()
        report = self.run_with_payload(
            {"strategy_contracts": [self.contract(1), self.contract(2)]}
        )
        self.assertEqual(2, report["ingestion"]["new_root_count"])
        self.assertEqual(2, report["ingestion"]["active_root_count_after"])

    def test_recovery_canary_counts_toward_total_bounded_root_cap(self) -> None:
        self.seed_active_roots(5)
        self.seed_active_roots(
            1,
            source_agent="recovery_bootstrap",
            strategy_ids=[research.RECOVERY_CANARY_STRATEGY_LAB_ID],
        )
        report = self.run_with_payload({"strategy_contracts": [self.contract(1)]})
        self.assertEqual(0, report["ingestion"]["new_root_count"])
        self.assertEqual(6, report["ingestion"]["active_root_count_after"])
        self.assertEqual("active_root_cap", report["ingestion"]["rejected"][0]["reason"])

    def test_full_settings_hash_rejects_legacy_expansion_only_hash(self) -> None:
        self.seed_campaign(research._campaign_hash(self.settings["paper_expansion"]))
        with self.assertRaisesRegex(SettingsError, "config hash"):
            self.run_with_payload({"strategy_contracts": [self.contract(1)]})

    def test_persisted_inflight_cycle_blocks_paid_research(self) -> None:
        with storage.connect(self.db_path, initialize=False) as conn:
            row = conn.execute(
                "select campaign_id,state_json from paper_expansion_campaign_state"
            ).fetchone()
            state = json.loads(row["state_json"])
            state["inflight_cycle"] = {"cycle_id": "active-radar-cycle"}
            conn.execute(
                "update paper_expansion_campaign_state set state_json=? where campaign_id=?",
                (json.dumps(state, sort_keys=True), row["campaign_id"]),
            )
            conn.commit()
        with self.assertRaisesRegex(SettingsError, "between bounded radar cycles"):
            self.run_with_payload({"strategy_contracts": [self.contract(1)]})

    def test_database_lease_blocks_duplicate_direct_invocation_and_releases(self) -> None:
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as first:
            lease, _ = research._claim_paid_research_lease(
                first,
                self.settings,
                campaign_id,
            )
        with storage.connect(self.db_path, initialize=False) as second:
            with self.assertRaisesRegex(SettingsError, "already owns"):
                research._claim_paid_research_lease(
                    second,
                    self.settings,
                    campaign_id,
                )
        with storage.connect(self.db_path, initialize=False) as first:
            research._release_paid_research_lease(
                first,
                campaign_id,
                lease,
                outcome="unit_test",
            )
        with storage.connect(self.db_path, initialize=False) as conn:
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0]
            )
        self.assertNotIn("paid_research_inflight", state)
        self.assertEqual("unit_test", state["last_paid_research_lease"]["outcome"])
        self.assertIn("lease_expires_at", state["last_paid_research_lease"])

    def test_missing_reliable_evidence_persists_a_soft_gate_outcome(self) -> None:
        empty_context = {
            "signal_stats": [],
            "evidence_contracts": [],
            "constraints": {"allowed_direct_crypto_venues": []},
        }
        with mock.patch.object(
            research,
            "_research_context",
            return_value=empty_context,
        ), mock.patch.object(research, "complete") as complete_mock:
            with self.assertRaisesRegex(SettingsError, "reliable direct crypto evidence"):
                research.run_once(
                    ROOT / "config" / "settings.bounded_crypto_paper.json"
                )
        complete_mock.assert_not_called()
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as conn:
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0]
            )
        released = state["last_paid_research_lease"]
        self.assertEqual("evidence_denied", released["operation_outcome"])
        self.assertEqual(
            "reliable_direct_evidence_unavailable", released["failure_category"]
        )
        self.assertIsNone(released["provider_event_id"])

    def test_budget_denial_persists_a_known_zero_call_outcome(self) -> None:
        with mock.patch.object(
            research,
            "completion_preflight_status",
            return_value={"ok": True, "status": "budget_ok"},
        ), mock.patch.object(
            research,
            "cost_budget_status",
            return_value={"allowed": False, "reason": "global_utc_call_guard"},
        ) as budget_mock, mock.patch.object(
            research,
            "_research_context",
            return_value=self.evidence_context(),
        ), mock.patch.object(research, "complete") as complete_mock:
            with self.assertRaisesRegex(SettingsError, "global_utc_call_guard"):
                research.run_once(
                    ROOT / "config" / "settings.bounded_crypto_paper.json"
                )
        complete_mock.assert_not_called()
        budget_mock.assert_called_once_with(
            agent_name="global_research_worker",
            replay_deferred=False,
        )
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as conn:
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0]
            )
        released = state["last_paid_research_lease"]
        self.assertEqual("budget_denied", released["operation_outcome"])
        self.assertEqual("cost_ceiling_or_call_limit", released["failure_category"])
        self.assertIsNone(released["provider_outcome"])

    def test_downstream_ingestion_failure_preserves_provider_attempt_identity(self) -> None:
        provider_result = self.model_result(
            {"strategy_contracts": [self.contract(77)]}
        )
        with mock.patch.object(
            research,
            "completion_preflight_status",
            return_value={"ok": True, "status": "budget_ok"},
        ), mock.patch.object(
            research,
            "cost_budget_status",
            return_value={"allowed": True, "status": "cost_budget_available"},
        ), mock.patch.object(
            research,
            "complete",
            return_value=provider_result,
        ), mock.patch.object(
            research,
            "_research_context",
            return_value=self.evidence_context(),
        ), mock.patch.object(
            research,
            "_ingest_contracts",
            side_effect=RuntimeError("test ingestion failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "test ingestion failure"):
                research.run_once(
                    ROOT / "config" / "settings.bounded_crypto_paper.json"
                )
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as conn:
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0]
            )
        released = state["last_paid_research_lease"]
        self.assertEqual(provider_result.status, released["provider_outcome"])
        self.assertEqual(provider_result.event_id, released["provider_event_id"])
        self.assertEqual(
            provider_result.estimated_cost_usd,
            released["provider_estimated_cost_usd"],
        )
        self.assertEqual("downstream_failure", released["operation_outcome"])
        self.assertEqual(
            "downstream_parse_and_ingest_failed", released["failure_category"]
        )

    def test_report_write_failure_preserves_provider_attempt_identity(self) -> None:
        provider_result = self.model_result({"strategy_contracts": []})
        with mock.patch.object(
            research,
            "completion_preflight_status",
            return_value={"ok": True, "status": "budget_ok"},
        ), mock.patch.object(
            research,
            "cost_budget_status",
            return_value={"allowed": True, "status": "cost_budget_available"},
        ), mock.patch.object(
            research,
            "complete",
            return_value=provider_result,
        ), mock.patch.object(
            research,
            "_research_context",
            return_value=self.evidence_context(),
        ), mock.patch.object(
            pathlib.Path,
            "write_text",
            side_effect=OSError("test report write failure"),
        ):
            with self.assertRaisesRegex(OSError, "test report write failure"):
                research.run_once(
                    ROOT / "config" / "settings.bounded_crypto_paper.json"
                )
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as conn:
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0]
            )
        released = state["last_paid_research_lease"]
        self.assertEqual(provider_result.status, released["provider_outcome"])
        self.assertEqual("downstream_failure", released["operation_outcome"])
        self.assertEqual(
            "downstream_report_write_failed", released["failure_category"]
        )

    def test_stale_paid_lease_hard_halts_and_cannot_be_taken_over(self) -> None:
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as conn:
            first, _ = research._claim_paid_research_lease(
                conn,
                self.settings,
                campaign_id,
            )
        with storage.connect(self.db_path, initialize=False) as conn:
            row = conn.execute(
                "select state_json from paper_expansion_campaign_state where campaign_id=?",
                (campaign_id,),
            ).fetchone()
            state = json.loads(row[0])
            state["paid_research_inflight"]["lease_expires_at"] = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
            ).isoformat()
            conn.execute(
                "update paper_expansion_campaign_state set state_json=? where campaign_id=?",
                (json.dumps(state, sort_keys=True), campaign_id),
            )
            conn.commit()
        with storage.connect(self.db_path, initialize=False) as conn:
            with self.assertRaisesRegex(
                SettingsError, "stale_paid_research_lease_requires_manual_reset"
            ):
                research._claim_paid_research_lease(
                    conn,
                    self.settings,
                    campaign_id,
                )
        with storage.connect(self.db_path, initialize=False) as conn:
            row = conn.execute(
                "select run_status,state_json from paper_expansion_campaign_state where campaign_id=?",
                (campaign_id,),
            ).fetchone()
            state = json.loads(row["state_json"])
        self.assertEqual("hard_halted", row["run_status"])
        self.assertEqual("hard_halted", state["run_status"])
        self.assertEqual(
            "stale_paid_research_lease_requires_manual_reset",
            state["hard_halt_reason"],
        )
        self.assertEqual(
            first["lease_id"], state["paid_research_inflight"]["lease_id"]
        )
        self.assertIn("stale_paid_research_lease_detected_at", state)

    def test_paid_call_uses_no_external_tools_and_clears_database_lease(self) -> None:
        with mock.patch.object(
            research,
            "completion_preflight_status",
            return_value={"ok": True, "status": "budget_ok"},
        ), mock.patch.object(
            research,
            "cost_budget_status",
            return_value={"allowed": True, "status": "cost_budget_available"},
        ), mock.patch.object(
            research,
            "complete",
            return_value=self.model_result({"strategy_contracts": []}),
        ), mock.patch.object(
            research,
            "_research_context",
            return_value=self.evidence_context(),
        ) as complete_mock:
            report = research.run_once(
                ROOT / "config" / "settings.bounded_crypto_paper.json"
            )
        self.assertFalse(report["external_tools_used"])
        self.assertNotIn("tools", complete_mock.call_args.kwargs)
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as conn:
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0]
            )
        self.assertNotIn("paid_research_inflight", state)
        released = state["last_paid_research_lease"]
        self.assertEqual("completed", released["operation_outcome"])
        self.assertIsNone(released["failure_category"])
        self.assertEqual("model_call:responses", released["provider_outcome"])

    def test_paid_completion_is_bound_to_the_persisted_research_lease_scope(self) -> None:
        observed_scope: dict = {}

        def scoped_completion(*_args, **_kwargs):
            observed_scope.update(current_autonomous_scope() or {})
            return self.model_result({"strategy_contracts": []})

        with mock.patch.object(
            research,
            "completion_preflight_status",
            return_value={"ok": True, "status": "budget_ok"},
        ), mock.patch.object(
            research,
            "cost_budget_status",
            return_value={"allowed": True, "status": "cost_budget_available"},
        ), mock.patch.object(
            research,
            "complete",
            side_effect=scoped_completion,
        ), mock.patch.object(
            research,
            "_research_context",
            return_value=self.evidence_context(),
        ):
            research.run_once(ROOT / "config" / "settings.bounded_crypto_paper.json")

        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as conn:
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0]
            )
        released = state["last_paid_research_lease"]
        self.assertEqual("paid_research_once", observed_scope["source"])
        self.assertEqual(released["lease_id"], observed_scope["scope_id"])
        self.assertTrue(observed_scope["enabled"])
        self.assertEqual(10, observed_scope["daily_paid_attempt_limit"])

    def test_blocked_model_preflight_consumes_no_daily_claim(self) -> None:
        with mock.patch.object(
            research,
            "completion_preflight_status",
            return_value={"ok": False, "status": "fallback_missing_provider_key"},
        ), mock.patch.object(
            research,
            "_research_context",
            return_value=self.evidence_context(),
        ), mock.patch.object(research, "complete") as complete_mock:
            with self.assertRaisesRegex(SettingsError, "missing_provider_key"):
                research.run_once(
                    ROOT / "config" / "settings.bounded_crypto_paper.json"
                )
        complete_mock.assert_not_called()
        claim_files = list((self.root / "runs" / "paid_research").glob("*.claim.json"))
        self.assertEqual([], claim_files)
        campaign_id = self.settings["paper_expansion"]["campaign_id"]
        with storage.connect(self.db_path, initialize=False) as conn:
            state = json.loads(
                conn.execute(
                    "select state_json from paper_expansion_campaign_state where campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0]
            )
        self.assertNotIn("paid_research_inflight", state)
        released = state["last_paid_research_lease"]
        self.assertEqual("preflight_denied", released["operation_outcome"])
        self.assertEqual("model_preflight_blocked", released["failure_category"])
        self.assertIsNone(released["provider_outcome"])

    def test_research_context_requires_exact_bounded_trade_lineage(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with storage.connect(self.db_path, initialize=False) as conn:
            conn.execute(
                """
                insert into signal_stats(signal_key,closed_count,wins,avg_pnl_bps,
                                         win_rate,score_adjustment,updated_at)
                values('UNPROVEN_AGGREGATE',100,100,999,1,99,?)
                """,
                (now,),
            )
            for index, signal_key in enumerate(("DIRECT_EXACT", "DIRECT_MISLINKED"), start=1):
                admission_key = f"paid-context-admission-{index}"
                episode_id = f"paid-context-episode-{index}"
                cursor = conn.execute(
                    """
                    insert into paper_trades(
                        opened_at,closed_at,venue,inst_id,direction,trade_type,signal_key,
                        base_score,learned_score,entry,exit,pnl_bps,status,thesis,
                        candidate_json,review_json,context_json,close_measurement_status,
                        admission_key,admission_episode_id
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        now, now, "OKX_SPOT", "BTC-USDT", "long_frontier_spot",
                        "frontier_crypto_venue_map", signal_key, 80.0, 80.0, 100.0, 99.0,
                        10.0, "closed", "test", json.dumps(
                            {
                                "venue": "OKX_SPOT",
                                "inst_id": "BTC-USDT",
                                "direction": "long_frontier_spot",
                                "trade_type": "frontier_crypto_venue_map",
                                "execution_feasibility": {"status": "standard"},
                                "paper_label_eligible": True,
                            },
                            sort_keys=True,
                        ), "{}",
                        json.dumps(
                            {
                                "signal_stats_scope": "direct",
                                "paper_route_status": "standard",
                                "paper_label_eligible": True,
                            }
                        ),
                        "valid",
                        admission_key,
                        episode_id,
                    ),
                )
                trade_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    insert into paper_admission_queue(
                        queue_id,admission_key,episode_id,evidence_fingerprint,
                        evidence_observed_at,lane,status,priority,venue,inst_id,
                        market_surface,lineage_root,direction,route_status,
                        candidate_json,eligibility_json,enqueued_at,updated_at,
                        paper_trade_id
                    ) values(?,?,?,?,?,'evidence','completed_valid',0,?,?,?,?,?,
                             'standard','{}','{}',?,?,?)
                    """,
                    (
                        f"paid-context-queue-{index}",
                        admission_key,
                        episode_id,
                        f"paid-context-fingerprint-{index}",
                        now,
                        "OKX_SPOT",
                        "BTC-USDT",
                        "frontier_crypto_venue_map",
                        "OKX_SPOT|frontier_crypto_venue_map",
                        "long_frontier_spot",
                        now,
                        now,
                        trade_id if signal_key == "DIRECT_EXACT" else trade_id + 1000,
                    ),
                )
            conn.commit()
            context = research._research_context(conn, self.settings)
        keys = {row["signal_key"] for row in context["signal_stats"]}
        self.assertEqual({"DIRECT_EXACT"}, keys)
        self.assertEqual(
            "reliable_timely_direct_standard_route_only",
            context["constraints"]["evidence_scope"],
        )
        self.assertEqual(
            [
                {
                    "venue": "OKX_SPOT",
                    "trade_type": "frontier_crypto_venue_map",
                    "direction": "long_frontier_spot",
                    "reliable_label_count": 1,
                }
            ],
            context["evidence_contracts"],
        )

    def test_supported_powershell_entrypoints_share_cycle_mutex_identity(self) -> None:
        supervisor = (ROOT / "scripts" / "run_bounded_paper_forever.ps1").read_text(
            encoding="utf-8"
        )
        one_shot = (ROOT / "scripts" / "run_paid_research_once.ps1").read_text(
            encoding="utf-8"
        )
        mutex_template = "Global\\AgenticTradingSwarm.BoundedPaperCycle.$IdentityHash"
        self.assertIn(mutex_template, supervisor)
        self.assertIn(mutex_template, one_shot)
        self.assertIn('WaitOne(0, $false)', supervisor)
        self.assertIn('WaitOne(0, $false)', one_shot)
        self.assertIn("$WrotePidFile", supervisor)
        self.assertIn('Remove-Item -LiteralPath "Env:RADAR_MODEL_CREDENTIAL_LOCK"', one_shot)
        self.assertIn('Remove-Item -LiteralPath "Env:RADAR_MODELS_DISABLED"', one_shot)
        self.assertIn("$ResearchExitCode = 75", one_shot)
        database_identity = "$material = $ProjectRoot.ToLowerInvariant()"
        self.assertIn(database_identity, supervisor)
        self.assertIn(database_identity, one_shot)
        self.assertNotIn("$ResolvedConfig.ToLowerInvariant()", supervisor)
        self.assertNotIn("$ResolvedConfig.ToLowerInvariant()", one_shot)

    def test_hidden_launcher_waits_for_radar_preflight_before_paid_supervisor(self) -> None:
        launcher = (ROOT / "scripts" / "start_bounded_paper_hidden.ps1").read_text(
            encoding="utf-8"
        )
        paid_supervisor = (
            ROOT / "scripts" / "run_paid_research_supervisor.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("run_paid_research_supervisor.ps1", launcher)
        self.assertLess(
            launcher.index("$radarProcess = Start-Process"),
            launcher.index("$researchProcess = Start-Process"),
        )
        self.assertLess(
            launcher.index("$researchProcess = Start-Process"),
            launcher.index('Remove-Item -LiteralPath "Env:$name"'),
        )
        self.assertIn("bounded_paper_supervisor.pid.json", launcher)
        self.assertIn("paid_research_supervisor.pid.json", launcher)
        self.assertIn("did not acknowledge a successful preflight", launcher)
        self.assertIn("Paid research supervisor did not acknowledge startup", launcher)
        self.assertIn("bounded radar was stopped", launcher)
        self.assertLess(
            launcher.index("$researchReady = $false"),
            launcher.index('Remove-Item -LiteralPath "Env:$name"'),
        )
        self.assertIn("$researchPidRecord.pid -eq $researchProcess.Id", launcher)
        self.assertIn("$researchRecordedRunner -eq $ResearchRunnerPath", launcher)
        self.assertIn("$researchRecordedConfig -eq $ResolvedConfig", launcher)
        self.assertIn('$currentBranch -ne "main"', launcher)
        self.assertIn("status --porcelain", launcher)
        self.assertIn("HEAD...$upstreamBranch", launcher)
        self.assertLess(
            launcher.index('$currentBranch = (& git'),
            launcher.index("$radarProcess = Start-Process"),
        )
        self.assertIn(
            "Global\\AgenticTradingSwarm.PaidResearchSupervisor.$IdentityHash",
            paid_supervisor,
        )
        self.assertIn("$CheckIntervalSeconds = 900", paid_supervisor)
        self.assertIn("$InitialDelaySeconds = 60", paid_supervisor)
        self.assertIn('Write-Heartbeat -State "initial_delay"', paid_supervisor)
        self.assertIn("WaitOne(0, $false)", paid_supervisor)
        self.assertIn("run_paid_research_once.ps1", paid_supervisor)
        self.assertIn("$TimeoutSeconds = 300", paid_supervisor)
        self.assertIn("$WrotePidFile", paid_supervisor)
        self.assertIn("$material = $ProjectRoot.ToLowerInvariant()", paid_supervisor)
        self.assertNotIn("$ResolvedConfig.ToLowerInvariant()", paid_supervisor)
        self.assertNotIn("OPENAI_API_KEY", paid_supervisor)
        self.assertIn('$exitCode -eq 75', paid_supervisor)
        self.assertIn('AddSeconds(60)', paid_supervisor)


if __name__ == "__main__":
    unittest.main()
