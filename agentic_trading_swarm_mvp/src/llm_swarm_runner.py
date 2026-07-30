#!/usr/bin/env python3
"""Five-agent cost-aware LLM swarm.

Uses LangGraph when installed. If it is absent, runs the same five agent nodes
sequentially. All model calls go through cost_router, which defaults to a
zero-cost fallback unless RADAR_USE_LITELLM=1 is set. The default policy is
mini-first with earned standard/frontier escalation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
from typing import Any, TypedDict

from cost_router import complete
from llm_bridge import INBOX, STATE_JSON
from memory_graph import query_memory
from settings import load_settings
from storage import RUNS_DIR, connect


COLLABORATION_MODE = "langgraph_typed_action_package"
FALLBACK_COLLABORATION_MODE = "sequential_typed_action_package"
LAST_SWARM_STATE: dict[str, Any] = {}


class SwarmState(TypedDict, total=False):
    packet: dict
    memory: list[dict]
    agent_outputs: list[dict]
    critiques: list[dict]
    ranked_actions: list[dict]
    rejected_actions: list[dict]
    graph_trace: list[dict]
    collaboration_mode: str


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
        "recent_memory": memory[:20],
        "allowed_actions": packet.get("allowed_recommendation_actions", []),
        "current_cycle_agent_outputs": packet.get("current_cycle_agent_outputs", [])[:10],
        "current_cycle_critiques": packet.get("current_cycle_critiques", [])[:10],
        "current_cycle_ranked_actions": packet.get("current_cycle_ranked_actions", [])[:10],
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
        "V1 runtime supports strategy_logic.type='candidate_filter' over "
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
        "rollback_criteria, evidence, implementation_mode, and optionally unified_diff. Allowed categories are "
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
        f"If uncertain, use action {agent['default_action']}.\n\n"
        f"STATE:\n{json.dumps(compact, sort_keys=True)}"
    )


def parse_recommendation(text: str, agent: dict, packet: dict) -> dict:
    allowed = set(packet.get("allowed_recommendation_actions", []))
    rec, parse_status, reason = _parse_json_recommendation(text)
    if rec is None:
        return _reject_recommendation(agent, text, parse_status, reason)
    rec["parse_status"] = parse_status
    if rec.get("action") not in allowed:
        rec["action"] = agent["default_action"]
    if rec.get("action") == "propose_code_change":
        shaped = _shape_actionable_code_change(rec, agent)
        if shaped:
            rec = shaped
        else:
            rec = {
                **rec,
                "action": "propose_build_task",
                "title": rec.get("title") or f"{agent['name']} code idea needs shaping",
                "rationale": rec.get("rationale") or rec.get("proposed_change") or "Code proposal lacked required Build Governor fields.",
                "evidence": {
                    **(rec.get("evidence") if isinstance(rec.get("evidence"), dict) else {}),
                    "downgraded_from_code_change": True,
                    "downgrade_reason": "missing_actionable_code_change_fields",
                    "parser": "structured_guard",
                },
            }
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


def _shape_actionable_code_change(rec: dict, agent: dict) -> dict | None:
    """Repair obvious market-growth code ideas into bounded Build Governor proposals."""
    code_change = rec.get("code_change") if isinstance(rec.get("code_change"), dict) else {}
    category = code_change.get("change_category") or rec.get("change_category") or rec.get("category")
    expected_files = code_change.get("expected_files") or rec.get("expected_files") or []
    proposed = str(rec.get("proposed_change") or rec.get("rationale") or "")
    title = str(rec.get("title") or "")
    haystack = f"{title}\n{proposed}\n{json.dumps(rec.get('evidence') or {}, sort_keys=True)}".lower()

    if category and expected_files:
        code_change.setdefault("change_category", category)
        code_change.setdefault("expected_files", expected_files)
        code_change.setdefault("implementation_mode", rec.get("implementation_mode") or "runtime_active")
        code_change.setdefault("tests_to_run", rec.get("tests_to_run") or [])
        code_change.setdefault("rollback_criteria", rec.get("rollback_criteria") or "Revert if tests fail, reports stop refreshing, or paper-only safety checks fail.")
        code_change.setdefault("evidence", rec.get("evidence") if isinstance(rec.get("evidence"), dict) else {})
        return {**rec, "code_change": code_change}

    market_growth_terms = (
        "market expansion",
        "depth enrichment",
        "quality coverage",
        "starved venue",
        "candidate cap",
        "frontier venue",
        "new markets tested",
        "public adapter",
    )
    if agent["name"] == "build_planner" and any(term in haystack for term in market_growth_terms):
        repaired = dict(rec)
        repaired["code_change"] = {
            "change_category": "scanner_expansion",
            "implementation_mode": "runtime_active",
            "expected_files": [
                "src/frontier_crypto_adapter.py",
                "src/frontier_data_quality.py",
                "tests/test_frontier_crypto_adapter.py",
                "tests/test_frontier_data_quality.py",
            ],
            "tests_to_run": [
                "python -m unittest tests/test_frontier_crypto_adapter.py tests/test_frontier_data_quality.py"
            ],
            "rollback_criteria": "Revert if depth-selection caps are exceeded, report generation fails, or paper-only safety checks fail.",
            "evidence": rec.get("evidence") if isinstance(rec.get("evidence"), dict) else {},
        }
        repaired.setdefault("priority", 75)
        repaired.setdefault("proposed_change", proposed or title)
        return repaired

    return None


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
        embedded = _json_objects(raw)
        if embedded:
            return embedded[0], "recovered_valid", None
        if _appears_truncated_json(raw):
            return None, "truncated_json", "truncated_json"
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


def _initial_state(packet: dict, memory: list[dict], mode: str) -> SwarmState:
    return {
        "packet": packet,
        "memory": memory,
        "agent_outputs": [],
        "critiques": [],
        "ranked_actions": [],
        "rejected_actions": [],
        "graph_trace": [],
        "collaboration_mode": mode,
    }


def _record_agent_result(state: SwarmState, agent: dict, rec: dict, elapsed_ms: int) -> SwarmState:
    model = rec.get("model") if isinstance(rec.get("model"), dict) else {}
    output = {
        "agent_name": agent["name"],
        "accepted": not _is_rejected(rec),
        "parse_status": rec.get("parse_status", "native_valid"),
        "recommendation": rec,
        "model": model,
    }
    state.setdefault("agent_outputs", []).append(output)
    if _is_rejected(rec):
        state.setdefault("rejected_actions", []).append(
            {
                "agent_name": agent["name"],
                "title": rec.get("title"),
                "parse_status": rec.get("parse_status"),
                "reason": rec.get("terminal_failure_reason"),
                "recommendation": rec,
            }
        )
    critique = _critique_from_recommendation(agent, rec)
    if critique:
        state.setdefault("critiques", []).append(critique)
    state.setdefault("graph_trace", []).append(
        {
            "node": agent["name"],
            "elapsed_ms": elapsed_ms,
            "accepted": not _is_rejected(rec),
            "parse_status": rec.get("parse_status", "native_valid"),
            "model_status": model.get("status"),
            "model_tier": model.get("tier"),
            "estimated_cost_usd": model.get("estimated_cost_usd"),
        }
    )
    return state


def _run_agent_node(agent: dict, state: SwarmState) -> SwarmState:
    started = time.perf_counter()
    agent_packet = dict(state["packet"])
    agent_packet["current_cycle_recommendations"] = _accepted_outputs(state)
    agent_packet["current_cycle_agent_outputs"] = state.get("agent_outputs", [])
    agent_packet["current_cycle_critiques"] = state.get("critiques", [])
    agent_packet["current_cycle_ranked_actions"] = state.get("ranked_actions", [])
    rec = run_agent(agent, agent_packet, state["memory"])
    return _record_agent_result(state, agent, rec, int((time.perf_counter() - started) * 1000))


def rank_action_package(state: SwarmState, max_items: int | None = None) -> SwarmState:
    seen: set[tuple] = set()
    ranked: list[dict] = []
    rejected = list(state.get("rejected_actions", []))
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
    state["ranked_actions"] = ranked
    state["rejected_actions"] = rejected
    state.setdefault("graph_trace", []).append(
        {
            "node": "ranker",
            "accepted_count": len(ranked),
            "rejected_count": len(rejected),
            "collaboration_mode": state.get("collaboration_mode"),
        }
    )
    return state


def run_sequential(packet: dict, memory: list[dict]) -> list[dict]:
    global LAST_SWARM_STATE
    state = _initial_state(packet, memory, FALLBACK_COLLABORATION_MODE)
    for agent in AGENTS:
        state = _run_agent_node(agent, state)
    state = rank_action_package(state)
    LAST_SWARM_STATE = dict(state)
    return list(state["ranked_actions"])


def run_langgraph_if_available(packet: dict, memory: list[dict]) -> list[dict]:
    global LAST_SWARM_STATE
    try:
        from langgraph.graph import END, StateGraph  # type: ignore
    except Exception:
        return run_sequential(packet, memory)

    def make_node(agent: dict):
        def node(state: SwarmState) -> SwarmState:
            return _run_agent_node(agent, state)

        return node

    def ranker_node(state: SwarmState) -> SwarmState:
        return rank_action_package(state)

    graph = StateGraph(SwarmState)
    previous = None
    for agent in AGENTS:
        graph.add_node(agent["name"], make_node(agent))
        if previous is None:
            graph.set_entry_point(agent["name"])
        else:
            graph.add_edge(previous, agent["name"])
        previous = agent["name"]
    graph.add_node("ranker", ranker_node)
    graph.add_edge(previous, "ranker")
    graph.add_edge("ranker", END)
    app = graph.compile()
    output = app.invoke(_initial_state(packet, memory, COLLABORATION_MODE))
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
    settings = settings or load_settings()
    if not force and not should_auto_run(settings):
        return []
    packet = load_state_packet()
    with connect() as conn:
        memory = query_memory(conn, limit=40)
    recommendations = run_langgraph_if_available(packet, memory)
    write_recommendations(
        recommendations,
        int(settings.get("llm_swarm", {}).get("max_recommendations_per_run", 10)),
        settings=settings,
        swarm_state=LAST_SWARM_STATE,
    )
    mark_auto_run()
    return recommendations


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the 5-agent cost-aware LLM swarm once.")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    recs = run_once(force=args.force)
    print(f"Generated {len(recs)} recommendations")
    for rec in recs:
        print(f"- P{rec.get('priority')} {rec.get('action')}: {rec.get('title')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
