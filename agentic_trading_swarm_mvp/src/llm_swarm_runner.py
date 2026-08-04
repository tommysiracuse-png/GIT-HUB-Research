#!/usr/bin/env python3
"""Collaborative six-agent cost-aware LLM swarm.

Uses LangGraph when installed. If it is absent, runs the same five agent nodes
sequentially. All model calls go through cost_router, which defaults to a
zero-cost fallback unless RADAR_USE_LITELLM=1 is set. The default policy is
mini-first with earned standard/frontier escalation.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import operator
import pathlib
import re
import sqlite3
import sys
import time
from typing import Annotated, Any, TypedDict

from cost_router import complete
from evolution.builder_context import resolve_repo_targets
from llm_bridge import INBOX, STATE_JSON
from memory_graph import (
    build_swarm_memory,
    query_memory,
    reflect_swarm,
    sync_graphiti,
    write_memory_exports,
)
from settings import load_settings
from storage import RUNS_DIR, connect


COLLABORATION_MODE = "langgraph_typed_action_package"
FALLBACK_COLLABORATION_MODE = "sequential_typed_action_package"
LAST_SWARM_STATE: dict[str, Any] = {}
ROOT = pathlib.Path(__file__).resolve().parents[1]


class SwarmState(TypedDict, total=False):
    packet: dict
    memory: list[dict]
    role_memory: dict[str, list[dict]]
    checkpoint_context: dict
    memory_context_counts: dict[str, int]
    cycle_id: str
    agent_outputs: Annotated[list[dict], operator.add]
    critiques: Annotated[list[dict], operator.add]
    node_rejections: Annotated[list[dict], operator.add]
    ranked_actions: list[dict]
    rejected_actions: list[dict]
    graph_trace: Annotated[list[dict], operator.add]
    collaboration_mode: str
    checkpoint: dict
    memory_reflection: dict


AGENTS = [
    {
        "name": "market_scout",
        "role": "Find new markets, weird assets, underserved venues, frontier regions, and data gaps.",
        "default_action": "request_market_adapter",
        "base_tier": "fast",
        "standard_escalation_reason": "Market expansion has broad coverage gaps; use standard reasoning before any frontier spend.",
        "frontier_escalation_reason": "High-value market expansion or severe frontier quality gap needs deeper reasoning.",
    },
    {
        "name": "cross_market_researcher",
        "role": "Infer causal chains and explain why reliable signal outcomes differ across market contexts.",
        "default_action": "propose_diagnostic_hypothesis",
        "base_tier": "fast",
        "standard_escalation_reason": "Cross-market causal analysis has enough live evidence to justify standard reasoning.",
        "frontier_escalation_reason": "Cross-market causal inference is a high-value frontier reasoning task.",
    },
    {
        "name": "strategy_lab",
        "role": "Invent new paper-testable strategy hypotheses that can become tracked Strategy Lab experiments.",
        "default_action": "propose_strategy_lab_experiment",
        "base_tier": "standard",
        "standard_escalation_reason": "Inventing new strategy hypotheses needs stronger reasoning than routine classification.",
        "frontier_escalation_reason": "Novel strategy invention from live market evidence is a high-value frontier reasoning task.",
    },
    {
        "name": "red_team",
        "role": "Diagnose losing or decaying signal families using reliable horizon labels and propose testable causal hypotheses.",
        "default_action": "propose_diagnostic_hypothesis",
        "base_tier": "fast",
        "standard_escalation_reason": "Signal failure diagnosis has enough reliable labels to justify standard reasoning.",
        "frontier_escalation_reason": "Signal failure root-cause analysis and tail-risk diagnosis require frontier reasoning.",
    },
    {
        "name": "execution_route_hunter",
        "role": "Find practical route requirements for conditional opportunities: brokers, permissions, borrow, fees, margin, APIs.",
        "default_action": "propose_build_task",
        "base_tier": "fast",
        "standard_escalation_reason": "Route blockers affect many paper opportunities; standard route reasoning is justified.",
        "frontier_escalation_reason": "Route blockers affect many paper opportunities and need deeper route reasoning.",
    },
    {
        "name": "build_planner",
        "role": "Convert evidence-backed hypotheses into bounded signal variants or paper-only code-evolution proposals.",
        "default_action": "propose_code_change",
        "base_tier": "fast",
        "standard_escalation_reason": "Build planning should use standard reasoning only after concrete tasks or growth evidence exist.",
        "frontier_escalation_reason": "Build planning for autonomous paper-only evolution requires frontier coding/reasoning.",
    },
]


def load_state_packet(path: pathlib.Path = STATE_JSON) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing LLM state packet: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def agent_prompt(agent: dict, packet: dict, memory: list[dict]) -> str:
    compact = {
        "summary": packet.get("summary"),
        "execution_summary": packet.get("execution_summary"),
        "llm_cost_summary": packet.get("llm_cost_summary"),
        "buckets": packet.get("buckets"),
        "top_reviewed": packet.get("top_reviewed", [])[:10],
        "horizon_outcomes": packet.get("horizon_outcomes", [])[:20],
        "contextual_stats": packet.get("contextual_stats", [])[:20],
        "crypto_venue_health": packet.get("crypto_venue_health", [])[:10],
        "frontier_crypto_venues": packet.get("frontier_crypto_venues", {}),
        "expansion_map": packet.get("expansion_map", {}),
        "route_intelligence": (packet.get("expansion_map", {}) or {}).get("route_intelligence", {}),
        "prediction_markets": (packet.get("expansion_map", {}) or {}).get("prediction_markets", {}),
        "hunter_directives": packet.get("hunter_directives", [])[:10],
        "growth_experiments": packet.get("growth_experiments", [])[:10],
        "improvement_tasks": packet.get("improvement_tasks", [])[:10],
        "signal_redesign": packet.get("signal_redesign", {}),
        "okx_signal_research": packet.get("okx_signal_research", {}),
        "strategy_reliability": packet.get("strategy_reliability", {}),
        "strategy_lab": packet.get("strategy_lab", {}),
        "self_improvement": packet.get("self_improvement", {}),
        "code_evolution": packet.get("code_evolution", {}),
        "agent_memory": packet.get("agent_memory", {}),
        "relevant_long_term_memory": memory,
        "allowed_actions": packet.get("allowed_recommendation_actions", []),
        "current_cycle_agent_outputs": packet.get("current_cycle_agent_outputs", [])[:10],
        "current_cycle_critiques": packet.get("current_cycle_critiques", [])[:10],
        "current_cycle_ranked_actions": packet.get("current_cycle_ranked_actions", [])[:10],
        "repository_grounding": packet.get("repository_grounding", {}),
    }
    build_planner_instruction = ""
    if agent["name"] == "build_planner":
        build_planner_instruction = (
            "As build_planner, prefer propose_code_change when code_evolution is enabled and no code change is "
            "currently workspace_applied_probation. You are allowed to evolve the paper-only system, including fixing prior "
            "generated code and wiring useful pieces into the running loop. Favor runtime evolution over orphan "
            "helper modules: "
            "wire reports/helpers into the radar loop or LLM packet, improve public-data adapters/parsers, add "
            "feature-flagged paper scoring or signal variants, improve self-improvement policies with TTL/revert "
            "logic, or repair prior generated code that was incomplete. Avoid broad rewrites, but do not limit "
            "yourself to read-only dashboard scaffolding when a bounded runtime integration is the better fix. "
            "For market coverage, depth enrichment, venue quotas, candidate caps, public adapter wiring, and "
            "market-tested reporting, set code_change.implementation_mode='runtime_active'. Use 'shadow_trial' "
            "only for uncertain new signal logic, not for basic data expansion. "
            "Use memory and reporting to keep the evolution legible: every autonomous change should leave an "
            "auditable report/state-packet trace of what changed and why. "
            "Expected files must be concrete repo paths under src/, tests/, config/, or docs files. "
            "Use repository_grounding to select exact existing files and symbols. For every propose_code_change, "
            "code_change.runtime_integration must name an existing entrypoint_file, an existing entrypoint_symbol, "
            "how the new behavior is invoked, and a behavioral_test that proves the running consumer uses it. "
            "A test that only imports a new module or checks that a constant/function exists is not sufficient. "
            "Cite the prior agent outputs used in evidence.used_agent_outputs. If you cannot ground the change in "
            "an existing runtime consumer, return action='no_action'; do not invent directories or emit a thin build task. "
            "If a prior generated patch was blocked for malformed diff or test failure, propose a narrower "
            "code change that fixes the failure or makes the previous generated work actually usable.\n"
        )
    return (
        f"You are {agent['name']}. Role: {agent['role']}\n"
        f"{build_planner_instruction}"
        "Return exactly one JSON object matching this schema:\n"
        "{"
        "\"action\": allowed action, "
        "\"priority\": integer 1-100, "
        "\"title\": short title, "
        "\"rationale\": reason, "
        "\"market_key\": optional market, "
        "\"signal_key\": optional signal, "
        "\"evidence\": object, "
        "\"frontier_escalation_reason\": required if a frontier model is used, "
        "\"proposed_change\": concrete bounded proposal, "
        "\"variant_config\": optional bounded config for propose_signal_variant, "
        "\"strategy_lab_experiment\": optional object for propose_strategy_lab_experiment, "
        "\"code_change\": optional object for propose_code_change"
        "}\n"
        "For propose_strategy_lab_experiment, emit a strategy_lab_experiment object with: "
        "strategy_lab_id, experiment_type, hypothesis, strategy_logic, data_requirements, "
        "risk_gates, and promotion_rules. experiment_type must be one of market_strategy, "
        "risk_filter, execution_filter, system_repair, or reporting_quality. Use "
        "market_strategy only for actual reusable trading hypotheses; use the other types for "
        "filters, route/execution gates, output repairs, or report-quality experiments. "
        "Runtime supports strategy_logic.type='candidate_filter' over "
        "existing candidate fields: venues, trade_types, directions, regions, asset_classes, "
        "min_edge_bps, min_score, min_liquidity_score, max_spread_bps, min_quality_score, "
        "max_stale_minutes, required_fields, max_candidates_per_loop, score_bonus, and "
        "edge_bonus_bps. Every paper-testable contract must scope at least one trade_type, venue, "
        "direction, region, or asset_class. Set allow_any_surface=true only when the hypothesis is "
        "deliberately cross-surface and its behavioral gates are explicit. trade_types are scanner families such as frontier_crypto_venue_map, "
        "perp_funding_basis, global_market_discovery_proxy, global_proxy_momentum, and "
        "prediction_market_probability. directions are trade actions such as long_frontier_spot, "
        "short_frontier_spot, long_proxy, short_proxy, funding_capture_long_perp, "
        "long_perp_short_spot, yes, or no. Do not put a direction in trade_types. "
        "For genuinely new alpha, prefer strategy_logic.type='observation_program'. It operates on "
        "raw normalized observations and stored five-minute history, so it does not require an existing "
        "scanner candidate. Supply universe (optional venues, inst_ids, trade_types, asset_classes, regions, "
        "market_types, quotes, or bases), calculated_features as named safe expressions, entry_expression, "
        "optional invalidation_expression, either direction='long'/'short' or long_expression and "
        "short_expression, edge_expression, score_expression, and route_surface='auto', 'spot', 'perp', "
        "'proxy', or 'prediction'. Available features include returns and momentum at 5m/15m/60m/4h/1d, "
        "60m/4h volatility and price z-scores, relative strength, spread, liquidity, quality, funding, "
        "basis, and cross-venue dislocation. Expressions may use arithmetic, comparisons, boolean logic, "
        "abs/min/max/round/sqrt/log/log1p/clip, and previously declared calculated features. Invent reusable "
        "market behavior; do not merely rename or filter an existing strategy. Missing feature names are "
        "valid research intent and will automatically become code-evolution proposals. "
        "A good Strategy Lab idea should describe a reusable condition or sub-strategy, not only "
        "one ticker/name. If you include venue names, also include behavioral gates such as route "
        "status, quality, depth, liquidity, spread, freshness, carry economics, regime, or outcome "
        "evidence so the evaluator can decide whether the idea generalizes or should split. "
        "Use this to create diverse sub-strategies that can be paper-tested "
        "before hard-coding. If the idea needs new data or a new formula outside this "
        "contract, ask for a code change or adapter instead of pretending it can run now.\n"
        "For propose_signal_variant, variant_config must contain exactly: "
        "reference_grouping, estimator='median', leave_one_out, min_unique_venues, "
        "min_dislocation_bps, max_spread_bps, min_liquidity_score, direction_mode, "
        "fee_bps_per_side, slippage_bps_per_side. Do not include code.\n"
        "When the evidence points to a clear paper-only implementation, prefer propose_code_change "
        "over a generic task/spec. The system is allowed to evolve itself through the Build Governor: "
        "fix prior generated code, wire useful helpers into runtime reports or the LLM packet, improve "
        "public adapters/parsers, add feature-flagged paper scoring, or improve policy/variant logic. "
        "Make code proposals narrow enough to pass tests, but substantial enough to affect the running "
        "paper system rather than creating unused helper files.\n"
        "For propose_code_change, include change_category, expected_files, tests_to_run, "
        "rollback_criteria, evidence, implementation_mode, runtime_integration, and optionally unified_diff. "
        "runtime_integration requires entrypoint_file, entrypoint_symbol, invocation_path, test_file, and "
        "behavioral_test. Allowed categories are "
        "runtime_pipeline_integration, public_data_adapter, parser_improvement, scanner_expansion, "
        "paper_signal_variant, paper_scoring_logic, self_improvement_policy, evolution_loop_improvement, "
        "report_dashboard, llm_prompt_state_packet, quality_scoring, "
        "read_only_route_intelligence, tests_fixtures. Code changes must be paper-only and "
        "Build-Governor bounded. Implementation modes are runtime_active, paper_policy, shadow_trial, and "
        "report_only; market-expansion work should normally be runtime_active.\n"
        "Do not place trades, enable live trading, add credentials, install dependencies, "
        "change startup/system tasks, or request unrestricted code mutation.\n"
        "When current-cycle outputs are present, explicitly cite which earlier agent output you used, "
        "refined, or rejected. Red-team weak ideas instead of repeating them.\n"
        "A hold, no-change conclusion, malformed-input warning, or request to rerun an agent is not an actionable "
        "recommendation. Return action='no_action' for those cases. Only use your default action when you have a "
        "concrete, evidence-backed next step.\n\n"
        f"STATE:\n{json.dumps(compact, sort_keys=True)}"
    )


def parse_recommendation(text: str, agent: dict, packet: dict) -> dict:
    allowed = set(packet.get("allowed_recommendation_actions", []))
    rec, parse_status, reason = _parse_json_recommendation(text)
    if rec is None:
        return _reject_recommendation(agent, text, parse_status, reason)
    rec["parse_status"] = parse_status
    if rec.get("action") == "no_action":
        return _reject_recommendation(
            agent,
            str(rec.get("rationale") or rec.get("title") or "Agent selected no action."),
            parse_status,
            "agent_no_action",
        )
    if rec.get("action") not in allowed:
        return _reject_recommendation(agent, text, "invalid_action", "action_not_allowed")
    if _describes_no_change(rec):
        return _reject_recommendation(
            agent,
            str(rec.get("rationale") or rec.get("proposed_change") or "Recommendation describes no change."),
            parse_status,
            "non_actionable_hold_or_rerun",
        )
    if rec.get("action") == "propose_code_change":
        if agent["name"] != "build_planner":
            rec["action"] = agent["default_action"]
            rec.setdefault("evidence", {})["build_planner_required"] = True
            rec["evidence"]["original_action"] = "propose_code_change"
            rec.pop("code_change", None)
            shaped = rec
        else:
            shaped = _shape_actionable_code_change(rec, agent, packet)
        if shaped:
            rec = shaped
        else:
            return _reject_recommendation(
                agent,
                str(rec.get("rationale") or rec.get("proposed_change") or "Ungrounded code proposal."),
                parse_status,
                "ungrounded_code_change",
            )
    rec["priority"] = _coerce_priority(rec.get("priority"), default=50)
    rec.setdefault("title", f"{agent['name']} recommendation")
    rec.setdefault("rationale", "Generated by LLM swarm.")
    rec.setdefault("market_key", agent["name"])
    rec.setdefault("evidence", {})
    rec.setdefault("proposed_change", rec.get("rationale", "Review recommendation."))
    rec["agent_name"] = agent["name"]
    rec["provenance"] = {
        "state_packet": str(STATE_JSON),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    return rec


def _shape_actionable_code_change(rec: dict, agent: dict, packet: dict) -> dict | None:
    """Validate a Build Planner proposal without inventing implementation details."""
    code_change = rec.get("code_change") if isinstance(rec.get("code_change"), dict) else {}
    category = code_change.get("change_category") or rec.get("change_category") or rec.get("category")
    expected_files = code_change.get("expected_files") or rec.get("expected_files") or []
    integration = code_change.get("runtime_integration")
    evidence = rec.get("evidence") if isinstance(rec.get("evidence"), dict) else {}
    used_outputs = evidence.get("used_agent_outputs")
    if not category or not isinstance(expected_files, list) or not expected_files:
        return None
    if not isinstance(used_outputs, list) or not used_outputs:
        return None
    if not isinstance(integration, dict):
        return None
    required = ("entrypoint_file", "entrypoint_symbol", "invocation_path", "test_file", "behavioral_test")
    if any(not str(integration.get(field) or "").strip() for field in required):
        return None
    entrypoint_file = _normalize_repo_path(integration["entrypoint_file"])
    test_file = _normalize_repo_path(integration["test_file"])
    expected_files = [_normalize_repo_path(path) for path in expected_files]
    if entrypoint_file not in expected_files or test_file not in expected_files:
        return None
    if not _existing_symbol(entrypoint_file, str(integration["entrypoint_symbol"])):
        return None
    grounding = packet.get("repository_grounding") if isinstance(packet.get("repository_grounding"), dict) else {}
    grounded_paths = {
        str(item.get("path") or "")
        for key in ("source_files", "test_files")
        for item in grounding.get(key, [])
        if isinstance(item, dict)
    }
    if grounded_paths and entrypoint_file not in grounded_paths:
        return None
    code_change["change_category"] = category
    code_change["expected_files"] = expected_files
    code_change["runtime_integration"] = {**integration, "entrypoint_file": entrypoint_file, "test_file": test_file}
    code_change.setdefault("evidence", evidence)
    result = {**rec, "code_change": code_change}
    result["recommendation_quality"] = {
        "grounded": True,
        "entrypoint_file": entrypoint_file,
        "entrypoint_symbol": integration["entrypoint_symbol"],
        "behavioral_test": integration["behavioral_test"],
    }
    return result


def _normalize_repo_path(value: object) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("./")


def _existing_symbol(relative_path: str, symbol: str) -> bool:
    path = ROOT / relative_path
    if not path.is_file() or path.suffix != ".py":
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol
        for node in ast.walk(tree)
    )


def _describes_no_change(rec: dict) -> bool:
    text = " ".join(
        str(rec.get(field) or "")
        for field in ("title", "rationale", "proposed_change")
    ).lower()
    return any(
        phrase in text
        for phrase in (
            "no trade change",
            "no portfolio change",
            "keep current settings unchanged",
            "re-run the researcher",
            "rerun the researcher",
            "code idea needs shaping",
        )
    )


def _coerce_priority(value: Any, default: int = 50) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return max(1, min(100, int(value)))
    text = str(value or "").strip().lower()
    if not text:
        return default
    labels = {
        "critical": 100,
        "urgent": 95,
        "highest": 95,
        "high": 90,
        "medium_high": 80,
        "medium-high": 80,
        "medium": 60,
        "normal": 50,
        "low": 35,
        "lowest": 15,
    }
    if text in labels:
        return labels[text]
    try:
        return max(1, min(100, int(float(text))))
    except (TypeError, ValueError):
        return default


def _as_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _route_blocker_count(packet: dict) -> int:
    route_intel = ((packet.get("expansion_map") or {}).get("route_intelligence") or {})
    blockers = (
        route_intel.get("blockers")
        or route_intel.get("blocker_counts")
        or route_intel.get("by_blocker")
        or route_intel.get("by_missing_requirement")
        or {}
    )
    if isinstance(blockers, dict):
        return int(sum(_as_number(value) for value in blockers.values()))
    if isinstance(blockers, list):
        total = 0
        for row in blockers:
            if isinstance(row, dict):
                total += int(_as_number(row.get("count") or row.get("n") or row.get("affected_count")))
        return total
    return 0


def select_model_policy(agent: dict, packet: dict) -> tuple[str, str | None, str | None]:
    tier = str(agent.get("base_tier") or "standard")
    reason = agent.get("frontier_escalation_reason") if tier == "frontier" else None
    reasoning_override = None

    expansion = packet.get("expansion_map") or {}
    frontier = expansion.get("frontier_crypto") or {}
    observation_count = _as_number(frontier.get("observation_count"))
    known_quality_rate = frontier.get("known_quality_rate")
    if known_quality_rate is None:
        known_quality_rate = 1.0
    known_quality_rate = _as_number(known_quality_rate, 1.0)
    growth_count = len(packet.get("growth_experiments") or [])

    if agent["name"] == "market_scout":
        severe_quality_gap = observation_count >= 500 and known_quality_rate < 0.25
        broad_growth_queue = growth_count >= 8
        regional_surface = _as_number(frontier.get("regional_observation_count")) >= 100
        if severe_quality_gap or broad_growth_queue or regional_surface:
            tier = "standard"
            reason = agent.get("standard_escalation_reason")

    if agent["name"] == "execution_route_hunter" and _route_blocker_count(packet) >= 20:
        tier = "standard"
        reason = agent.get("standard_escalation_reason")

    if agent["name"] in {"cross_market_researcher", "red_team"}:
        reliable_labels = _as_number(((packet.get("signal_redesign") or {}).get("summary") or {}).get("valid_60m_count"))
        high_failure_pressure = len(packet.get("improvement_tasks") or []) >= 5 or growth_count >= 8
        if reliable_labels >= 100 or high_failure_pressure:
            tier = "standard"
            reason = agent.get("standard_escalation_reason")

    if agent["name"] == "build_planner":
        pending_build_tasks = len(packet.get("improvement_tasks") or [])
        if pending_build_tasks or growth_count >= 8:
            tier = "standard"
            reason = agent.get("standard_escalation_reason")
            reasoning_override = "medium"

    return tier, reason, reasoning_override


def run_agent(agent: dict, packet: dict, memory: list[dict]) -> dict:
    system = "You are a bounded AI research agent for a paper-trading market radar. Output JSON only."
    prompt = agent_prompt(agent, packet, memory)
    tier, escalation_reason, reasoning_override = select_model_policy(agent, packet)
    result = complete(
        agent["name"],
        prompt,
        system=system,
        tier_override=tier,
        operation="llm_swarm_recommendation",
        frontier_escalation_reason=escalation_reason if tier == "frontier" else None,
        reasoning_effort_override=reasoning_override,
        structured_json=True,
    )
    rec = parse_recommendation(result.text, agent, packet)
    if _should_retry_schema(
        rec,
        {
            "status": result.status,
            "tier": result.model_tier,
        },
    ):
        retry = complete(
            agent["name"],
            _schema_retry_prompt(agent, result.text),
            system=system,
            tier_override=tier,
            operation="llm_swarm_schema_retry",
            frontier_escalation_reason=escalation_reason if tier == "frontier" else None,
            reasoning_effort_override=reasoning_override,
            structured_json=True,
        )
        retry_rec = parse_recommendation(retry.text, agent, packet)
        retry_rec["retry_count"] = 1
        retry_rec["initial_parse_status"] = rec.get("parse_status")
        result = retry
        rec = retry_rec
    if result.model_tier == "frontier" and not rec.get("frontier_escalation_reason"):
        rec["frontier_escalation_reason"] = escalation_reason or "Frontier tier selected by model policy."
    rec["model"] = {
        "name": result.model_name,
        "tier": result.model_tier,
        "status": result.status,
        "estimated_cost_usd": result.estimated_cost_usd,
        "api": result.api,
        "reasoning_effort": result.reasoning_effort,
        "reasoning_mode": result.reasoning_mode,
        "verbosity": result.verbosity,
        "structured_json": result.structured_json,
        "frontier_escalation_reason": rec.get("frontier_escalation_reason"),
    }
    return rec


def _json_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    visited: set[int] = set()
    for match in re.finditer(r"\{", text or ""):
        start = match.start()
        if start in visited:
            continue
        visited.add(start)
        try:
            value, _end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _appears_truncated_json(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or "{" not in stripped:
        return False
    in_string = False
    escaped = False
    depth = 0
    for char in stripped[stripped.find("{") :]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return depth > 0 or in_string


def _parse_json_recommendation(text: str) -> tuple[dict | None, str, str | None]:
    raw = (text or "").strip()
    if not raw:
        return None, "empty_response", "empty_response"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # A truncated top-level object can contain complete nested objects. Recovering
        # one of those would misclassify evidence/config as the recommendation itself.
        if _appears_truncated_json(raw):
            return None, "truncated_json", "truncated_json"
        embedded = _json_objects(raw)
        if embedded:
            return embedded[0], "recovered_valid", None
        return None, "invalid_json", "no_complete_json_object"
    if not isinstance(parsed, dict):
        return None, "invalid_schema", "top_level_json_not_object"
    return parsed, "native_valid", None


def _reject_recommendation(agent: dict, text: str, parse_status: str, reason: str | None) -> dict:
    return {
        "_rejected": True,
        "accepted": False,
        "action": "no_action",
        "priority": 0,
        "title": f"{agent['name']} rejected output",
        "rationale": (text or "")[:1000],
        "market_key": agent["name"],
        "evidence": {"parser": "strict", "parse_status": parse_status},
        "proposed_change": "",
        "agent_name": agent["name"],
        "parse_status": parse_status,
        "terminal_failure_reason": reason or parse_status,
        "provenance": {
            "state_packet": str(STATE_JSON),
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    }


def _is_rejected(rec: dict) -> bool:
    return bool(rec.get("_rejected") or rec.get("accepted") is False or rec.get("action") == "no_action")


def _should_retry_schema(rec: dict, model: dict) -> bool:
    status = str(model.get("status") or "").lower()
    if status.startswith("fallback_") or "budget_guard" in status:
        return False
    return _is_rejected(rec) and rec.get("parse_status") in {"invalid_json", "truncated_json", "empty_response", "invalid_schema"}


def _schema_retry_prompt(agent: dict, original_text: str) -> str:
    return (
        f"The previous {agent['name']} response was not a complete valid JSON recommendation. "
        "Return exactly one complete JSON object with: action, priority, title, rationale, "
        "market_key, evidence, proposed_change, and optional code_change or variant_config. "
        "No markdown, no commentary, no arrays. Keep it paper-only.\n\n"
        f"Previous response preview:\n{(original_text or '')[:1200]}"
    )


def _dedupe_key(rec: dict) -> tuple:
    code_change = rec.get("code_change") if isinstance(rec.get("code_change"), dict) else {}
    lab = rec.get("strategy_lab_experiment") if isinstance(rec.get("strategy_lab_experiment"), dict) else {}
    files = tuple(sorted(str(path) for path in code_change.get("expected_files", []) or rec.get("expected_files", []) or []))
    title = re.sub(r"\s+", " ", str(rec.get("title") or "").strip().lower())
    return (
        str(rec.get("action") or ""),
        str(rec.get("market_key") or ""),
        str(rec.get("signal_key") or ""),
        str(lab.get("strategy_lab_id") or ""),
        files,
        title,
    )


def _critique_from_recommendation(agent: dict, rec: dict) -> dict | None:
    if agent["name"] not in {"red_team", "build_planner"}:
        return None
    evidence = rec.get("evidence") if isinstance(rec.get("evidence"), dict) else {}
    return {
        "agent_name": agent["name"],
        "title": rec.get("title"),
        "rationale": rec.get("rationale"),
        "reject_market_keys": evidence.get("reject_market_keys", []),
        "reject_signal_keys": evidence.get("reject_signal_keys", []),
        "reject_titles": evidence.get("reject_titles", []),
        "used_agent_outputs": evidence.get("used_agent_outputs", []),
        "rejected_agent_outputs": evidence.get("rejected_agent_outputs", []),
    }


def _rejected_by_critiques(rec: dict, critiques: list[dict]) -> str | None:
    market = str(rec.get("market_key") or "")
    signal = str(rec.get("signal_key") or "")
    title = str(rec.get("title") or "")
    for critique in critiques:
        if market and market in set(str(item) for item in critique.get("reject_market_keys", []) or []):
            return f"rejected_by_{critique.get('agent_name')}:market_key"
        if signal and signal in set(str(item) for item in critique.get("reject_signal_keys", []) or []):
            return f"rejected_by_{critique.get('agent_name')}:signal_key"
        if title and title in set(str(item) for item in critique.get("reject_titles", []) or []):
            return f"rejected_by_{critique.get('agent_name')}:title"
    return None


def _accepted_outputs(state: SwarmState) -> list[dict]:
    accepted: list[dict] = []
    for item in state.get("agent_outputs", []):
        rec = item.get("recommendation") if isinstance(item, dict) else None
        if isinstance(rec, dict) and not _is_rejected(rec):
            accepted.append(rec)
    return accepted


def _repository_grounding(state: SwarmState) -> dict:
    """Resolve current-cycle ideas to the real repo before Build Planner runs."""
    accepted = _accepted_outputs(state)
    if not accepted:
        return {"source_files": [], "test_files": [], "resolved_from": []}
    conceptual_paths: list[str] = []
    for rec in accepted:
        code_change = rec.get("code_change") if isinstance(rec.get("code_change"), dict) else {}
        conceptual_paths.extend(str(path) for path in code_change.get("expected_files", []) or [])
    proposal = {
        "title": " | ".join(str(rec.get("title") or "") for rec in accepted),
        "rationale": " | ".join(str(rec.get("rationale") or "") for rec in accepted),
        "proposed_change": " | ".join(str(rec.get("proposed_change") or "") for rec in accepted),
        "market_key": " | ".join(str(rec.get("market_key") or "") for rec in accepted),
        "signal_key": " | ".join(str(rec.get("signal_key") or "") for rec in accepted),
    }
    resolved = resolve_repo_targets(ROOT, proposal, conceptual_paths=conceptual_paths)
    ranked = {str(item.get("path") or ""): item for item in resolved.get("ranked", [])}

    def entries(paths: list[str]) -> list[dict]:
        return [
            {
                "path": path,
                "symbols": list((ranked.get(path) or {}).get("symbols") or [])[:12],
            }
            for path in paths
        ]

    return {
        "source_files": entries(resolved.get("source_files", [])),
        "test_files": entries(resolved.get("test_files", [])),
        "resolved_from": [
            {
                "agent_name": rec.get("agent_name"),
                "title": rec.get("title"),
                "action": rec.get("action"),
            }
            for rec in accepted
        ],
        "rule": (
            "Select an existing source entrypoint and symbol from this map. A new file is allowed only when an "
            "existing entrypoint calls it and a behavioral test proves that call path."
        ),
    }


ADDITIVE_STATE_FIELDS = {"agent_outputs", "critiques", "node_rejections", "graph_trace"}


def _coerce_role_memory(memory: list[dict] | dict[str, list[dict]]) -> dict[str, list[dict]]:
    if isinstance(memory, dict):
        return {name: list(memory.get(name) or []) for name in [agent["name"] for agent in AGENTS]}
    return {agent["name"]: list(memory or []) for agent in AGENTS}


def _compact_checkpoint_context(packet: dict, role_memory: dict[str, list[dict]]) -> dict:
    encoded_packet = json.dumps(packet, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    memory_ids = {
        name: [
            str(item.get("memory_id") or item.get("id"))
            for item in items
            if item.get("memory_id") or item.get("id")
        ]
        for name, items in role_memory.items()
    }
    return {
        "runtime_context_mode": "reference_only",
        "packet_sha256": hashlib.sha256(encoded_packet).hexdigest(),
        "packet_bytes": len(encoded_packet),
        "role_memory_counts": {name: len(items) for name, items in role_memory.items()},
        "role_memory_ids": memory_ids,
        "durable_memory_source": "runs/radar.sqlite",
        "note": "Large packet and memory payloads are runtime-only and are not duplicated into every graph checkpoint.",
    }


def _initial_state(
    packet: dict,
    memory: list[dict] | dict[str, list[dict]],
    mode: str,
    cycle_id: str | None = None,
    *,
    persist_runtime_context: bool = True,
) -> SwarmState:
    role_memory = _coerce_role_memory(memory)
    state: SwarmState = {
        "cycle_id": cycle_id or f"swarm:{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}",
        "agent_outputs": [],
        "critiques": [],
        "node_rejections": [],
        "ranked_actions": [],
        "rejected_actions": [],
        "graph_trace": [],
        "collaboration_mode": mode,
        "checkpoint": {},
        "memory_reflection": {},
    }
    if persist_runtime_context:
        state["packet"] = packet
        state["memory"] = list(memory) if isinstance(memory, list) else []
        state["role_memory"] = role_memory
    else:
        state["checkpoint_context"] = _compact_checkpoint_context(packet, role_memory)
        state["memory_context_counts"] = {name: len(items) for name, items in role_memory.items()}
    return state


def _merge_state(state: SwarmState, update: SwarmState) -> SwarmState:
    merged = dict(state)
    for key, value in update.items():
        if key in ADDITIVE_STATE_FIELDS:
            merged[key] = [*(merged.get(key) or []), *(value or [])]
        else:
            merged[key] = value
    return merged  # type: ignore[return-value]


def _record_agent_result(
    agent: dict,
    rec: dict,
    elapsed_ms: int,
    memory: list[dict],
) -> SwarmState:
    model = rec.get("model") if isinstance(rec.get("model"), dict) else {}
    output = {
        "agent_name": agent["name"],
        "accepted": not _is_rejected(rec),
        "parse_status": rec.get("parse_status", "native_valid"),
        "recommendation": rec,
        "model": model,
        "memory_ids": [item.get("memory_id") or item.get("id") for item in memory if item.get("memory_id") or item.get("id")],
    }
    rejected: list[dict] = []
    if _is_rejected(rec):
        rejected.append(
            {
                "agent_name": agent["name"],
                "title": rec.get("title"),
                "parse_status": rec.get("parse_status"),
                "reason": rec.get("terminal_failure_reason"),
                "recommendation": rec,
            }
        )
    critique = _critique_from_recommendation(agent, rec)
    return {
        "agent_outputs": [output],
        "critiques": [critique] if critique else [],
        "node_rejections": rejected,
        "graph_trace": [
            {
                "node": agent["name"],
                "elapsed_ms": elapsed_ms,
                "accepted": not _is_rejected(rec),
                "parse_status": rec.get("parse_status", "native_valid"),
                "model_status": model.get("status"),
                "model_tier": model.get("tier"),
                "estimated_cost_usd": model.get("estimated_cost_usd"),
                "memory_count": len(memory),
                "memory_ids": output["memory_ids"],
            }
        ],
    }


def _run_agent_node(
    agent: dict,
    state: SwarmState,
    *,
    runtime_packet: dict | None = None,
    runtime_role_memory: dict[str, list[dict]] | None = None,
) -> SwarmState:
    started = time.perf_counter()
    agent_packet = dict(runtime_packet if runtime_packet is not None else state.get("packet") or {})
    agent_packet["current_cycle_recommendations"] = _accepted_outputs(state)
    agent_packet["current_cycle_agent_outputs"] = state.get("agent_outputs", [])
    agent_packet["current_cycle_critiques"] = state.get("critiques", [])
    agent_packet["current_cycle_ranked_actions"] = state.get("ranked_actions", [])
    if agent["name"] == "build_planner":
        agent_packet["repository_grounding"] = _repository_grounding(state)
    role_memory = runtime_role_memory if runtime_role_memory is not None else state.get("role_memory") or {}
    memory = list(role_memory.get(agent["name"]) or state.get("memory") or [])
    rec = run_agent(agent, agent_packet, memory)
    return _record_agent_result(agent, rec, int((time.perf_counter() - started) * 1000), memory)


def rank_action_package(state: SwarmState, max_items: int | None = None) -> SwarmState:
    seen: set[tuple] = set()
    ranked: list[dict] = []
    rejected = [*state.get("node_rejections", []), *state.get("rejected_actions", [])]
    for rec in _accepted_outputs(state):
        if _is_fallback_recommendation(rec):
            rejected.append({"agent_name": rec.get("agent_name"), "title": rec.get("title"), "reason": "fallback_suppressed", "recommendation": rec})
            continue
        critique_reason = _rejected_by_critiques(rec, state.get("critiques", []))
        if critique_reason:
            rejected.append({"agent_name": rec.get("agent_name"), "title": rec.get("title"), "reason": critique_reason, "recommendation": rec})
            continue
        key = _dedupe_key(rec)
        if key in seen:
            rejected.append({"agent_name": rec.get("agent_name"), "title": rec.get("title"), "reason": "duplicate_same_cycle", "recommendation": rec})
            continue
        seen.add(key)
        ranked.append(rec)
    for item in ranked:
        item["priority"] = _coerce_priority(item.get("priority"), default=50)
    ranked.sort(key=lambda item: (item["priority"], item.get("agent_name") == "build_planner"), reverse=True)
    if max_items is not None:
        overflow = ranked[max_items:]
        ranked = ranked[:max_items]
        for rec in overflow:
            rejected.append({"agent_name": rec.get("agent_name"), "title": rec.get("title"), "reason": "ranked_below_limit", "recommendation": rec})
    return {
        "ranked_actions": ranked,
        "rejected_actions": rejected,
        "graph_trace": [
            {
                "node": "ranker",
                "accepted_count": len(ranked),
                "rejected_count": len(rejected),
                "collaboration_mode": state.get("collaboration_mode"),
            }
        ],
    }


def run_sequential(
    packet: dict,
    memory: list[dict] | dict[str, list[dict]],
    settings: dict | None = None,
    cycle_id: str | None = None,
) -> list[dict]:
    global LAST_SWARM_STATE
    state = _initial_state(packet, memory, FALLBACK_COLLABORATION_MODE, cycle_id)
    for agent in AGENTS:
        state = _merge_state(state, _run_agent_node(agent, state))
    state = _merge_state(state, rank_action_package(state))
    state["checkpoint"] = {"status": "not_used", "reason": "sequential_fallback"}
    LAST_SWARM_STATE = dict(state)
    return list(state["ranked_actions"])


def _checkpoint_path(settings: dict) -> pathlib.Path:
    configured = pathlib.Path(str(settings.get("agent_memory", {}).get("checkpoint_path", "runs/langgraph_checkpoints.sqlite")))
    return configured if configured.is_absolute() else ROOT / configured


def _checkpoint_storage_stats(conn: sqlite3.Connection) -> dict:
    page_size = int(conn.execute("pragma page_size").fetchone()[0])
    page_count = int(conn.execute("pragma page_count").fetchone()[0])
    freelist_count = int(conn.execute("pragma freelist_count").fetchone()[0])
    checkpoint_bytes = int(
        conn.execute(
            "select coalesce(sum(coalesce(length(checkpoint), 0) + coalesce(length(metadata), 0)), 0) "
            "from checkpoints"
        ).fetchone()[0]
    )
    write_bytes = int(
        conn.execute("select coalesce(sum(coalesce(length(value), 0)), 0) from writes").fetchone()[0]
    )
    thread_count = int(
        conn.execute(
            "select count(distinct thread_id) from checkpoints where thread_id like 'swarm:%'"
        ).fetchone()[0]
    )
    return {
        "thread_count": thread_count,
        "db_size_bytes": page_size * page_count,
        "free_bytes": page_size * freelist_count,
        "live_payload_bytes": checkpoint_bytes + write_bytes,
    }


def _checkpoint_thread_usage(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        with checkpoint_usage as (
            select thread_id,
                   max(rowid) as latest_row,
                   sum(coalesce(length(checkpoint), 0) + coalesce(length(metadata), 0)) as checkpoint_bytes
            from checkpoints
            where thread_id like 'swarm:%'
            group by thread_id
        ),
        write_usage as (
            select thread_id, sum(coalesce(length(value), 0)) as write_bytes
            from writes
            where thread_id like 'swarm:%'
            group by thread_id
        )
        select c.thread_id,
               c.latest_row,
               c.checkpoint_bytes + coalesce(w.write_bytes, 0) as payload_bytes
        from checkpoint_usage c
        left join write_usage w on w.thread_id = c.thread_id
        order by c.latest_row desc
        """
    ).fetchall()
    return [
        {
            "thread_id": str(row[0]),
            "latest_row": int(row[1]),
            "payload_bytes": int(row[2] or 0),
        }
        for row in rows
    ]


