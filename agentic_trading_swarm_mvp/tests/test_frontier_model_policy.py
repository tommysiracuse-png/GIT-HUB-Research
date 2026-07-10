from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cost_router
import llm_swarm_runner
import storage
from cost_router import ModelResult


class FrontierModelPolicyTests(unittest.TestCase):
    def test_config_is_mini_first_with_explicit_escalation_tiers(self) -> None:
        cfg = cost_router.load_llm_config()
        self.assertEqual(cfg["tiers"]["frontier"]["model"], "openai/gpt-5.6-sol")
        self.assertEqual(cfg["tiers"]["frontier"]["api"], "responses")
        self.assertEqual(cfg["tiers"]["frontier"]["reasoning_effort"], "max")
        self.assertEqual(cfg["tiers"]["frontier"]["reasoning_mode"], "pro")
        self.assertEqual(cfg["tiers"]["standard"]["model"], "openai/gpt-5.4")
        self.assertEqual(cfg["tiers"]["fast"]["model"], "openai/gpt-5.4-mini")
        self.assertEqual(cfg["tiers"]["codex"]["model"], "openai/gpt-5.3-codex")
        self.assertEqual(cfg["agents"]["cross_market_researcher"]["tier"], "fast")
        self.assertEqual(cfg["agents"]["red_team"]["tier"], "fast")
        self.assertEqual(cfg["agents"]["build_planner"]["tier"], "fast")
        self.assertGreaterEqual(cfg["daily_budget_usd"], 500.0)
        self.assertGreaterEqual(cfg["agents"]["autonomous_builder"]["daily_budget_usd"], 100.0)
        self.assertGreaterEqual(cfg["agents"]["code_evolution"]["daily_budget_usd"], 100.0)

    def test_router_fallback_logs_responses_metadata_without_key(self) -> None:
        captured: list[ModelResult] = []
        with mock.patch.dict(os.environ, {"RADAR_USE_LITELLM": ""}, clear=False):
            with mock.patch.object(cost_router, "_log", lambda _agent, result: captured.append(result)):
                result = cost_router.complete(
                    "build_planner",
                    "Return JSON.",
                    tier_override="frontier",
                    operation="llm_swarm_recommendation",
                    frontier_escalation_reason="unit-test frontier path",
                    structured_json=True,
                )
        self.assertEqual(result.status, "fallback_no_cost")
        self.assertEqual(result.model_name, "openai/gpt-5.6-sol")
        self.assertEqual(result.api, "responses")
        self.assertEqual(result.reasoning_effort, "max")
        self.assertEqual(result.reasoning_mode, "pro")
        self.assertTrue(result.structured_json)
        self.assertEqual(result.frontier_escalation_reason, "unit-test frontier path")
        self.assertEqual(captured[0].operation, "llm_swarm_recommendation")

    def test_router_uses_openai_responses_for_frontier_when_enabled(self) -> None:
        captured: list[ModelResult] = []
        with mock.patch.dict(os.environ, {"RADAR_USE_LITELLM": "1", "OPENAI_API_KEY": "test"}, clear=False):
            with mock.patch.object(cost_router, "_spent_today", return_value=0.0):
                with mock.patch.object(cost_router, "_log", lambda _agent, result: captured.append(result)):
                    with mock.patch.object(cost_router, "_complete_openai_responses", return_value=('{"ok": true}', 10, 20)) as call:
                        result = cost_router.complete(
                            "red_team",
                            "Return JSON.",
                            tier_override="frontier",
                            operation="root_cause_analysis",
                            frontier_escalation_reason="unit-test root cause",
                            reasoning_effort_override="xhigh",
                            structured_json=True,
                        )
        self.assertEqual(result.status, "model_call:responses")
        self.assertEqual(result.reasoning_effort, "xhigh")
        self.assertEqual(result.prompt_tokens, 10)
        self.assertEqual(result.completion_tokens, 20)
        self.assertGreater(result.estimated_cost_usd, 0)
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs["model_name"], "gpt-5.6-sol")
        self.assertEqual(kwargs["reasoning_effort"], "xhigh")
        self.assertEqual(kwargs["reasoning_mode"], "pro")
        self.assertTrue(kwargs["structured_json"])
        self.assertEqual(captured[0].api, "responses")

    def test_swarm_escalates_market_scout_for_large_quality_gap(self) -> None:
        packet = {
            "expansion_map": {
                "frontier_crypto": {
                    "observation_count": 994,
                    "known_quality_rate": 0.06,
                    "regional_observation_count": 214,
                }
            },
            "growth_experiments": [{} for _ in range(2)],
        }
        tier, reason, reasoning = llm_swarm_runner.select_model_policy(llm_swarm_runner.AGENTS[0], packet)
        self.assertEqual(tier, "standard")
        self.assertIn("coverage gaps", reason)
        self.assertIsNone(reasoning)

    def test_swarm_escalates_route_hunter_for_many_blockers(self) -> None:
        packet = {
            "expansion_map": {
                "route_intelligence": {
                    "blocker_counts": {
                        "spot_borrow": 25,
                        "prediction_markets_account": 20,
                    }
                }
            },
            "growth_experiments": [],
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "execution_route_hunter")
        tier, reason, _reasoning = llm_swarm_runner.select_model_policy(agent, packet)
        self.assertEqual(tier, "standard")
        self.assertIn("Route blockers", reason)

    def test_swarm_recommendation_includes_frontier_escalation_reason(self) -> None:
        result = ModelResult(
            text='{"action":"propose_diagnostic_hypothesis","priority":88,"title":"Root cause","rationale":"Evidence-backed","evidence":{},"proposed_change":"Test hypothesis"}',
            model_name="openai/gpt-5.6-sol",
            model_tier="frontier",
            prompt_tokens=10,
            completion_tokens=20,
            estimated_cost_usd=0.01,
            status="model_call:responses",
            api="responses",
            reasoning_effort="high",
            verbosity="medium",
            structured_json=True,
        )
        packet = {"allowed_recommendation_actions": ["propose_diagnostic_hypothesis"], "growth_experiments": []}
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "red_team")
        with mock.patch.object(llm_swarm_runner, "complete", return_value=result):
            rec = llm_swarm_runner.run_agent(agent, packet, [])
        self.assertEqual(rec["model"]["tier"], "frontier")
        self.assertIn("frontier_escalation_reason", rec)
        self.assertEqual(rec["model"]["frontier_escalation_reason"], rec["frontier_escalation_reason"])

    def test_build_planner_unstructured_output_is_not_fake_code_change(self) -> None:
        packet = {
            "allowed_recommendation_actions": [
                "propose_code_change",
                "propose_build_task",
            ]
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "build_planner")

        rec = llm_swarm_runner.parse_recommendation("not json", agent, packet)

        self.assertEqual(rec["action"], "propose_build_task")
        self.assertTrue(rec["evidence"]["downgraded_from_code_change"])

    def test_build_planner_malformed_code_change_is_downgraded(self) -> None:
        packet = {
            "allowed_recommendation_actions": [
                "propose_code_change",
                "propose_build_task",
            ]
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "build_planner")

        rec = llm_swarm_runner.parse_recommendation(
            json.dumps(
                {
                    "action": "propose_code_change",
                    "priority": 50,
                    "title": "empty code proposal",
                    "rationale": "",
                    "evidence": {},
                    "proposed_change": "",
                }
            ),
            agent,
            packet,
        )

        self.assertEqual(rec["action"], "propose_build_task")
        self.assertEqual(rec["evidence"]["downgrade_reason"], "missing_actionable_code_change_fields")

    def test_build_planner_shapes_market_growth_code_change(self) -> None:
        packet = {
            "allowed_recommendation_actions": [
                "propose_code_change",
                "propose_build_task",
            ]
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "build_planner")

        rec = llm_swarm_runner.parse_recommendation(
            json.dumps(
                {
                    "action": "propose_code_change",
                    "priority": 82,
                    "title": "Market expansion quality coverage",
                    "rationale": "Increase depth enrichment for starved venues.",
                    "evidence": {"known_quality_rate": 0.06},
                    "proposed_change": "Add quality coverage for starved venue depth enrichment.",
                }
            ),
            agent,
            packet,
        )

        self.assertEqual(rec["action"], "propose_code_change")
        self.assertEqual(rec["code_change"]["change_category"], "scanner_expansion")
        self.assertEqual(rec["code_change"]["implementation_mode"], "runtime_active")
        self.assertIn("src/frontier_crypto_adapter.py", rec["code_change"]["expected_files"])

    def test_swarm_suppresses_fallback_recommendations_from_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_inbox = llm_swarm_runner.INBOX
            old_runs = llm_swarm_runner.RUNS_DIR
            try:
                llm_swarm_runner.RUNS_DIR = pathlib.Path(tmp)
                llm_swarm_runner.INBOX = pathlib.Path(tmp) / "llm_recommendations_inbox.jsonl"
                rec = {
                    "action": "propose_hunter_directive",
                    "title": "fallback",
                    "market_key": "fallback_llm_bridge",
                    "evidence": {"mode": "fallback"},
                    "model": {"status": "fallback_error:429 insufficient_quota"},
                }

                llm_swarm_runner.write_recommendations(
                    [rec],
                    10,
                    settings={"llm_swarm": {"write_fallback_recommendations_to_inbox": False}},
                )

                self.assertEqual(llm_swarm_runner.INBOX.read_text(encoding="utf-8"), "")
                latest = json.loads((pathlib.Path(tmp) / "llm_swarm_latest.json").read_text(encoding="utf-8"))
                self.assertEqual(latest["recommendations"], [])
                self.assertEqual(latest["suppressed_count"], 1)
            finally:
                llm_swarm_runner.INBOX = old_inbox
                llm_swarm_runner.RUNS_DIR = old_runs


class LlmCostStorageTests(unittest.TestCase):
    def test_cost_summary_groups_by_model_and_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            storage.record_llm_cost_event(
                conn,
                "red_team",
                "frontier",
                "openai/gpt-5.6-sol",
                100,
                200,
                0.01,
                "model_call:responses",
                provider="openai",
                api="responses",
                reasoning_effort="high",
                verbosity="medium",
                operation="root_cause_analysis",
                prompt_cache_key="radar:red_team:frontier",
                frontier_escalation_reason="unit-test",
                structured_json=True,
            )
            summary = storage.llm_cost_summary(conn)
            conn.close()
        self.assertEqual(summary["daily_estimated_cost_usd"], 0.01)
        self.assertEqual(summary["by_model"][0]["model_name"], "openai/gpt-5.6-sol")
        self.assertEqual(summary["by_operation"][0]["operation"], "root_cause_analysis")


if __name__ == "__main__":
    unittest.main()

