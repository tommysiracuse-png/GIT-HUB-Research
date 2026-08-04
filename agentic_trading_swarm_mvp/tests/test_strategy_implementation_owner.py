from __future__ import annotations

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
        self.assertEqual("auto_executed", self.conn.execute(
            "select status from llm_recommendations where recommendation_id='rec-strategy-1'"
        ).fetchone()[0])
        self.assertEqual(0, self.conn.execute("select count(*) from code_evolution_proposals").fetchone()[0])

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


if __name__ == "__main__":
    unittest.main()
