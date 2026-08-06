#!/usr/bin/env python3
"""Collaborative core and persistent dynamic-agent cost-aware LLM swarm.

Uses LangGraph when installed. If it is absent, runs the same nodes sequentially.
All model calls go through cost_router, which defaults to a
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
from recommendation_schema import (
    CROSS_MARKET_RESEARCHER_ALLOWED_ACTIONS,
    REQUIRED_RECOMMENDATION_KEYS,
    cross_market_researcher_schema_fallback,
    finalize_cross_market_researcher_response,
    finalize_red_team_response,
    finalize_recommendation_response,
    paper_only_no_action_fallback,
    red_team_schema_fallback,
    validate_recommendation_object,
)
from settings import load_settings
from storage import RUNS_DIR, connect
from dynamic_agents import (
    architect_recommendation,
    build_dynamic_memory_contexts,
    decorate_dynamic_recommendation,
    normalize_agent_spec,
    prepare_dynamic_agent_cycle,
    record_dynamic_agent_runs,
    write_dynamic_agent_reports,
)


COLLABORATION_MODE = "langgraph_typed_action_package"
FALLBACK_COLLABORATION_MODE = "sequential_typed_action_package"
LAST_SWARM_STATE: dict[str, Any] = {}
ROOT = pathlib.Path(__file__).resolve().parents[1]
PUBLISH_OPTIONAL_RECOMMENDATION_FIELDS = (
    "signal_key",
    "directive",
    "variant_config",
    "strategy_lab_experiment",
    "code_change",
    "agent_spec",
)
PUBLISH_REQUIRED_ACTION_PAYLOADS = {
    "spawn_agent": "agent_spec",
    "propose_strategy_lab_experiment": "strategy_lab_experiment",
    "propose_code_change": "code_change",
    "propose_signal_variant": "variant_config",
}


EXECUTION_ROUTE_REQUIREMENT_LABELS = (
    "borrow_availability",
    "fee_pressure",
    "margin_needs",
    "api_borrow_feasibility",
)
_EXECUTION_ROUTE_HUNTER_SAFE_ROUTE_STATUSES = {
    "actionable",
    "executable",
    "executable_proxy",
    "executable_standard",
    "feasible_for_paper",
    "feasible_with_simulation_assumptions",
    "paper_observation",
    "paper_testable_proxy",
    "paper_testable_via_proxy",
    "proxy_only",
    "route_supported",
    "supported",
    "standard",
}
_EXECUTION_ROUTE_HUNTER_ROUTE_CONTAINER_KEYS = (
    "paper_safe_route",
    "selected_paper_route",
    "paper_route",
    "paper_route_review",
    "execution_route",
    "route",
)
_EXECUTION_ROUTE_HUNTER_ROUTE_STATUS_KEYS = (
    "route_status",
    "paper_route_status",
    "status",
    "route_decision",
    "route_recommendation_status",
    "route_actionability",
    "feasibility_status",
)
_EXECUTION_ROUTE_HUNTER_INCOMPLETE_ROUTE_LABELS = {
    "requires_api_and_borrow_confirmation",
    "requires_borrow_confirmation",
    "fee_pressure_unmeasured",
    "margin_needs_confirmation",
}


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
    dynamic_agent_cycle: dict
    dynamic_agents: dict


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


def _first_route_value(*values: Any) -> Any:
    """Return the first non-empty route value without interpreting it as a gate."""

    for value in values:
        if value not in (None, "", [], {}, ()):
            return value
    return None


def _route_label(value: Any, fallback: str) -> str:
    """Normalize a display label while keeping unknown route facts visible."""

    if isinstance(value, bool):
        return "available" if value else "unavailable"
    if value in (None, "", [], {}, ()):
        return fallback
    return str(value)


def _missing_recommendation_fields(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return list(REQUIRED_RECOMMENDATION_KEYS)
    missing: list[str] = []
    for field in REQUIRED_RECOMMENDATION_KEYS:
        item = value.get(field)
        if item is None:
            missing.append(field)
        elif isinstance(item, str) and not item.strip():
            missing.append(field)
        elif field in {"evidence", "proposed_change"} and item == {}:
            missing.append(field)
    return missing


def _execution_route_hunter_route_status(value: Any) -> bool:
    return str(value or "").strip().lower() in _EXECUTION_ROUTE_HUNTER_SAFE_ROUTE_STATUSES


def _mapping_has_explicit_paper_safe_route(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key in _EXECUTION_ROUTE_HUNTER_ROUTE_STATUS_KEYS:
        if _execution_route_hunter_route_status(value.get(key)):
            return True
    if value.get("route_feasible_paper") is True:
        return True
    if value.get("direct_route_actionable") is True or value.get("route_eligible") is True:
        return True
    route_id = value.get("route_id") or value.get("paper_proxy_id") or value.get("selected_proxy_id")
    return bool(route_id) and any(
        _execution_route_hunter_route_status(value.get(key))
        for key in _EXECUTION_ROUTE_HUNTER_ROUTE_STATUS_KEYS
    )


def _recommendation_has_explicit_paper_safe_route(rec: dict) -> bool:
    for container in (rec, rec.get("evidence"), rec.get("proposed_change")):
        if _mapping_has_explicit_paper_safe_route(container):
            return True
        if not isinstance(container, dict):
            continue
        for key in _EXECUTION_ROUTE_HUNTER_ROUTE_CONTAINER_KEYS:
            if _mapping_has_explicit_paper_safe_route(container.get(key)):
                return True
    return False


def _route_summary_has_explicit_paper_safe_route(summary: Any) -> bool:
    if not isinstance(summary, dict):
        return False
    routes = summary.get("routes")
    if not isinstance(routes, list):
        return False
    for row in routes:
        if not isinstance(row, dict):
            continue
        labels = row.get("labels")
        if not isinstance(labels, dict):
            continue
        values = [
            str(labels.get(label) or "").strip().lower()
            for label in EXECUTION_ROUTE_REQUIREMENT_LABELS
        ]
        if values and all(value and value not in _EXECUTION_ROUTE_HUNTER_INCOMPLETE_ROUTE_LABELS for value in values):
            return True
    return False


def _paper_route_requirement_summaries_have_explicit_route(summary: Any) -> bool:
    if not isinstance(summary, dict):
        return False
    candidates = summary.get("candidates")
    if not isinstance(candidates, list):
        return False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        missing_flags = candidate.get("missing_data_flags")
        if isinstance(missing_flags, list) and not missing_flags:
            return True
    return False


def _packet_has_route_context(packet: dict) -> bool:
    return bool(
        packet.get("execution_route_requirement_summary")
        or packet.get("paper_route_requirement_summaries")
        or packet.get("route_resolver")
    )


def _packet_has_explicit_paper_safe_route(packet: dict) -> bool:
    if _route_summary_has_explicit_paper_safe_route(packet.get("execution_route_requirement_summary")):
        return True
    if _paper_route_requirement_summaries_have_explicit_route(packet.get("paper_route_requirement_summaries")):
        return True
    route_resolver = packet.get("route_resolver")
    if isinstance(route_resolver, dict):
        by_status = route_resolver.get("by_route_status")
        if isinstance(by_status, dict) and any(
            str(key).strip().lower() in _EXECUTION_ROUTE_HUNTER_SAFE_ROUTE_STATUSES and bool(value)
            for key, value in by_status.items()
        ):
            return True
        route_intelligence = route_resolver.get("route_intelligence")
        if isinstance(route_intelligence, dict):
            if any(
                int(route_intelligence.get(key) or 0) > 0
                for key in ("paper_proxy_available_count", "paper_research_available_count")
            ):
                return True
            for key in ("paper_proxy_available", "paper_research_available"):
                items = route_intelligence.get(key)
                if isinstance(items, list) and items:
                    return True
    return False


def _execution_route_hunter_fallback(
    rec: dict,
    packet: dict,
    reason: str,
) -> dict:
    fallback = paper_only_no_action_fallback(
        market_key="paper.execution_route_hunter",
        rationale=(
            "The execution_route_hunter recommendation was incomplete or lacked an "
            "explicit paper-safe route, so it was converted into a paper-only "
            "no_action record."
        ),
    )
    fallback["evidence"] = {
        **fallback["evidence"],
        "paper_only": True,
        "explicit_paper_safe_route_required": True,
        "schema_violation": reason,
        "original_action": str(rec.get("action") or ""),
    }
    route_summary = packet.get("execution_route_requirement_summary")
    if isinstance(route_summary, dict) and route_summary:
        fallback["evidence"]["execution_route_requirement_summary"] = route_summary
    paper_summaries = packet.get("paper_route_requirement_summaries")
    if isinstance(paper_summaries, dict) and paper_summaries:
        fallback["evidence"]["paper_route_requirement_summaries"] = paper_summaries
    fallback["parse_status"] = "invalid_schema"
    fallback["terminal_failure_reason"] = reason
    fallback["_rejected"] = True
    fallback["accepted"] = False
    return fallback


def _guard_execution_route_hunter_recommendation(
    agent: dict,
    rec: dict,
    packet: dict,
) -> dict:
    if agent.get("name") != "execution_route_hunter" or rec.get("action") == "no_action":
        return rec
    missing = _missing_recommendation_fields(rec)
    if missing:
        return _execution_route_hunter_fallback(
            rec,
            packet,
            f"missing_required_fields:{','.join(missing)}",
        )
    if _recommendation_has_explicit_paper_safe_route(rec):
        return rec
    if not _packet_has_route_context(packet):
        return rec
    if not _packet_has_explicit_paper_safe_route(packet):
        return _execution_route_hunter_fallback(
            rec,
            packet,
            "missing_explicit_paper_safe_route",
        )
    return rec


def build_execution_route_requirement_summary(packet: dict) -> dict:
    """Build the hunter's read-only pre-ranking route-requirement summary.

    Short-frontier outcome telemetry intentionally contains performance evidence,
    not account or broker state.  This projection labels missing borrow, fee,
    margin, and API facts as confirmation work rather than treating them as
    eligibility gates.  It is attached before recommendation ranking so both
    the hunter and downstream Build Planner can see the same paper-only facts.
    """

    outcome_report = packet.get("short_frontier_spot_route_outcomes")
    outcome_report = outcome_report if isinstance(outcome_report, dict) else {}
    route_rows = outcome_report.get("routes")
    route_rows = route_rows if isinstance(route_rows, list) else []
    summaries: list[dict[str, Any]] = []

    for route in route_rows:
        if not isinstance(route, dict):
            continue
        existing = route.get("route_requirement_summary")
        existing = existing if isinstance(existing, dict) else {}
        borrow = existing.get("short_borrow_availability")
        borrow = borrow if isinstance(borrow, dict) else {}
        fee = existing.get("fee_estimate")
        fee = fee if isinstance(fee, dict) else {}
        margin = existing.get("margin_mode")
        margin = margin if isinstance(margin, dict) else {}
        api = existing.get("api_entitlement")
        api = api if isinstance(api, dict) else {}

        borrow_value = _first_route_value(
            borrow.get("availability_status"),
            route.get("borrow_availability"),
            route.get("borrow_availability_status"),
            route.get("borrowable"),
        )
        fee_value = _first_route_value(
            fee.get("pressure"),
            fee.get("route_cost_bps_paper"),
            fee.get("estimated_round_trip_taker_bps"),
            route.get("fee_pressure"),
            route.get("route_cost_bps_paper"),
            route.get("estimated_round_trip_cost_bps"),
        )
        margin_value = _first_route_value(
            margin.get("required"),
            margin.get("mode"),
            route.get("margin_required"),
            route.get("margin_mode"),
        )
        api_value = _first_route_value(
            api.get("path_readiness"),
            api.get("entitlement_status"),
            route.get("api_borrow_feasibility"),
            route.get("api_route_status"),
            route.get("api_access_status"),
        )
        summaries.append(
            {
                "venue": str(route.get("venue") or "unknown"),
                "signal_key": str(route.get("signal_key") or "unknown"),
                "direction": str(route.get("direction") or "short_frontier_spot"),
                "outcome_status": str(route.get("outcome_status") or "paper_outcome_observed"),
                "labels": {
                    "borrow_availability": _route_label(borrow_value, "requires_borrow_confirmation"),
                    "fee_pressure": _route_label(fee_value, "fee_pressure_unmeasured"),
                    "margin_needs": _route_label(margin_value, "margin_needs_confirmation"),
                    "api_borrow_feasibility": _route_label(
                        api_value,
                        "requires_api_and_borrow_confirmation",
                    ),
                },
                "observed_outcome": dict(route.get("observed_outcome") or {}),
                "ranking_input": {
                    "mode": "paper_ordering_only",
                    "action": "diagnostic_labels_before_recommendation_ranking",
                    "score_adjustment": 0.0,
                },
                "paper_candidate_emission": "retained_for_paper_exploration",
                "hard_blocking": False,
                "entry_blocked": False,
                "routing_decision_changed": False,
            }
        )

    return {
        "summary_version": "execution_route_hunter_requirements_v1",
        "paper_only": True,
        "read_only": True,
        "prepared_before_recommendation_ranking": True,
        "labels": list(EXECUTION_ROUTE_REQUIREMENT_LABELS),
        "route_count": len(summaries),
        "routes": summaries,
        "ranking_policy": "diagnostic_only_no_eligibility_or_quarantine_change",
        "hard_blocking": False,
        "entry_blocked": False,
        "routing_decision_changed": False,
    }


def _attach_execution_route_requirement_summary(rec: dict, summary: dict) -> dict:
    """Attach deterministic route diagnostics to the hunter result pre-ranker."""

    decorated = dict(rec)
    evidence = decorated.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, dict) else {}
    evidence["execution_route_requirement_summary"] = summary
    evidence["paper_only"] = True
    decorated["evidence"] = evidence
    decorated["execution_route_requirement_summary"] = summary
    return decorated


def _strategy_invention_context(packet: dict, memory: list[dict]) -> dict:
    lab = packet.get("strategy_lab") if isinstance(packet.get("strategy_lab"), dict) else {}
    recent_experiments = []
    for item in (lab.get("recent") or [])[:18]:
        if not isinstance(item, dict):
            continue
        logic = item.get("compiled_strategy_logic") or item.get("strategy_logic") or {}
        recent_experiments.append(
            {
                "strategy_lab_id": item.get("strategy_lab_id"),
                "status": item.get("status"),
                "source_surface": item.get("source_surface"),
                "logic_type": logic.get("type") if isinstance(logic, dict) else None,
                "hypothesis": str(item.get("hypothesis") or "")[:420],
            }
        )
    observed_surfaces: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for item in (packet.get("top_reviewed") or [])[:30]:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("trade_type") or ""),
            str(item.get("venue") or ""),
            str(item.get("direction") or ""),
        )
        if not any(key) or key in seen:
            continue
        seen.add(key)
        observed_surfaces.append(
            {
                "trade_type": key[0],
                "venue": key[1],
                "direction": key[2],
                "decision": item.get("decision"),
                "net_edge_bps": item.get("net_edge_bps"),
                "route_status": item.get("route_status"),
            }
        )
        if len(observed_surfaces) >= 16:
            break
    discovery = packet.get("global_market_discovery") if isinstance(packet.get("global_market_discovery"), dict) else {}
    discovered_surfaces = [
        {
            "surface": item.get("surface_type_classified"),
            "venue": item.get("venue_or_source"),
            "region": item.get("region"),
            "next_action": item.get("recommended_next_action"),
        }
        for item in (discovery.get("top_candidates") or [])[:12]
        if isinstance(item, dict)
    ]
    recent_text = " ".join(
        str(item.get("hypothesis") or "") for item in recent_experiments
    ).lower()
    underrepresented = []
    for item in discovered_surfaces:
        surface = str(item.get("surface") or "").strip()
        venue = str(item.get("venue") or "").strip()
        if (surface and surface.lower() not in recent_text) or (venue and venue.lower() not in recent_text):
            underrepresented.append(item)
    relevant_memory = []
    for item in memory[:24]:
        if not isinstance(item, dict):
            continue
        encoded = json.dumps(item, sort_keys=True, default=str)
        if any(term in encoded.lower() for term in ("strategy", "paper outcome", "market", "signal")):
            relevant_memory.append(encoded[:900])
        if len(relevant_memory) >= 8:
            break
    reliable_outcomes = [
        {
            "trade_id": item.get("trade_id"),
            "horizon_minutes": item.get("horizon_minutes"),
            "pnl_bps": item.get("pnl_bps"),
            "measurement_status": item.get("measurement_status"),
            "price_source": item.get("price_source"),
        }
        for item in (packet.get("horizon_outcomes") or [])[:12]
        if isinstance(item, dict)
    ]
    current_outputs = [
        {
            "agent_name": item.get("agent_name"),
            "action": item.get("action"),
            "title": item.get("title"),
            "market_key": item.get("market_key"),
            "rationale": str(item.get("rationale") or "")[:500],
        }
        for item in (packet.get("current_cycle_agent_outputs") or [])[:8]
        if isinstance(item, dict)
    ]
    current_critiques = [
        {
            "agent_name": item.get("agent_name"),
            "title": item.get("title"),
            "rationale": str(item.get("rationale") or "")[:500],
        }
        for item in (packet.get("current_cycle_critiques") or [])[:8]
        if isinstance(item, dict)
    ]
    return {
        "paper_summary": packet.get("summary"),
        "strategy_lab_counts": {
            key: lab.get(key)
            for key in ("status_counts", "compile_status_counts", "novelty_status_counts")
        },
        "recent_experiments": recent_experiments,
        "observed_runtime_surfaces": observed_surfaces,
        "underrepresented_discoveries": underrepresented[:10],
        "reliable_outcome_examples": reliable_outcomes,
        "contextual_performance": (packet.get("contextual_stats") or [])[:16],
        "current_cycle_agent_outputs": current_outputs,
        "current_cycle_critiques": current_critiques,
        "relevant_memory": relevant_memory,
    }


def _strategy_lab_agent_prompt(agent: dict, packet: dict, memory: list[dict]) -> str:
    context = _strategy_invention_context(packet, memory)
    return (
        f"You are {agent['name']}. Your job is to invent one genuinely reusable paper-testable trading hypothesis from the current evidence.\n"
        "Return exactly one JSON object. action must be propose_strategy_lab_experiment or no_action. "
        "Do not return refine, modify, hold, or prose outside JSON. priority must be an integer from 1 to 100.\n"
        "The recommendation object requires action, priority, title, rationale, market_key, evidence, proposed_change, and strategy_lab_experiment.\n"
        "strategy_lab_experiment requires strategy_lab_id, version, experiment_type='market_strategy', hypothesis, source_surface, "
        "a non-empty permitted_target_surface, strategy_logic, data_requirements, risk_gates, and promotion_rules.\n"
        "experiment_type must be one of market_strategy, risk_filter, execution_filter, system_repair, or reporting_quality; this invention agent should normally use market_strategy. "
        "trade_types are scanner families and directions are trade actions. Do not put a direction in trade_types.\n"
        "Paper exploration is enabled: use weak performance as evidence and do not propose new hard quarantines for priceable candidates.\n"
        "Prefer strategy_logic.type='observation_program' so the idea can operate on normalized observations rather than merely filtering an old scanner. "
        "Define universe, calculated_features, entry_expression, optional invalidation_expression, direction or long_expression/short_expression, "
        "edge_expression, score_expression, and route_surface. Available features include returns and momentum at 5m/15m/60m/4h/1d, "
        "volatility, z-scores, relative strength, spread, liquidity, quality, funding, basis, and cross-venue dislocation. "
        "Use implementable safe expressions with arithmetic, comparisons, boolean logic, abs/min/max/round/sqrt/log/log1p/clip.\n"
        "Invent a market behavior, not a one-ticker trade and not another renamed quality gate. Compare against recent experiments below. "
        "Prefer an underrepresented market surface or a different causal mechanism such as cross-sectional relative value, term structure, "
        "volatility regime, event response, reversal, continuation, seasonality, or carry when the evidence supports it. These are examples, not an allowlist. "
        "If an idea needs a missing feature, state it in data_requirements; do not pretend the feature already exists. "
        "Keep live trading disabled and keep route limits diagnostic so synthetic paper research remains possible.\n"
        "CURRENT INVENTION CONTEXT\n"
        + json.dumps(context, sort_keys=True, default=str)
    )


def agent_prompt(agent: dict, packet: dict, memory: list[dict]) -> str:
    if agent.get("name") == "strategy_lab":
        return _strategy_lab_agent_prompt(agent, packet, memory)
    compact = {
        "summary": packet.get("summary"),
        "execution_summary": packet.get("execution_summary"),
        "llm_cost_summary": packet.get("llm_cost_summary"),
        "buckets": packet.get("buckets"),
        "top_reviewed": packet.get("top_reviewed", [])[:10],
        "horizon_outcomes": packet.get("horizon_outcomes", [])[:20],
        "contextual_stats": packet.get("contextual_stats", [])[:20],
        "crypto_venue_health": packet.get("crypto_venue_health", [])[:10],
        "frontier_gap_summary": packet.get("frontier_gap_summary", {}),
        "frontier_crypto_venues": packet.get("frontier_crypto_venues", {}),
        "expansion_map": packet.get("expansion_map", {}),
        "route_intelligence": (packet.get("expansion_map", {}) or {}).get("route_intelligence", {}),
        "short_frontier_spot_route_outcomes": packet.get("short_frontier_spot_route_outcomes", {}),
        "execution_route_requirement_summary": packet.get("execution_route_requirement_summary", {}),
        "paper_route_requirement_summaries": packet.get("paper_route_requirement_summaries", {}),
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
        "dynamic_agents": packet.get("dynamic_agents", {}),
        "relevant_long_term_memory": memory,
        "allowed_actions": packet.get("allowed_recommendation_actions", []),
        "current_cycle_agent_outputs": packet.get("current_cycle_agent_outputs", [])[:10],
        "current_cycle_critiques": packet.get("current_cycle_critiques", [])[:10],
        "current_cycle_ranked_actions": packet.get("current_cycle_ranked_actions", [])[:10],
        "repository_grounding": packet.get("repository_grounding", {}),
        "strategy_invention_context": (
            _strategy_invention_context(packet, memory)
            if "propose_strategy_lab_experiment" in set(agent.get("allowed_actions") or [])
            else {}
        ),
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
    route_hunter_instruction = ""
    if agent["name"] == "execution_route_hunter":
        route_hunter_instruction = (
            "Use execution_route_requirement_summary and paper_route_requirement_summaries, prepared before recommendation ranking, to emit read-only "
            "route diagnostics for borrow availability, fee pressure, margin needs, API/borrow feasibility, "
            "permissions, spread/liquidity, and carry. "
            "Weak observed paper PnL is route-specific diagnostic and paper-ordering evidence only: retain "
            "candidate emission and do not recommend suppression, quarantine, or a paper-entry block. "
            "If you return an actionable route recommendation, include evidence.paper_safe_route as a JSON object "
            "with explicit validated paper-only route facts such as route_status, route_decision, route_id, or "
            "route_actionability. If no explicit paper-safe route is available, return action='no_action'.\n"
        )
    dynamic_instruction = ""
    if agent.get("dynamic_agent_id"):
        invention_context = ""
        if "propose_strategy_lab_experiment" in set(agent.get("allowed_actions") or []):
            invention_context = (
                "Current strategy-invention context: "
                + json.dumps(_strategy_invention_context(packet, memory), sort_keys=True, default=str)[:6000]
                + "\n"
            )
        dynamic_instruction = (
            f"You are persistent specialist {agent.get('display_name')}. Your durable objective is: {agent['role']}\n"
            f"Your allowed actions are exactly: {agent.get('allowed_actions', [])}. "
            f"Use these evidence inputs when present: {agent.get('evidence_inputs', [])}. "
            f"Your success measure is: {agent.get('success_measure', {})}. "
            "You may propose code, but all code must go through the normal serialized code-evolution path.\n"
            f"{invention_context}"
        )
    return (
        f"You are {agent['name']}. Role: {agent['role']}\n"
        f"{dynamic_instruction}"
        f"{build_planner_instruction}"
        f"{route_hunter_instruction}"
        "Paper exploration is enabled. Treat weak performance, route limits, low quality, spread, liquidity, and cost as diagnostic evidence, ranking, sizing, synthetic-paper routing, or guard-value measurement; do not propose new hard quarantines, candidate suppression, or paper-entry blocks for priceable candidates. Only invalid or dangerously stale prices, critically malformed data, undefined PnL, missing required multi-leg prices without a proxy, duplicate exposure, or capacity deferral may prevent a paper experiment.\n"
        "Return exactly one JSON object matching this schema:\n"
        "{"
        "\"action\": allowed action, "
        "\"priority\": integer 1-100, "
        "\"title\": short title, "
        "\"rationale\": reason, "
        "\"market_key\": market identifier, "
        "\"signal_key\": optional signal, "
        "\"evidence\": object, "
        "\"frontier_escalation_reason\": required if a frontier model is used, "
        "\"proposed_change\": concrete bounded proposal, "
        "\"variant_config\": optional bounded config for propose_signal_variant, "
        "\"strategy_lab_experiment\": optional object for propose_strategy_lab_experiment, "
        "\"code_change\": optional object for propose_code_change, "
        "\"agent_spec\": optional object for spawn_agent"
        "}\n"
        "For spawn_agent, include agent_spec with name, objective, triggers, evidence_inputs, memory_policy, "
        "model_tier, allowed_actions, and success_measure. Triggers may use always, any_packet_paths, "
        "all_packet_paths, any_terms, all_terms, conditions, and cooldown_minutes. Create a persistent "
        "specialist only when a durable objective deserves repeated attention; exact duplicates are merged.\n"
        "For propose_strategy_lab_experiment, emit a strategy_lab_experiment object with: "
        "strategy_lab_id, experiment_type, hypothesis, source_surface, permitted_target_surface, "
        "strategy_logic, data_requirements, "
        "risk_gates, and promotion_rules. experiment_type must be one of market_strategy, "
        "risk_filter, execution_filter, system_repair, or reporting_quality. Use "
        "market_strategy only for actual reusable trading hypotheses; use the other types for "
        "filters, route/execution gates, output repairs, or report-quality experiments. "
        "Runtime supports strategy_logic.type='candidate_filter' over "
        "existing candidate fields: venues, trade_types, directions, regions, asset_classes, "
        "min_edge_bps, min_score, min_liquidity_score, max_spread_bps, min_quality_score, "
        "max_stale_minutes, required_fields, max_candidates_per_loop, score_bonus, and "
        "edge_bonus_bps. Every paper-testable contract must scope at least one trade_type, venue, "
        "direction, region, or asset_class. source_surface must name the market context from which the idea "
        "was derived. permitted_target_surface must be a non-empty string or list of exact target surfaces; "
        "missing metadata and unlisted targets are quarantined. allow_any_surface does not bypass this policy. "
        "trade_types are scanner families such as frontier_crypto_venue_map, "
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
    rec, parse_status, reason = _parse_json_recommendation(
        text,
        strict_contract=agent.get("name") == "cross_market_researcher",
    )
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
    evidence = rec.get("evidence")
    if not isinstance(evidence, dict):
        if isinstance(evidence, str) and evidence.strip():
            rec["evidence"] = {"summary": evidence.strip(), "source_format": "string"}
        elif isinstance(evidence, list):
            rec["evidence"] = {"items": evidence, "source_format": "list"}
        else:
            rec["evidence"] = {}
    agent_allowed = set(agent.get("allowed_actions") or [])
    if agent.get("dynamic_agent_id") and agent_allowed and rec.get("action") not in agent_allowed:
        return _reject_recommendation(agent, text, "invalid_action", "action_not_allowed_for_dynamic_agent")
    if rec.get("action") == "spawn_agent":
        try:
            normalized_spec = normalize_agent_spec(
                rec.get("agent_spec"),
                source_agent=str(agent.get("dynamic_agent_id") or agent["name"]),
            )
        except (TypeError, ValueError) as exc:
            return _reject_recommendation(agent, text, "invalid_schema", f"invalid_agent_spec:{exc}")
        rec["agent_spec"] = normalized_spec
    if _describes_no_change(rec):
        return _reject_recommendation(
            agent,
            str(rec.get("rationale") or rec.get("proposed_change") or "Recommendation describes no change."),
            parse_status,
            "non_actionable_hold_or_rerun",
        )
    if rec.get("action") == "propose_code_change":
        if agent["name"] != "build_planner" and not agent.get("dynamic_agent_id"):
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
    if agent["name"] == "execution_route_hunter":
        guarded = _guard_execution_route_hunter_recommendation(agent, rec, packet)
        if guarded is not rec:
            return guarded
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
    generation_attempts = {"initial": _generation_metadata(result)}
    rec = parse_recommendation(result.text, agent, packet)
    strict_failure = _strict_contract_failure(agent, result.text)
    if strict_failure is not None:
        parse_status, reason = strict_failure
        rec = _reject_recommendation(agent, result.text, parse_status, reason)
    _record_post_processor_output(generation_attempts["initial"], rec)
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
        strict_failure = _strict_contract_failure(agent, retry.text)
        if strict_failure is not None:
            parse_status, reason = strict_failure
            retry_rec = _reject_recommendation(agent, retry.text, parse_status, reason)
        generation_attempts["retry"] = _generation_metadata(retry)
        _record_post_processor_output(generation_attempts["retry"], retry_rec)
        retry_rec["retry_count"] = 1
        retry_rec["initial_parse_status"] = rec.get("parse_status")
        result = retry
        rec = retry_rec
    rec = _finalize_strict_contract_recommendation(
        agent,
        rec,
        result.text,
        generation_attempts,
    )
    rec = _finalize_agent_recommendation(
        agent,
        rec,
        generation_attempts,
    )
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


def _generation_metadata(result: Any) -> dict[str, Any]:
    """Keep model output context with a fallback without exposing configuration secrets."""
    response_text = str(getattr(result, "text", "") or "")
    stop_reason = (
        getattr(result, "stop_reason", None)
        or getattr(result, "finish_reason", None)
        or getattr(result, "status", None)
    )
    return {
        # ``response_text`` remains for existing report readers.  The explicit
        # raw/post-processor fields make it possible to distinguish provider
        # truncation from a transformation in the recommendation parser.
        "response_text": response_text,
        "raw_model_output": response_text,
        "model_name": getattr(result, "model_name", None),
        "model_tier": getattr(result, "model_tier", None),
        "status": getattr(result, "status", None),
        "api": getattr(result, "api", None),
        "prompt_tokens": getattr(result, "prompt_tokens", None),
        "completion_tokens": getattr(result, "completion_tokens", None),
        "stop_reason": stop_reason,
        "reasoning_effort": getattr(result, "reasoning_effort", None),
        "reasoning_mode": getattr(result, "reasoning_mode", None),
        "verbosity": getattr(result, "verbosity", None),
        "structured_json": getattr(result, "structured_json", None),
        "transport_integrity": _transport_integrity(response_text, result),
    }


def _transport_integrity(raw_response: str, result: Any) -> dict[str, Any]:
    """Record observable transport integrity without guessing provider limits.

    A complete raw JSON object proves that the application received a complete
    recommendation.  A partial object is marked as a suspected cutoff, but we
    do not claim whether the provider token limit or an upstream transport was
    responsible because that metadata is not available on every provider.
    """
    raw_schema_valid = False
    raw_schema_error: str | None = None
    try:
        finalize_recommendation_response(raw_response)
        raw_schema_valid = True
    except ValueError as exc:
        raw_schema_error = str(exc)
    truncated = _appears_truncated_json(raw_response)
    prompt_tokens = getattr(result, "prompt_tokens", None)
    completion_tokens = getattr(result, "completion_tokens", None)
    max_output_tokens = getattr(result, "max_output_tokens", None)
    stop_reason = (
        getattr(result, "stop_reason", None)
        or getattr(result, "finish_reason", None)
        or getattr(result, "status", None)
    )
    token_limit_reached = (
        isinstance(completion_tokens, int)
        and not isinstance(completion_tokens, bool)
        and isinstance(max_output_tokens, int)
        and not isinstance(max_output_tokens, bool)
        and completion_tokens >= max_output_tokens
    )
    if raw_schema_valid and not token_limit_reached:
        cutoff_assessment = "not_detected_complete_schema_object_below_token_limit"
    elif truncated and token_limit_reached:
        cutoff_assessment = "token_limit_cutoff_suspected"
    elif truncated:
        cutoff_assessment = "suspected_incomplete_payload"
    elif token_limit_reached:
        cutoff_assessment = "token_limit_reached_complete_schema_object"
    else:
        cutoff_assessment = "not_confirmed_schema_invalid"
    return {
        "raw_characters": len(raw_response),
        "raw_payload_size_bytes": len(raw_response.encode("utf-8")),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "max_output_tokens": max_output_tokens,
        "stop_reason": stop_reason,
        "token_limit_reached": token_limit_reached,
        "raw_schema_valid": raw_schema_valid,
        "raw_schema_error": raw_schema_error,
        "truncation_suspected": truncated,
        "application_buffer_limit": None,
        "buffer_limit_detected": False,
        "cutoff_assessment": cutoff_assessment,
    }


def _record_post_processor_output(attempt: dict[str, Any], recommendation: Any) -> None:
    """Persist the parser output separately from the provider's raw output."""
    try:
        attempt["post_processor_output"] = json.dumps(
            recommendation,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        attempt["post_processor_schema_valid"] = _has_complete_recommendation_schema(
            recommendation
        )
    except (TypeError, ValueError):
        attempt["post_processor_output"] = None
        attempt["post_processor_schema_valid"] = False


def _has_complete_recommendation_schema(candidate: Any) -> bool:
    """Check the final object shape, including meaningful required values."""
    if not validate_recommendation_object(candidate):
        return False
    if not isinstance(candidate, dict):
        return False
    try:
        serialized = json.dumps(candidate, ensure_ascii=False, allow_nan=False)
        decoded = json.loads(serialized)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(decoded, dict):
        return False
    if any(
        decoded.get(field) is None
        or (isinstance(decoded.get(field), str) and not decoded[field].strip())
        for field in REQUIRED_RECOMMENDATION_KEYS
    ):
        return False
    if not isinstance(decoded.get("action"), str):
        return False
    if isinstance(decoded.get("priority"), bool) or not isinstance(
        decoded.get("priority"), int
    ):
        return False
    if not isinstance(decoded.get("market_key"), str):
        return False
    return isinstance(decoded.get("evidence"), dict) and isinstance(
        decoded.get("proposed_change"), (dict, str)
    )


def _finalize_agent_recommendation(
    agent: dict,
    recommendation: Any,
    generation_attempts: dict[str, dict[str, Any]],
) -> dict:
    """Final schema guard for every recommendation emitted by the swarm.

    Parsing can enrich an otherwise valid object with provenance.  This final
    gate validates that post-processor object immediately before it is returned
    to the graph and replaces an invalid result with a minimal paper-only
    ``no_action`` object.  Both sides of the transformation remain in the
    report audit trail.
    """
    _record_post_processor_output(
        generation_attempts.setdefault("final", {}), recommendation
    )
    if not _has_complete_recommendation_schema(recommendation):
        fallback = paper_only_no_action_fallback(
            market_key=f"paper.{agent.get('name') or 'agent'}.schema_guard",
            rationale=(
                "The post-processor did not return one complete recommendation "
                "object with every required field."
            ),
        )
        fallback.update(
            {
                "_rejected": True,
                "accepted": False,
                "agent_name": agent.get("name"),
                "parse_status": "schema_fallback",
                "terminal_failure_reason": "post_processor_schema_violation",
                "provenance": {
                    "state_packet": str(STATE_JSON),
                    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                },
            }
        )
        _record_post_processor_output(generation_attempts["final"], fallback)
        recommendation = fallback
    else:
        recommendation = dict(recommendation)
    recommendation["model_output_audit"] = generation_attempts
    return recommendation


def _finalize_strict_contract_recommendation(
    agent: dict,
    recommendation: dict,
    raw_response: str,
    generation_attempts: dict[str, dict[str, Any]],
) -> dict:
    """Apply final strict schema gates for agents with locked response contracts."""
    name = agent.get("name")
    if name == "cross_market_researcher":
        try:
            finalize_cross_market_researcher_response(raw_response)
        except ValueError as exc:
            fallback = cross_market_researcher_schema_fallback(
                str(exc),
                raw_generation_metadata=generation_attempts,
            )
            fallback.update(
                {
                    "agent_name": agent["name"],
                    "parse_status": "schema_fallback",
                    "terminal_failure_reason": "cross_market_response_schema_violation",
                    "provenance": {
                        "state_packet": str(STATE_JSON),
                        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    },
                }
            )
            for field in ("retry_count", "initial_parse_status"):
                if field in recommendation:
                    fallback[field] = recommendation[field]
            return fallback
        return recommendation
    if name == "red_team":
        try:
            finalize_red_team_response(raw_response)
        except ValueError as exc:
            fallback = red_team_schema_fallback(
                str(exc),
                raw_generation_metadata=generation_attempts,
            )
            fallback.update(
                {
                    "agent_name": agent["name"],
                    "parse_status": "schema_fallback",
                    "terminal_failure_reason": "red_team_response_schema_violation",
                    "provenance": {
                        "state_packet": str(STATE_JSON),
                        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    },
                }
            )
            for field in ("retry_count", "initial_parse_status"):
                if field in recommendation:
                    fallback[field] = recommendation[field]
            return fallback
    return recommendation


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


def _parse_json_recommendation(
    text: str,
    *,
    strict_contract: bool = False,
) -> tuple[dict | None, str, str | None]:
    raw = (text or "").strip()
    if not raw:
        return None, "empty_response", "empty_response"
    if strict_contract:
        try:
            return finalize_cross_market_researcher_response(raw), "native_valid", None
        except ValueError:
            if _appears_truncated_json(raw):
                return None, "truncated_json", "truncated_json"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return None, "invalid_json", "extra_text_or_invalid_json"
            if not isinstance(parsed, dict):
                return None, "invalid_schema", "top_level_json_not_object"
            missing = [key for key in REQUIRED_RECOMMENDATION_KEYS if key not in parsed]
            if missing:
                return None, "invalid_schema", f"missing_required_fields:{','.join(missing)}"
            if parsed.get("action") not in CROSS_MARKET_RESEARCHER_ALLOWED_ACTIONS:
                return None, "invalid_action", "action_not_allowed_for_cross_market_researcher"
            priority = parsed.get("priority")
            if isinstance(priority, bool) or not isinstance(priority, int):
                return None, "invalid_schema", "priority_must_be_integer"
            return None, "invalid_schema", "recommendation_schema_invalid"
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
    return _is_rejected(rec) and rec.get("parse_status") in {
        "invalid_json",
        "truncated_json",
        "empty_response",
        "invalid_schema",
        "invalid_action",
    }


def _strict_contract_failure(agent: dict, raw_response: str) -> tuple[str, str] | None:
    """Return a parse status and reason when an agent's raw contract is violated."""
    name = str(agent.get("name") or "")
    if name not in {"cross_market_researcher", "red_team"}:
        return None
    try:
        if name == "cross_market_researcher":
            finalize_cross_market_researcher_response(raw_response)
        else:
            finalize_red_team_response(raw_response)
        return None
    except ValueError:
        if _appears_truncated_json(raw_response):
            return "truncated_json", "truncated_json"
        try:
            parsed = json.loads(raw_response)
        except json.JSONDecodeError:
            return "invalid_json", "extra_text_or_invalid_json"
        if not isinstance(parsed, dict):
            return "invalid_schema", "top_level_json_not_object"
        missing = [key for key in REQUIRED_RECOMMENDATION_KEYS if key not in parsed]
        if missing:
            return "invalid_schema", f"missing_required_fields:{','.join(missing)}"
        if name == "cross_market_researcher":
            if parsed.get("action") not in CROSS_MARKET_RESEARCHER_ALLOWED_ACTIONS:
                return "invalid_action", "action_not_allowed_for_cross_market_researcher"
        else:
            if parsed.get("action") not in {"no_action", "propose_diagnostic_hypothesis"}:
                return "invalid_action", "action_not_allowed_for_red_team"
            unexpected = [key for key in parsed if key not in REQUIRED_RECOMMENDATION_KEYS]
            if unexpected:
                return "invalid_schema", f"unexpected_fields:{','.join(sorted(unexpected))}"
        priority = parsed.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            return "invalid_schema", "priority_must_be_integer"
        return "invalid_schema", "recommendation_schema_invalid"


def _schema_retry_prompt(agent: dict, original_text: str) -> str:
    allowed_actions = list(agent.get("allowed_actions") or [agent.get("default_action")])
    allowed_actions = [str(action) for action in allowed_actions if action]
    allowed_actions.append("no_action")
    strategy_contract = ""
    if agent.get("name") == "strategy_lab" or "propose_strategy_lab_experiment" in allowed_actions:
        strategy_contract = (
            " If action is propose_strategy_lab_experiment, include strategy_lab_experiment with "
            "strategy_lab_id, version, experiment_type='market_strategy', hypothesis, source_surface, "
            "non-empty permitted_target_surface, strategy_logic, data_requirements, risk_gates, and promotion_rules."
        )
    if agent.get("name") == "red_team":
        return (
            "The previous red_team response was not a complete valid JSON recommendation. "
            "Return exactly one JSON object and no prose. Use exactly these top-level keys: "
            "action, priority, title, rationale, market_key, evidence, proposed_change. "
            "action must be either \"no_action\" or \"propose_diagnostic_hypothesis\". "
            "priority must be an integer 1-100. evidence and proposed_change must be JSON objects. "
            "Use this exact schema-locked template shape:\n"
            "{\"action\":\"propose_diagnostic_hypothesis\",\"priority\":50,\"title\":\"...\","
            "\"rationale\":\"...\",\"market_key\":\"paper.red_team.<scope>\","
            "\"evidence\":{\"issue\":\"...\"},\"proposed_change\":{\"summary\":\"...\"}}\n"
            "No markdown, no commentary, no extra keys, no arrays at the top level. Keep it paper-only.\n\n"
            f"Previous response preview:\n{(original_text or '')[:1200]}"
        )
    if agent.get("name") == "cross_market_researcher":
        return (
            "The previous cross_market_researcher response was not a complete valid JSON recommendation. "
            "Return exactly one JSON object and no prose. Use exactly these top-level keys: "
            "action, priority, title, rationale, market_key, evidence, proposed_change. "
            "action must be either \"no_action\" or \"propose_diagnostic_hypothesis\". "
            "priority must be an integer 1-100. Every required key must be present and non-empty. "
            "title, rationale, and market_key must be non-empty strings. "
            "evidence and proposed_change must be non-empty JSON objects. "
            "Do not emit a market recommendation unless the evidence object contains explicit cross-market support facts in-schema. "
            "If the available market evidence is insufficient for a reliable thesis, "
            "default to action=\"propose_diagnostic_hypothesis\" with a paper-only diagnostic hypothesis and make clear that the market recommendation is blocked until sufficient cross-market evidence is supplied in-schema. "
            "Use this exact schema-locked template shape:\n"
            "{\"action\":\"propose_diagnostic_hypothesis\",\"priority\":100,\"title\":\"...\"," 
            "\"rationale\":\"...\",\"market_key\":\"paper.cross_market_researcher.<scope>\","
            "\"evidence\":{\"schema_violation\":\"...\",\"market_recommendation_blocked\":true,\"paper_only\":true},"
            "\"proposed_change\":{\"summary\":\"...\",\"paper_only\":true}}\n"
            "No markdown, no commentary, no extra keys, no arrays at the top level. Keep it paper-only.\n\n"
            f"Previous response preview:\n{(original_text or '')[:1200]}"
        )
    if agent.get("name") == "execution_route_hunter":
        return (
            "The previous execution_route_hunter response was not a complete valid paper-only route recommendation. "
            "Return exactly one JSON object and no prose. Use exactly these top-level keys: "
            "action, priority, title, rationale, market_key, evidence, proposed_change. "
            "action must be one of the allowed actions or \"no_action\". priority must be an integer 1-100. "
            "If action is not \"no_action\", evidence must include paper_safe_route as a JSON object with explicit "
            "validated paper-only route facts such as route_status, route_decision, route_id, or route_actionability. "
            "If no explicit paper-safe route is available, return action=\"no_action\" with the failure captured in evidence. "
            "No markdown, no commentary, no extra keys, no arrays at the top level. Keep it paper-only.\n\n"
            f"Previous response preview:\n{(original_text or '')[:1200]}"
        )
    return (
        f"The previous {agent['name']} response was not a complete valid JSON recommendation. "
        "Return exactly one complete JSON object with: action, priority, title, rationale, "
        "market_key, evidence, proposed_change, and optional code_change, variant_config, "
        "strategy_lab_experiment, or agent_spec. "
        f"action must be one of {sorted(set(allowed_actions))}; priority must be an integer 1-100."
        f"{strategy_contract} "
        "Do not use refine, modify, hold, revise_recommendation, or any other action. "
        "No markdown, no commentary, no top-level arrays. Keep it paper-only.\n\n"
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


def _coerce_role_memory(
    memory: list[dict] | dict[str, list[dict]],
    agent_names: list[str] | None = None,
) -> dict[str, list[dict]]:
    names = agent_names or [agent["name"] for agent in AGENTS]
    if isinstance(memory, dict):
        if agent_names is None:
            names = list(dict.fromkeys([*names, *memory.keys()]))
        return {name: list(memory.get(name) or []) for name in names}
    return {name: list(memory or []) for name in names}


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
        "dynamic_agent_id": agent.get("dynamic_agent_id"),
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
                "dynamic_agent_id": agent.get("dynamic_agent_id"),
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
    route_requirement_summary: dict | None = None
    if agent["name"] == "execution_route_hunter":
        route_requirement_summary = build_execution_route_requirement_summary(agent_packet)
        agent_packet["execution_route_requirement_summary"] = route_requirement_summary
    if agent["name"] == "build_planner" or (
        agent.get("dynamic_agent_id") and "propose_code_change" in set(agent.get("allowed_actions") or [])
    ):
        agent_packet["repository_grounding"] = _repository_grounding(state)
    role_memory = runtime_role_memory if runtime_role_memory is not None else state.get("role_memory") or {}
    memory = list(role_memory.get(agent["name"]) or state.get("memory") or [])
    rec = run_agent(agent, agent_packet, memory)
    if route_requirement_summary is not None:
        rec = _attach_execution_route_requirement_summary(rec, route_requirement_summary)
    if agent["name"] == "build_planner":
        upstream_runs = [
            item.get("recommendation", {}).get("dynamic_agent_run_id")
            for item in state.get("agent_outputs", [])
            if isinstance(item, dict)
            and isinstance(item.get("recommendation"), dict)
            and item.get("recommendation", {}).get("dynamic_agent_run_id")
        ]
        if upstream_runs:
            rec.setdefault("evidence", {})["upstream_dynamic_agent_runs"] = list(dict.fromkeys(upstream_runs))
    rec = decorate_dynamic_recommendation(agent, rec, str(state.get("cycle_id") or "unknown_cycle"))
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


def _run_agent_architect_node(state: SwarmState) -> SwarmState:
    rec = architect_recommendation(state.get("dynamic_agent_cycle") or {})
    if rec is None:
        return {"graph_trace": [{"node": "agent_architect", "status": "no_spawn_candidate"}]}
    return _record_agent_result(
        {"name": "agent_architect", "role": "Create persistent specialists for recurring uncovered objectives."},
        rec,
        0,
        [],
    )


def run_sequential(
    packet: dict,
    memory: list[dict] | dict[str, list[dict]],
    settings: dict | None = None,
    cycle_id: str | None = None,
    dynamic_agents: list[dict] | None = None,
    dynamic_cycle: dict | None = None,
) -> list[dict]:
    global LAST_SWARM_STATE
    dynamic_agents = list(dynamic_agents or [])
    state = _initial_state(packet, memory, FALLBACK_COLLABORATION_MODE, cycle_id)
    state["dynamic_agent_cycle"] = dynamic_cycle or {}
    by_name = {agent["name"]: agent for agent in AGENTS}
    for name in ("market_scout", "cross_market_researcher", "strategy_lab"):
        state = _merge_state(state, _run_agent_node(by_name[name], state))
    for agent in dynamic_agents:
        state = _merge_state(state, _run_agent_node(agent, state))
    state = _merge_state(state, _run_agent_architect_node(state))
    for name in ("red_team", "execution_route_hunter", "build_planner"):
        state = _merge_state(state, _run_agent_node(by_name[name], state))
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
    dynamic_agents: list[dict] | None = None,
    dynamic_cycle: dict | None = None,
) -> list[dict]:
    global LAST_SWARM_STATE
    settings = settings or load_settings()
    try:
        from langgraph.graph import END, START, StateGraph  # type: ignore
    except Exception:
        return run_sequential(packet, memory, settings, cycle_id, dynamic_agents, dynamic_cycle)

    dynamic_agents = list(dynamic_agents or [])
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
    for agent in [*AGENTS, *dynamic_agents]:
        graph.add_node(agent["name"], make_node(agent))
    graph.add_node("research_join", phase_node("research_join"))
    graph.add_node("critique_join", phase_node("critique_join"))
    graph.add_node("ranker", ranker_node)
    graph.add_node("agent_architect", _run_agent_architect_node)
    graph.add_node("memory_checkpoint", phase_node("memory_checkpoint"))

    graph.add_edge(START, "market_scout")
    graph.add_edge(START, "cross_market_researcher")
    graph.add_edge(["market_scout", "cross_market_researcher"], "research_join")
    graph.add_edge("research_join", "strategy_lab")
    specialist_tail = "strategy_lab"
    if dynamic_agents:
        effective = max(1, int((dynamic_cycle or {}).get("concurrency", {}).get("effective", 8)))
        first_size = max(0, effective - 1)
        offset = 0
        if first_size:
            first_batch = dynamic_agents[:first_size]
            for agent in first_batch:
                graph.add_edge("research_join", agent["name"])
            graph.add_node("dynamic_join_0", phase_node("dynamic_join_0"))
            graph.add_edge(["strategy_lab", *[agent["name"] for agent in first_batch]], "dynamic_join_0")
            specialist_tail = "dynamic_join_0"
            offset = len(first_batch)
        batch_index = 1
        while offset < len(dynamic_agents):
            batch = dynamic_agents[offset : offset + effective]
            for agent in batch:
                graph.add_edge(specialist_tail, agent["name"])
            join_name = f"dynamic_join_{batch_index}"
            graph.add_node(join_name, phase_node(join_name))
            sources = [agent["name"] for agent in batch]
            graph.add_edge(sources if len(sources) > 1 else sources[0], join_name)
            specialist_tail = join_name
            offset += len(batch)
            batch_index += 1
    graph.add_edge(specialist_tail, "agent_architect")
    graph.add_edge("agent_architect", "red_team")
    graph.add_edge("agent_architect", "execution_route_hunter")
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
    initial["dynamic_agent_cycle"] = dynamic_cycle or {}
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


def _json_safe_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _paper_scoped_market_key(value: Any, *, agent_name: str | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        text = f"{agent_name or 'agent'}.recommendation"
    text = re.sub(r"[^A-Za-z0-9._:-]+", "_", text).strip("._:-")
    if not text.lower().startswith("paper"):
        text = f"paper.{text or agent_name or 'agent'}"
    return text


def _publish_evidence(value: Any, recommendation: dict) -> dict[str, Any]:
    if isinstance(value, dict) and value:
        evidence = _json_safe_clone(value)
    else:
        evidence = {}
    if not evidence:
        parse_status = str(recommendation.get("parse_status") or "").strip()
        failure_reason = str(recommendation.get("terminal_failure_reason") or "").strip()
        if parse_status:
            evidence["parse_status"] = parse_status
        if failure_reason:
            evidence["terminal_failure_reason"] = failure_reason
    if not evidence:
        evidence["issue"] = "missing_publish_evidence"
    return evidence


def _publish_proposed_change(value: Any, recommendation: dict) -> dict[str, Any]:
    if isinstance(value, dict) and value:
        proposed_change = _json_safe_clone(value)
    else:
        text = str(value or "").strip() or str(recommendation.get("rationale") or "").strip()
        proposed_change = {"summary": text or "Keep the recommendation paper-only and auditable."}
    return proposed_change


def _publish_fallback_recommendation(recommendation: dict, reason: str) -> dict[str, Any]:
    fallback = paper_only_no_action_fallback(
        market_key=_paper_scoped_market_key(
            recommendation.get("market_key"),
            agent_name=str(recommendation.get("agent_name") or "agent"),
        ),
        rationale=(
            "The recommendation could not be normalized into one complete JSON object "
            "for downstream consumers."
        ),
    )
    fallback["evidence"] = {
        **fallback["evidence"],
        "mode": "fallback",
        "schema_violation": reason,
        "original_action": str(recommendation.get("action") or ""),
    }
    fallback["proposed_change"] = {
        "summary": "Suppress the malformed recommendation and keep the paper-only audit trail.",
        "paper_trade_instruction": "No action. Simulation and reporting only; no execution.",
    }
    return fallback


def _publishable_recommendation_payload(recommendation: dict) -> dict[str, Any]:
    action = str(recommendation.get("action") or "").strip()
    payload = {
        "action": action or "no_action",
        "priority": _coerce_priority(recommendation.get("priority"), default=1),
        "title": str(recommendation.get("title") or "Paper-only schema guard").strip(),
        "rationale": str(
            recommendation.get("rationale")
            or "The recommendation was normalized for downstream paper-only consumers."
        ).strip(),
        "market_key": _paper_scoped_market_key(
            recommendation.get("market_key"),
            agent_name=str(recommendation.get("agent_name") or "agent"),
        ),
        "evidence": _publish_evidence(recommendation.get("evidence"), recommendation),
        "proposed_change": _publish_proposed_change(
            recommendation.get("proposed_change"),
            recommendation,
        ),
    }
    signal_key = str(recommendation.get("signal_key") or "").strip()
    if signal_key:
        payload["signal_key"] = signal_key
    directive = str(recommendation.get("directive") or "").strip()
    if directive:
        payload["directive"] = directive
    for field in ("variant_config", "strategy_lab_experiment", "code_change", "agent_spec"):
        value = recommendation.get(field)
        if isinstance(value, dict) and value:
            payload[field] = _json_safe_clone(value)

    required_payload = PUBLISH_REQUIRED_ACTION_PAYLOADS.get(payload["action"])
    if required_payload and required_payload not in payload:
        return _publish_fallback_recommendation(
            recommendation,
            f"missing_required_action_payload:{required_payload}",
        )

    try:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        finalized = finalize_recommendation_response(serialized)
    except ValueError as exc:
        return _publish_fallback_recommendation(recommendation, str(exc))
    if any(
        field in finalized and not isinstance(finalized[field], dict)
        for field in ("evidence", "proposed_change", "variant_config", "strategy_lab_experiment", "code_change", "agent_spec")
    ):
        return _publish_fallback_recommendation(
            recommendation,
            "non_object_nested_payload",
        )
    return finalized


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
    actionable: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for rec in recommendations:
        published = _publishable_recommendation_payload(rec)
        publish_is_fallback = _is_fallback_recommendation(published) or published.get("action") == "no_action"
        if _is_rejected(rec) or (publish_is_fallback and not write_fallback) or (
            not write_fallback and _is_fallback_recommendation(rec)
        ):
            suppressed.append(published)
            continue
        actionable.append(published)
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
                "dynamic_agent_cycle": state.get("dynamic_agent_cycle", {}),
                "dynamic_agents": state.get("dynamic_agents", {}),
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


def _database_locked(exc: BaseException) -> bool:
    return "database is locked" in str(exc).lower()


def _record_post_model_state(settings: dict, cycle_id: str, dynamic_cycle: dict) -> None:
    """Persist swarm bookkeeping when possible without discarding paid model output."""

    global LAST_SWARM_STATE
    try:
        with connect() as conn:
            reflection = reflect_swarm(conn, LAST_SWARM_STATE, cycle_id, settings)
            dynamic_run_report = record_dynamic_agent_runs(conn, LAST_SWARM_STATE, dynamic_cycle, cycle_id)
            dynamic_summary = write_dynamic_agent_reports(conn, settings)
            graphiti = sync_graphiti(conn, settings)
            write_memory_exports(conn, settings)
    except sqlite3.OperationalError as exc:
        if not _database_locked(exc):
            raise
        deferred = {
            "status": "database_busy_retry_later",
            "stage": "post_model_persistence",
            "reason": str(exc),
        }
        LAST_SWARM_STATE["memory_reflection"] = deferred
        LAST_SWARM_STATE["dynamic_agent_cycle"] = {
            **dynamic_cycle,
            "run_recording": deferred,
        }
        LAST_SWARM_STATE["dynamic_agents"] = deferred
        LAST_SWARM_STATE["post_model_persistence"] = deferred
        return
    LAST_SWARM_STATE["memory_reflection"] = {**reflection, "graphiti": graphiti}
    LAST_SWARM_STATE["dynamic_agent_cycle"] = {**dynamic_cycle, "run_recording": dynamic_run_report}
    LAST_SWARM_STATE["dynamic_agents"] = dynamic_summary
    LAST_SWARM_STATE["post_model_persistence"] = {"status": "recorded"}


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
        dynamic_cycle = prepare_dynamic_agent_cycle(conn, packet, settings, cycle_id)
        dynamic_agents = list(dynamic_cycle.get("matched_agents") or [])
        dynamic_memory = build_dynamic_memory_contexts(
            conn,
            packet,
            dynamic_agents,
            settings,
            cycle_id,
        )
        if isinstance(memory, dict):
            memory.update(dynamic_memory)
        else:
            memory = {
                **_coerce_role_memory(memory),
                **dynamic_memory,
            }
    recommendations = run_langgraph_if_available(
        packet,
        memory,
        settings,
        cycle_id,
        dynamic_agents=dynamic_agents,
        dynamic_cycle=dynamic_cycle,
    )
    _record_post_model_state(settings, cycle_id, dynamic_cycle)
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