def _prune_checkpoint_threads(
    saver: Any,
    conn: sqlite3.Connection,
    retain: int,
    max_storage_mb: float | None = None,
) -> int:
    retain = max(4, int(retain))
    saver.setup()
    rows = _checkpoint_thread_usage(conn)
    keep_count = min(retain, len(rows))
    if max_storage_mb is not None and rows:
        budget_bytes = max(1024, int(float(max_storage_mb) * 1024 * 1024))
        budget_keep_count = 0
        retained_payload = 0
        for index, row in enumerate(rows[:retain]):
            next_payload = retained_payload + row["payload_bytes"]
            if index >= 2 and next_payload > budget_bytes:
                break
            retained_payload = next_payload
            budget_keep_count = index + 1
        keep_count = min(keep_count, max(2, budget_keep_count))
    removed = 0
    for row in rows[keep_count:]:
        saver.delete_thread(row["thread_id"])
        removed += 1
    conn.commit()
    return removed


def _compact_checkpoint_database(conn: sqlite3.Connection, minimum_reclaim_mb: float) -> bool:
    stats = _checkpoint_storage_stats(conn)
    minimum_reclaim_bytes = max(0, int(float(minimum_reclaim_mb) * 1024 * 1024))
    if stats["free_bytes"] < minimum_reclaim_bytes:
        return False
    conn.commit()
    conn.execute("vacuum")
    return True


