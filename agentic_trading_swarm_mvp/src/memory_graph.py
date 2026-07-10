"""Temporal memory graph with optional Graphiti-compatible export."""

from __future__ import annotations

import json
import pathlib
import sqlite3

from storage import RUNS_DIR, add_memory_fact, recent_memory_facts


GRAPHITI_EXPORT = RUNS_DIR / "graphiti_memory_export.jsonl"
MEMORY_MD = RUNS_DIR / "memory_facts_latest.md"


def ingest_radar_memory(conn: sqlite3.Connection, payload: dict) -> list[dict]:
    """Persist compact facts from each radar loop."""
    added = []
    summary = payload.get("summary", {})
    if summary:
        fact = {
            "fact_type": "performance_summary",
            "subject": "radar",
            "predicate": "has_summary",
            "object_value": json.dumps(summary, sort_keys=True),
            "confidence": 1.0,
            "source": "radar_loop",
            "metadata": {"summary": summary},
        }
        add_memory_fact(conn, **fact)
        added.append(fact)

    for directive in payload.get("market_hunter_directives", [])[:20]:
        fact = {
            "fact_type": "hunter_directive",
            "subject": directive.get("market_key", "unknown"),
            "predicate": directive.get("directive", "observed"),
            "object_value": directive.get("rationale", ""),
            "confidence": 0.85,
            "source": "market_hunter",
            "metadata": directive,
        }
        add_memory_fact(conn, **fact)
        added.append(fact)

    for item in payload.get("signal_stats", [])[:20]:
        fact = {
            "fact_type": "signal_stat",
            "subject": item.get("signal_key", "unknown"),
            "predicate": "has_score_adjustment",
            "object_value": str(item.get("score_adjustment")),
            "confidence": 0.9,
            "source": "learning",
            "metadata": item,
        }
        add_memory_fact(conn, **fact)
        added.append(fact)

    for venue in payload.get("crypto_venue_health", [])[:20]:
        fact = {
            "fact_type": "venue_health",
            "subject": venue.get("venue", "unknown"),
            "predicate": "is_reachable" if venue.get("reachable") else "is_unreachable",
            "object_value": str(venue.get("status", ""))[:180],
            "confidence": 0.95,
            "source": "crypto_venue_scanner",
            "metadata": venue,
        }
        add_memory_fact(conn, **fact)
        added.append(fact)

    frontier_crypto = payload.get("frontier_crypto_venues", {})
    for venue in frontier_crypto.get("observations", [])[:20]:
        fact = {
            "fact_type": "frontier_crypto_venue",
            "subject": venue.get("venue", "unknown"),
            "predicate": venue.get("data_status", "unknown"),
            "object_value": str(venue.get("http_status", ""))[:180],
            "confidence": 0.9,
            "source": "frontier_crypto_adapter",
            "metadata": venue,
        }
        add_memory_fact(conn, **fact)
        added.append(fact)

    route_resolver = payload.get("route_resolver", {})
    if route_resolver:
        summary = route_resolver.get("summary", {})
        fact = {
            "fact_type": "route_resolver",
            "subject": "execution_routes",
            "predicate": "has_route_summary",
            "object_value": json.dumps(summary.get("by_route_status", {}), sort_keys=True),
            "confidence": 0.9,
            "source": "route_resolver",
            "metadata": summary,
        }
        add_memory_fact(conn, **fact)
        added.append(fact)

    self_improvement = payload.get("self_improvement", {})
    contextual = payload.get("contextual_failure_filters", {})
    for item in contextual.get("top_failing_contexts", [])[:10]:
        fact = {
            "fact_type": "contextual_failure",
            "subject": item.get("signal_key", "unknown"),
            "predicate": f"{item.get('dimension')}={item.get('value')}",
            "object_value": str(item.get("avg_pnl_bps")),
            "confidence": 0.84,
            "source": "contextual_failure_filters",
            "metadata": item,
        }
        add_memory_fact(conn, **fact)
        added.append(fact)

    for item in contextual.get("created_policies", [])[:10]:
        group = item.get("group", {})
        fact = {
            "fact_type": "contextual_failure_policy",
            "subject": group.get("signal_key", "unknown"),
            "predicate": "created_policy",
            "object_value": item.get("policy_id", ""),
            "confidence": 0.88,
            "source": "contextual_failure_filters",
            "metadata": item,
        }
        add_memory_fact(conn, **fact)
        added.append(fact)

    for policy in self_improvement.get("active_policies", [])[:20]:
        fact = {
            "fact_type": "self_improvement_policy",
            "subject": policy.get("signal_key", "unknown"),
            "predicate": "is_active",
            "object_value": policy.get("policy_type", "policy"),
            "confidence": 0.9,
            "source": "self_improvement_executor",
            "metadata": policy,
        }
        add_memory_fact(conn, **fact)
        added.append(fact)

    for item in self_improvement.get("evaluated", [])[:20]:
        fact = {
            "fact_type": "self_improvement_evaluation",
            "subject": str(item.get("experiment_id", "unknown")),
            "predicate": item.get("decision", "checked"),
            "object_value": item.get("status", ""),
            "confidence": 0.9,
            "source": "self_improvement_executor",
            "metadata": item,
        }
        add_memory_fact(conn, **fact)
        added.append(fact)

    write_memory_exports(conn)
    return added


def query_memory(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    return recent_memory_facts(conn, limit=limit)


def write_memory_exports(conn: sqlite3.Connection) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    facts = recent_memory_facts(conn, limit=200)
    with GRAPHITI_EXPORT.open("w", encoding="utf-8") as fh:
        for fact in facts:
            fh.write(json.dumps(fact, sort_keys=True) + "\n")

    lines = ["# Memory Facts", ""]
    for fact in facts[:50]:
        lines.append(
            f"- `{fact['fact_type']}` `{fact['subject']}` {fact['predicate']} "
            f"`{fact['object'][:160]}` confidence={fact['confidence']}"
        )
    MEMORY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
