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
        self.assertEqual(result.max_output_tokens, 8000)
        self.assertGreater(result.estimated_cost_usd, 0)
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs["model_name"], "gpt-5.6-sol")
        self.assertEqual(kwargs["reasoning_effort"], "xhigh")
        self.assertEqual(kwargs["reasoning_mode"], "pro")
        self.assertTrue(kwargs["structured_json"])
        self.assertEqual(kwargs["max_output_tokens"], result.max_output_tokens)
        self.assertEqual(captured[0].api, "responses")

    def test_quota_failure_opens_shared_circuit_before_more_paid_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            cost_router, "QUOTA_STATE_PATH", pathlib.Path(tmp) / "quota.json"
        ), mock.patch.dict(
            os.environ, {"RADAR_USE_LITELLM": "1", "OPENAI_API_KEY": "test"}, clear=False
        ), mock.patch.object(
            cost_router, "_spent_today", return_value=0.0
        ), mock.patch.object(
            cost_router, "_log"
        ), mock.patch.object(
            cost_router,
            "_complete_openai_responses",
            side_effect=RuntimeError("429 insufficient_quota"),
        ) as call:
            first = cost_router.complete("market_scout", "one", tier_override="fast")
            second = cost_router.complete("red_team", "two", tier_override="fast")

        self.assertIn("insufficient_quota", first.status)
        self.assertTrue(second.status.startswith("quota_circuit_open_until:"))
        self.assertEqual(1, call.call_count)

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

    def test_route_hunter_attaches_read_only_requirement_summary_before_ranking(self) -> None:
        packet = {
            "allowed_recommendation_actions": ["propose_hunter_directive"],
            "short_frontier_spot_route_outcomes": {
                "paper_only": True,
                "routes": [
                    {
                        "venue": "MEXC",
                        "signal_key": "SYNTHETIC_RESEARCH|MEXC|frontier_crypto_venue_map|short_frontier_spot|conditional",
                        "direction": "short_frontier_spot",
                        "outcome_status": "weak_paper_outcome",
                        "observed_outcome": {"avg_pnl_bps": -79.377, "closed_count": 17},
                        "route_requirement_summary": {
                            "short_borrow_availability": {"availability_status": "available"},
                            "fee_estimate": {"route_cost_bps_paper": 14.5},
                            "margin_mode": {"required": True},
                            "api_entitlement": {"path_readiness": "observed"},
                        },
                    },
                    {
                        "venue": "VALR",
                        "signal_key": "SYNTHETIC_RESEARCH|VALR|frontier_crypto_venue_map|short_frontier_spot|conditional",
                        "direction": "short_frontier_spot",
                        "outcome_status": "weak_paper_outcome",
                        "observed_outcome": {"avg_pnl_bps": -41.539, "closed_count": 19},
                    },
                ],
            },
        }
        seen_by_hunter: dict = {}

        def fake_run_agent(agent: dict, agent_packet: dict, _memory: list[dict]) -> dict:
            if agent["name"] == "execution_route_hunter":
                seen_by_hunter.update(agent_packet.get("execution_route_requirement_summary") or {})
            return {
                "action": "propose_hunter_directive",
                "priority": 50,
                "title": agent["name"],
                "rationale": "unit test",
                "market_key": agent["name"],
                "evidence": {},
                "proposed_change": "unit test",
                "agent_name": agent["name"],
            }

        with mock.patch.object(llm_swarm_runner, "run_agent", side_effect=fake_run_agent):
            llm_swarm_runner.run_sequential(packet, [])

        self.assertTrue(seen_by_hunter["prepared_before_recommendation_ranking"])
        self.assertEqual(
            seen_by_hunter["labels"],
            list(llm_swarm_runner.EXECUTION_ROUTE_REQUIREMENT_LABELS),
        )
        mexc, valr = seen_by_hunter["routes"]
        self.assertEqual(mexc["labels"], {
            "borrow_availability": "available",
            "fee_pressure": "14.5",
            "margin_needs": "available",
            "api_borrow_feasibility": "observed",
        })
        self.assertEqual(valr["labels"]["borrow_availability"], "requires_borrow_confirmation")
        self.assertEqual(valr["labels"]["fee_pressure"], "fee_pressure_unmeasured")
        self.assertFalse(valr["entry_blocked"])

        hunter_output = next(
            row["recommendation"]
            for row in llm_swarm_runner.LAST_SWARM_STATE["agent_outputs"]
            if row["agent_name"] == "execution_route_hunter"
        )
        summary = hunter_output["execution_route_requirement_summary"]
        self.assertTrue(summary["read_only"])
        self.assertEqual(summary["ranking_policy"], "diagnostic_only_no_eligibility_or_quarantine_change")
        self.assertFalse(summary["hard_blocking"])
        self.assertEqual(
            hunter_output["evidence"]["execution_route_requirement_summary"],
            summary,
        )

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

    def test_malformed_agent_output_retries_once_with_schema_prompt(self) -> None:
        first = ModelResult(
            text='{"action":"propose_hunter_directive","priority":88',
            model_name="openai/gpt-5.4",
            model_tier="standard",
            prompt_tokens=10,
            completion_tokens=10,
            estimated_cost_usd=0.01,
            status="model_call:responses",
            api="responses",
            reasoning_effort="medium",
            verbosity="medium",
            structured_json=True,
        )
        second = ModelResult(
            text='{"action":"propose_hunter_directive","priority":88,"title":"Retry ok","rationale":"fixed","market_key":"OKX","evidence":{"source":"retry"},"proposed_change":{"summary":"Probe"}}',
            model_name="openai/gpt-5.4",
            model_tier="standard",
            prompt_tokens=10,
            completion_tokens=10,
            estimated_cost_usd=0.01,
            status="model_call:responses",
            api="responses",
            reasoning_effort="medium",
            verbosity="medium",
            structured_json=True,
        )
        packet = {"allowed_recommendation_actions": ["propose_hunter_directive"], "growth_experiments": []}
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "market_scout")

        with mock.patch.object(llm_swarm_runner, "complete", side_effect=[first, second]) as call:
            rec = llm_swarm_runner.run_agent(agent, packet, [])

        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.kwargs["operation"], "llm_swarm_schema_retry")
        self.assertEqual(rec["title"], "Retry ok")
        self.assertEqual(rec["retry_count"], 1)
        self.assertEqual(rec["initial_parse_status"], "truncated_json")

    def test_strategy_lab_prompt_puts_invention_evidence_before_generic_state(self) -> None:
        packet = {
            "allowed_recommendation_actions": ["propose_strategy_lab_experiment", "no_action"],
            "summary": {"closed": 100, "avg_pnl_bps": -5},
            "strategy_lab": {
                "recent": [
                    {
                        "strategy_lab_id": "existing_okx",
                        "status": "active_testing",
                        "source_surface": "perp_funding_basis",
                        "hypothesis": "Persistent OKX funding carry.",
                    }
                ]
            },
            "global_market_discovery": {
                "top_candidates": [
                    {
                        "surface_type_classified": "commodity_auction",
                        "venue_or_source": "NZX",
                        "region": "Oceania",
                    }
                ]
            },
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "strategy_lab")

        prompt = llm_swarm_runner.agent_prompt(agent, packet, [])

        self.assertIn("CURRENT INVENTION CONTEXT", prompt)
        self.assertIn("commodity_auction", prompt)
        self.assertIn("observation_program", prompt)
        self.assertNotIn("action must be allowed action", prompt)

    def test_strategy_lab_schema_retry_requires_complete_contract_and_valid_action(self) -> None:
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "strategy_lab")

        prompt = llm_swarm_runner._schema_retry_prompt(agent, '{"action":"refine"}')

        self.assertIn("propose_strategy_lab_experiment", prompt)
        self.assertIn("permitted_target_surface", prompt)
        self.assertIn("Do not use refine", prompt)

    def test_strategy_lab_run_agent_blocks_unhydrated_market_context_before_model_call(self) -> None:
        packet = {
            "allowed_recommendation_actions": ["propose_strategy_lab_experiment", "no_action"],
            "summary": {"closed": 3, "avg_pnl_bps": -2.5},
            "strategy_lab": {"recent": []},
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "strategy_lab")

        with mock.patch.object(llm_swarm_runner, "complete") as complete_call:
            rec = llm_swarm_runner.run_agent(agent, packet, [])

        complete_call.assert_not_called()
        self.assertEqual(rec["action"], "no_action")
        self.assertEqual(rec["parse_status"], "input_precondition_failed")
        self.assertEqual(rec["terminal_failure_reason"], "market_snapshot_not_hydrated")
        self.assertIn("missing_market_snapshot", rec["evidence"]["validation_errors"])
        self.assertTrue(rec["evidence"]["structured_input_logged"])
        self.assertEqual(rec["model_output_audit"]["input"]["structured_input"]["market_key"], None)
        self.assertEqual(rec["model_output_audit"]["preflight"]["status"], "failed")
        self.assertIsNotNone(
            rec["model_output_audit"]["input"]["token_budget_controls"]["max_output_tokens"]
        )

    def test_strategy_lab_run_agent_records_structured_input_audit(self) -> None:
        packet = {
            "allowed_recommendation_actions": ["propose_strategy_lab_experiment", "no_action"],
            "summary": {"closed": 40, "avg_pnl_bps": 6.5},
            "strategy_lab": {"recent": []},
            "top_reviewed": [
                {
                    "market_key": "OKX|perp_funding_basis|funding_capture_long_perp",
                    "signal_key": "okx_funding_capture",
                    "venue": "OKX",
                    "inst_id": "BTC-USDT-SWAP",
                    "trade_type": "perp_funding_basis",
                    "direction": "funding_capture_long_perp",
                    "score": 0.74,
                    "freshness_age_seconds": 45,
                    "data_status": "fresh",
                }
            ],
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "strategy_lab")
        result = ModelResult(
            text=json.dumps(
                {
                    "action": "propose_strategy_lab_experiment",
                    "priority": 92,
                    "title": "OKX carry observation",
                    "rationale": "Fresh OKX carry observations justify a paper-only experiment.",
                    "market_key": "paper.okx.funding_capture",
                    "evidence": {"signal_count": 1, "source": "top_reviewed"},
                    "proposed_change": {"summary": "Create a paper-only OKX carry experiment."},
                    "strategy_lab_experiment": {
                        "strategy_lab_id": "okx_carry_obs_v1",
                        "version": 1,
                        "experiment_type": "market_strategy",
                        "hypothesis": "Carry persists briefly after positive funding dislocations.",
                        "source_surface": "perp_funding_basis",
                        "permitted_target_surface": ["perp_funding_basis"],
                        "strategy_logic": {"type": "observation_program"},
                        "data_requirements": {"paper_only": True},
                        "risk_gates": {},
                        "promotion_rules": {},
                    },
                }
            ),
            model_name="openai/gpt-5.4",
            model_tier="standard",
            prompt_tokens=120,
            completion_tokens=90,
            estimated_cost_usd=0.02,
            status="model_call:responses",
            api="responses",
            reasoning_effort="medium",
            reasoning_mode="standard",
            verbosity="medium",
            structured_json=True,
            max_output_tokens=4000,
        )

        with mock.patch.object(llm_swarm_runner, "complete", return_value=result) as complete_call:
            rec = llm_swarm_runner.run_agent(agent, packet, [])

        complete_call.assert_called_once()
        self.assertEqual(rec["action"], "propose_strategy_lab_experiment")
        self.assertEqual(
            rec["model_output_audit"]["input"]["structured_input"]["market_key"],
            "OKX|perp_funding_basis|funding_capture_long_perp",
        )
        self.assertEqual(
            len(rec["model_output_audit"]["input"]["structured_input"]["signal_set"]),
            1,
        )
        self.assertEqual(rec["model_output_audit"]["preflight"]["status"], "ok")
        self.assertIsNotNone(
            rec["model_output_audit"]["input"]["token_budget_controls"]["max_output_tokens"]
        )
        self.assertTrue(rec["model_output_audit"]["initial"]["post_processor_schema_valid"])

    def test_build_planner_unstructured_output_is_not_fake_code_change(self) -> None:
        packet = {
            "allowed_recommendation_actions": [
                "propose_code_change",
                "propose_build_task",
            ]
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "build_planner")

        rec = llm_swarm_runner.parse_recommendation("not json", agent, packet)

        self.assertTrue(rec["_rejected"])
        self.assertEqual(rec["parse_status"], "invalid_json")
        self.assertEqual(rec["action"], "no_action")

    def test_embedded_json_is_recovered_instead_of_fallback(self) -> None:
        packet = {"allowed_recommendation_actions": ["propose_hunter_directive"]}
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "market_scout")

        rec = llm_swarm_runner.parse_recommendation(
            'Sure. {"action":"propose_hunter_directive","priority":81,"title":"Regional depth probe",'
            '"rationale":"Evidence-backed","market_key":"LUNO","evidence":{},'
            '"proposed_change":"Probe regional depth."}',
            agent,
            packet,
        )

        self.assertFalse(rec.get("_rejected", False))
        self.assertEqual(rec["parse_status"], "recovered_valid")
        self.assertEqual(rec["priority"], 81)

    def test_market_scout_prompt_includes_frontier_gap_summary(self) -> None:
        packet = {
            "allowed_recommendation_actions": ["propose_hunter_directive"],
            "frontier_gap_summary": {
                "paper_only": True,
                "frontier_candidates": 40,
                "active_paper_review_candidates": 9,
                "quote_gap_counts": {
                    "needs_same_venue_stablecoin_reference": 162,
                },
                "priority_gaps": [
                    {
                        "gap_type": "quote_adapter",
                        "reason": "missing_same_venue_stablecoin_reference",
                        "count": 162,
                        "recommended_request": "request_quote_adapter",
                    }
                ],
            },
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "market_scout")

        prompt = llm_swarm_runner.agent_prompt(agent, packet, [])

        self.assertIn("frontier_gap_summary", prompt)
        self.assertIn("missing_same_venue_stablecoin_reference", prompt)
        self.assertIn("request_quote_adapter", prompt)

    def test_non_planner_code_request_preserves_string_evidence_without_crashing(self) -> None:
        packet = {
            "allowed_recommendation_actions": [
                "propose_code_change",
                "propose_diagnostic_hypothesis",
            ]
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "red_team")

        rec = llm_swarm_runner.parse_recommendation(
            json.dumps(
                {
                    "action": "propose_code_change",
                    "priority": 85,
                    "title": "Repair malformed outcome lineage",
                    "rationale": "The current packet shows missing lineage.",
                    "market_key": "strategy_lab",
                    "evidence": "Three current-cycle records lack strategy_lab_id.",
                    "proposed_change": "Ask the build planner to repair outcome lineage.",
                }
            ),
            agent,
            packet,
        )

        self.assertFalse(rec.get("_rejected", False))
        self.assertEqual(rec["action"], "propose_diagnostic_hypothesis")
        self.assertEqual(rec["evidence"]["summary"], "Three current-cycle records lack strategy_lab_id.")
        self.assertTrue(rec["evidence"]["build_planner_required"])

    def test_truncated_outer_object_does_not_recover_nested_evidence(self) -> None:
        packet = {"allowed_recommendation_actions": ["propose_strategy_lab_experiment", "no_action"]}
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "strategy_lab")
        raw = (
            '{"action":"propose_strategy_lab_experiment","priority":92,'
            '"title":"OKX funding quality","evidence":{"avg_pnl_bps":119.8},'
            '"strategy_lab_experiment":{"strategy_lab_id":"okx_quality"'
        )

        rec = llm_swarm_runner.parse_recommendation(raw, agent, packet)

        self.assertTrue(rec["_rejected"])
        self.assertEqual(rec["parse_status"], "truncated_json")
        self.assertEqual(rec["terminal_failure_reason"], "truncated_json")

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

        self.assertTrue(rec["_rejected"])
        self.assertEqual(rec["action"], "no_action")
        self.assertEqual(rec["terminal_failure_reason"], "ungrounded_code_change")

    def test_build_planner_does_not_infer_market_growth_implementation(self) -> None:
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

        self.assertTrue(rec["_rejected"])
        self.assertEqual(rec["terminal_failure_reason"], "ungrounded_code_change")

    def test_build_planner_accepts_repo_grounded_behavioral_code_contract(self) -> None:
        packet = {
            "allowed_recommendation_actions": ["propose_code_change", "no_action"],
            "repository_grounding": {
                "source_files": [
                    {
                        "path": "src/frontier_crypto_adapter.py",
                        "symbols": ["paper_only_premarket_liquidity_gate"],
                    }
                ],
                "test_files": [{"path": "tests/test_frontier_crypto_adapter.py", "symbols": []}],
            },
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "build_planner")
        rec = llm_swarm_runner.parse_recommendation(
            json.dumps(
                {
                    "action": "propose_code_change",
                    "priority": 82,
                    "title": "Apply measured liquidity gate",
                    "rationale": "Current candidates include low-liquidity entries.",
                    "market_key": "frontier_crypto",
                    "evidence": {"used_agent_outputs": ["red_team:liquidity-failures"]},
                    "proposed_change": "Wire the measured liquidity gate into frontier candidate admission.",
                    "code_change": {
                        "change_category": "paper_scoring_logic",
                        "implementation_mode": "runtime_active",
                        "expected_files": [
                            "src/frontier_crypto_adapter.py",
                            "tests/test_frontier_crypto_adapter.py",
                        ],
                        "tests_to_run": ["python -m unittest tests/test_frontier_crypto_adapter.py"],
                        "rollback_criteria": "Revert if frontier candidate generation fails.",
                        "runtime_integration": {
                            "entrypoint_file": "src/frontier_crypto_adapter.py",
                            "entrypoint_symbol": "paper_only_premarket_liquidity_gate",
                            "invocation_path": "frontier candidate admission calls the gate before review",
                            "test_file": "tests/test_frontier_crypto_adapter.py",
                            "behavioral_test": "assert a low-liquidity candidate is rejected by normal frontier candidate admission",
                        },
                    },
                }
            ),
            agent,
            packet,
        )

        self.assertEqual(rec["action"], "propose_code_change")
        self.assertTrue(rec["recommendation_quality"]["grounded"])
        self.assertEqual(rec["recommendation_quality"]["entrypoint_symbol"], "paper_only_premarket_liquidity_gate")

    def test_explicit_no_action_is_not_rewritten_to_agent_default(self) -> None:
        packet = {"allowed_recommendation_actions": ["propose_code_change", "no_action"]}
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "build_planner")

        rec = llm_swarm_runner.parse_recommendation(
            json.dumps(
                {
                    "action": "no_action",
                    "priority": 0,
                    "title": "No grounded implementation",
                    "rationale": "The current evidence does not identify an existing consumer.",
                    "market_key": "system",
                    "evidence": {"reason": "missing_runtime_consumer"},
                    "proposed_change": "",
                }
            ),
            agent,
            packet,
        )

        self.assertTrue(rec["_rejected"])
        self.assertEqual(rec["action"], "no_action")
        self.assertEqual(rec["terminal_failure_reason"], "agent_no_action")

    def test_hold_recommendation_is_not_ranked_as_code_work(self) -> None:
        packet = {"allowed_recommendation_actions": ["propose_diagnostic_hypothesis", "no_action"]}
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "cross_market_researcher")

        rec = llm_swarm_runner.parse_recommendation(
            json.dumps(
                {
                    "action": "propose_diagnostic_hypothesis",
                    "priority": 80,
                    "title": "No trade change until schema recovers",
                    "rationale": "Re-run the researcher before changing paper behavior.",
                    "market_key": "paper.system",
                    "evidence": {
                        "market_recommendation_blocked": True,
                        "validation_error": "schema recovering",
                    },
                    "proposed_change": {"decision": "no portfolio change"},
                }
            ),
            agent,
            packet,
        )

        self.assertTrue(rec["_rejected"])
        self.assertEqual(rec["terminal_failure_reason"], "non_actionable_hold_or_rerun")

    def test_execution_route_hunter_missing_required_fields_becomes_no_action_fallback(self) -> None:
        packet = {
            "allowed_recommendation_actions": ["propose_hunter_directive", "no_action"],
            "execution_route_requirement_summary": {
                "routes": [
                    {
                        "labels": {
                            "borrow_availability": "available",
                            "fee_pressure": "14.5",
                            "margin_needs": "available",
                            "api_borrow_feasibility": "observed",
                        }
                    }
                ]
            },
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "execution_route_hunter")

        rec = llm_swarm_runner.parse_recommendation(
            json.dumps(
                {
                    "action": "propose_hunter_directive",
                    "priority": 88,
                    "title": "Missing rationale",
                    "market_key": "paper.execution_route_hunter",
                    "evidence": {"paper_safe_route": {"route_status": "paper_testable_proxy"}},
                    "proposed_change": {"summary": "adjust route"},
                }
            ),
            agent,
            packet,
        )

        self.assertTrue(rec["_rejected"])
        self.assertEqual("no_action", rec["action"])
        self.assertEqual(
            "missing_required_fields:rationale",
            rec["terminal_failure_reason"],
        )

    def test_execution_route_hunter_requires_explicit_paper_safe_route_with_route_context(self) -> None:
        packet = {
            "allowed_recommendation_actions": ["propose_hunter_directive", "no_action"],
            "execution_route_requirement_summary": {
                "routes": [
                    {
                        "labels": {
                            "borrow_availability": "requires_borrow_confirmation",
                            "fee_pressure": "fee_pressure_unmeasured",
                            "margin_needs": "margin_needs_confirmation",
                            "api_borrow_feasibility": "requires_api_and_borrow_confirmation",
                        }
                    }
                ]
            },
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "execution_route_hunter")

        rec = llm_swarm_runner.parse_recommendation(
            json.dumps(
                {
                    "action": "propose_hunter_directive",
                    "priority": 88,
                    "title": "Route review",
                    "rationale": "Change the route after validation.",
                    "market_key": "paper.execution_route_hunter",
                    "evidence": {"issue": "route costs changed"},
                    "proposed_change": {"summary": "adjust route"},
                }
            ),
            agent,
            packet,
        )

        self.assertTrue(rec["_rejected"])
        self.assertEqual("no_action", rec["action"])
        self.assertEqual(
            "missing_explicit_paper_safe_route",
            rec["terminal_failure_reason"],
        )
        self.assertTrue(rec["evidence"]["explicit_paper_safe_route_required"])

    def test_execution_route_hunter_accepts_explicit_paper_safe_route(self) -> None:
        packet = {
            "allowed_recommendation_actions": ["propose_hunter_directive", "no_action"],
            "execution_route_requirement_summary": {
                "routes": [
                    {
                        "labels": {
                            "borrow_availability": "available",
                            "fee_pressure": "14.5",
                            "margin_needs": "available",
                            "api_borrow_feasibility": "observed",
                        }
                    }
                ]
            },
        }
        agent = next(row for row in llm_swarm_runner.AGENTS if row["name"] == "execution_route_hunter")

        rec = llm_swarm_runner.parse_recommendation(
            json.dumps(
                {
                    "action": "propose_hunter_directive",
                    "priority": 88,
                    "title": "Use validated paper proxy route",
                    "rationale": "A maintained paper proxy route is already validated.",
                    "market_key": "paper.execution_route_hunter",
                    "evidence": {
                        "paper_safe_route": {
                            "route_id": "okx_derivatives_paper",
                            "route_status": "paper_testable_proxy",
                            "paper_only": True,
                        }
                    },
                    "proposed_change": {"summary": "review the validated proxy route"},
                }
            ),
            agent,
            packet,
        )

        self.assertFalse(rec.get("_rejected", False))
        self.assertEqual("propose_hunter_directive", rec["action"])

    def test_sequential_swarm_passes_prior_outputs_to_later_agents(self) -> None:
        seen_counts: list[int] = []

        def fake_run_agent(agent: dict, packet: dict, _memory: list[dict]) -> dict:
            seen_counts.append(len(packet.get("current_cycle_recommendations") or []))
            return {
                "action": "propose_hunter_directive",
                "priority": 50,
                "title": agent["name"],
                "rationale": "unit test",
                "market_key": agent["name"],
                "evidence": {},
                "proposed_change": "unit test",
                "agent_name": agent["name"],
            }

        with mock.patch.object(llm_swarm_runner, "run_agent", side_effect=fake_run_agent):
            recs = llm_swarm_runner.run_sequential({"allowed_recommendation_actions": ["propose_hunter_directive"]}, [])

        self.assertEqual(len(recs), len(llm_swarm_runner.AGENTS))
        self.assertEqual(seen_counts, list(range(len(llm_swarm_runner.AGENTS))))
        self.assertEqual(llm_swarm_runner.LAST_SWARM_STATE["collaboration_mode"], llm_swarm_runner.FALLBACK_COLLABORATION_MODE)

    def test_build_planner_receives_repo_grounding_from_prior_outputs(self) -> None:
        planner_grounding: dict = {}

        def fake_run_agent(agent: dict, packet: dict, _memory: list[dict]) -> dict:
            if agent["name"] == "build_planner":
                planner_grounding.update(packet.get("repository_grounding") or {})
            return {
                "action": "propose_hunter_directive",
                "priority": 60,
                "title": f"Inspect frontier quality in {agent['name']}",
                "rationale": "Use frontier candidate evidence and the existing scanner.",
                "market_key": "frontier_crypto",
                "evidence": {},
                "proposed_change": "Inspect frontier quality coverage.",
                "agent_name": agent["name"],
            }

        with mock.patch.object(llm_swarm_runner, "run_agent", side_effect=fake_run_agent):
            llm_swarm_runner.run_sequential(
                {"allowed_recommendation_actions": ["propose_hunter_directive", "no_action"]},
                [],
            )

        self.assertTrue(planner_grounding["source_files"])
        self.assertTrue(any(row["path"].startswith("src/") for row in planner_grounding["source_files"]))
        self.assertEqual(len(planner_grounding["resolved_from"]), len(llm_swarm_runner.AGENTS) - 1)

    def test_langgraph_swarm_builds_ranked_action_package(self) -> None:
        calls: list[str] = []

        def fake_run_agent(agent: dict, packet: dict, _memory: list[dict]) -> dict:
            calls.append(agent["name"])
            return {
                "action": "propose_hunter_directive",
                "priority": 60 + len(calls),
                "title": agent["name"],
                "rationale": "unit test",
                "market_key": agent["name"],
                "evidence": {},
                "proposed_change": "unit test",
                "agent_name": agent["name"],
                "model": {"status": "model_call:test", "tier": "fast", "estimated_cost_usd": 0.0},
            }

        packet = {"allowed_recommendation_actions": ["propose_hunter_directive"]}
        with mock.patch.object(llm_swarm_runner, "run_agent", side_effect=fake_run_agent):
            recs = llm_swarm_runner.run_langgraph_if_available(packet, [])

        self.assertCountEqual(calls, [agent["name"] for agent in llm_swarm_runner.AGENTS])
        self.assertEqual(llm_swarm_runner.LAST_SWARM_STATE["collaboration_mode"], llm_swarm_runner.COLLABORATION_MODE)
        trace_nodes = [item["node"] for item in llm_swarm_runner.LAST_SWARM_STATE["graph_trace"]]
        self.assertLess(trace_nodes.index("research_join"), trace_nodes.index("strategy_lab"))
        self.assertLess(trace_nodes.index("critique_join"), trace_nodes.index("build_planner"))
        self.assertLess(trace_nodes.index("ranker"), trace_nodes.index("memory_checkpoint"))
        self.assertEqual(recs[0]["title"], "build_planner")

    def test_langgraph_ranker_coerces_label_priorities(self) -> None:
        by_agent = {
            "market_scout": {
                "action": "propose_hunter_directive",
                "priority": "high",
                "title": "High label",
                "rationale": "unit test",
                "market_key": "LABEL",
                "evidence": {},
                "proposed_change": "Probe",
                "agent_name": "market_scout",
            },
            "cross_market_researcher": {
                "action": "propose_hunter_directive",
                "priority": 70,
                "title": "Numeric",
                "rationale": "unit test",
                "market_key": "NUMERIC",
                "evidence": {},
                "proposed_change": "Probe",
                "agent_name": "cross_market_researcher",
            },
        }
        by_agent.update(
            {
                agent["name"]: {
                "action": "propose_hunter_directive",
                "priority": 40,
                "title": f"low {idx}",
                "rationale": "unit test",
                "market_key": f"LOW{idx}",
                "evidence": {},
                "proposed_change": "Probe",
                "agent_name": agent["name"],
            }
            for idx, agent in enumerate(llm_swarm_runner.AGENTS[2:], start=1)
            }
        )

        with mock.patch.object(
            llm_swarm_runner,
            "run_agent",
            side_effect=lambda agent, _packet, _memory: by_agent[agent["name"]],
        ):
            recs = llm_swarm_runner.run_langgraph_if_available(
                {"allowed_recommendation_actions": ["propose_hunter_directive"]},
                [],
            )

        self.assertEqual(recs[0]["title"], "High label")
        self.assertEqual(recs[0]["priority"], 90)

    def test_red_team_can_reject_prior_market_idea(self) -> None:
        packet = {"allowed_recommendation_actions": ["propose_hunter_directive", "propose_diagnostic_hypothesis"]}
        scout = {
            "action": "propose_hunter_directive",
            "priority": 90,
            "title": "Scout bad market",
            "rationale": "unit test",
            "market_key": "BAD_MARKET",
            "evidence": {},
            "proposed_change": "Probe",
            "agent_name": "market_scout",
        }
        red_team = {
            "action": "propose_diagnostic_hypothesis",
            "priority": 95,
            "title": "Reject bad market",
            "rationale": "unit test",
            "market_key": "red_team",
            "evidence": {"reject_market_keys": ["BAD_MARKET"]},
            "proposed_change": "Reject",
            "agent_name": "red_team",
        }
        by_agent = {
            "market_scout": scout,
            "cross_market_researcher": {**scout, "agent_name": "cross_market_researcher", "market_key": "OTHER"},
            "strategy_lab": {**scout, "agent_name": "strategy_lab", "market_key": "LAB"},
            "red_team": red_team,
            "execution_route_hunter": {**scout, "agent_name": "execution_route_hunter", "market_key": "ROUTE"},
            "build_planner": {**scout, "agent_name": "build_planner", "market_key": "BUILD"},
        }

        with mock.patch.object(
            llm_swarm_runner,
            "run_agent",
            side_effect=lambda agent, _packet, _memory: by_agent[agent["name"]],
        ):
            recs = llm_swarm_runner.run_langgraph_if_available(packet, [])

        self.assertNotIn("BAD_MARKET", {rec["market_key"] for rec in recs})
        rejected_reasons = [row["reason"] for row in llm_swarm_runner.LAST_SWARM_STATE["rejected_actions"]]
        self.assertTrue(any("rejected_by_red_team" in reason for reason in rejected_reasons))

    def test_same_cycle_duplicate_recommendations_are_suppressed(self) -> None:
        packet = {"allowed_recommendation_actions": ["propose_hunter_directive"]}
        duplicate = {
            "action": "propose_hunter_directive",
            "priority": 80,
            "title": "Duplicate",
            "rationale": "unit test",
            "market_key": "DUP",
            "evidence": {},
            "proposed_change": "Probe",
            "agent_name": "market_scout",
        }
        with mock.patch.object(llm_swarm_runner, "run_agent", return_value=duplicate):
            recs = llm_swarm_runner.run_langgraph_if_available(packet, [])

        self.assertEqual(len(recs), 1)
        self.assertTrue(any(row["reason"] == "duplicate_same_cycle" for row in llm_swarm_runner.LAST_SWARM_STATE["rejected_actions"]))

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

                llm_swarm_runner.write_recommendations(
                    [
                        {
                            "action": "propose_hunter_directive",
                            "title": "ok",
                            "priority": 80,
                            "market_key": "OK",
                            "evidence": {},
                            "proposed_change": "Probe",
                            "agent_name": "market_scout",
                        }
                    ],
                    10,
                    settings={"llm_swarm": {"write_fallback_recommendations_to_inbox": False}},
                    swarm_state={
                        "collaboration_mode": llm_swarm_runner.COLLABORATION_MODE,
                        "graph_trace": [{"node": "ranker"}],
                        "agent_outputs": [{"agent_name": "market_scout"}],
                        "critiques": [{"agent_name": "red_team"}],
                        "rejected_actions": [{"reason": "duplicate_same_cycle"}],
                    },
                )
                latest = json.loads((pathlib.Path(tmp) / "llm_swarm_latest.json").read_text(encoding="utf-8"))
                self.assertEqual(latest["collaboration_mode"], llm_swarm_runner.COLLABORATION_MODE)
                self.assertIn("graph_trace", latest)
                self.assertIn("agent_outputs", latest)
                self.assertIn("critiques", latest)
                self.assertIn("ranked_actions", latest)
                self.assertIn("rejected_actions", latest)
            finally:
                llm_swarm_runner.INBOX = old_inbox
                llm_swarm_runner.RUNS_DIR = old_runs

    def test_write_recommendations_publishes_strict_schema_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_inbox = llm_swarm_runner.INBOX
            old_runs = llm_swarm_runner.RUNS_DIR
            try:
                llm_swarm_runner.RUNS_DIR = pathlib.Path(tmp)
                llm_swarm_runner.INBOX = pathlib.Path(tmp) / "llm_recommendations_inbox.jsonl"
                llm_swarm_runner.write_recommendations(
                    [
                        {
                            "action": "spawn_agent",
                            "priority": 84,
                            "title": "Create recurring strategy lab specialist",
                            "rationale": "Recurring strategy-lab work should become a persistent paper-only specialist.",
                            "market_key": "strategy_lab_recommendation",
                            "evidence": {"dynamic_agent_objective": "Own recurring strategy_lab recommendation work."},
                            "proposed_change": "Register the specialist for the next cycle.",
                            "agent_spec": {
                                "name": "strategy_lab_specialist",
                                "objective": "Own recurring strategy-lab work and turn it into paper-testable artifacts.",
                                "parent_agent_id": "strategy_lab",
                                "triggers": {"any_packet_paths": ["strategy_lab"], "cooldown_minutes": 60},
                                "evidence_inputs": ["strategy_lab", "horizon_outcomes"],
                                "memory_policy": {
                                    "namespaces": ["strategies", "outcomes", "recommendations"],
                                    "keywords": ["strategy lab", "recommendation"],
                                    "retrieval_limit": 24,
                                },
                                "model_tier": "standard",
                                "allowed_actions": ["propose_strategy_lab_experiment", "propose_code_change", "spawn_agent"],
                                "success_measure": {"primary": "paper_candidates_generated"},
                            },
                            "agent_name": "novel_strategy_discovery_specialist",
                            "dynamic_agent_id": "agent_123",
                            "parse_status": "native_valid",
                            "provenance": {"state_packet": "runs/llm_state_packet.json"},
                            "model": {"status": "model_call:test"},
                        }
                    ],
                    10,
                    settings={"llm_swarm": {"write_fallback_recommendations_to_inbox": False}},
                )

                published = json.loads(llm_swarm_runner.INBOX.read_text(encoding="utf-8").strip())
                self.assertEqual(
                    set(published),
                    {
                        "action",
                        "priority",
                        "title",
                        "rationale",
                        "market_key",
                        "evidence",
                        "proposed_change",
                        "agent_spec",
                    },
                )
                self.assertEqual("spawn_agent", published["action"])
                self.assertTrue(published["market_key"].startswith("paper."))
                self.assertEqual(
                    published["proposed_change"],
                    {"summary": "Register the specialist for the next cycle."},
                )
                self.assertNotIn("agent_name", published)
                self.assertNotIn("model", published)
                self.assertNotIn("dynamic_agent_id", published)
            finally:
                llm_swarm_runner.INBOX = old_inbox
                llm_swarm_runner.RUNS_DIR = old_runs

    def test_write_recommendations_suppresses_missing_action_payload_with_schema_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_inbox = llm_swarm_runner.INBOX
            old_runs = llm_swarm_runner.RUNS_DIR
            try:
                llm_swarm_runner.RUNS_DIR = pathlib.Path(tmp)
                llm_swarm_runner.INBOX = pathlib.Path(tmp) / "llm_recommendations_inbox.jsonl"
                llm_swarm_runner.write_recommendations(
                    [
                        {
                            "action": "propose_strategy_lab_experiment",
                            "priority": 79,
                            "title": "Recurring strategy lab idea",
                            "rationale": "A strict publish contract should not release a strategy-lab action without its experiment payload.",
                            "market_key": "strategy_lab_recommendation",
                            "evidence": {"dynamic_agent_objective": "Own recurring strategy_lab recommendation work."},
                            "proposed_change": {"summary": "Try again with a complete experiment object."},
                            "agent_name": "novel_strategy_discovery_specialist",
                        }
                    ],
                    10,
                    settings={"llm_swarm": {"write_fallback_recommendations_to_inbox": False}},
                )

                self.assertEqual("", llm_swarm_runner.INBOX.read_text(encoding="utf-8"))
                latest = json.loads((pathlib.Path(tmp) / "llm_swarm_latest.json").read_text(encoding="utf-8"))
                self.assertEqual([], latest["recommendations"])
                self.assertEqual("no_action", latest["suppressed_recommendations"][0]["action"])
                self.assertEqual(
                    "missing_required_action_payload:strategy_lab_experiment",
                    latest["suppressed_recommendations"][0]["evidence"]["schema_violation"],
                )
            finally:
                llm_swarm_runner.INBOX = old_inbox
                llm_swarm_runner.RUNS_DIR = old_runs

    def test_post_model_database_lock_preserves_generated_recommendations(self) -> None:
        llm_swarm_runner.LAST_SWARM_STATE = {"ranked_actions": [{"title": "paid result"}]}
        with mock.patch.object(
            llm_swarm_runner,
            "connect",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            llm_swarm_runner._record_post_model_state({}, "cycle:test", {"matched_agents": []})

        deferred = llm_swarm_runner.LAST_SWARM_STATE["post_model_persistence"]
        self.assertEqual("database_busy_retry_later", deferred["status"])
        self.assertEqual([{"title": "paid result"}], llm_swarm_runner.LAST_SWARM_STATE["ranked_actions"])


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

