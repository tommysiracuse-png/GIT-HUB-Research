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
import sys
from typing import Any

from cost_router import complete
from llm_bridge import INBOX, STATE_JSON
from memory_graph import query_memory
from settings import load_settings
from storage import RUNS_DIR, connect


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
        "self_improvement": packet.get("self_improvement", {}),
        "code_evolution": packet.get("code_evolution", {}),
        "recent_memory": memory[:20],
        "allowed_actions": packet.get("allowed_recommendation_actions", []),
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
        "\"code_change\": optional object for propose_code_change"
        "}\n"
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
        f"If uncertain, use action {agent['default_action']}.\n\n"
        f"STATE:\n{json.dumps(compact, sort_keys=True)}"
    )


def parse_recommendation(text: str, agent: dict, packet: dict) -> dict:
    allowed = set(packet.get("allowed_recommendation_actions", []))
    parsed_from_fallback = False
    try:
        rec = json.loads(text)
    except json.JSONDecodeError:
        parsed_from_fallback = True
        fallback_action = agent["default_action"]
        if agent["name"] == "build_planner":
            fallback_action = "propose_build_task"
        rec = {
            "action": fallback_action,
            "priority": 50,
            "title": f"{agent['name']} unstructured recommendation",
            "rationale": text[:1000],
            "market_key": agent["name"],
            "evidence": {"parser": "fallback", "downgraded_from_code_change": agent["name"] == "build_planner"},
            "proposed_change": text[:1000],
        }
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
                    "parser": "fallback" if parsed_from_fallback else "structured_guard",
                },
            }
    rec.setdefault("priority", 50)
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


def run_sequential(packet: dict, memory: list[dict]) -> list[dict]:
    recommendations: list[dict] = []
    for agent in AGENTS:
        agent_packet = dict(packet)
        agent_packet["current_cycle_recommendations"] = recommendations
        recommendations.append(run_agent(agent, agent_packet, memory))
    return recommendations


def run_langgraph_if_available(packet: dict, memory: list[dict]) -> list[dict]:
    try:
        from langgraph.graph import END, StateGraph  # type: ignore
    except Exception:
        return run_sequential(packet, memory)

    try:
        def make_node(agent: dict):
            def node(state: dict) -> dict:
                agent_packet = dict(state["packet"])
                agent_packet["current_cycle_recommendations"] = state["recommendations"]
                state["recommendations"].append(run_agent(agent, agent_packet, state["memory"]))
                return state

            return node

        graph = StateGraph(dict)
        previous = None
        for agent in AGENTS:
            graph.add_node(agent["name"], make_node(agent))
            if previous is None:
                graph.set_entry_point(agent["name"])
            else:
                graph.add_edge(previous, agent["name"])
            previous = agent["name"]
        graph.add_edge(previous, END)
        app = graph.compile()
        output = app.invoke({"packet": packet, "memory": memory, "recommendations": []})
        return output["recommendations"]
    except Exception:
        return run_sequential(packet, memory)


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


def write_recommendations(recommendations: list[dict], max_items: int, settings: dict | None = None) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = (settings or {}).get("llm_swarm", {})
    write_fallback = bool(cfg.get("write_fallback_recommendations_to_inbox", False))
    actionable = [
        rec
        for rec in recommendations
        if write_fallback or not _is_fallback_recommendation(rec)
    ]
    suppressed = [
        rec
        for rec in recommendations
        if not write_fallback and _is_fallback_recommendation(rec)
    ]
    selected = actionable[:max_items]
    action_package = {
        "ranked_actions": selected,
        "rejected_or_suppressed": suppressed[:max_items],
        "collaboration_mode": "shared_current_cycle_state",
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
                "suppressed_count": len(suppressed),
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