def _invoke_graph(graph: Any, initial: SwarmState, settings: dict) -> tuple[SwarmState, dict]:
    cfg = settings.get("agent_memory", {})
    if not cfg.get("checkpoint_enabled", True):
        app = graph.compile()
        return app.invoke(initial), {"status": "disabled_by_policy"}
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore
    except Exception as exc:
        app = graph.compile()
        return app.invoke(initial), {"status": "package_missing", "error": str(exc)[:240]}

    path = _checkpoint_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_conn = sqlite3.connect(path, check_same_thread=False)
    try:
        saver = SqliteSaver(checkpoint_conn)
        app = graph.compile(checkpointer=saver)
        output = app.invoke(initial, config={"configurable": {"thread_id": initial["cycle_id"]}})
        pruned = _prune_checkpoint_threads(
            saver,
            checkpoint_conn,
            int(cfg.get("checkpoint_retention_cycles", 48)),
            float(cfg.get("checkpoint_max_storage_mb", 64)),
        )
        compacted = False
        if pruned and cfg.get("checkpoint_compact_on_prune", True):
            compacted = _compact_checkpoint_database(
                checkpoint_conn,
                float(cfg.get("checkpoint_vacuum_min_reclaim_mb", 16)),
            )
        storage = _checkpoint_storage_stats(checkpoint_conn)
    finally:
        checkpoint_conn.close()
    return output, {
        "status": "saved",
        "path": str(path),
        "thread_id": initial["cycle_id"],
        "pruned_threads": pruned,
        "compacted": compacted,
        "retention_cycles": int(cfg.get("checkpoint_retention_cycles", 48)),
        "max_storage_mb": float(cfg.get("checkpoint_max_storage_mb", 64)),
        **storage,
    }


