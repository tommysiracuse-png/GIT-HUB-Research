"""Temporal memory facade used by the radar and LangGraph swarm."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Iterable

from storage import recent_memory_facts
from temporal_memory import (
    build_role_memory_contexts,
    memory_system_summary,
    record_swarm_reflection,
    refresh_evidence_memories,
    retrieve_role_memories,
    sync_graphiti_memories,
    upsert_memory_fact,
    write_memory_reports,
)


def _memory_enabled(settings: dict | None) -> bool:
    effective = settings or {"agent_memory": {"enabled": True}}
    return bool(effective.get("agent_memory", {}).get("enabled", True))


def ingest_radar_memory(conn: sqlite3.Connection, payload: dict, settings: dict | None = None) -> list[dict]:
    """Persist compact, deduplicated facts and refresh outcome-linked memory."""
    settings = settings or {"agent_memory": {"enabled": True}}
    cfg = settings.get("agent_memory", {})
    if not _memory_enabled(settings):
        return []
    profile_hours = float(cfg.get("profile_version_hours", 6.0))
    added: list[dict] = []

    def store(fact: dict, **kwargs) -> None:
        result = upsert_memory_fact(
            conn,
            fact["fact_type"],
            fact["subject"],
            fact["predicate"],
            fact["object_value"],
            fact["confidence"],
            fact["source"],
            fact["metadata"],
            profile_version_hours=profile_hours,
            commit=False,
            **kwargs,
        )
        added.append({**fact, **result})

    summary = payload.get("summary", {})
    if summary:
        store(
            {
                "fact_type": "performance_summary",
                "subject": "radar",
                "predicate": "has_summary",
                "object_value": json.dumps(summary, sort_keys=True),
                "confidence": 1.0,
                "source": "radar_loop",
                "metadata": {"summary": summary},
            },
            namespace="outcomes",
            importance=0.82,
        )

    for directive in payload.get("market_hunter_directives", [])[:20]:
        store(
            {
                "fact_type": "hunter_directive",
                "subject": directive.get("market_key", "unknown"),
                "predicate": directive.get("directive", "observed"),
                "object_value": directive.get("rationale", ""),
                "confidence": 0.85,
                "source": "market_hunter",
                "metadata": directive,
            },
            namespace="recommendations",
            source_id=str(directive.get("id") or "") or None,
            importance=min(0.9, float(directive.get("priority") or 50) / 100.0),
        )

    for item in payload.get("signal_stats", [])[:20]:
        store(
            {
                "fact_type": "signal_stat",
                "subject": item.get("signal_key", "unknown"),
                "predicate": "has_score_adjustment",
                "object_value": str(item.get("score_adjustment")),
                "confidence": 0.9,
                "source": "learning",
                "metadata": item,
            },
            namespace="outcomes",
            source_id=str(item.get("signal_key") or ""),
            outcome_score=max(-1.0, min(1.0, float(item.get("avg_pnl_bps") or 0.0) / 50.0)),
        )

    for venue in payload.get("crypto_venue_health", [])[:20]:
        reachable = bool(venue.get("reachable"))
        store(
            {
                "fact_type": "venue_health",
                "subject": venue.get("venue", "unknown"),
                "predicate": "is_reachable" if reachable else "is_unreachable",
                "object_value": str(venue.get("status", ""))[:180],
                "confidence": 0.95,
                "source": "crypto_venue_scanner",
                "metadata": venue,
            },
            namespace="markets",
            importance=0.72 if not reachable else 0.55,
            outcome_score=0.25 if reachable else -0.45,
        )

    frontier_crypto = payload.get("frontier_crypto_venues", {})
    for venue in frontier_crypto.get("observations", [])[:20]:
        status = str(venue.get("data_status", "unknown"))
        store(
            {
                "fact_type": "frontier_crypto_venue",
                "subject": venue.get("venue", "unknown"),
                "predicate": status,
                "object_value": str(venue.get("http_status", ""))[:180],
                "confidence": 0.9,
                "source": "frontier_crypto_adapter",
                "metadata": venue,
            },
            namespace="markets",
            importance=0.68 if status not in {"ok", "reachable", "verified"} else 0.5,
        )

    route_resolver = payload.get("route_resolver", {})
    if route_resolver:
        route_summary = route_resolver.get("summary", {})
        store(
            {
                "fact_type": "route_resolver",
                "subject": "execution_routes",
                "predicate": "has_route_summary",
                "object_value": json.dumps(route_summary.get("by_route_status", {}), sort_keys=True),
                "confidence": 0.9,
                "source": "route_resolver",
                "metadata": route_summary,
            },
            namespace="routes",
            importance=0.74,
        )

    self_improvement = payload.get("self_improvement", {})
    contextual = payload.get("contextual_failure_filters", {})
    for item in contextual.get("top_failing_contexts", [])[:10]:
        pnl = float(item.get("avg_pnl_bps") or 0.0)
        store(
            {
                "fact_type": "contextual_failure",
                "subject": item.get("signal_key", "unknown"),
                "predicate": f"{item.get('dimension')}={item.get('value')}",
                "object_value": str(item.get("avg_pnl_bps")),
                "confidence": 0.84,
                "source": "contextual_failure_filters",
                "metadata": item,
            },
            namespace="outcomes",
            importance=min(0.94, 0.65 + abs(pnl) / 400.0),
            outcome_score=max(-1.0, min(0.0, pnl / 50.0)),
        )

    for item in contextual.get("created_policies", [])[:10]:
        group = item.get("group", {})
        store(
            {
                "fact_type": "contextual_failure_policy",
                "subject": group.get("signal_key", "unknown"),
                "predicate": "created_policy",
                "object_value": item.get("policy_id", ""),
                "confidence": 0.88,
                "source": "contextual_failure_filters",
                "metadata": item,
            },
            namespace="policies",
            memory_type="episodic",
            source_id=str(item.get("policy_id") or ""),
            importance=0.78,
        )

    for policy in self_improvement.get("active_policies", [])[:20]:
        store(
            {
                "fact_type": "self_improvement_policy",
                "subject": policy.get("signal_key", "unknown"),
                "predicate": "is_active",
                "object_value": policy.get("policy_type", "policy"),
                "confidence": 0.9,
                "source": "self_improvement_executor",
                "metadata": policy,
            },
            namespace="policies",
            source_id=str(policy.get("policy_id") or ""),
            importance=0.7,
        )

    for item in self_improvement.get("evaluated", [])[:20]:
        store(
            {
                "fact_type": "self_improvement_evaluation",
                "subject": str(item.get("experiment_id", "unknown")),
                "predicate": item.get("decision", "checked"),
                "object_value": item.get("status", ""),
                "confidence": 0.9,
                "source": "self_improvement_executor",
                "metadata": item,
            },
            namespace="policies",
            memory_type="episodic",
            source_id=str(item.get("experiment_id") or ""),
            importance=0.82,
        )

    conn.commit()
    refresh_evidence_memories(conn, settings)
    write_memory_reports(conn, settings)
    return added


def query_memory(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    """Compatibility query; role-specific swarm retrieval uses the functions below."""
    return recent_memory_facts(conn, limit=limit)


def query_role_memory(
    conn: sqlite3.Connection,
    packet: dict,
    agent_name: str,
    settings: dict,
    cycle_id: str,
) -> list[dict]:
    if not _memory_enabled(settings):
        return []
    return retrieve_role_memories(conn, packet, agent_name, settings, cycle_id=cycle_id)


def build_swarm_memory(
    conn: sqlite3.Connection,
    packet: dict,
    settings: dict,
    agent_names: Iterable[str],
) -> tuple[dict, str]:
    names = list(agent_names)
    if not _memory_enabled(settings):
        cycle_id = f"swarm:memory-disabled:{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        return {name: [] for name in names}, cycle_id
    return build_role_memory_contexts(conn, packet, settings, names)


def reflect_swarm(conn: sqlite3.Connection, state: dict, cycle_id: str, settings: dict) -> dict:
    if not _memory_enabled(settings):
        return {"status": "disabled"}
    result = record_swarm_reflection(conn, state, cycle_id, settings)
    write_memory_reports(conn, settings)
    return result


def memory_summary(conn: sqlite3.Connection, settings: dict) -> dict:
    if not _memory_enabled(settings):
        return {"enabled": False, "status": "disabled_by_config"}
    return memory_system_summary(conn, settings)


def sync_graphiti(conn: sqlite3.Connection, settings: dict) -> dict:
    if not _memory_enabled(settings):
        return {"status": "disabled", "synced": 0}
    return sync_graphiti_memories(conn, settings)


def write_memory_exports(conn: sqlite3.Connection, settings: dict | None = None) -> None:
    if not _memory_enabled(settings):
        return
    write_memory_reports(conn, settings)
