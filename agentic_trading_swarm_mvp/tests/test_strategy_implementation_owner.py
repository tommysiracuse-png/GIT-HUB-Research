from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import evolution_owner_scheduler
import strategy_implementation_owner as owner
from storage import add_llm_recommendation, init_db


class StrategyImplementationOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.settings = {
            "strategy_implementation_owner": {
                "enabled": True,
                "salvage_invalid_backlog": False,
                "memory_context_chars": 20000,
                "memory_retrieval_limit": 10,
            },
            "agent_memory": {"enabled": False},
        }

    def tearDown(self) -> None:
        self.conn.close()

    def _recommendation(self, rec_id: str = "rec-strategy-1") -> dict:
        payload = {
            "action": "propose_strategy_lab_experiment",
            "priority": 91,
            "title": "Test cross-market relative strength",
            "rationale": "Buy liquid country proxies only when own momentum and relative strength agree.",
        }
        add_llm_recommendation(
            self.conn, rec_id, payload["action"], payload["title"], payload["rationale"], payload
        )
        return {
            "recommendation_id": rec_id,
            "title": payload["title"],
            "rationale": payload["rationale"],
            "payload": payload,
        }

    def test_recommendation_gets_durable_task_before_handled(self) -> None:
        artifact = owner.enqueue_recommendation(self.conn, self._recommendation(), self.settings)
        self.assertEqual("strategy_owner_task", artifact["artifact"])
        task = self.conn.execute("select * from strategy_owner_tasks").fetchone()
        self.assertEqual("queued", task["status"])
        status = self.conn.execute(
            "select status from llm_recommendations where recommendation_id='rec-strategy-1'"
        ).fetchone()[0]
        self.assertEqual("owner_queued", status)

    def test_historical_auto_executed_without_artifact_is_reopened_and_queued(self) -> None:
        self._recommendation("rec-orphaned")
        self.conn.execute(
            "update llm_recommendations set status='auto_executed' where recommendation_id='rec-orphaned'"
        )
        self.conn.commit()

        result = owner.sync_backlog(self.conn, self.settings)

        self.assertEqual(1, result["artifact_lifecycle"]["recommendations_reopened"])
        self.assertEqual("owner_queued", self.conn.execute(
            "select status from llm_recommendations where recommendation_id='rec-orphaned'"
        ).fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "select count(*) from recommendation_artifact_links where recommendation_id='rec-orphaned'"
        ).fetchone()[0])

    def test_historical_materialized_experiment_repairs_recommendation_status(self) -> None:
        self._recommendation("rec-materialized")
        self.conn.execute(
            "update llm_recommendations set status='auto_executed' where recommendation_id='rec-materialized'"
        )
        now = owner._utc_now()
        self.conn.execute(
            """
            insert into strategy_lab_experiments(
                strategy_lab_id,version,experiment_type,status,hypothesis,strategy_logic_json,
                data_requirements_json,risk_gates_json,promotion_rules_json,source_agent,
                source_recommendation_id,created_at,updated_at
            ) values('lab-materialized',1,'market_strategy','active_testing','test','{}','{}','{}','{}',
                     'strategy_owner','rec-materialized',?,?)
            """,
            (now, now),
        )
        self.conn.commit()

        result = owner.sync_backlog(self.conn, self.settings)

        self.assertGreaterEqual(result["artifact_lifecycle"]["artifact_links_backfilled"], 1)
        self.assertEqual("experiment_materialized", self.conn.execute(
            "select status from llm_recommendations where recommendation_id='rec-materialized'"
        ).fetchone()[0])

    def test_decision_schema_is_strict_and_decodes_flexible_contract_json(self) -> None:
        schema = owner._decision_schema()
        self.assertFalse(schema["additionalProperties"])
        for key in ("strategy_experiment", "code_goal", "blocker"):
            variants = schema["properties"][key]["anyOf"]
            self.assertEqual({"string", "null"}, {variant["type"] for variant in variants})
        decoded = owner._decode_decision_payload(
            {
                "decision": "wait_for_data",
                "rationale": "Need a feature.",
                "strategy_experiment": None,
                "code_goal": '{"title":"Add feature"}',
                "dependencies": '[{"feature":"residual_return"}]',
                "acceptance_criteria": [],
                "tests_to_run": [],
                "blocker": '{"type":"missing_feature"}',
                "memory_note": "Remember the missing feature.",
            }
        )
        self.assertEqual("Add feature", decoded["code_goal"]["title"])
        self.assertEqual("residual_return", decoded["dependencies"][0]["feature"])
        self.assertEqual("missing_feature", decoded["blocker"]["type"])

    def test_prose_contract_turn_materializes_experiment_without_code_diff(self) -> None:
        rec = self._recommendation()
        owner.enqueue_recommendation(self.conn, rec, self.settings)
        decision = {
            "decision": "materialize_experiment",
            "rationale": "The idea fits the existing candidate schema.",
            "strategy_experiment": {
                "strategy_lab_id": "lab_relative_strength_general",
                "version": 1,
                "experiment_type": "market_strategy",
                "hypothesis": "Aligned own momentum and relative strength predict positive 60m returns.",
                "source_surface": "proxy_momentum",
                "permitted_target_surface": ["proxy_momentum"],
                "strategy_logic": {
                    "type": "candidate_filter",
                    "trade_types": ["global_proxy_momentum"],
                    "directions": ["long_proxy"],
                    "min_edge_bps": 5,
                },
                "data_requirements": {"paper_only": True},
                "risk_gates": {"require_route_feasible": True},
                "promotion_rules": {},
            },
            "code_goal": None,
            "dependencies": [],
            "acceptance_criteria": ["Experiment is persisted"],
            "tests_to_run": [],
            "blocker": None,
            "memory_note": "Reusable relative-strength strategy.",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            owner, "_ensure_worktree", return_value=(pathlib.Path(tmp), "strategy-owner/test", None)
        ), mock.patch.object(
            owner, "_memory_context", return_value=([], "context-hash")
        ), mock.patch.object(
            owner,
            "run_structured_codex_turn",
            return_value={
                "status": "completed",
                "started_at": owner._utc_now(),
                "session_id": "thread-1",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "decision": decision,
            },
        ), mock.patch.object(owner, "_record_memory"):
            result = owner.process_one(self.conn, self.settings, cycle_id="cycle-1")
        self.assertEqual("active_testing", result["status"])
        experiment = self.conn.execute(
            "select status from strategy_lab_experiments where strategy_lab_id='lab_relative_strength_general'"
        ).fetchone()
        self.assertIsNotNone(experiment)
        self.assertEqual("experiment_materialized", self.conn.execute(
            "select status from llm_recommendations where recommendation_id='rec-strategy-1'"
        ).fetchone()[0])
        self.assertEqual(0, self.conn.execute("select count(*) from code_evolution_proposals").fetchone()[0])

    def test_contract_intake_materializes_diverse_batch_without_codex_writer(self) -> None:
        rec_a = self._recommendation("rec-frontier-intake")
        rec_a["payload"].update(
            {
                "market_key": "frontier_crypto_spot",
                "proposed_change": {"entry_logic": "Cross-venue washout then rebound."},
            }
        )
        self.conn.execute(
            "update llm_recommendations set payload_json=? where recommendation_id=?",
            (json.dumps(rec_a["payload"]), rec_a["recommendation_id"]),
        )
        owner.enqueue_recommendation(self.conn, rec_a, self.settings)
        payload_b = {
            "action": "propose_strategy_lab_experiment",
            "priority": 90,
            "title": "Proxy residual reversal",
            "rationale": "Test residual reversal after market-relative overextension.",
            "market_key": "global_proxy_momentum",
            "proposed_change": {"entry_logic": "Fade extreme SPY-relative residuals after deceleration."},
        }
        add_llm_recommendation(
            self.conn,
            "rec-proxy-intake",
            payload_b["action"],
            payload_b["title"],
            payload_b["rationale"],
            payload_b,
        )
        rec_b = {
            "recommendation_id": "rec-proxy-intake",
            "title": payload_b["title"],
            "rationale": payload_b["rationale"],
            "payload": payload_b,
        }
        owner.enqueue_recommendation(self.conn, rec_b, self.settings)
        decisions = {
            "items": [
                {
                    "task_id": row["task_id"],
                    "decision": "materialize_experiment",
                    "rationale": "Runnable with current candidate fields.",
                    "strategy_experiment": {
                        "strategy_lab_id": f"intake_{index}",
                        "version": 1,
                        "experiment_type": "market_strategy",
                        "hypothesis": "A reusable conditional pattern has positive paper expectancy.",
                        "source_surface": "frontier_crypto_spot" if index == 1 else "global_proxy_momentum",
                        "permitted_target_surface": ["frontier_crypto_spot" if index == 1 else "global_proxy_momentum"],
                        "strategy_logic": {
                            "type": "candidate_filter",
                            "trade_types": ["frontier_crypto_venue_map" if index == 1 else "global_proxy_momentum"],
                            "directions": ["long_frontier_spot" if index == 1 else "long_proxy"],
                        },
                        "data_requirements": {"paper_only": True},
                        "risk_gates": {},
                        "promotion_rules": {},
                    },
                    "code_goal": None,
                    "dependencies": [],
                    "acceptance_criteria": ["Experiment persists"],
                    "tests_to_run": [],
                    "blocker": None,
                    "memory_note": "Compiled without a repository coding session.",
                }
                for index, row in enumerate(
                    self.conn.execute(
                        "select task_id from strategy_owner_tasks order by task_id"
                    ).fetchall(),
                    start=1,
                )
            ]
        }
        model_result = mock.Mock(
            text=json.dumps(decisions),
            status="model_call:responses",
            model_name="openai/gpt-5.4",
            model_tier="standard",
            reasoning_effort="medium",
            estimated_cost_usd=0.04,
        )

        with mock.patch.object(owner, "complete", return_value=model_result) as complete_call, mock.patch.object(
            owner, "_record_memory"
        ):
            result = owner.process_contract_intake_batch(
                self.conn,
                {**self.settings, "strategy_implementation_owner": {**self.settings["strategy_implementation_owner"], "contract_intake_batch_size": 6}},
                cycle_id="contract-cycle",
            )

        self.assertEqual("processed", result["status"])
        self.assertEqual(2, result["by_status"]["active_testing"])
        self.assertEqual(2, self.conn.execute("select count(*) from strategy_lab_experiments").fetchone()[0])
        self.assertEqual(1, complete_call.call_count)
        self.assertNotIn("run_structured_codex_turn", str(complete_call.call_args))

    def test_code_decision_reuses_owner_codex_session(self) -> None:
        rec = self._recommendation()
        owner.enqueue_recommendation(self.conn, rec, self.settings)
        decision = {
            "decision": "implement_code",
            "rationale": "A required reusable feature is absent.",
            "strategy_experiment": None,
            "code_goal": {"title": "Add rolling residual feature", "goal": "Add and test a rolling residual feature."},
            "dependencies": [],
            "acceptance_criteria": ["Feature is available to observation programs"],
            "tests_to_run": ["python -m unittest tests.test_strategy_program"],
            "blocker": None,
            "memory_note": "Residual feature required.",
        }
        captured = {}

        def fake_process(_conn, recommendation, _settings):
            captured.update(recommendation["payload"])
            return [{"action_status": "created", "proposal_id": "proposal-1", "status": "promoted"}]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            owner, "_ensure_worktree", return_value=(pathlib.Path(tmp), "strategy-owner/test", None)
        ), mock.patch.object(owner, "_memory_context", return_value=([], "context-hash")), mock.patch.object(
            owner, "run_structured_codex_turn", return_value={
                "status": "completed", "started_at": owner._utc_now(), "session_id": "thread-code-1",
                "model": "gpt-5.6-sol", "reasoning_effort": "high", "decision": decision,
            }
        ), mock.patch.object(owner, "process_code_change_recommendation", side_effect=fake_process), mock.patch.object(
            owner, "_record_memory"
        ):
            result = owner.process_one(self.conn, self.settings, cycle_id="cycle-2")
        self.assertEqual("analyzing", result["status"])
        self.assertEqual("resume_same_task_and_materialize_experiment", result["next_action"])
        self.assertEqual("thread-code-1", captured["strategy_owner_codex_session_id"])
        task = self.conn.execute("select * from strategy_owner_tasks").fetchone()
        self.assertEqual("proposal-1", task["code_proposal_id"])

    def test_dead_claim_is_reclaimed_and_live_claim_is_preserved(self) -> None:
        owner.enqueue_recommendation(self.conn, self._recommendation(), self.settings)
        self.conn.execute(
            "update strategy_owner_tasks set claimed_by='old', claimed_pid=99999999, status='coding'"
        )
        self.conn.commit()
        self.assertEqual(1, owner._reclaim_dead_leases(self.conn))
        row = self.conn.execute("select status, claimed_by from strategy_owner_tasks").fetchone()
        self.assertEqual("implementation_paused", row["status"])
        self.assertIsNone(row["claimed_by"])

    def test_interrupted_turn_resumes_same_task_and_codex_session(self) -> None:
        owner.enqueue_recommendation(self.conn, self._recommendation(), self.settings)
        calls = []

        def fake_turn(**kwargs):
            calls.append(kwargs.get("session_id"))
            if len(calls) == 1:
                return {
                    "status": "implementation_paused",
                    "reason": "codex_turn_timeout",
                    "session_id": "thread-resume-1",
                    "started_at": owner._utc_now(),
                }
            return {
                "status": "completed",
                "session_id": "thread-resume-1",
                "started_at": owner._utc_now(),
                "decision": {
                    "decision": "wait_for_data",
                    "rationale": "Need one additional feature.",
                    "strategy_experiment": None,
                    "code_goal": None,
                    "dependencies": [{"feature": "residual_return"}],
                    "acceptance_criteria": [],
                    "tests_to_run": [],
                    "blocker": {"type": "missing_feature"},
                    "memory_note": "Resume preserved repository analysis.",
                },
            }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            owner, "_ensure_worktree", return_value=(pathlib.Path(tmp), "strategy-owner/test", None)
        ), mock.patch.object(owner, "_memory_context", return_value=([], "hash")), mock.patch.object(
            owner, "run_structured_codex_turn", side_effect=fake_turn
        ), mock.patch.object(owner, "_record_memory"):
            first = owner.process_one(self.conn, self.settings, cycle_id="cycle-a")
            self.conn.execute("update strategy_owner_tasks set next_retry_at=null")
            self.conn.commit()
            second = owner.process_one(self.conn, self.settings, cycle_id="cycle-b")
        self.assertEqual("implementation_paused", first["status"])
        self.assertEqual("waiting_data", second["status"])
        self.assertEqual([None, "thread-resume-1"], calls)

    def test_runtime_contract_mismatch_requeues_one_canonical_owner_repair(self) -> None:
        artifact = owner.enqueue_recommendation(self.conn, self._recommendation(), self.settings)
        now = owner._utc_now()
        mismatch = {
            "repairable": True,
            "reason": "compiled_universe_does_not_match_available_observations",
            "observation_count": 10629,
            "universe_match_count": 0,
            "missing_features": [],
            "mismatches": [
                {
                    "universe_key": "market_types",
                    "runtime_field": "market_type",
                    "required_values": ["PERP"],
                    "observed_values": ["<MISSING>"],
                }
            ],
            "owner_objective": "repair_runtime_contract",
        }
        self.conn.execute(
            """
            insert into strategy_lab_experiments(
                strategy_lab_id,version,experiment_type,status,hypothesis,strategy_logic_json,
                data_requirements_json,risk_gates_json,promotion_rules_json,source_agent,
                created_at,updated_at,compile_status,evaluation_json
            ) values('runtime-contract-lab',1,'market_strategy','needs_contract_revision','test',
                     '{}','{}','{}','{}','strategy_owner',?,?, 'compiled',?)
            """,
            (
                now,
                now,
                json.dumps(
                    {"generation_diagnostic": {"runtime_contract_mismatch": mismatch}}
                ),
            ),
        )
        self.conn.execute(
            """
            update strategy_owner_tasks
            set strategy_lab_id='runtime-contract-lab', status='waiting_data', priority=92
            where task_id=?
            """,
            (artifact["task_id"],),
        )
        self.conn.execute(
            """
            insert into strategy_owner_tasks(
                task_id,created_at,updated_at,dedupe_key,objective_type,priority,status,
                strategy_lab_id,strategy_lab_version,hypothesis,acceptance_json,dependency_json
            ) values('duplicate-repair',?,?, 'duplicate-repair-key','add_missing_strategy_features',
                     84,'waiting_data','runtime-contract-lab',1,'test','{}','{}')
            """,
            (now, now),
        )
        self.conn.commit()

        result = owner.monitor_tasks(self.conn)

        canonical = self.conn.execute(
            "select status,objective_type,priority,dependency_json from strategy_owner_tasks where task_id=?",
            (artifact["task_id"],),
        ).fetchone()
        duplicate = self.conn.execute(
            "select status from strategy_owner_tasks where task_id='duplicate-repair'"
        ).fetchone()
        self.assertEqual("analyzing", canonical["status"])
        self.assertEqual("repair_runtime_contract", canonical["objective_type"])
        self.assertEqual(99, canonical["priority"])
        self.assertIn("runtime_contract_mismatch", canonical["dependency_json"])
        self.assertEqual("superseded_duplicate", duplicate["status"])
        self.assertEqual(2, len(result["transitions"]))

    def test_round_robin_is_persistent_and_equal(self) -> None:
        seen = []
        for cycle in range(6):
            order, _ = evolution_owner_scheduler.lane_order(self.conn)
            seen.append(order[0])
            evolution_owner_scheduler.record_turn(
                self.conn, order[0], cycle_id=str(cycle), status="used", consumed_writer=True
            )
        self.assertEqual(["strategy", "adapter", "general", "strategy", "adapter", "general"], seen)
        summary = evolution_owner_scheduler.scheduler_summary(self.conn)
        self.assertEqual({"strategy": 2, "adapter": 2, "general": 2}, summary["turns_by_lane"])

    def test_owner_prioritizes_new_strategy_when_testing_portfolio_is_thin(self) -> None:
        now = owner._utc_now()
        for task_id, objective, priority in (
            ("repair-task", "repair_runtime_contract", 99),
            ("materialize-task", "materialize_hypothesis", 84),
        ):
            self.conn.execute(
                """
                insert into strategy_owner_tasks(
                    task_id,created_at,updated_at,dedupe_key,objective_type,priority,status,
                    hypothesis,acceptance_json,dependency_json
                ) values(?,?,?,?,?,?,'queued',?,'{}','{}')
                """,
                (task_id, now, now, task_id, objective, priority, objective),
            )
        self.conn.commit()

        claimed = owner.claim_task(
            self.conn,
            {"strategy_implementation_owner": {"minimum_concurrent_experiments": 8}},
        )

        self.assertEqual("materialize-task", claimed["task_id"])

    def test_relaxed_descendants_do_not_inflate_strategy_portfolio_floor(self) -> None:
        now = owner._utc_now()
        parent = None
        for index in range(10):
            strategy_id = "carry-root" if index == 0 else f"carry-root__relaxed_r{index}"
            self.conn.execute(
                """
                insert into strategy_lab_experiments (
                    strategy_lab_id,version,parent_strategy_lab_id,experiment_type,status,
                    hypothesis,strategy_logic_json,data_requirements_json,risk_gates_json,
                    promotion_rules_json,source_agent,compile_status,created_at,updated_at
                ) values (?,1,?,'market_strategy','active_testing',?,'{}','{}','{}','{}',
                          'strategy_feasibility_profiler','compiled',?,?)
                """,
                (strategy_id, parent, strategy_id, now, now),
            )
            parent = strategy_id
        for task_id, objective, priority in (
            ("repair-task", "repair_runtime_contract", 99),
            ("materialize-task", "materialize_hypothesis", 84),
        ):
            self.conn.execute(
                """
                insert into strategy_owner_tasks(
                    task_id,created_at,updated_at,dedupe_key,objective_type,priority,status,
                    hypothesis,acceptance_json,dependency_json
                ) values(?,?,?,?,?,?,'queued',?,'{}','{}')
                """,
                (task_id, now, now, task_id, objective, priority, objective),
            )
        self.conn.commit()

        portfolio = owner._strategy_portfolio_stats(self.conn)
        claimed = owner.claim_task(
            self.conn,
            {"strategy_implementation_owner": {"minimum_concurrent_experiments": 8}},
        )

        self.assertEqual(10, portfolio["compiled_experiments"])
        self.assertEqual(1, portfolio["distinct_lineages"])
        self.assertEqual("materialize-task", claimed["task_id"])

    def test_invalid_backlog_collapses_by_novelty_signature(self) -> None:
        now = owner._utc_now()
        for suffix in ("a", "b"):
            self.conn.execute(
                """
                insert into strategy_lab_experiments (
                    strategy_lab_id,version,experiment_type,status,hypothesis,strategy_logic_json,
                    data_requirements_json,risk_gates_json,promotion_rules_json,source_agent,
                    created_at,updated_at
                ) values (?,1,'market_strategy','rejected_invalid',?,'{}','{}','{}','{}','test',?,?)
                """,
                (f"invalid-{suffix}", "Cross venue residual mean reversion after costs", now, now),
            )
        self.conn.commit()
        settings = {"strategy_implementation_owner": {"salvage_invalid_backlog": True, "salvage_limit_per_cycle": 10}}
        result = owner.sync_backlog(self.conn, settings)
        self.assertEqual(1, result["historical_experiments_salvaged"])

    def test_zero_output_experiment_is_requeued_for_diagnosis(self) -> None:
        now = owner._utc_now()
        evaluation = {
            "generation_diagnostic": {
                "generated_candidate_count": 0,
                "feasibility": {"universe_match_count": 421, "candidate_count": 2},
            }
        }
        self.conn.execute(
            """
            insert into strategy_lab_experiments (
                strategy_lab_id,version,experiment_type,status,hypothesis,strategy_logic_json,
                data_requirements_json,risk_gates_json,promotion_rules_json,source_agent,
                created_at,updated_at,evaluation_json,compile_status
            ) values (?,1,'market_strategy','needs_more_evidence',?,'{}','{}','{}','{}','test',?,?,?,'compiled')
            """,
            ("zero-output-child", "A valid universe should emit candidates", now, now, json.dumps(evaluation)),
        )
        self.conn.commit()
        settings = {"strategy_implementation_owner": {"salvage_invalid_backlog": True, "salvage_limit_per_cycle": 10}}
        result = owner.sync_backlog(self.conn, settings)
        task = self.conn.execute(
            "select objective_type,status from strategy_owner_tasks where strategy_lab_id='zero-output-child'"
        ).fetchone()
        self.assertEqual(1, result["historical_experiments_salvaged"])
        self.assertEqual("diagnose_zero_output", task["objective_type"])
        self.assertEqual("queued", task["status"])
        transitions = owner.monitor_tasks(self.conn)
        task = self.conn.execute(
            "select objective_type,status,priority from strategy_owner_tasks where strategy_lab_id='zero-output-child'"
        ).fetchone()
        self.assertEqual("analyzing", task["status"])
        self.assertEqual("diagnose_zero_output", task["objective_type"])
        self.assertEqual(96, task["priority"])
        self.assertTrue(any(item["task_id"] for item in transitions["transitions"]))
        self.conn.execute(
            "update strategy_owner_tasks set priority=84 where strategy_lab_id='zero-output-child'"
        )
        self.conn.commit()
        owner.monitor_tasks(self.conn)
        task = self.conn.execute(
            "select status,priority from strategy_owner_tasks where strategy_lab_id='zero-output-child'"
        ).fetchone()
        self.assertEqual("analyzing", task["status"])
        self.assertEqual(96, task["priority"])
        self.conn.execute(
            "update strategy_owner_tasks set status='monitoring_evidence',attempt_count=1 where strategy_lab_id='zero-output-child'"
        )
        self.conn.commit()
        transitions = owner.monitor_tasks(self.conn)
        task = self.conn.execute(
            "select status from strategy_owner_tasks where strategy_lab_id='zero-output-child'"
        ).fetchone()
        self.assertEqual("monitoring_evidence", task["status"])
        self.assertFalse(any(item["task_id"] for item in transitions["transitions"]))

    def test_parent_with_adaptive_child_stays_in_monitoring(self) -> None:
        now = owner._utc_now()
        evaluation = {
            "generation_diagnostic": {
                "generated_candidate_count": 0,
                "relaxed_child": {"status": "created", "strategy_lab_id": "repair-child"},
            }
        }
        self.conn.execute(
            """
            insert into strategy_lab_experiments (
                strategy_lab_id,version,experiment_type,status,hypothesis,strategy_logic_json,
                data_requirements_json,risk_gates_json,promotion_rules_json,source_agent,
                created_at,updated_at,evaluation_json,compile_status
            ) values ('repair-parent',1,'market_strategy','needs_contract_revision',?,'{}','{}','{}','{}','test',?,?,?,'compiled')
            """,
            ("Parent strategy repaired by an adaptive child", now, now, json.dumps(evaluation)),
        )
        self.conn.execute(
            """
            insert into strategy_owner_tasks (
                task_id,created_at,updated_at,dedupe_key,objective_type,priority,status,
                strategy_lab_id,strategy_lab_version,hypothesis,acceptance_json,dependency_json
            ) values ('repair-parent-task',?,?,?,'repair_runtime_contract',99,'analyzing','repair-parent',1,?,'{}','{}')
            """,
            (now, now, "repair-parent-dedupe", "Parent strategy repaired by an adaptive child"),
        )
        self.conn.commit()
        owner.monitor_tasks(self.conn)
        task = self.conn.execute(
            "select status from strategy_owner_tasks where task_id='repair-parent-task'"
        ).fetchone()
        self.assertEqual("monitoring_evidence", task["status"])


if __name__ == "__main__":
    unittest.main()