def run_langgraph_if_available(
    packet: dict,
    memory: list[dict] | dict[str, list[dict]],
    settings: dict | None = None,
    cycle_id: str | None = None,
) -> list[dict]:
    global LAST_SWARM_STATE
    settings = settings or load_settings()
    try:
        from langgraph.graph import END, START, StateGraph  # type: ignore
    except Exception:
        return run_sequential(packet, memory, settings, cycle_id)

    runtime_role_memory = _coerce_role_memory(memory)

    def make_node(agent: dict):
        def node(state: SwarmState) -> SwarmState:
            return _run_agent_node(
                agent,
                state,
                runtime_packet=packet,
                runtime_role_memory=runtime_role_memory,
            )

        return node

    def ranker_node(state: SwarmState) -> SwarmState:
        return rank_action_package(state)

    def phase_node(name: str):
        def node(_state: SwarmState) -> SwarmState:
            return {"graph_trace": [{"node": name, "status": "joined"}]}

        return node

    graph = StateGraph(SwarmState)
    for agent in AGENTS:
        graph.add_node(agent["name"], make_node(agent))
    graph.add_node("research_join", phase_node("research_join"))
    graph.add_node("critique_join", phase_node("critique_join"))
    graph.add_node("ranker", ranker_node)
    graph.add_node("memory_checkpoint", phase_node("memory_checkpoint"))

    graph.add_edge(START, "market_scout")
    graph.add_edge(START, "cross_market_researcher")
    graph.add_edge(["market_scout", "cross_market_researcher"], "research_join")
    graph.add_edge("research_join", "strategy_lab")
    graph.add_edge("strategy_lab", "red_team")
    graph.add_edge("strategy_lab", "execution_route_hunter")
    graph.add_edge(["red_team", "execution_route_hunter"], "critique_join")
    graph.add_edge("critique_join", "build_planner")
    graph.add_edge("build_planner", "ranker")
    graph.add_edge("ranker", "memory_checkpoint")
    graph.add_edge("memory_checkpoint", END)
    initial = _initial_state(
        packet,
        runtime_role_memory,
        COLLABORATION_MODE,
        cycle_id,
        persist_runtime_context=False,
    )
    output, checkpoint = _invoke_graph(graph, initial, settings)
    output["checkpoint"] = {
        **checkpoint,
        "runtime_context": output.get("checkpoint_context", {}),
    }
    output["memory_context_counts"] = {
        name: len(items)
        for name, items in runtime_role_memory.items()
    }
    LAST_SWARM_STATE = dict(output)
    return list(output.get("ranked_actions", []))


