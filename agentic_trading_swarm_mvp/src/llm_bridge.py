"""LLM bridge for state reading and bounded recommendations.

This module does not call an LLM by itself. It creates a compact state packet
that LLM agents can read, and it ingests recommendations from a JSONL inbox into
safe internal artifacts: build tasks, growth experiments, and hunter directives.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3

from storage import (
    RUNS_DIR,
    active_signal_policies,
    add_growth_experiment,
    add_hunter_directive,
    add_improvement_task,
    add_llm_recommendation,
    open_adapter_specs,
    open_experiments,
    open_hunter_directives,
    open_route_probe_tasks,
    open_self_improvement_experiments,
    open_tasks,
)
from code_evolution import code_evolution_summary
from self_improvement_open_pack import IMPLEMENTED_STATUS as OPEN_PACK_IMPLEMENTED_STATUS
from self_improvement_open_pack import is_duplicate_open_pack_text


STATE_JSON = RUNS_DIR / "llm_state_packet.json"
STATE_MD = RUNS_DIR / "llm_state_packet.md"
INBOX = RUNS_DIR / "llm_recommendations_inbox.jsonl"
PROCESSED = RUNS_DIR / "llm_recommendations_processed.jsonl"

IMPLEMENTED_MANUAL_STATUSES = {
    "route_requirements": (
        "implemented_route_requirements",
        ("improvement_tasks", "route_probe_tasks"),
    ),
    "frontier_crypto_adapter": (
        "implemented_frontier_crypto_adapter",
        ("improvement_tasks", "adapter_specs"),
    ),
    "failure_diagnostics": (
        "implemented_failure_diagnostics",
        ("improvement_tasks", "adapter_specs"),
    ),
    "signal_redesign": (
        "implemented_signal_redesign",
        ("improvement_tasks", "adapter_specs"),
    ),
    "frontier_data_quality": (
        "implemented_frontier_data_quality",
        ("improvement_tasks", "adapter_specs"),
    ),
    "okx_basis_signal_research": (
        "implemented_okx_basis_signal_research",
        ("adapter_specs",),
    ),
    "regional_frontier_data": (
        "implemented_regional_frontier_data",
        ("improvement_tasks", "adapter_specs"),
    ),
    "frontier_systemic_redesign": (
        "implemented_frontier_systemic_redesign",
        ("improvement_tasks", "growth_experiments"),
    ),
    "okx_reliable_outcomes": (
        "implemented_okx_reliable_outcomes",
        ("improvement_tasks",),
    ),
    "strategy_reliability_pack": (
        "implemented_strategy_reliability_pack",
        ("improvement_tasks", "growth_experiments"),
    ),
    "self_improvement_open_pack": (
        OPEN_PACK_IMPLEMENTED_STATUS,
        ("improvement_tasks", "growth_experiments"),
    ),
    "regional_fx_frontier_prediction_pack": (
        "implemented_regional_fx_frontier_prediction_pack",
        ("route_probe_tasks", "improvement_tasks", "adapter_specs"),
    ),
    "bybit_quality_decay_expansion_pack": (
        "implemented_bybit_quality_decay_expansion_pack",
        ("improvement_tasks", "growth_experiments"),
    ),
    "kucoin_long_repair_diagnostics": (
        "implemented_kucoin_long_repair_diagnostics",
        ("improvement_tasks", "growth_experiments"),
    ),
}

REQUIRED_RECOMMENDATION_FIELDS = (
    "action",
    "priority",
    "title",
    "rationale",
    "market_key",
    "evidence",
    "proposed_change",
)

MARKET_SCOUT_FALLBACK_RECOMMENDATION = {
    "action": "hold",
    "priority": 50,
    "title": "Fallback paper recommendation",
    "rationale": "Auto-generated because the primary response failed schema validation.",
    "market_key": "paper_system.integrity.market_scout",
    "evidence": {
        "issue": "schema_validation_failed",
    },
    "proposed_change": {"goal": "preserve parser compatibility"},
}

EXECUTION_ROUTE_HUNTER_FALLBACK_RECOMMENDATION = {
    "action": "refine",
    "priority": 85,
    "title": "Refine paper execution route recommendation",
    "rationale": (
        "Auto-generated because the primary execution-route response failed strict "
        "single-object validation, omitted required fields, or did not provide "
        "enough paper-only evidence to support routing analysis."
    ),
    "market_key": "paper.execution_route_hunter",
    "evidence": {
        "issue": "route_validation_failed",
        "validation_error": "schema_validation_failed",
        "paper_only": True,
    },
    "proposed_change": {
        "summary": "Return one schema-complete paper-only route recommendation or a conservative hold/refine decision.",
        "fallback_behavior": "Prefer refine or hold when route confidence or payload completeness is insufficient.",
        "required_fields": list(REQUIRED_RECOMMENDATION_FIELDS),
        "safety_mode": "paper_only",
        "suppress_live_execution_wording": True,
    },
}


def _signal_stats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at
        from signal_stats
        order by score_adjustment desc, closed_count desc
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _contextual_stats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select context_key, closed_count, wins, avg_pnl_bps, win_rate, updated_at
        from contextual_stats
        order by closed_count desc, abs(avg_pnl_bps) desc
        limit 50
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _bucketize(stats: list[dict], directives: list[dict]) -> dict:
    buckets = {"exploit": [], "explore": [], "diagnose": []}

    for item in stats:
        if item["score_adjustment"] > 2 and item["closed_count"] >= 3:
            buckets["exploit"].append(
                {
                    "signal_key": item["signal_key"],
                    "reason": "positive learned score adjustment",
                    "evidence": item,
                }
            )
        elif item["closed_count"] < 5:
            buckets["explore"].append(
                {
                    "signal_key": item["signal_key"],
                    "reason": "insufficient evidence; preserve exploration",
                    "evidence": item,
                }
            )
        elif item["score_adjustment"] < -2 or item["avg_pnl_bps"] < 0:
            buckets["diagnose"].append(
                {
                    "signal_key": item["signal_key"],
                    "reason": "negative performance or learned penalty",
                    "evidence": item,
                }
            )

    for directive in directives:
        if directive["directive"] == "exploit_more":
            buckets["exploit"].append(directive)
        elif directive["directive"] in {"observe", "expand_route_resolver", "route_resolver_active", "collect_market_hours_data"}:
            buckets["explore"].append(directive)
        elif directive["directive"] in {"demote_or_filter", "decay_watch", "red_team", "market_decay_watch"}:
            buckets["diagnose"].append(directive)

    return buckets


def _recommendation_schema(allowed_actions: list[str]) -> dict:
    required_fields = list(REQUIRED_RECOMMENDATION_FIELDS)
    return {
        "action": "one allowed action",
        "required_fields": required_fields,
        "response_contract": "Return exactly one top-level JSON object and nothing else.",
        "top_level_shape": "A single JSON object is required; top-level arrays are invalid.",
        "format_guardrails": "No markdown, commentary, code fences, or wrapper arrays around the recommendation object.",
        "paper_only_default": "Recommendations must remain limited to paper-trading simulation, reports, tests, adapters, routing analysis, and code evolution.",
        "priority": "integer 1-100",
        "fallback_behavior": (
            "If any required field is unavailable, route construction fails validation, "
            "or the response would otherwise be partial, emit one conservative paper-only "
            "fallback recommendation instead of partial output."
        ),
        "validation_policy": {
            "publish_only_single_json_object": True,
            "reject_non_json": True,
            "reject_wrapper_arrays": True,
            "reject_missing_required_fields": True,
            "required_fields": required_fields,
            "paper_execution_route_hunter_fallback": "refine_or_hold_with_validation_evidence",
        },
        "title": "short directive",
        "rationale": "why this matters",
        "signal_key": "optional",
        "market_key": "required stable routing key",
        "evidence": "object",
        "proposed_change": "what should be built/tested/researched",
        "variant_config": "required only for propose_signal_variant; bounded frontier variant object",
        "code_change": {
            "required_only_for": "propose_code_change",
            "change_category": "one allowed code-evolution category",
            "implementation_mode": "runtime_active, paper_policy, shadow_trial, or report_only",
            "expected_files": "list of repo-relative files expected to change",
            "tests_to_run": "safe unittest commands or empty list for full regression",
            "rollback_criteria": "when the governor should revert/demote",
            "unified_diff": "optional patch; if missing, GPT-5.5 Build Planner may generate one",
            "frontier_escalation_reason": "required for GPT-5.5 code evolution",
        },
        "market_key_contracts": {
            "paper.execution_route_hunter": (
                "Always emit exactly one schema-complete top-level JSON object with "
                "action, priority, title, rationale, market_key, evidence, and "
                "proposed_change. No markdown, commentary, wrapper arrays, or live "
                "execution wording. If route construction fails validation or context "
                "is incomplete, emit the provided paper-only refine/hold fallback "
                "recommendation object with the validation failure captured in evidence."
            ),
            "paper_system.integrity.market_scout": "Always emit exactly one schema-complete top-level JSON object with action, priority, title, rationale, market_key, evidence, and proposed_change. If generation or validation fails, emit the provided fallback paper-only hold recommendation object instead of partial output.",
        },
        "paper_safety_policies": {
            "paper.execution_route_hunter": {
                "mode": "paper_only",
                "forbid_live_execution_wording": True,
                "required_fields": required_fields,
            },
        },
        "fallback_recommendations": {"paper.execution_route_hunter": EXECUTION_ROUTE_HUNTER_FALLBACK_RECOMMENDATION, "paper_system.integrity.market_scout": MARKET_SCOUT_FALLBACK_RECOMMENDATION},
        "allowed_actions": allowed_actions,
    }


def write_llm_state_packet(conn: sqlite3.Connection, payload: dict, settings: dict) -> dict:
    if not settings.get("llm_bridge", {}).get("enabled", True):
        return {}

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stats = _signal_stats(conn)
    contextual_stats = _contextual_stats(conn)
    directives = open_hunter_directives(conn)
    tasks = open_tasks(conn)
    experiments = open_experiments(conn)
    policies = [_compact_policy(item) for item in active_signal_policies(conn)]
    self_improvement_experiments = [_compact_experiment(item) for item in open_self_improvement_experiments(conn, limit=50)]
    route_probe_tasks = open_route_probe_tasks(conn, limit=30)
    adapter_specs = open_adapter_specs(conn, limit=30)
    buckets = _bucketize(stats, directives)
    global_market_discovery = _compact_global_market_discovery(payload.get("research_worker"))
    hunter_allocation = payload.get("hunter_allocation", {})
    allowed_actions = settings.get("llm_bridge", {}).get("allowed_actions", [])
    packet = {
        "purpose": "Read-only state packet for LLM agents. Recommend actions through llm_recommendations_inbox.jsonl only.",
        "mode": settings.get("mode"),
        "live_trading_allowed": bool(settings.get("allow_live_trading", False)),
        "summary": payload.get("summary", {}),
        "execution_summary": payload.get("execution_summary", {}),
        "route_resolver": _compact_route_resolver(payload.get("route_resolver", {})),
        "expansion_map": payload.get("expansion_map", {}),
        "global_market_discovery": global_market_discovery,
        "hunter_allocation": hunter_allocation,
        "llm_cost_summary": payload.get("llm_cost_summary", {}),
        "llm_inbox": payload.get("llm_inbox", {}),
        "maintenance": payload.get("maintenance", {}),
        "horizon_outcomes": payload.get("horizon_outcomes", []),
        "crypto_venue_health": payload.get("crypto_venue_health", []),
        "frontier_crypto_venues": _compact_frontier_crypto(payload.get("frontier_crypto_venues", {})),
        "signal_redesign": _compact_signal_redesign(payload.get("signal_redesign", {})),
        "okx_signal_research": _compact_okx_signal_research(payload.get("okx_signal_research", {})),
        "strategy_reliability": _compact_strategy_reliability(payload.get("strategy_reliability", {})),
        "autonomous_builder": payload.get("autonomous_builder", {}),
        "self_improvement_open_pack": _compact_self_improvement_open_pack(
            payload.get("self_improvement_open_pack")
            or (payload.get("self_improvement", {}) or {}).get("self_improvement_open_pack", {})
        ),
        "contextual_failure_filters": _compact_contextual_failures(payload.get("contextual_failure_filters", {})),
        "buckets": buckets,
        "top_reviewed": payload.get("top_reviewed", [])[:20],
        "recent_opened": payload.get("opened", []),
        "recent_closed": payload.get("closed", []),
        "signal_stats": stats[:50],
        "contextual_stats": contextual_stats,
        "hunter_directives": directives[:50],
        "growth_experiments": experiments[:50],
        "improvement_tasks": tasks[:50],
        "self_improvement": {
            "latest_executor_report": _compact_executor_report(payload.get("self_improvement", {})),
            "active_signal_policies": policies[:50],
            "experiments": self_improvement_experiments[:50],
            "route_probe_tasks": route_probe_tasks,
            "adapter_specs": adapter_specs,
        },
        "code_evolution": code_evolution_summary(conn),
        "memory_artifacts": {
            "latest_markdown": str(RUNS_DIR / "memory_facts_latest.md"),
            "graphiti_export": str(RUNS_DIR / "graphiti_memory_export.jsonl"),
        },
        "allowed_recommendation_actions": allowed_actions,
        "recommendation_schema": _recommendation_schema(allowed_actions),
        "hard_limits": [
            "Do not place live trades.",
            "Do not rewrite code directly except through propose_code_change and the deterministic Build Governor.",
            "Do not run raw installer commands; Python dependencies may be declared in requirements-autonomous.txt or requirements-llm.txt for sandbox-validated installation.",
            "Do not use non-public or illegal data.",
            "Market-expansion code proposals should default to implementation_mode='runtime_active' when they change public-data coverage, scanner breadth, quality scoring, or report/LLM packet wiring.",
            "Use implementation_mode='shadow_trial' only for uncertain new signal logic, not for basic data-coverage expansion.",
            "Autonomous execution is limited to paper-only bounded policies, route probes, adapter specs, memory, reports, tests, and Build-Governor-approved code evolution.",
            "Live trading, credentials, real notional, destructive data changes, startup changes, and broker writes remain blocked.",
            "Global market discovery may research any public market surface worldwide; non-public, stolen, hacked, or credential-only information must not be used as a trading signal.",
            "Recommendation responses must be a single top-level JSON object only; do not emit markdown, commentary, or wrapper arrays.",
        ],
    }
    STATE_JSON.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    STATE_MD.write_text(_packet_to_markdown(packet), encoding="utf-8")
    return packet


def _compact_policy(item: dict) -> dict:
    return {
        "policy_id": item.get("policy_id"),
        "signal_key": item.get("signal_key"),
        "market_key": item.get("market_key"),
        "policy_type": item.get("policy_type"),
        "status": item.get("status"),
        "min_score_delta": item.get("min_score_delta"),
        "min_net_edge_bps": item.get("min_net_edge_bps"),
        "max_spread_bps": item.get("max_spread_bps"),
        "allocation_multiplier": item.get("allocation_multiplier"),
        "pause_entries": item.get("pause_entries"),
        "expires_after_trades": item.get("expires_after_trades"),
        "applied_count": item.get("applied_count"),
        "filtered_count": item.get("filtered_count"),
        "opened_count": item.get("opened_count"),
    }


def _compact_route_resolver(report: dict) -> dict:
    if not report:
        return {}
    summary = report.get("summary", {})
    return {
        "generated_at": report.get("generated_at"),
        "by_route_status": summary.get("by_route_status", {}),
        "by_route_id": summary.get("by_route_id", {}),
        "by_missing_requirement": summary.get("by_missing_requirement", {}),
        "by_requirement_category": summary.get("by_requirement_category", {}),
        "by_requirement_id": summary.get("by_requirement_id", {}),
        "by_route_alternative_status": summary.get("by_route_alternative_status", {}),
        "paper_proxy_available_count": summary.get("paper_proxy_available_count", 0),
        "paper_research_available_count": summary.get("paper_research_available_count", 0),
        "top_manual_actions": summary.get("top_manual_actions", [])[:10],
        "route_intelligence": {
            "blocker_counts": (report.get("route_intelligence") or {}).get("blocker_counts", {}),
            "spot_borrow_assets": (report.get("route_intelligence") or {}).get("spot_borrow_assets", {}),
            "paper_proxy_available_count": (report.get("route_intelligence") or {}).get("paper_proxy_available_count", 0),
            "paper_research_available_count": (report.get("route_intelligence") or {}).get("paper_research_available_count", 0),
            "paper_proxy_available": (report.get("route_intelligence") or {}).get("paper_proxy_available", [])[:10],
            "paper_research_available": (report.get("route_intelligence") or {}).get("paper_research_available", [])[:10],
            "interesting_but_not_executable_count": (report.get("route_intelligence") or {}).get(
                "interesting_but_not_executable_count", 0
            ),
            "potentially_executable_soon_count": (report.get("route_intelligence") or {}).get(
                "potentially_executable_soon_count", 0
            ),
            "route_decision_pack": (report.get("route_intelligence") or {}).get("route_decision_pack", {}),
            "report": str(RUNS_DIR / "route_intelligence_report.md"),
        },
        "hard_limits": report.get("hard_limits", []),
        "report": str(RUNS_DIR / "route_resolver_report.md"),
    }


def _compact_frontier_crypto(report: dict) -> dict:
    if not report:
        return {}
    summary = report.get("summary", {})
    observations = []
    for row in report.get("observations", [])[:20]:
        observations.append(
            {
                "venue": row.get("venue"),
                "region": row.get("region"),
                "market_type": row.get("market_type"),
                "symbol": row.get("symbol"),
                "base": row.get("base"),
                "quote": row.get("quote"),
                "quote_normalization_status": row.get("quote_normalization_status"),
                "quote_normalization_source": row.get("quote_normalization_source"),
                "data_status": row.get("data_status"),
                "http_status": row.get("http_status"),
                "latency_ms": row.get("latency_ms"),
                "last": row.get("last"),
                "spread_bps": row.get("spread_bps"),
                "quote_volume_24h": row.get("quote_volume_24h"),
                "funding_rate": row.get("funding_rate"),
                "quality_status": row.get("quality_status"),
                "quality_score": row.get("quality_score"),
                "freshness_age_seconds": row.get("freshness_age_seconds"),
                "depth_latency_ms": row.get("depth_latency_ms"),
                "anomaly_flags": row.get("anomaly_flags", [])[:5],
                "notes": row.get("notes", [])[:3],
            }
        )
    candidates = []
    for row in report.get("candidates", [])[:20]:
        candidates.append(
            {
                "inst_id": row.get("inst_id"),
                "venue": row.get("venue"),
                "region": row.get("region"),
                "base": row.get("base"),
                "quote": row.get("quote"),
                "quote_normalization_status": row.get("quote_normalization_status"),
                "direction": row.get("direction"),
                "score": row.get("score"),
                "edge_bps_estimate": row.get("edge_bps_estimate"),
                "gross_edge_bps_estimate": row.get("gross_edge_bps_estimate"),
                "estimated_round_trip_cost_bps": row.get("estimated_round_trip_cost_bps"),
                "venue_deviation_bps": row.get("venue_deviation_bps"),
                "quality_status": row.get("quality_status"),
                "quality_score": row.get("quality_score"),
                "quality_action": row.get("quality_action"),
                "anomaly_flags": row.get("anomaly_flags", [])[:5],
                "data_status": row.get("data_status"),
                "route_status": (row.get("execution_feasibility") or {}).get("status"),
                "route_blockers": (row.get("execution_feasibility") or {}).get("route_blockers", []),
                "best_route_alternative": (row.get("execution_feasibility") or {}).get("best_route_alternative"),
                "candidate_reject_reason": row.get("candidate_reject_reason"),
            }
        )
    return {
        "generated_at": report.get("generated_at"),
        "summary": summary,
        "observations": observations,
        "candidates": candidates,
        "report": str(RUNS_DIR / "frontier_crypto_venues_report.md"),
    }


def _compact_contextual_failures(report: dict) -> dict:
    if not report:
        return {}
    def compact_context(item: dict) -> dict:
        return {
            "signal_key": item.get("signal_key"),
            "dimension": item.get("dimension"),
            "value": item.get("value"),
            "status": item.get("status"),
            "failure_domain": item.get("failure_domain"),
            "closed_count": item.get("closed_count"),
            "avg_pnl_bps": item.get("avg_pnl_bps"),
            "win_rate": item.get("win_rate"),
            "recent_avg_pnl_bps": item.get("recent_avg_pnl_bps"),
            "recent_win_rate": item.get("recent_win_rate"),
            "worst_bps": item.get("worst_bps"),
            "failure_score": item.get("failure_score"),
            "recovery_score": item.get("recovery_score"),
            "context_filter": item.get("context_filter"),
        }

    return {
        "generated_at": report.get("generated_at"),
        "summary": report.get("summary", {}),
        "created_policies": [
            {
                "policy_id": item.get("policy_id"),
                "experiment_id": item.get("experiment_id"),
                "group": compact_context(item.get("group", {})),
                "policy": item.get("policy", {}),
            }
            for item in report.get("created_policies", [])[:10]
        ],
        "top_failing_contexts": [compact_context(item) for item in report.get("top_failing_contexts", [])[:15]],
        "top_working_contexts": [compact_context(item) for item in report.get("top_working_contexts", [])[:10]],
        "recovery_candidates": [compact_context(item) for item in report.get("recovery_candidates", [])[:10]],
        "protected_working_slices": [
            compact_context(item) for item in report.get("protected_working_slices", [])[:10]
        ],
        "route_or_data_quality_failures": [
            compact_context(item) for item in report.get("route_or_data_quality_failures", [])[:10]
        ],
        "report": str(RUNS_DIR / "contextual_failure_report.md"),
    }


def _compact_signal_redesign(report: dict) -> dict:
    if not report:
        return {}
    return {
        "generated_at": report.get("generated_at"),
        "summary": report.get("summary", {}),
        "variants": [
            {
                "variant_id": item.get("variant_id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "consecutive_passes": item.get("consecutive_passes"),
                "config": item.get("config"),
                "evaluation": item.get("evaluation"),
            }
            for item in report.get("variants", [])[:15]
        ],
        "evaluations": report.get("evaluations", [])[:10],
        "promotion_blockers": report.get("promotion_blockers", {}),
        "top_failures": report.get("diagnostics", {}).get("top_failures", [])[:10],
        "top_working": report.get("diagnostics", {}).get("top_working", [])[:10],
        "direction_side": report.get("diagnostics", {}).get("direction_side", [])[:10],
        "venue_route_quality_failures": report.get("diagnostics", {}).get("venue_route_quality_failures", [])[:10],
        "promotion_gates": report.get("promotion_gates", {}),
        "report": str(RUNS_DIR / "signal_redesign_report.md"),
    }


def _compact_okx_signal_research(report: dict) -> dict:
    if not report:
        return {}
    return {
        "generated_at": report.get("generated_at"),
        "summary": report.get("summary", {}),
        "variants": [
            {
                "variant_id": item.get("variant_id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "consecutive_passes": item.get("consecutive_passes"),
                "config": item.get("config"),
                "evaluation": item.get("evaluation"),
            }
            for item in report.get("variants", [])[:15]
        ],
        "evaluations": report.get("evaluations", [])[:10],
        "top_failures": report.get("diagnostics", {}).get("top_failures", [])[:10],
        "top_working": report.get("diagnostics", {}).get("top_working", [])[:10],
        "carry_economics": report.get("carry_economics", {}),
        "promotion_gates": report.get("promotion_gates", {}),
        "report": str(RUNS_DIR / "okx_signal_research_report.md"),
    }


def _compact_strategy_reliability(report: dict) -> dict:
    if not report:
        return {}
    return {
        "generated_at": report.get("generated_at"),
        "summary": report.get("summary", {}),
        "top_adjustments": report.get("top_adjustments", [])[:15],
        "covered_improvement_task_ids": report.get("covered_improvement_task_ids", []),
        "covered_growth_experiment_ids": report.get("covered_growth_experiment_ids", []),
        "report": str(RUNS_DIR / "strategy_reliability_report.md"),
    }


def _compact_self_improvement_open_pack(report: dict) -> dict:
    if not report:
        return {}
    borrow = report.get("route_borrow_intelligence") or {}
    africa = report.get("africa_rail_watchlist") or {}
    kalshi = report.get("kalshi_public_coverage") or {}
    diagnostics = report.get("signal_repair_diagnostics") or {}
    return {
        "generated_at": report.get("generated_at"),
        "paper_only": report.get("paper_only"),
        "covered_improvement_task_ids": report.get("covered_improvement_task_ids", [])[:30],
        "covered_growth_experiment_ids": report.get("covered_growth_experiment_ids", [])[:30],
        "route_borrow": {
            "record_count": borrow.get("record_count", 0),
            "shadow_only_unconfirmed_count": borrow.get("shadow_only_unconfirmed_count", 0),
            "by_venue": borrow.get("by_venue", {}),
            "top_records": (borrow.get("records") or [])[:10],
        },
        "africa_rails": {
            "venue_count": africa.get("venue_count", 0),
            "instrument_count": africa.get("instrument_count", 0),
            "by_venue_availability": africa.get("by_venue_availability", {}),
            "rails": (africa.get("rails") or [])[:6],
        },
        "kalshi_public_coverage": {
            "current_candidate_count": kalshi.get("current_candidate_count", 0),
            "route_status": kalshi.get("route_status"),
            "route_blockers": kalshi.get("route_blockers", {}),
            "orderbook_status_counts": kalshi.get("orderbook_status_counts", {}),
        },
        "signal_repair_diagnostics": {
            "active_loosenings_created": diagnostics.get("active_loosenings_created", 0),
            "frontier_count": len(diagnostics.get("frontier_weak_signal_diagnostics", [])),
            "yahoo_count": len(diagnostics.get("yahoo_proxy_diagnostics", [])),
            "okx_count": len(diagnostics.get("okx_basis_funding_diagnostics", [])),
            "positive_shadow_expansions": len(diagnostics.get("positive_shadow_expansion_variants", [])),
            "top_frontier": (diagnostics.get("frontier_weak_signal_diagnostics") or [])[:10],
        },
        "report": str(RUNS_DIR / "self_improvement_open_pack.md"),
    }


def _compact_executor_report(report: dict) -> dict:
    if not report:
        return {}
    return {
        "enabled": report.get("enabled"),
        "generated_at": report.get("generated_at"),
        "consumed_count": len(report.get("consumed", [])),
        "evaluated_count": len(report.get("evaluated", [])),
        "superseded_count": len(report.get("superseded", [])),
        "active_policy_count": len(report.get("active_policies", [])),
        "route_probe_task_count": len(report.get("route_probe_tasks", [])),
        "adapter_spec_count": len(report.get("adapter_specs", [])),
        "consumed": [
            {
                "task_type": item.get("task_type"),
                "title": item.get("title"),
                "created_count": len(
                    [row for row in item.get("created", []) if row.get("action_status", "created") == "created"]
                ),
                "skipped_count": len([row for row in item.get("created", []) if row.get("action_status") == "skipped"]),
            }
            for item in report.get("consumed", [])[:10]
        ],
        "evaluated": report.get("evaluated", [])[:10],
    }


def _compact_experiment(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "source_agent": item.get("source_agent"),
        "task_type": item.get("task_type"),
        "priority": item.get("priority"),
        "market_key": item.get("market_key"),
        "signal_key": item.get("signal_key"),
        "hypothesis": item.get("hypothesis"),
        "status": item.get("status"),
        "decision": item.get("decision"),
        "baseline": item.get("baseline"),
        "evaluation": item.get("evaluation"),
    }


def _compact_global_market_discovery(report: dict | None = None) -> dict:
    if not report:
        path = RUNS_DIR / "research_worker_latest.json"
        if path.exists():
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                report = {"status": "unreadable", "report": str(path)}
    if not report:
        return {
            "status": "missing",
            "report": str(RUNS_DIR / "research_worker_report.md"),
            "candidate_ledger": str(RUNS_DIR / "market_discovery_candidates.jsonl"),
        }
    summary = report.get("summary", {})
    return {
        "status": report.get("status"),
        "global_market_discovery": report.get("global_market_discovery"),
        "web_research_enabled": report.get("web_research_enabled"),
        "candidate_count": summary.get("candidate_count", 0),
        "new_candidate_count": summary.get("new_candidate_count", 0),
        "total_known_candidate_count": summary.get("total_known_candidate_count", 0),
        "by_surface_type": summary.get("by_surface_type", {}),
        "by_region": summary.get("by_region", {}),
        "by_recommended_next_action": summary.get("by_recommended_next_action", {}),
        "inserted_artifact_counts": summary.get("inserted_artifact_counts", {}),
        "top_candidates": summary.get("top_candidates", [])[:10],
        "report": str(RUNS_DIR / "research_worker_report.md"),
        "candidate_ledger": str(RUNS_DIR / "market_discovery_candidates.jsonl"),
    }


def _packet_to_markdown(packet: dict) -> str:
    lines = [
        "# LLM State Packet",
        "",
        packet["purpose"],
        "",
        f"- Mode: `{packet['mode']}`",
        f"- Live trading allowed: `{packet['live_trading_allowed']}`",
        f"- Summary: `{packet['summary']}`",
        f"- Execution: `{packet['execution_summary']}`",
        f"- Route resolver: `{packet.get('route_resolver', {})}`",
        f"- Expansion map: `{packet.get('expansion_map', {})}`",
        f"- Global market discovery: `{packet.get('global_market_discovery', {})}`",
        f"- Hunter allocation: `{packet.get('hunter_allocation', {})}`",
        f"- Frontier crypto venues: `{packet.get('frontier_crypto_venues', {})}`",
        f"- Signal redesign: `{packet.get('signal_redesign', {})}`",
        f"- OKX signal research: `{packet.get('okx_signal_research', {})}`",
        f"- Strategy reliability: `{packet.get('strategy_reliability', {})}`",
        f"- Self-improvement open pack: `{packet.get('self_improvement_open_pack', {})}`",
        f"- Code evolution: `{packet.get('code_evolution', {})}`",
        f"- Contextual failure filters: `{packet.get('contextual_failure_filters', {})}`",
        f"- LLM cost: `{packet['llm_cost_summary']}`",
        "",
        "## Exploit",
        "",
    ]
    for item in packet["buckets"]["exploit"][:20]:
        lines.append(f"- `{item.get('signal_key') or item.get('market_key')}`: {item.get('reason') or item.get('rationale')}")
    lines.extend(["", "## Explore", ""])
    for item in packet["buckets"]["explore"][:20]:
        lines.append(f"- `{item.get('signal_key') or item.get('market_key')}`: {item.get('reason') or item.get('rationale')}")
    lines.extend(["", "## Diagnose", ""])
    for item in packet["buckets"]["diagnose"][:20]:
        lines.append(f"- `{item.get('signal_key') or item.get('market_key')}`: {item.get('reason') or item.get('rationale')}")
    lines.extend(["", "## Self-Improvement", ""])
    for policy in packet.get("self_improvement", {}).get("active_signal_policies", [])[:10]:
        lines.append(
            f"- Active policy `{policy['policy_id']}` for `{policy['signal_key']}` "
            f"filtered={policy.get('filtered_count')} opened={policy.get('opened_count')}"
        )
    for exp in packet.get("self_improvement", {}).get("experiments", [])[:10]:
        lines.append(f"- Experiment #{exp['id']} `{exp['task_type']}` status={exp['status']} signal=`{exp.get('signal_key')}`")
    lines.extend(["", "## How To Recommend", ""])
    lines.append(f"Write JSONL recommendations to `{INBOX}` using actions: {packet['allowed_recommendation_actions']}")
    lines.append("")
    lines.append("Example:")
    lines.append("")
    lines.append("```json")
    lines.append('{"action":"request_market_adapter","priority":85,"title":"Add prediction market scanner","rationale":"Prediction markets may expose event-latency edges.","market_key":"prediction_markets","evidence":{"reason":"uncovered market surface"},"proposed_change":"Build public Kalshi/Polymarket scanner."}')
    lines.append("```")
    return "\n".join(lines) + "\n"


def ingest_llm_recommendations(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    cfg = settings.get("llm_bridge", {})
    if not cfg.get("enabled", True) or not cfg.get("ingest_recommendations", True):
        return []
    if not INBOX.exists():
        return []

    allowed = set(cfg.get("allowed_actions", []))
    max_items = int(cfg.get("max_recommendations_per_loop", 20))
    accepted = []
    remaining = []
    lines = INBOX.read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        if len(accepted) >= max_items:
            remaining.append(line)
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = item.get("action")
        if action not in allowed:
            continue
        title = str(item.get("title") or action)[:180]
        rationale = str(item.get("rationale") or item.get("proposed_change") or "")[:2000]
        priority = int(item.get("priority", 50))
        priority = max(1, min(100, priority))
        rec_id = hashlib.sha256(json.dumps(item, sort_keys=True).encode("utf-8")).hexdigest()
        if not add_llm_recommendation(conn, rec_id, action, title, rationale, item):
            continue
        _apply_recommendation(conn, action, title, rationale, priority, item)
        accepted.append({"id": rec_id, "action": action, "title": title, "priority": priority})
        with PROCESSED.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": rec_id, "item": item}, sort_keys=True) + "\n")

    INBOX.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
    return accepted


def _implemented_manual_category(text: str) -> str | None:
    text = text.lower()
    if is_duplicate_open_pack_text(text):
        return "self_improvement_open_pack"
    if any(
        term in text
        for term in (
            "bybit quality",
            "bybit long-frontier",
            "bybit long frontier",
            "expand profitable bybit",
            "tighten bybit",
            "bybit decay",
        )
    ):
        return "bybit_quality_decay_expansion_pack"
    if any(
        term in text
        for term in (
            "kucoin long-frontier",
            "kucoin long frontier",
            "tighten kucoin",
            "kucoin weak",
            "kucoin repair",
        )
    ):
        return "kucoin_long_repair_diagnostics"
    if any(
        term in text
        for term in (
            "strategy reliability",
            "venue-direction reliability",
            "venue direction reliability",
            "frontier venue signal repair",
            "frontier long weak",
            "frontier short weak",
            "long-frontier",
            "short-frontier",
            "yahoo proxy short",
            "proxy short",
            "funding/basis split",
            "funding basis split",
            "positive slice expansion",
            "market-specific factors",
            "microstructure and liquidity",
            "microstructure divergence",
            "weak win-rate",
            "weak win rate",
            "expand okx funding",
            "expand gate frontier short",
            "expand mexc frontier short",
            "expand binance_us frontier short",
        )
    ):
        return "strategy_reliability_pack"
    if "okx" in text and any(
        term in text
        for term in (
            "reliable outcome",
            "reliable label",
            "legacy_unverified",
            "variant learning",
            "valid labels",
        )
    ):
        return "okx_reliable_outcomes"
    if "frontier" in text and any(
        term in text
        for term in (
            "systemic",
            "negative performance",
            "poor signal performance",
            "venue-map",
            "venue map",
            "underserved frontier",
        )
    ):
        return "frontier_systemic_redesign"
    if "okx" in text and "perp" in text and "funding" in text and any(
        term in text
        for term in (
            "basis signal",
            "basis signals",
            "basis signal research",
            "investigate and improve okx",
            "funding basis signal",
            "funding basis signals",
        )
    ):
        return "okx_basis_signal_research"
    if "frontier crypto" in text and any(
        term in text
        for term in (
            "africa",
            "southeast asia",
            "emerging frontier",
            "regional frontier",
            "regional venue",
        )
    ):
        return "regional_frontier_data"
    if "frontier crypto" in text and any(
        term in text
        for term in (
            "enhanced data coverage",
            "data quality",
            "order book",
            "orderbook",
            "market depth",
            "freshness",
            "slippage",
            "liquidity quality",
        )
    ):
        return "frontier_data_quality"
    if any(
        term in text
        for term in (
            "regional fx",
            "fx reference",
            "public fx midpoint",
            "fiat-stablecoin reference",
            "quote normalization",
            "africa rail",
            "african stablecoin",
        )
    ):
        return "regional_fx_frontier_prediction_pack"
    if "frontier" in text and any(
        term in text
        for term in (
            "adaptive depth",
            "depth enrichment",
            "known quality",
            "quality coverage",
        )
    ):
        return "regional_fx_frontier_prediction_pack"
    if "prediction" in text and any(
        term in text
        for term in (
            "event classification",
            "event intelligence",
            "expired",
            "resolution",
            "order-book",
            "orderbook",
        )
    ):
        return "regional_fx_frontier_prediction_pack"
    if any(
        term in text
        for term in (
            "signal redesign",
            "root cause analysis",
            "root-cause analysis",
            "investigate and improve poorly performing signals",
            "improve frontier crypto spot signals across venues",
        )
    ):
        return "signal_redesign"
    if (
        "route requirement" in text
        or "execution route requirements" in text
        or ("conditional opportunit" in text and "requirements" in text)
    ):
        return "route_requirements"
    if "frontier crypto" in text and any(
        term in text
        for term in (
            "data adapter",
            "market coverage",
            "venue adapter",
            "undercovered",
            "poor performance",
        )
    ):
        return "frontier_crypto_adapter"
    if any(
        term in text
        for term in (
            "failure filter",
            "demote or filter",
            "demote and filter",
            "tighten filters for losing signal",
        )
    ):
        return "failure_diagnostics"
    return None


def _implemented_manual_category_exists(conn: sqlite3.Connection, category: str | None) -> bool:
    if not category or category not in IMPLEMENTED_MANUAL_STATUSES:
        return False
    status, tables = IMPLEMENTED_MANUAL_STATUSES[category]
    for table in tables:
        row = conn.execute(f"select 1 from {table} where status = ? limit 1", (status,)).fetchone()
        if row:
            return True
    return False


def _apply_recommendation(
    conn: sqlite3.Connection,
    action: str,
    title: str,
    rationale: str,
    priority: int,
    item: dict,
) -> None:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    signal_key = item.get("signal_key") or item.get("market_key") or "llm_recommendation"
    proposed_change = str(item.get("proposed_change") or rationale)

    if action in {"propose_build_task", "request_data_source", "request_market_adapter", "request_red_team"}:
        duplicate_category = _implemented_manual_category(" ".join([title, rationale, proposed_change]))
        if _implemented_manual_category_exists(conn, duplicate_category):
            return
        add_improvement_task(conn, priority, f"LLM: {title}", proposed_change or rationale)
    elif action == "propose_growth_experiment":
        duplicate_category = _implemented_manual_category(" ".join([title, rationale, proposed_change, signal_key]))
        if _implemented_manual_category_exists(conn, duplicate_category):
            return
        add_growth_experiment(conn, priority, signal_key, title, proposed_change, evidence)
    elif action == "propose_hunter_directive":
        directive = str(item.get("directive") or "llm_research_directive")
        add_hunter_directive(conn, signal_key, directive, priority, rationale, evidence)