def _is_fallback_recommendation(rec: dict) -> bool:
    model = rec.get("model") if isinstance(rec.get("model"), dict) else {}
    status = str(model.get("status") or "").lower()
    evidence = rec.get("evidence") if isinstance(rec.get("evidence"), dict) else {}
    return (
        status.startswith("fallback_")
        or evidence.get("mode") == "fallback"
        or rec.get("market_key") == "fallback_llm_bridge"
    )


def _latest_failure_cooldown_active(settings: dict) -> bool:
    cfg = settings.get("llm_swarm", {})
    if not cfg.get("cooldown_on_model_unavailable", True):
        return False
    cooldown_minutes = float(cfg.get("model_failure_cooldown_minutes", 60))
    if cooldown_minutes <= 0:
        return False
    report = RUNS_DIR / "llm_swarm_latest.json"
    if not report.exists():
        return False
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
        generated_at = dt.datetime.fromisoformat(str(payload.get("generated_at")).replace("Z", "+00:00"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=dt.timezone.utc)
    age_minutes = (dt.datetime.now(dt.timezone.utc) - generated_at).total_seconds() / 60.0
    if age_minutes >= cooldown_minutes:
        return False
    recommendations = (payload.get("recommendations") or []) + (payload.get("suppressed_recommendations") or [])
    if not recommendations:
        return False
    statuses = [
        str((rec.get("model") if isinstance(rec.get("model"), dict) else {}).get("status") or "").lower()
        for rec in recommendations
        if isinstance(rec, dict)
    ]
    unavailable = ("fallback_error", "fallback_missing_provider_key", "fallback_no_cost", "agent_budget_guard", "global_budget_guard")
    return bool(statuses) and all(any(token in status for token in unavailable) for status in statuses)


def write_recommendations(
    recommendations: list[dict],
    max_items: int,
    settings: dict | None = None,
    swarm_state: dict | None = None,
) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = (settings or {}).get("llm_swarm", {})
    write_fallback = bool(cfg.get("write_fallback_recommendations_to_inbox", False))
    state = swarm_state or LAST_SWARM_STATE or {}
    state_rejected = list(state.get("rejected_actions") or [])
    actionable = [
        rec
        for rec in recommendations
        if not _is_rejected(rec) and (write_fallback or not _is_fallback_recommendation(rec))
    ]
    suppressed = [
        rec
        for rec in recommendations
        if _is_rejected(rec) or (not write_fallback and _is_fallback_recommendation(rec))
    ]
    selected = actionable[:max_items]
    action_package = {
        "ranked_actions": selected,
        "rejected_or_suppressed": [*state_rejected, *suppressed][: max_items * 2],
        "collaboration_mode": state.get("collaboration_mode") or COLLABORATION_MODE,
    }
    with INBOX.open("a", encoding="utf-8") as fh:
        for rec in selected:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    report = RUNS_DIR / "llm_swarm_latest.json"
    report.write_text(
        json.dumps(
            {
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "recommendations": selected,
                "suppressed_recommendations": suppressed,
                "suppressed_count": len(suppressed) + len(state_rejected),
                "collaboration_mode": action_package["collaboration_mode"],
                "graph_trace": state.get("graph_trace", []),
                "agent_outputs": state.get("agent_outputs", []),
                "critiques": state.get("critiques", []),
                "ranked_actions": selected,
                "rejected_actions": [*state_rejected, *suppressed],
                "action_package": action_package,
                "checkpoint": state.get("checkpoint", {}),
                "memory_reflection": state.get("memory_reflection", {}),
                "memory_context_counts": state.get("memory_context_counts") or {
                    name: len(items)
                    for name, items in (state.get("role_memory") or {}).items()
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return report


def should_auto_run(settings: dict) -> bool:
    cfg = settings.get("llm_swarm", {})
    if not cfg.get("enabled", True) or not cfg.get("auto_run", False):
        return False
    if _latest_failure_cooldown_active(settings):
        return False
    marker = RUNS_DIR / "llm_swarm_last_run.txt"
    min_minutes = float(cfg.get("min_minutes_between_runs", 60))
    if not marker.exists():
        return True
    try:
        last = dt.datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
    except ValueError:
        return True
    age = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60.0
    return age >= min_minutes


def mark_auto_run() -> None:
    (RUNS_DIR / "llm_swarm_last_run.txt").write_text(dt.datetime.now(dt.timezone.utc).isoformat(), encoding="utf-8")


def run_once(settings: dict | None = None, force: bool = False) -> list[dict]:
    global LAST_SWARM_STATE
    settings = settings or load_settings()
    if not force and not should_auto_run(settings):
        return []
    packet = load_state_packet()
    with connect() as conn:
        try:
            memory, cycle_id = build_swarm_memory(
                conn,
                packet,
                settings,
                [agent["name"] for agent in AGENTS],
            )
        except Exception:
            fallback = query_memory(conn, limit=40)
            memory = _coerce_role_memory(fallback)
            cycle_id = f"swarm:{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    recommendations = run_langgraph_if_available(packet, memory, settings, cycle_id)
    with connect() as conn:
        reflection = reflect_swarm(conn, LAST_SWARM_STATE, cycle_id, settings)
        graphiti = sync_graphiti(conn, settings)
        write_memory_exports(conn, settings)
    LAST_SWARM_STATE["memory_reflection"] = {**reflection, "graphiti": graphiti}
    write_recommendations(
        recommendations,
        int(settings.get("llm_swarm", {}).get("max_recommendations_per_run", 10)),
        settings=settings,
        swarm_state=LAST_SWARM_STATE,
    )
    mark_auto_run()
    return recommendations


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the collaborative six-agent LLM swarm once.")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    recs = run_once(force=args.force)
    print(f"Generated {len(recs)} recommendations")
    for rec in recs:
        print(f"- P{rec.get('priority')} {rec.get('action')}: {rec.get('title')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
