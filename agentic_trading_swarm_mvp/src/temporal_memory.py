"""Outcome-linked temporal memory for the agent swarm.

The radar database remains the source of truth. This module turns selected
state transitions and measured outcomes into a compact temporal memory layer
that agents can retrieve by role, subject, relevance, and time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from typing import Any, Iterable


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
REPORT_JSON = RUNS_DIR / "temporal_memory_report.json"
REPORT_MD = RUNS_DIR / "temporal_memory_report.md"
GRAPHITI_EXPORT = RUNS_DIR / "graphiti_memory_export.jsonl"
MEMORY_MD = RUNS_DIR / "memory_facts_latest.md"

ACTIVE_STATUS = "active"
SUPERSEDED_STATUS = "superseded"
PROVISIONAL_STATUS = "provisional"

PROFILE_FACT_TYPES = {
    "performance_summary",
    "signal_stat",
    "signal_performance",
    "venue_health",
    "frontier_crypto_venue",
    "route_resolver",
    "self_improvement_policy",
    "contextual_failure",
    "strategy_lab_evaluation",
    "code_evolution_outcome",
    "recommendation_outcome",
    "self_improvement_outcome",
    "market_admission",
    "agent_effectiveness",
}

PRESERVE_EVERY_CHANGE_FACT_TYPES = {
    "venue_health",
    "code_evolution_outcome",
    "recommendation_outcome",
    "strategy_lab_evaluation",
    "self_improvement_outcome",
}

ROLE_MEMORY_POLICIES = {
    "market_scout": {
        "namespaces": ["markets", "routes", "outcomes", "recommendations"],
        "keywords": ["market venue discovery adapter admission coverage region asset liquidity data"],
    },
    "cross_market_researcher": {
        "namespaces": ["outcomes", "strategies", "markets", "routes"],
        "keywords": ["cross market regime causal context signal outcome funding basis momentum"],
    },
    "strategy_lab": {
        "namespaces": ["strategies", "outcomes", "markets", "recommendations"],
        "keywords": ["strategy hypothesis experiment variant promotion retired labels pnl win rate tail"],
    },
    "red_team": {
        "namespaces": ["outcomes", "strategies", "policies", "code"],
        "keywords": ["failure decay loss tail risk regression invalidated rejected policy diagnosis"],
    },
    "execution_route_hunter": {
        "namespaces": ["routes", "markets", "outcomes"],
        "keywords": ["route broker account permission borrow margin fee api jurisdiction executable"],
    },
    "build_planner": {
        "namespaces": ["code", "recommendations", "strategies", "outcomes", "policies"],
        "keywords": ["code patch commit test promotion failure target file runtime integration implementation"],
    },
    "generic": {
        "namespaces": ["outcomes", "strategies", "markets", "routes", "code", "policies"],
        "keywords": ["system memory outcome market strategy code"],
    },
}

STOPWORDS = {
    "about", "after", "again", "agent", "allowed", "before", "current", "from", "into",
    "latest", "market", "memory", "paper", "should", "system", "that", "their", "these",
    "this", "through", "using", "with", "would",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        parsed = dt.datetime.fromisoformat(str(value or _utc_now()).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _hash(*parts: Any, length: int = 40) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:length]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _namespace_for_fact(fact_type: str, source: str = "") -> str:
    text = f"{fact_type} {source}".lower()
    if any(token in text for token in ("route", "borrow", "margin", "broker", "jurisdiction")):
        return "routes"
    if any(token in text for token in ("venue", "admission", "discovery", "adapter", "frontier_crypto")):
        return "markets"
    if any(token in text for token in ("code", "patch", "builder", "evolution")):
        return "code"
    if "recommendation" in text or "hunter_directive" in text:
        return "recommendations"
    if any(token in text for token in ("policy", "safety", "filter")):
        return "policies"
    if any(token in text for token in ("performance", "outcome", "failure", "signal_stat")):
        return "outcomes"
    if any(token in text for token in ("strategy", "variant", "experiment")):
        return "strategies"
    return "system"


def _memory_type_for_fact(fact_type: str, predicate: str) -> str:
    if fact_type in PROFILE_FACT_TYPES:
        return "semantic"
    if predicate.lower() in {"created", "activated", "promoted", "retired", "reverted", "rejected"}:
        return "episodic"
    return "semantic"


def _default_importance(fact_type: str, predicate: str) -> float:
    text = f"{fact_type} {predicate}".lower()
    if any(token in text for token in ("promoted", "reverted", "retired", "code_evolution", "outcome")):
        return 0.9
    if any(token in text for token in ("failure", "policy", "safety", "route", "strategy")):
        return 0.72
    if any(token in text for token in ("performance", "signal", "admission")):
        return 0.62
    return 0.5


def _summary_text(value: Any, limit: int = 260) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[: max(1, limit - 3)].rstrip() + "..."


def _summary_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _summary_bps(value: Any) -> str | None:
    number = _summary_number(value)
    return f"{number:+.2f} bps" if number is not None else None


def _summary_percent(value: Any) -> str | None:
    number = _summary_number(value)
    if number is None:
        return None
    if abs(number) <= 1.0:
        number *= 100.0
    return f"{number:.1f}%"


def _summary_list(values: Any, limit: int = 5) -> str:
    if not isinstance(values, (list, tuple, set)):
        return ""
    items = [_summary_text(value, 120) for value in values if _summary_text(value, 120)]
    if not items:
        return ""
    suffix = f" and {len(items) - limit} more" if len(items) > limit else ""
    return ", ".join(items[:limit]) + suffix


def _summary_groups(groups: Any, limit: int = 3) -> str:
    if not isinstance(groups, dict):
        return ""
    ranked: list[tuple[int, str]] = []
    for name, raw in groups.items():
        if not isinstance(raw, dict):
            continue
        count = int(_summary_number(raw.get("count")) or 0)
        avg = _summary_bps(raw.get("avg_pnl_bps"))
        win_rate = _summary_percent(raw.get("win_rate"))
        detail = f"{name}: {avg or 'average unavailable'}"
        if count:
            detail += f" across {count} labels"
        if win_rate:
            detail += f", {win_rate} profitable"
        ranked.append((count, detail))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return "; ".join(item[1] for item in ranked[:limit])


def _summary_mapping(data: Any, limit: int = 7) -> str:
    if not isinstance(data, dict):
        return _summary_text(data, 400)
    details: list[str] = []
    for key, value in data.items():
        if value in (None, "", [], {}):
            continue
        label = str(key).replace("_", " ")
        if isinstance(value, dict):
            compact = ", ".join(
                f"{child_key}={child_value}"
                for child_key, child_value in list(value.items())[:4]
                if not isinstance(child_value, (dict, list))
            )
            if compact:
                details.append(f"{label}: {compact}")
        elif isinstance(value, (list, tuple, set)):
            compact = _summary_list(value, 4)
            if compact:
                details.append(f"{label}: {compact}")
        else:
            details.append(f"{label}: {_summary_text(value, 120)}")
        if len(details) >= limit:
            break
    return "; ".join(details)


def _signal_performance_summary(subject: str, data: dict) -> str:
    count = int(_summary_number(data.get("valid_labels")) or 0)
    venue = _summary_text(data.get("venue") or "unknown venue", 80)
    trade_type = _summary_text(data.get("trade_type") or "unknown strategy family", 100)
    direction = _summary_text(data.get("direction") or "unknown direction", 100)
    avg = _summary_bps(data.get("avg_pnl_bps")) or "an unavailable average"
    win_rate = _summary_percent(data.get("win_rate")) or "an unavailable win rate"
    tail = _summary_bps(data.get("worst_decile_bps")) or "unavailable"
    worst = _summary_bps(data.get("worst_bps")) or "unavailable"
    best = _summary_bps(data.get("best_bps")) or "unavailable"
    route_status = subject.rsplit("|", 1)[-1] if "|" in subject else "not encoded"
    return (
        f"Reliable 60-minute paper evidence for the exact signal lineage {subject}. "
        f"It represents {direction} trades from {venue} under {trade_type}, with route status "
        f"{route_status}. Across {count} valid labels, the average net outcome was {avg} and "
        f"{win_rate} of labels were profitable. The worst decile was {tail}; individual outcomes "
        f"ranged from {worst} to {best}. This is aggregate lineage evidence, so instrument, session, "
        f"liquidity, and regime slices should be checked before changing the whole family."
    )


def _strategy_lab_summary(subject: str, data: dict) -> str:
    evaluation = data.get("evaluation") if isinstance(data.get("evaluation"), dict) else {}
    evidence = evaluation.get("outcomes") if isinstance(evaluation.get("outcomes"), dict) else evaluation
    metrics = evidence.get("metrics") if isinstance(evidence.get("metrics"), dict) else evidence
    hypothesis = _summary_text(data.get("hypothesis") or "No hypothesis text was recorded.", 300)
    status = _summary_text(data.get("status") or evaluation.get("decision") or "unknown", 80)
    valid_count = int(
        _summary_number(evidence.get("valid_count"))
        or _summary_number(metrics.get("count"))
        or 0
    )
    trade_count = int(_summary_number(evidence.get("trade_count")) or valid_count)
    avg = _summary_bps(metrics.get("avg_pnl_bps")) or "unavailable"
    win_rate = _summary_percent(metrics.get("win_rate")) or "unavailable"
    tail = _summary_bps(metrics.get("worst_decile_pnl_bps") or metrics.get("worst_decile_bps")) or "unavailable"
    active_hours = _summary_number(evaluation.get("active_hours"))
    venue_detail = _summary_groups(evidence.get("by_venue"), 3)
    route_counts = _summary_mapping(evidence.get("route_status_counts"), 4)
    raw_strategy_logic = data.get("strategy_logic") if isinstance(data.get("strategy_logic"), dict) else {}
    strategy_logic = _summary_mapping(raw_strategy_logic, 8)
    data_requirements = _summary_mapping(data.get("data_requirements"), 5)
    risk_gates = _summary_mapping(data.get("risk_gates"), 5)
    diagnostic = evaluation.get("generation_diagnostic") if isinstance(evaluation.get("generation_diagnostic"), dict) else {}
    source_candidates = int(_summary_number(diagnostic.get("source_candidate_count")) or 0)
    generated_candidates = int(_summary_number(diagnostic.get("generated_candidate_count")) or 0)
    reject_reasons = _summary_mapping(diagnostic.get("dominant_reject_reasons"), 5)
    generic_hypothesis = hypothesis.lower().rstrip(".") in {
        "generated by llm swarm", "generated by strategy lab", "strategy lab experiment"
    }
    parts = [f"Strategy Lab experiment {subject} recorded this hypothesis: {hypothesis}"]
    if generic_hypothesis:
        parts.append(
            "That hypothesis text is underspecified and does not identify a reusable market rule on its own."
        )
    if strategy_logic and set(raw_strategy_logic) - {"type"}:
        parts.append(f"Its executable strategy contract was {strategy_logic}.")
    elif strategy_logic:
        parts.append(
            f"Its executable contract recorded only {strategy_logic}, with no market, direction, or feature gates."
        )
    else:
        parts.append("No executable strategy logic was persisted, so the hypothesis cannot yet be reproduced.")
    if data_requirements:
        parts.append(f"Required data: {data_requirements}.")
    if risk_gates:
        parts.append(f"Paper risk gates: {risk_gates}.")
    parts.append(
        f"Its current decision is {status}. It has {valid_count} valid reliable outcomes from "
        f"{trade_count} tracked paper trades, averaging {avg}, with {win_rate} profitable and a "
        f"worst-decile outcome of {tail}."
    )
    if diagnostic:
        parts.append(
            f"The latest generator examined {source_candidates} source candidates and emitted "
            f"{generated_candidates}; dominant rejection evidence was {reject_reasons or 'not recorded'}."
        )
    if active_hours is not None:
        parts.append(f"The evaluation covers approximately {active_hours:.1f} active hours.")
    if venue_detail:
        parts.append(f"The largest venue samples were {venue_detail}.")
    if route_counts:
        parts.append(f"Route evidence was {route_counts}.")
    return " ".join(parts)


def _code_evolution_summary(subject: str, data: dict) -> str:
    status = _summary_text(data.get("status") or "unknown", 80)
    title = _summary_text(data.get("title") or subject, 220)
    category = _summary_text(data.get("category") or "uncategorized", 100)
    agent = _summary_text(data.get("source_agent") or "unknown agent", 100)
    files = _summary_list(data.get("changed_files"), 6) or "no changed files were recorded"
    commit = _summary_text(data.get("candidate_commit") or "none", 50)
    reason = _summary_text(data.get("promotion_reason"), 280)
    tests = data.get("tests") if isinstance(data.get("tests"), dict) else {}
    stages = _summary_list(list(tests.keys()), 6) or "no test-stage summary"
    summary = (
        f"Code-evolution proposal {subject}, titled '{title}', was produced by {agent} for category "
        f"{category}. Final status: {status}. It targeted {files}. Candidate commit: {commit}. "
        f"Recorded validation stages: {stages}."
    )
    if reason:
        summary += f" Promotion or disposition reason: {reason}."
    return summary


def _recommendation_summary(subject: str, data: dict) -> str:
    agent = _summary_text(data.get("agent_name") or "unknown agent", 100)
    action = _summary_text(data.get("action") or "unspecified action", 100)
    title = _summary_text(data.get("title") or subject, 220)
    status = _summary_text(data.get("status") or "unknown", 80)
    rationale = _summary_text(data.get("rationale"), 330)
    proposed = _summary_text(data.get("proposed_change"), 330)
    target = _summary_text(data.get("signal_key") or data.get("market_key"), 180)
    downstream = data.get("downstream_code") if isinstance(data.get("downstream_code"), list) else []
    downstream_text = ", ".join(
        f"{item.get('proposal_id')}={item.get('status')}"
        for item in downstream[:4]
        if isinstance(item, dict)
    )
    summary = (
        f"Agent recommendation {subject} came from {agent} as action {action}. It proposed "
        f"'{title}' and currently has pipeline status {status}."
    )
    if target:
        summary += f" The targeted market or signal was {target}."
    if rationale:
        summary += f" Reasoning: {rationale}"
    if proposed:
        summary += f" Intended implementation: {proposed}"
    generic_phrases = {"generated by llm swarm", "generated by strategy lab", "recommendation"}
    if (
        (not rationale or rationale.lower().rstrip(".") in generic_phrases)
        and (not proposed or proposed.lower().rstrip(".") in generic_phrases)
    ):
        summary += (
            " The source payload is underspecified: it did not preserve a substantive rationale or "
            "implementation description."
        )
    if downstream_text:
        summary += f" Downstream code results: {downstream_text}."
    elif status in {"accepted", "pending"}:
        summary += " No downstream code proposal has been recorded yet."
    return summary


def _self_improvement_summary(subject: str, data: dict) -> str:
    hypothesis = _summary_text(data.get("hypothesis") or "No hypothesis text was recorded.", 320)
    signal_key = _summary_text(data.get("signal_key") or "unknown signal", 180)
    task_type = _summary_text(data.get("task_type") or "unknown task type", 100)
    status = _summary_text(data.get("status") or "unknown", 80)
    decision = _summary_text(data.get("decision") or "not decided", 100)
    reflection = _summary_text(data.get("reflection"), 300)
    evaluation = data.get("evaluation") if isinstance(data.get("evaluation"), dict) else {}
    evaluation_text = _summary_mapping(evaluation, 5)
    summary = (
        f"Self-improvement experiment {subject} tested {task_type} against {signal_key}. "
        f"Hypothesis: {hypothesis} Current status: {status}; decision: {decision}."
    )
    if evaluation_text:
        summary += f" Measured evaluation: {evaluation_text}."
    if reflection:
        summary += f" Recorded reflection: {reflection}"
    return summary


def _market_health_summary(fact_type: str, subject: str, predicate: str, data: dict, detail: str) -> str:
    status = _summary_text(data.get("data_status") or data.get("status") or detail or predicate, 180)
    symbol = _summary_text(data.get("symbol") or data.get("inst_id"), 100)
    source_url = _summary_text(data.get("source_url") or data.get("url"), 180)
    latency = _summary_number(data.get("latency_ms"))
    quality = _summary_number(data.get("quality_score"))
    spread = _summary_bps(data.get("spread_bps"))
    freshness = _summary_number(data.get("data_age_seconds") or data.get("age_seconds"))
    parts = [f"Market-data memory for {subject}: {predicate}. Latest observed status: {status}."]
    if symbol:
        parts.append(f"The observation concerned {symbol}.")
    measures: list[str] = []
    if latency is not None:
        measures.append(f"latency {latency:.0f} ms")
    if quality is not None:
        measures.append(f"quality score {quality:.1f}/100")
    if spread:
        measures.append(f"spread {spread}")
    if freshness is not None:
        measures.append(f"data age {freshness:.1f} seconds")
    if measures:
        parts.append("Measured data context: " + ", ".join(measures) + ".")
    if source_url:
        parts.append(f"Evidence source: {source_url}.")
    if fact_type == "venue_health" and predicate == "is_unreachable":
        parts.append("This is a data-access condition, not evidence that a trading strategy lost money.")
    return " ".join(parts)


def _route_summary(data: dict) -> str:
    statuses = data.get("by_route_status") if isinstance(data.get("by_route_status"), dict) else data
    status_text = _summary_mapping(statuses, 6) or "no route-status counts were available"
    blockers = data.get("by_missing_requirement") if isinstance(data.get("by_missing_requirement"), dict) else {}
    blocker_text = _summary_mapping(blockers, 6)
    actions = data.get("top_manual_actions") if isinstance(data.get("top_manual_actions"), list) else []
    action_text = "; ".join(
        f"{item.get('requirement_id')} affects {item.get('count')} opportunities and suggests "
        f"{_summary_text(item.get('suggested_action'), 170)}"
        for item in actions[:3]
        if isinstance(item, dict)
    )
    summary = f"Execution-route state across the current candidate set: {status_text}."
    if blocker_text:
        summary += f" The leading unresolved requirements are {blocker_text}."
    if action_text:
        summary += f" Highest-unlock actions: {action_text}."
    summary += " Conditional and blocked counts describe feasibility, not strategy profitability."
    return summary


def _memory_summary(fact_type: str, subject: str, predicate: str, object_value: str, metadata: dict) -> str:
    provided = metadata.get("memory_summary") or metadata.get("summary_text")
    if provided:
        return _summary_text(provided, 1800)
    decoded = _decode(object_value, None)
    data: dict = {}
    if isinstance(decoded, dict):
        data.update(decoded)
    if isinstance(metadata, dict):
        data.update(metadata)

    if fact_type == "signal_performance":
        return _signal_performance_summary(subject, data)[:1800]
    if fact_type == "strategy_lab_evaluation":
        return _strategy_lab_summary(subject, data)[:1800]
    if fact_type == "code_evolution_outcome":
        return _code_evolution_summary(subject, data)[:1800]
    if fact_type in {"recommendation_outcome", "agent_recommendation", "hunter_directive"}:
        return _recommendation_summary(subject, data)[:1800]
    if fact_type in {"self_improvement_outcome", "self_improvement_evaluation"}:
        return _self_improvement_summary(subject, data)[:1800]
    if fact_type in {"venue_health", "frontier_crypto_venue", "market_admission"}:
        return _market_health_summary(fact_type, subject, predicate, data, object_value)[:1800]
    if fact_type == "route_resolver":
        return _route_summary(data)[:1800]
    if fact_type in {"signal_stat", "contextual_failure", "performance_summary"}:
        context = _summary_mapping(data, 9)
        return (
            f"Measured {fact_type.replace('_', ' ')} for {subject}: {predicate}. "
            f"The current evidence says {context or _summary_text(object_value, 700)}. "
            "Treat this as measured context for the named lineage, not as a universal market rule."
        )[:1800]
    if fact_type in {"self_improvement_policy", "contextual_failure_policy"}:
        context = _summary_mapping(data, 8)
        return (
            f"Paper-policy memory for {subject}: {predicate}. {context or _summary_text(object_value, 700)}. "
            "This records a bounded paper behavior change and does not enable live trading."
        )[:1800]

    context = _summary_mapping(data, 8)
    detail = context or _summary_text(object_value, 900)
    return (
        f"{fact_type.replace('_', ' ').title()} concerning {subject}: {predicate}. "
        f"Relevant recorded context: {detail}."
    )[:1800]


def ensure_temporal_schema(conn: sqlite3.Connection) -> dict:
    """Create the optional FTS index and repair it when first enabled."""
    fts_enabled = True
    try:
        conn.execute(
            "create virtual table if not exists temporal_memories_fts "
            "using fts5(memory_id unindexed, text)"
        )
        marker = conn.execute(
            "select state_value_json from memory_system_state where state_key = 'temporal_fts_v2_ready'"
        ).fetchone()
        if not marker:
            current = int(conn.execute("select count(*) from temporal_memories_fts").fetchone()[0])
            total = int(conn.execute("select count(*) from temporal_memories").fetchone()[0])
            if current == 0 and total:
                conn.execute(
                    "insert into temporal_memories_fts(memory_id, text) "
                    "select memory_id, summary || ' ' || subject || ' ' || predicate || ' ' || object "
                    "from temporal_memories"
                )
            conn.execute(
                "insert or replace into memory_system_state(state_key, state_value_json, updated_at) "
                "values ('temporal_fts_v2_ready', ?, ?)",
                (_json({"indexed_rows": total}), _utc_now()),
            )
    except sqlite3.OperationalError:
        fts_enabled = False
    return {"fts_enabled": fts_enabled}


def _index_memory(conn: sqlite3.Connection, memory_id: str, text: str) -> None:
    try:
        conn.execute("delete from temporal_memories_fts where memory_id = ?", (memory_id,))
        conn.execute("insert into temporal_memories_fts(memory_id, text) values (?, ?)", (memory_id, text))
    except sqlite3.OperationalError:
        return


def upsert_memory_fact(
    conn: sqlite3.Connection,
    fact_type: str,
    subject: str,
    predicate: str,
    object_value: str,
    confidence: float,
    source: str,
    metadata: dict,
    *,
    namespace: str | None = None,
    memory_type: str | None = None,
    source_id: str | None = None,
    importance: float | None = None,
    outcome_score: float = 0.0,
    provenance: dict | None = None,
    outcome: dict | None = None,
    tags: Iterable[str] | None = None,
    observed_at: str | None = None,
    profile_version_hours: float = 6.0,
    commit: bool = True,
) -> dict:
    ensure_temporal_schema(conn)
    now = observed_at or _utc_now()
    fact_type = str(fact_type or "system_fact")[:120]
    subject = str(subject or "unknown")[:500]
    predicate = str(predicate or "observed")[:240]
    object_value = str(object_value or "")[:12000]
    source = str(source or "unknown")[:240]
    metadata = dict(metadata or {})
    namespace = namespace or _namespace_for_fact(fact_type, source)
    memory_type = memory_type or _memory_type_for_fact(fact_type, predicate)
    importance = _clamp(importance if importance is not None else _default_importance(fact_type, predicate))
    confidence = _clamp(confidence)
    outcome_score = _clamp(outcome_score, -1.0, 1.0)
    summary = _memory_summary(fact_type, subject, predicate, object_value, metadata)
    tags_list = sorted({str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()})
    source_id = str(source_id or metadata.get("source_id") or metadata.get("recommendation_id") or "") or None
    content_hash = _hash(object_value, summary, _json(outcome or {}))

    if memory_type == "episodic":
        event_id = source_id or metadata.get("proposal_id") or metadata.get("experiment_id") or content_hash
        identity_key = _hash(namespace, fact_type, subject, predicate, event_id)
    else:
        identity_key = _hash(namespace, fact_type, subject, predicate)

    active = conn.execute(
        "select * from temporal_memories where identity_key = ? and status in ('active', 'provisional') "
        "order by version desc limit 1",
        (identity_key,),
    ).fetchone()
    active_dict = dict(active) if active else None
    preserve_change = fact_type in PRESERVE_EVERY_CHANGE_FACT_TYPES

    if active_dict and active_dict["content_hash"] == content_hash:
        memory_id = active_dict["memory_id"]
        conn.execute(
            """
            update temporal_memories
            set last_seen_at = ?, updated_at = ?, observation_count = observation_count + 1,
                confidence = max(confidence, ?), importance = max(importance, ?),
                outcome_score = ?, metadata_json = ?, provenance_json = ?, outcome_json = ?, tags_json = ?
            where memory_id = ?
            """,
            (
                now, now, confidence, importance, outcome_score, _json(metadata), _json(provenance or {}),
                _json(outcome or {}), _json(tags_list), memory_id,
            ),
        )
        if commit:
            conn.commit()
        return {"memory_id": memory_id, "operation": "reinforced", "version": active_dict["version"]}

    create_version = active_dict is None
    if active_dict:
        age_hours = (_parse_iso(now) - _parse_iso(active_dict["valid_from"])).total_seconds() / 3600.0
        create_version = memory_type == "episodic" or preserve_change or age_hours >= max(0.0, profile_version_hours)

    if active_dict and not create_version:
        memory_id = active_dict["memory_id"]
        metadata.setdefault("previous_content_hash", active_dict["content_hash"])
        conn.execute(
            """
            update temporal_memories
            set object = ?, summary = ?, confidence = ?, importance = ?, outcome_score = ?,
                last_seen_at = ?, updated_at = ?, observation_count = observation_count + 1,
                source = ?, source_id = ?, content_hash = ?, metadata_json = ?, provenance_json = ?,
                outcome_json = ?, tags_json = ?
            where memory_id = ?
            """,
            (
                object_value, summary, confidence, importance, outcome_score, now, now, source, source_id,
                content_hash, _json(metadata), _json(provenance or {}), _json(outcome or {}), _json(tags_list),
                memory_id,
            ),
        )
        _index_memory(conn, memory_id, f"{summary} {subject} {predicate} {object_value} {' '.join(tags_list)}")
        if commit:
            conn.commit()
        return {"memory_id": memory_id, "operation": "updated_profile", "version": active_dict["version"]}

    next_version = int(active_dict["version"] or 0) + 1 if active_dict else 1
    if active_dict:
        conn.execute(
            "update temporal_memories set status = ?, valid_to = ?, updated_at = ? where memory_id = ?",
            (SUPERSEDED_STATUS, now, now, active_dict["memory_id"]),
        )
    memory_id = f"mem:{_hash(identity_key, next_version)}"
    status = PROVISIONAL_STATUS if metadata.get("provisional") else ACTIVE_STATUS
    conn.execute(
        """
        insert into temporal_memories (
            memory_id, identity_key, version, namespace, memory_type, fact_type, subject, predicate,
            object, summary, confidence, importance, outcome_score, utility_score, success_count,
            failure_count, last_validated_at, status, valid_from, valid_to,
            first_seen_at, last_seen_at, last_accessed_at, observation_count, access_count, source,
            source_id, content_hash, metadata_json, provenance_json, outcome_json, tags_json,
            created_at, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, null, ?, ?, null, ?, ?, null, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id, identity_key, next_version, namespace, memory_type, fact_type, subject, predicate,
            object_value, summary, confidence, importance, outcome_score, status, now, now, now, source,
            source_id, content_hash, _json(metadata), _json(provenance or {}), _json(outcome or {}),
            _json(tags_list), now, now,
        ),
    )
    _index_memory(conn, memory_id, f"{summary} {subject} {predicate} {object_value} {' '.join(tags_list)}")
    if commit:
        conn.commit()
    return {"memory_id": memory_id, "operation": "inserted", "version": next_version}


def upsert_memory_link(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: str,
    relation: str,
    target_type: str,
    target_id: str,
    *,
    confidence: float = 1.0,
    evidence: dict | None = None,
) -> None:
    now = _utc_now()
    conn.execute(
        """
        insert into temporal_memory_links (
            source_type, source_id, relation, target_type, target_id,
            first_seen_at, last_seen_at, confidence, evidence_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(source_type, source_id, relation, target_type, target_id) do update set
            last_seen_at = excluded.last_seen_at,
            confidence = max(temporal_memory_links.confidence, excluded.confidence),
            evidence_json = excluded.evidence_json
        """,
        (
            str(source_type), str(source_id), str(relation), str(target_type), str(target_id),
            now, now, _clamp(confidence), _json(evidence or {}),
        ),
    )


def _get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("select state_value_json from memory_system_state where state_key = ?", (key,)).fetchone()
    return _decode(row[0], default) if row else default


def _set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        """
        insert into memory_system_state(state_key, state_value_json, updated_at) values (?, ?, ?)
        on conflict(state_key) do update set state_value_json = excluded.state_value_json,
            updated_at = excluded.updated_at
        """,
        (key, _json(value), _utc_now()),
    )


def bootstrap_legacy_memory(conn: sqlite3.Connection, settings: dict) -> dict:
    cfg = settings.get("agent_memory", {})
    if not cfg.get("legacy_bootstrap_enabled", True):
        return {"status": "disabled", "imported": 0}
    marker = _get_state(conn, "legacy_memory_bootstrap_v2")
    if marker:
        return {"status": "already_complete", **marker}
    limit = max(0, int(cfg.get("legacy_bootstrap_limit", 20000)))
    if limit == 0:
        return {"status": "empty", "imported": 0}
    rows = conn.execute(
        """
        select m.created_at, m.fact_type, m.subject, m.predicate, m.object,
               m.confidence, m.source, m.metadata_json
        from memory_facts m
        join (
            select fact_type, subject, predicate, max(id) as max_id
            from memory_facts
            group by fact_type, subject, predicate
        ) latest on latest.max_id = m.id
        order by m.id desc
        limit ?
        """,
        (limit,),
    ).fetchall()
    imported = 0
    for raw in reversed(rows):
        row = dict(raw)
        upsert_memory_fact(
            conn,
            row["fact_type"], row["subject"], row["predicate"], row["object"],
            float(row["confidence"] or 0.5), row["source"], _decode(row["metadata_json"], {}),
            observed_at=row["created_at"],
            profile_version_hours=float(cfg.get("profile_version_hours", 6.0)),
            commit=False,
        )
        imported += 1
    result = {"status": "complete", "imported": imported, "completed_at": _utc_now()}
    _set_state(conn, "legacy_memory_bootstrap_v2", result)
    conn.commit()
    return result


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(math.floor((len(ordered) - 1) * percentile))))
    return float(ordered[index])


def _refresh_signal_outcomes(conn: sqlite3.Connection, cfg: dict) -> int:
    rows = conn.execute(
        """
        select p.signal_key, p.venue, p.trade_type, p.direction, o.pnl_bps
        from paper_trade_outcomes o
        join paper_trades p on p.id = o.trade_id
        where o.horizon_minutes = 60 and o.measurement_status = 'valid' and o.pnl_bps is not null
        """
    ).fetchall()
    grouped: dict[str, dict] = {}
    for raw in rows:
        row = dict(raw)
        key = str(row["signal_key"])
        item = grouped.setdefault(
            key,
            {"venue": row["venue"], "trade_type": row["trade_type"], "direction": row["direction"], "pnls": []},
        )
        item["pnls"].append(float(row["pnl_bps"]))
    for signal_key, item in grouped.items():
        pnls = item.pop("pnls")
        count = len(pnls)
        avg = sum(pnls) / count
        outcome = {
            **item,
            "horizon_minutes": 60,
            "valid_labels": count,
            "avg_pnl_bps": round(avg, 4),
            "win_rate": round(sum(value > 0 for value in pnls) / count, 4),
            "worst_decile_bps": round(_percentile(pnls, 0.10) or 0.0, 4),
            "best_bps": round(max(pnls), 4),
            "worst_bps": round(min(pnls), 4),
        }
        upsert_memory_fact(
            conn,
            "signal_performance", signal_key, "has_reliable_60m_outcomes", _json(outcome), 0.95,
            "paper_outcome_engine", outcome,
            namespace="outcomes", source_id=signal_key,
            importance=min(1.0, 0.55 + min(0.3, count / 200.0) + min(0.15, abs(avg) / 100.0)),
            outcome_score=math.tanh(avg / 50.0), outcome=outcome,
            tags=[item["venue"], item["trade_type"], item["direction"], "reliable_60m"],
            profile_version_hours=float(cfg.get("profile_version_hours", 6.0)), commit=False,
        )
    return len(grouped)


def _refresh_code_evolution(conn: sqlite3.Connection, cfg: dict) -> int:
    rows = conn.execute(
        "select * from code_evolution_proposals order by updated_at desc limit 500"
    ).fetchall()
    promoted = {"promoted", "kept"}
    failed = {
        "discarded_patch_apply_failure", "invalid_patch_format", "discarded_test_failure",
        "rejected_preflight_invalid_target", "rejected_preflight_no_runtime_integration",
        "release_preflight_failed", "invalid_test_commands", "blocked_safety",
    }
    for raw in rows:
        row = dict(raw)
        status = str(row.get("status") or "unknown")
        payload = {
            "proposal_id": row["proposal_id"],
            "source_recommendation_id": row.get("source_recommendation_id"),
            "source_agent": row.get("source_agent"),
            "title": row.get("title"),
            "category": row.get("category"),
            "status": status,
            "changed_files": _decode(row.get("changed_files_json"), []),
            "tests": _decode(row.get("tests_json"), {}),
            "candidate_commit": row.get("candidate_commit"),
            "promotion_reason": row.get("promotion_reason"),
            "updated_at": row.get("updated_at"),
        }
        score = 1.0 if status in promoted else (-0.8 if status in failed else 0.0)
        importance = 0.95 if status in promoted else (0.82 if status in failed else 0.55)
        upsert_memory_fact(
            conn,
            "code_evolution_outcome", row["proposal_id"], status, _json(payload), 0.98,
            "code_evolution", payload,
            namespace="code", source_id=row["proposal_id"], importance=importance,
            outcome_score=score, outcome=payload,
            tags=[str(row.get("category") or "unknown"), str(row.get("source_agent") or "unknown"), status],
            profile_version_hours=float(cfg.get("profile_version_hours", 6.0)), commit=False,
        )
        rec_id = row.get("source_recommendation_id")
        if rec_id:
            upsert_memory_link(
                conn, "recommendation", rec_id, "implemented_as", "code_evolution_proposal", row["proposal_id"],
                evidence={"status": status, "candidate_commit": row.get("candidate_commit")},
            )
        if row.get("candidate_commit"):
            upsert_memory_link(
                conn, "code_evolution_proposal", row["proposal_id"], "produced", "git_commit",
                row["candidate_commit"], evidence={"status": status},
            )
    return len(rows)


def _refresh_strategy_lab(conn: sqlite3.Connection, cfg: dict) -> int:
    rows = conn.execute("select * from strategy_lab_experiments order by updated_at desc").fetchall()
    for raw in rows:
        row = dict(raw)
        evaluation = _decode(row.get("evaluation_json"), {})
        payload = {
            "strategy_lab_id": row["strategy_lab_id"],
            "version": row.get("version"),
            "status": row.get("status"),
            "hypothesis": row.get("hypothesis"),
            "experiment_type": row.get("experiment_type"),
            "strategy_logic": _decode(row.get("strategy_logic_json"), {}),
            "data_requirements": _decode(row.get("data_requirements_json"), {}),
            "risk_gates": _decode(row.get("risk_gates_json"), {}),
            "promotion_rules": _decode(row.get("promotion_rules_json"), {}),
            "source_agent": row.get("source_agent"),
            "source_recommendation_id": row.get("source_recommendation_id"),
            "evaluation": evaluation,
            "promoted_proposal_id": row.get("promoted_proposal_id"),
        }
        metrics = evaluation.get("metrics") if isinstance(evaluation.get("metrics"), dict) else evaluation
        avg = float(metrics.get("avg_pnl_bps") or 0.0) if isinstance(metrics, dict) else 0.0
        status = str(row.get("status") or "unknown")
        importance = 0.9 if status in {"promote_candidate", "promotion_queued", "promoted_to_code", "retired_bad_evidence"} else 0.68
        upsert_memory_fact(
            conn,
            "strategy_lab_evaluation", row["strategy_lab_id"], status, _json(payload), 0.94,
            "strategy_lab", payload,
            namespace="strategies", source_id=row["strategy_lab_id"], importance=importance,
            outcome_score=math.tanh(avg / 50.0), outcome=evaluation,
            tags=[status, str(row.get("experiment_type") or "market_strategy"), str(row.get("source_agent") or "unknown")],
            profile_version_hours=float(cfg.get("profile_version_hours", 6.0)), commit=False,
        )
        rec_id = row.get("source_recommendation_id")
        if rec_id:
            upsert_memory_link(
                conn, "recommendation", rec_id, "created", "strategy_lab_experiment", row["strategy_lab_id"],
                evidence={"status": status},
            )
        if row.get("promoted_proposal_id"):
            upsert_memory_link(
                conn, "strategy_lab_experiment", row["strategy_lab_id"], "promoted_as",
                "code_evolution_proposal", row["promoted_proposal_id"], evidence=evaluation,
            )
    return len(rows)


def _refresh_self_improvement(conn: sqlite3.Connection, cfg: dict) -> int:
    rows = conn.execute(
        "select * from self_improvement_experiments order by id desc limit 500"
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        evaluation = _decode(row.get("evaluation_json"), {})
        payload = {
            "experiment_id": row["id"],
            "source_recommendation_id": row.get("source_recommendation_id"),
            "source_agent": row.get("source_agent"),
            "task_type": row.get("task_type"),
            "signal_key": row.get("signal_key"),
            "hypothesis": row.get("hypothesis"),
            "status": row.get("status"),
            "decision": row.get("decision"),
            "reflection": row.get("reflection"),
            "evaluation": evaluation,
        }
        decision = str(row.get("decision") or row.get("status") or "unknown")
        score = 0.7 if decision in {"promoted", "released", "kept"} else (-0.6 if decision in {"reverted", "expired", "demoted"} else 0.0)
        upsert_memory_fact(
            conn,
            "self_improvement_outcome", str(row["id"]), decision, _json(payload), 0.92,
            "self_improvement_executor", payload,
            namespace="policies", source_id=str(row["id"]), importance=0.72,
            outcome_score=score, outcome=evaluation,
            tags=[str(row.get("task_type") or "unknown"), str(row.get("signal_key") or "unknown"), decision],
            profile_version_hours=float(cfg.get("profile_version_hours", 6.0)), commit=False,
        )
        rec_id = row.get("source_recommendation_id")
        if rec_id:
            upsert_memory_link(
                conn, "recommendation", rec_id, "created", "self_improvement_experiment", str(row["id"]),
                evidence={"status": row.get("status"), "decision": row.get("decision")},
            )
    return len(rows)


def _refresh_recommendations(conn: sqlite3.Connection, cfg: dict) -> int:
    table_exists = conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'llm_recommendations'"
    ).fetchone()
    if not table_exists:
        return 0
    rows = conn.execute(
        "select * from llm_recommendations order by created_at desc limit 1000"
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        payload_data = _decode(row.get("payload_json"), {})
        downstream = conn.execute(
            "select proposal_id, status, candidate_commit from code_evolution_proposals "
            "where source_recommendation_id = ? order by updated_at desc",
            (row["recommendation_id"],),
        ).fetchall()
        downstream_items = [dict(item) for item in downstream]
        payload = {
            "recommendation_id": row["recommendation_id"],
            "action": row.get("action"),
            "title": row.get("title"),
            "status": row.get("status"),
            "agent_name": payload_data.get("agent_name"),
            "market_key": payload_data.get("market_key"),
            "signal_key": payload_data.get("signal_key"),
            "rationale": row.get("rationale") or payload_data.get("rationale"),
            "proposed_change": payload_data.get("proposed_change"),
            "evidence": payload_data.get("evidence"),
            "frontier_escalation_reason": payload_data.get("frontier_escalation_reason"),
            "downstream_code": downstream_items,
        }
        promoted = any(item.get("status") in {"promoted", "kept"} for item in downstream_items)
        failed = bool(downstream_items) and all(item.get("status") not in {"promoted", "kept"} for item in downstream_items)
        score = 0.8 if promoted else (-0.45 if failed else 0.0)
        importance = 0.85 if downstream_items else 0.48
        upsert_memory_fact(
            conn,
            "recommendation_outcome", row["recommendation_id"], str(row.get("status") or "accepted"),
            _json(payload), 0.9, "llm_recommendation_pipeline", payload,
            namespace="recommendations", source_id=row["recommendation_id"], importance=importance,
            outcome_score=score, outcome={"downstream_code": downstream_items},
            tags=[str(row.get("action") or "unknown"), str(payload_data.get("agent_name") or "unknown")],
            profile_version_hours=float(cfg.get("profile_version_hours", 6.0)), commit=False,
        )
    return len(rows)


def _refresh_memory_utility(conn: sqlite3.Connection) -> int:
    """Reward or penalize memories according to outcomes they influenced."""
    rows = conn.execute(
        """
        select l.source_id as memory_id, l.target_id as recommendation_id
        from temporal_memory_links l
        where l.source_type = 'memory'
          and l.relation = 'informed'
          and l.target_type = 'recommendation'
        """
    ).fetchall()
    evidence_by_memory: dict[str, list[float]] = defaultdict(list)
    successful_code = {"promoted", "kept"}
    failed_code = {
        "discarded_patch_apply_failure", "invalid_patch_format", "discarded_test_failure",
        "rejected_preflight_invalid_target", "rejected_preflight_no_runtime_integration",
        "release_preflight_failed", "invalid_test_commands", "blocked_safety", "archived_failed",
    }
    for raw in rows:
        memory_id = str(raw["memory_id"])
        recommendation_id = str(raw["recommendation_id"])
        code_rows = conn.execute(
            "select status from code_evolution_proposals where source_recommendation_id = ?",
            (recommendation_id,),
        ).fetchall()
        for code_row in code_rows:
            status = str(code_row["status"] or "")
            if status in successful_code:
                evidence_by_memory[memory_id].append(1.0)
            elif status in failed_code:
                evidence_by_memory[memory_id].append(-0.65)
        lab_rows = conn.execute(
            "select status, evaluation_json from strategy_lab_experiments where source_recommendation_id = ?",
            (recommendation_id,),
        ).fetchall()
        for lab_row in lab_rows:
            status = str(lab_row["status"] or "")
            evaluation = _decode(lab_row["evaluation_json"], {})
            metrics = evaluation.get("metrics") if isinstance(evaluation.get("metrics"), dict) else evaluation
            avg = float(metrics.get("avg_pnl_bps") or 0.0) if isinstance(metrics, dict) else 0.0
            if status in {"promote_candidate", "promotion_queued", "promoted_to_code"}:
                evidence_by_memory[memory_id].append(max(0.4, math.tanh(avg / 30.0)))
            elif status == "retired_bad_evidence":
                evidence_by_memory[memory_id].append(min(-0.4, math.tanh(avg / 30.0)))
        policy_rows = conn.execute(
            "select decision from self_improvement_experiments where source_recommendation_id = ?",
            (recommendation_id,),
        ).fetchall()
        for policy_row in policy_rows:
            decision = str(policy_row["decision"] or "")
            if decision in {"promoted", "released", "kept"}:
                evidence_by_memory[memory_id].append(0.65)
            elif decision in {"reverted", "demoted"}:
                evidence_by_memory[memory_id].append(-0.55)

    now = _utc_now()
    for memory_id, outcomes in evidence_by_memory.items():
        successes = sum(value > 0 for value in outcomes)
        failures = sum(value < 0 for value in outcomes)
        weighted = sum(outcomes) / max(1, len(outcomes))
        confidence_weight = min(1.0, math.log1p(len(outcomes)) / math.log(8.0))
        utility = _clamp(weighted * confidence_weight, -1.0, 1.0)
        conn.execute(
            """
            update temporal_memories
            set utility_score = ?, success_count = ?, failure_count = ?,
                last_validated_at = ?, updated_at = ?
            where memory_id = ?
            """,
            (utility, successes, failures, now, now, memory_id),
        )
    return len(evidence_by_memory)


def _refresh_agent_effectiveness(conn: sqlite3.Connection, cfg: dict) -> int:
    rows = conn.execute(
        """
        select coalesce(source_agent, 'unknown') as agent_name,
               count(*) as proposals,
               sum(case when status in ('promoted', 'kept') then 1 else 0 end) as useful,
               sum(case when status in (
                   'discarded_patch_apply_failure', 'invalid_patch_format', 'discarded_test_failure',
                   'rejected_preflight_invalid_target', 'rejected_preflight_no_runtime_integration',
                   'release_preflight_failed', 'invalid_test_commands', 'blocked_safety'
               ) then 1 else 0 end) as failed
        from code_evolution_proposals
        group by coalesce(source_agent, 'unknown')
        """
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        proposals = int(row["proposals"] or 0)
        useful = int(row["useful"] or 0)
        failed = int(row["failed"] or 0)
        payload = {
            "agent_name": row["agent_name"],
            "code_proposals": proposals,
            "useful_code_changes": useful,
            "failed_code_changes": failed,
            "useful_merge_rate": round(useful / proposals, 4) if proposals else 0.0,
        }
        upsert_memory_fact(
            conn,
            "agent_effectiveness", row["agent_name"], "has_code_evolution_record", _json(payload), 0.96,
            "memory_reflection", payload,
            namespace="code", source_id=row["agent_name"], importance=0.78,
            outcome_score=(useful - failed) / max(1, proposals), outcome=payload,
            tags=[row["agent_name"], "agent_scorecard"],
            profile_version_hours=float(cfg.get("profile_version_hours", 6.0)), commit=False,
        )
    return len(rows)


def refresh_memory_prose(conn: sqlite3.Connection) -> dict:
    """One-time presentation migration for active records created before rich prose."""
    marker_key = "temporal_memory_rich_prose_v1"
    existing = _get_state(conn, marker_key)
    if existing:
        return {**existing, "status": "already_complete"}
    rows = conn.execute(
        "select * from temporal_memories where status in ('active', 'provisional')"
    ).fetchall()
    updated = 0
    for raw in rows:
        row = dict(raw)
        metadata = _decode(row.get("metadata_json"), {})
        outcome = _decode(row.get("outcome_json"), {})
        summary = _memory_summary(
            str(row.get("fact_type") or "system_fact"),
            str(row.get("subject") or "unknown"),
            str(row.get("predicate") or "observed"),
            str(row.get("object") or ""),
            metadata,
        )
        if summary == row.get("summary"):
            continue
        content_hash = _hash(row.get("object"), summary, _json(outcome))
        conn.execute(
            "update temporal_memories set summary = ?, content_hash = ?, updated_at = ? where memory_id = ?",
            (summary, content_hash, _utc_now(), row["memory_id"]),
        )
        tags = _decode(row.get("tags_json"), [])
        _index_memory(
            conn,
            row["memory_id"],
            f"{summary} {row.get('subject', '')} {row.get('predicate', '')} "
            f"{row.get('object', '')} {' '.join(str(tag) for tag in tags)}",
        )
        updated += 1
    result = {"status": "complete", "examined": len(rows), "updated": updated, "completed_at": _utc_now()}
    _set_state(conn, marker_key, result)
    return result


def refresh_evidence_memories(conn: sqlite3.Connection, settings: dict) -> dict:
    cfg = settings.get("agent_memory", {})
    if not cfg.get("enabled", True):
        return {"status": "disabled"}
    ensure_temporal_schema(conn)
    bootstrap = bootstrap_legacy_memory(conn, settings)
    counts = {
        "signal_outcomes": _refresh_signal_outcomes(conn, cfg),
        "code_evolution": _refresh_code_evolution(conn, cfg),
        "strategy_lab": _refresh_strategy_lab(conn, cfg),
        "self_improvement": _refresh_self_improvement(conn, cfg),
        "recommendations": _refresh_recommendations(conn, cfg),
        "agent_effectiveness": _refresh_agent_effectiveness(conn, cfg),
        "memory_utility": _refresh_memory_utility(conn),
    }
    counts["rich_prose"] = refresh_memory_prose(conn)
    retention = max(100, int(cfg.get("retrieval_log_retention_rows", 10000)))
    conn.execute(
        "delete from memory_retrieval_events where id not in "
        "(select id from memory_retrieval_events order by id desc limit ?)",
        (retention,),
    )
    _set_state(conn, "last_evidence_refresh", {"at": _utc_now(), "counts": counts})
    conn.commit()
    return {"status": "ok", "bootstrap": bootstrap, "refreshed": counts}


def _collect_packet_terms(packet: dict, agent_name: str, policy_override: dict | None = None) -> list[str]:
    policy = policy_override or ROLE_MEMORY_POLICIES.get(agent_name, ROLE_MEMORY_POLICIES["generic"])
    fragments = list(policy["keywords"])
    for item in (packet.get("top_reviewed") or [])[:20]:
        fragments.extend([str(item.get("inst_id") or ""), str(item.get("direction") or ""), str(item.get("route_status") or "")])
    for item in (packet.get("signal_stats") or [])[:30]:
        fragments.append(str(item.get("signal_key") or ""))
    for key in ("hunter_directives", "growth_experiments", "improvement_tasks"):
        for item in (packet.get(key) or [])[:15]:
            fragments.extend(
                str(item.get(field) or "")
                for field in ("market_key", "signal_key", "title", "hypothesis", "directive")
            )
    terms: list[str] = []
    seen: set[str] = set()
    for fragment in fragments:
        for token in re.findall(r"[A-Za-z0-9_]{3,}", fragment):
            lowered = token.lower()
            if lowered in STOPWORDS or lowered in seen:
                continue
            seen.add(lowered)
            terms.append(lowered)
            if len(terms) >= 36:
                return terms
    return terms


def _row_to_memory(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    for key in ("metadata_json", "provenance_json", "outcome_json", "tags_json"):
        target = key.removesuffix("_json")
        item[target] = _decode(item.pop(key, None), [] if key == "tags_json" else {})
    return item


def _prompt_memory(item: dict, max_summary_chars: int) -> dict:
    return {
        "memory_id": item.get("memory_id"),
        "namespace": item.get("namespace"),
        "memory_type": item.get("memory_type"),
        "fact_type": item.get("fact_type"),
        "subject": item.get("subject"),
        "predicate": item.get("predicate"),
        "summary": str(item.get("summary") or "")[:max_summary_chars],
        "confidence": item.get("confidence"),
        "importance": item.get("importance"),
        "outcome_score": item.get("outcome_score"),
        "utility_score": item.get("utility_score"),
        "status": item.get("status"),
        "valid_from": item.get("valid_from"),
        "valid_to": item.get("valid_to"),
        "last_seen_at": item.get("last_seen_at"),
        "observation_count": item.get("observation_count"),
        "source": item.get("source"),
        "source_id": item.get("source_id"),
        "tags": item.get("tags", []),
        "relevance_score": item.get("relevance_score"),
        "why_retrieved": item.get("why_retrieved", {}),
        "temporal_relation": item.get("temporal_relation", "current"),
    }


def retrieve_role_memories(
    conn: sqlite3.Connection,
    packet: dict,
    agent_name: str,
    settings: dict,
    *,
    cycle_id: str,
    policy_override: dict | None = None,
) -> list[dict]:
    cfg = settings.get("agent_memory", {})
    if not cfg.get("enabled", True):
        return []
    ensure_temporal_schema(conn)
    policy = policy_override or ROLE_MEMORY_POLICIES.get(agent_name, ROLE_MEMORY_POLICIES["generic"])
    limit = max(1, int(policy.get("retrieval_limit") or cfg.get("retrieval_limit_per_agent", 24)))
    pool_limit = max(limit, int(cfg.get("retrieval_candidate_pool", 160)))
    minimum_importance = float(cfg.get("minimum_importance", 0.15))
    half_life = max(0.25, float(cfg.get("recency_half_life_days", 14.0)))
    preferred_namespaces = set(policy["namespaces"])
    terms = _collect_packet_terms(packet, agent_name, policy)
    query_text = " ".join(terms)
    graph_relation_bonus = float(cfg.get("graph_relation_bonus", 0.08))

    candidate_rows: dict[str, dict] = {}
    try:
        fts_query = " OR ".join(f'"{term}"' for term in terms[:24])
        if fts_query:
            rows = conn.execute(
                """
                select m.*, bm25(temporal_memories_fts) as fts_rank
                from temporal_memories_fts
                join temporal_memories m on m.memory_id = temporal_memories_fts.memory_id
                where temporal_memories_fts match ?
                  and m.status in ('active', 'provisional') and m.importance >= ?
                order by fts_rank
                limit ?
                """,
                (fts_query, minimum_importance, pool_limit),
            ).fetchall()
            for row in rows:
                item = _row_to_memory(row)
                candidate_rows[item["memory_id"]] = item
    except sqlite3.OperationalError:
        pass

    rows = conn.execute(
        """
        select *, null as fts_rank from temporal_memories
        where status in ('active', 'provisional') and importance >= ?
        order by importance desc, abs(outcome_score) desc, last_seen_at desc
        limit ?
        """,
        (minimum_importance, pool_limit),
    ).fetchall()
    for row in rows:
        item = _row_to_memory(row)
        candidate_rows.setdefault(item["memory_id"], item)

    per_namespace_limit = max(8, int(math.ceil(pool_limit / max(1, len(policy["namespaces"])))))
    for namespace in policy["namespaces"]:
        namespace_rows = conn.execute(
            """
            select *, null as fts_rank from temporal_memories
            where status in ('active', 'provisional') and importance >= ? and namespace = ?
            order by importance desc, abs(outcome_score) desc, utility_score desc, last_seen_at desc
            limit ?
            """,
            (minimum_importance, namespace, per_namespace_limit),
        ).fetchall()
        for row in namespace_rows:
            item = _row_to_memory(row)
            candidate_rows.setdefault(item["memory_id"], item)

    candidate_ids = list(candidate_rows)[:pool_limit]
    linked_recommendations: set[str] = set()
    if candidate_ids:
        placeholders = ",".join("?" for _ in candidate_ids)
        linked_recommendations.update(
            str(row[0])
            for row in conn.execute(
                f"select target_id from temporal_memory_links where source_type='memory' "
                f"and relation='informed' and source_id in ({placeholders}) limit 80",
                candidate_ids,
            )
        )
    linked_recommendations.update(
        str(item.get("source_id"))
        for item in candidate_rows.values()
        if item.get("fact_type") == "recommendation_outcome" and item.get("source_id")
    )
    graph_memory_ids: set[str] = set()
    if linked_recommendations:
        recommendation_ids = list(linked_recommendations)[:80]
        placeholders = ",".join("?" for _ in recommendation_ids)
        graph_memory_ids.update(
            str(row[0])
            for row in conn.execute(
                f"select source_id from temporal_memory_links where source_type='memory' "
                f"and relation='informed' and target_id in ({placeholders}) limit 80",
                recommendation_ids,
            )
        )
        linked_rows = conn.execute(
            f"select *, null as fts_rank from temporal_memories where status in ('active','provisional') "
            f"and (source_id in ({placeholders}) or subject in ({placeholders})) limit 80",
            [*recommendation_ids, *recommendation_ids],
        ).fetchall()
        for row in linked_rows:
            item = _row_to_memory(row)
            candidate_rows.setdefault(item["memory_id"], item)
            graph_memory_ids.add(item["memory_id"])

    now = dt.datetime.now(dt.timezone.utc)
    term_set = set(terms)
    scored: list[dict] = []
    for item in candidate_rows.values():
        text = f"{item.get('summary', '')} {item.get('subject', '')} {item.get('predicate', '')}".lower()
        text_terms = set(re.findall(r"[a-z0-9_]{3,}", text))
        lexical = len(term_set & text_terms) / max(1, min(8, len(term_set)))
        age_days = max(0.0, (now - _parse_iso(item["last_seen_at"])).total_seconds() / 86400.0)
        recency = math.pow(0.5, age_days / half_life)
        namespace_fit = 1.0 if item["namespace"] in preferred_namespaces else 0.0
        strength = min(1.0, math.log1p(int(item.get("observation_count") or 1)) / 5.0)
        provisional_penalty = 0.18 if item.get("status") == PROVISIONAL_STATUS else 0.0
        score = (
            0.30 * lexical
            + 0.20 * float(item.get("importance") or 0)
            + 0.14 * float(item.get("confidence") or 0)
            + 0.12 * recency
            + 0.12 * namespace_fit
            + 0.06 * abs(float(item.get("outcome_score") or 0))
            + 0.08 * max(-1.0, min(1.0, float(item.get("utility_score") or 0)))
            + 0.04 * strength
            + (graph_relation_bonus if item["memory_id"] in graph_memory_ids else 0.0)
            - provisional_penalty
        )
        item["relevance_score"] = round(score, 6)
        item["why_retrieved"] = {
            "lexical": round(lexical, 4),
            "namespace_fit": bool(namespace_fit),
            "recency": round(recency, 4),
            "importance": item.get("importance"),
            "outcome_score": item.get("outcome_score"),
            "utility_score": item.get("utility_score"),
            "graph_linked": item["memory_id"] in graph_memory_ids,
        }
        scored.append(item)
    scored.sort(key=lambda item: item["relevance_score"], reverse=True)

    selected: list[dict] = []
    selected_ids: set[str] = set()
    namespace_counts: Counter[str] = Counter()
    fact_counts: Counter[str] = Counter()
    max_per_namespace = max(4, int(math.ceil(limit * 0.42)))
    max_per_fact = max(3, int(math.ceil(limit * 0.30)))
    preferred_target = min(
        limit,
        max(1, int(math.ceil(limit * float(cfg.get("preferred_namespace_fraction", 0.67))))),
    )
    preferred_queues = {
        namespace: [item for item in scored if item["namespace"] == namespace]
        for namespace in policy["namespaces"]
    }
    while len(selected) < preferred_target:
        added = False
        for namespace in policy["namespaces"]:
            queue = preferred_queues[namespace]
            while queue and queue[0]["memory_id"] in selected_ids:
                queue.pop(0)
            if not queue:
                continue
            item = queue.pop(0)
            selected.append(item)
            selected_ids.add(item["memory_id"])
            namespace_counts[item["namespace"]] += 1
            fact_counts[item["fact_type"]] += 1
            added = True
            if len(selected) >= preferred_target:
                break
        if not added:
            break
    for item in scored:
        if item["memory_id"] in selected_ids:
            continue
        if namespace_counts[item["namespace"]] >= max_per_namespace:
            continue
        if fact_counts[item["fact_type"]] >= max_per_fact:
            continue
        selected.append(item)
        selected_ids.add(item["memory_id"])
        namespace_counts[item["namespace"]] += 1
        fact_counts[item["fact_type"]] += 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for item in scored:
            if item["memory_id"] in selected_ids:
                continue
            selected.append(item)
            selected_ids.add(item["memory_id"])
            namespace_counts[item["namespace"]] += 1
            fact_counts[item["fact_type"]] += 1
            if len(selected) >= limit:
                break

    history_limit = max(0, min(limit // 3, int(round(limit * float(cfg.get("historical_context_fraction", 0.17))))))
    historical: list[dict] = []
    for current in selected[: max(history_limit * 3, history_limit)]:
        if len(historical) >= history_limit:
            break
        previous = conn.execute(
            "select *, null as fts_rank from temporal_memories "
            "where identity_key = ? and status = 'superseded' order by version desc limit 1",
            (current["identity_key"],),
        ).fetchone()
        if not previous:
            continue
        item = _row_to_memory(previous)
        item["relevance_score"] = round(max(0.0, float(current["relevance_score"]) - 0.04), 6)
        item["why_retrieved"] = {
            "temporal_predecessor_of": current["memory_id"],
            "version": item.get("version"),
        }
        item["temporal_relation"] = "previous_version"
        historical.append(item)
    if historical:
        selected = [*selected[: limit - len(historical)], *historical]

    ids = [item["memory_id"] for item in selected]
    if ids:
        conn.executemany(
            "update temporal_memories set last_accessed_at = ?, access_count = access_count + 1 where memory_id = ?",
            [(_utc_now(), memory_id) for memory_id in ids],
        )
    conn.execute(
        """
        insert into memory_retrieval_events (
            created_at, cycle_id, agent_name, query_text, memory_ids_json,
            scores_json, selected_count, namespace_counts_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _utc_now(), cycle_id, agent_name, query_text, _json(ids),
            _json({item["memory_id"]: item["relevance_score"] for item in selected}),
            len(selected), _json(dict(namespace_counts)),
        ),
    )
    conn.commit()
    max_summary_chars = max(200, int(cfg.get("max_memory_summary_chars", 800)))
    return [_prompt_memory(item, max_summary_chars) for item in selected]


def build_role_memory_contexts(conn: sqlite3.Connection, packet: dict, settings: dict, agent_names: Iterable[str]) -> tuple[dict, str]:
    started_at = dt.datetime.now(dt.timezone.utc)
    cycle_material = {"packet": packet.get("generated_at") or packet.get("summary"), "started_at": started_at.isoformat()}
    cycle_id = f"swarm:{started_at.strftime('%Y%m%dT%H%M%S%fZ')}:{_hash(_json(cycle_material), length=12)}"
    names = list(agent_names)
    contexts = {
        name: retrieve_role_memories(conn, packet, name, settings, cycle_id=cycle_id)
        for name in names
    }
    graph_contexts, graph_status = retrieve_graphiti_contexts(packet, settings, names)
    limit = max(1, int(settings.get("agent_memory", {}).get("retrieval_limit_per_agent", 24)))
    for name, graph_items in graph_contexts.items():
        if not graph_items:
            continue
        local_limit = max(0, limit - len(graph_items))
        contexts[name] = [*contexts.get(name, [])[:local_limit], *graph_items[:limit]]
    _set_state(conn, "last_graphiti_retrieval", {"cycle_id": cycle_id, **graph_status})
    conn.commit()
    return contexts, cycle_id


def record_swarm_reflection(conn: sqlite3.Connection, state: dict, cycle_id: str, settings: dict) -> dict:
    cfg = settings.get("agent_memory", {})
    if not cfg.get("enabled", True):
        return {"status": "disabled"}
    recorded = 0
    for output in state.get("agent_outputs", []) or []:
        rec = output.get("recommendation") if isinstance(output, dict) else None
        if not isinstance(rec, dict):
            continue
        rec_id = hashlib.sha256(json.dumps(rec, sort_keys=True).encode("utf-8")).hexdigest()
        rejected = not bool(output.get("accepted"))
        payload = {
            "cycle_id": cycle_id,
            "agent_name": output.get("agent_name"),
            "accepted": not rejected,
            "parse_status": output.get("parse_status"),
            "action": rec.get("action"),
            "priority": rec.get("priority"),
            "title": rec.get("title"),
            "market_key": rec.get("market_key"),
            "signal_key": rec.get("signal_key"),
            "rationale": rec.get("rationale"),
            "proposed_change": rec.get("proposed_change"),
            "evidence": rec.get("evidence"),
            "frontier_escalation_reason": rec.get("frontier_escalation_reason"),
        }
        upsert_memory_fact(
            conn,
            "agent_recommendation", rec_id, "rejected" if rejected else "proposed", _json(payload),
            0.7 if rejected else 0.8, str(output.get("agent_name") or "llm_swarm"),
            {**payload, "provisional": not rejected},
            namespace="recommendations", memory_type="episodic", source_id=rec_id,
            importance=min(0.95, max(0.35, float(rec.get("priority") or 50) / 100.0)),
            outcome_score=-0.2 if rejected else 0.0,
            provenance={"cycle_id": cycle_id, "graph_trace": state.get("graph_trace", [])},
            tags=[str(output.get("agent_name") or "unknown"), str(rec.get("action") or "unknown")],
            commit=False,
        )
        upsert_memory_link(
            conn, "swarm_cycle", cycle_id, "produced", "recommendation", rec_id,
            confidence=0.8, evidence={"accepted": not rejected, "agent_name": output.get("agent_name")},
        )
        for memory_id in output.get("memory_ids", []) or []:
            upsert_memory_link(
                conn, "memory", str(memory_id), "informed", "recommendation", rec_id,
                confidence=0.75,
                evidence={"cycle_id": cycle_id, "agent_name": output.get("agent_name")},
            )
        recorded += 1
    _set_state(conn, "last_swarm_reflection", {"cycle_id": cycle_id, "recorded": recorded, "at": _utc_now()})
    conn.commit()
    return {"status": "ok", "cycle_id": cycle_id, "recorded": recorded}


def graphiti_status(settings: dict) -> dict:
    cfg = settings.get("agent_memory", {}).get("graphiti", {})
    mode = str(cfg.get("mode", "auto")).lower()
    uri = os.getenv(str(cfg.get("uri_env", "GRAPHITI_URI")))
    user = os.getenv(str(cfg.get("user_env", "GRAPHITI_USER")))
    password = os.getenv(str(cfg.get("password_env", "GRAPHITI_PASSWORD")))
    configured = bool(uri and user and password)
    installed = importlib.util.find_spec("graphiti_core") is not None
    if mode == "disabled":
        status = "disabled"
    elif not installed:
        status = "package_missing"
    elif not configured:
        status = "waiting_for_graph_backend"
    else:
        status = "ready"
    return {"mode": mode, "installed": installed, "configured": configured, "status": status}


async def _retrieve_graphiti_contexts_async(
    packet: dict,
    settings: dict,
    agent_names: list[str],
) -> dict[str, list[dict]]:
    from graphiti_core import Graphiti

    cfg = settings.get("agent_memory", {}).get("graphiti", {})
    graph = Graphiti(
        os.getenv(str(cfg.get("uri_env", "GRAPHITI_URI"))),
        os.getenv(str(cfg.get("user_env", "GRAPHITI_USER"))),
        os.getenv(str(cfg.get("password_env", "GRAPHITI_PASSWORD"))),
    )
    max_results = max(1, int(cfg.get("max_search_results_per_agent", 6)))

    async def search_role(name: str) -> tuple[str, list[dict]]:
        policy = ROLE_MEMORY_POLICIES.get(name, ROLE_MEMORY_POLICIES["generic"])
        query = " ".join(_collect_packet_terms(packet, name)[:24])
        edges = await graph.search(
            query,
            group_ids=[f"agentic-trading:{namespace}" for namespace in policy["namespaces"]],
            num_results=max_results,
        )
        return name, [
            {
                "memory_id": f"graphiti:{edge.uuid}",
                "namespace": str(edge.group_id).removeprefix("agentic-trading:"),
                "memory_type": "graph_relation",
                "fact_type": "graphiti_entity_edge",
                "subject": edge.source_node_uuid,
                "predicate": edge.name,
                "summary": str(edge.fact)[:800],
                "confidence": None,
                "importance": 0.8,
                "outcome_score": 0.0,
                "utility_score": 0.0,
                "status": "active" if edge.invalid_at is None else "historical",
                "valid_from": edge.valid_at.isoformat() if edge.valid_at else None,
                "valid_to": edge.invalid_at.isoformat() if edge.invalid_at else None,
                "last_seen_at": edge.reference_time.isoformat() if edge.reference_time else None,
                "observation_count": len(edge.episodes or []),
                "source": "graphiti",
                "source_id": edge.uuid,
                "tags": [edge.name, "graphiti"],
                "relevance_score": None,
                "why_retrieved": {"mode": "graphiti_hybrid_search", "query": query},
                "temporal_relation": "graph_relation",
            }
            for edge in edges
        ]

    try:
        pairs = await asyncio.gather(*(search_role(name) for name in agent_names))
        return dict(pairs)
    finally:
        await graph.close()


def retrieve_graphiti_contexts(
    packet: dict,
    settings: dict,
    agent_names: list[str],
) -> tuple[dict[str, list[dict]], dict]:
    status = graphiti_status(settings)
    cfg = settings.get("agent_memory", {}).get("graphiti", {})
    if status["status"] != "ready" or not cfg.get("search_enabled", True):
        return {name: [] for name in agent_names}, status | {"retrieved": 0}
    try:
        contexts = asyncio.run(_retrieve_graphiti_contexts_async(packet, settings, agent_names))
    except Exception as exc:
        return {name: [] for name in agent_names}, status | {
            "status": "search_error",
            "retrieved": 0,
            "error": str(exc)[:1000],
        }
    return contexts, status | {
        "status": "search_ok",
        "retrieved": sum(len(items) for items in contexts.values()),
        "by_agent": {name: len(items) for name, items in contexts.items()},
    }


async def _sync_graphiti_async(conn: sqlite3.Connection, settings: dict, rows: list[dict]) -> dict:
    from graphiti_core import Graphiti
    from graphiti_core.nodes import EpisodeType

    cfg = settings.get("agent_memory", {}).get("graphiti", {})
    graph = Graphiti(
        os.getenv(str(cfg.get("uri_env", "GRAPHITI_URI"))),
        os.getenv(str(cfg.get("user_env", "GRAPHITI_USER"))),
        os.getenv(str(cfg.get("password_env", "GRAPHITI_PASSWORD"))),
    )
    synced = 0
    failed = 0
    try:
        await graph.build_indices_and_constraints()
        for row in rows:
            try:
                body = _json(
                    {
                        "memory_id": row["memory_id"],
                        "namespace": row["namespace"],
                        "memory_type": row["memory_type"],
                        "fact_type": row["fact_type"],
                        "subject": row["subject"],
                        "predicate": row["predicate"],
                        "object": row["object"],
                        "summary": row["summary"],
                        "confidence": row["confidence"],
                        "importance": row["importance"],
                        "outcome_score": row["outcome_score"],
                        "valid_from": row["valid_from"],
                        "valid_to": row["valid_to"],
                        "source": row["source"],
                    }
                )
                await graph.add_episode(
                    name=row["memory_id"],
                    episode_body=body,
                    source_description=f"Agentic trading temporal memory from {row['source']}",
                    reference_time=_parse_iso(row["valid_from"]),
                    source=EpisodeType.json,
                    group_id=f"agentic-trading:{row['namespace']}",
                    uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, row["memory_id"])),
                )
                conn.execute(
                    """
                    insert into graphiti_memory_sync(memory_id, content_hash, status, attempts, last_attempt_at, synced_at, error)
                    values (?, ?, 'synced', 1, ?, ?, null)
                    on conflict(memory_id) do update set content_hash=excluded.content_hash, status='synced',
                        attempts=graphiti_memory_sync.attempts+1, last_attempt_at=excluded.last_attempt_at,
                        synced_at=excluded.synced_at, error=null
                    """,
                    (row["memory_id"], row["content_hash"], _utc_now(), _utc_now()),
                )
                synced += 1
            except Exception as exc:
                conn.execute(
                    """
                    insert into graphiti_memory_sync(memory_id, content_hash, status, attempts, last_attempt_at, error)
                    values (?, ?, 'failed', 1, ?, ?)
                    on conflict(memory_id) do update set status='failed', attempts=graphiti_memory_sync.attempts+1,
                        last_attempt_at=excluded.last_attempt_at, error=excluded.error
                    """,
                    (row["memory_id"], row["content_hash"], _utc_now(), str(exc)[:1000]),
                )
                failed += 1
    finally:
        await graph.close()
    conn.commit()
    return {"status": "ok" if not failed else "partial", "synced": synced, "failed": failed}


def sync_graphiti_memories(conn: sqlite3.Connection, settings: dict) -> dict:
    status = graphiti_status(settings)
    if status["status"] != "ready":
        return status | {"synced": 0}
    cfg = settings.get("agent_memory", {}).get("graphiti", {})
    limit = max(1, int(cfg.get("max_episodes_per_cycle", 10)))
    minimum = float(cfg.get("minimum_importance", 0.8))
    rows = conn.execute(
        """
        select m.* from temporal_memories m
        left join graphiti_memory_sync s on s.memory_id = m.memory_id and s.content_hash = m.content_hash
        where m.status = 'active' and m.importance >= ? and (s.status is null or s.status != 'synced')
        order by m.importance desc, abs(m.outcome_score) desc, m.updated_at asc
        limit ?
        """,
        (minimum, limit),
    ).fetchall()
    if not rows:
        return status | {"synced": 0}
    try:
        result = asyncio.run(_sync_graphiti_async(conn, settings, [dict(row) for row in rows]))
    except Exception as exc:
        return status | {"status": "backend_error", "synced": 0, "error": str(exc)[:1000]}
    return status | result


def memory_system_summary(conn: sqlite3.Connection, settings: dict | None = None) -> dict:
    settings = settings or {"agent_memory": {}}
    ensure_temporal_schema(conn)
    total = int(conn.execute("select count(*) from temporal_memories").fetchone()[0])
    by_status = {row[0]: int(row[1]) for row in conn.execute("select status, count(*) from temporal_memories group by status")}
    by_namespace = {row[0]: int(row[1]) for row in conn.execute("select namespace, count(*) from temporal_memories where status in ('active','provisional') group by namespace")}
    by_type = {row[0]: int(row[1]) for row in conn.execute("select memory_type, count(*) from temporal_memories where status in ('active','provisional') group by memory_type")}
    links = int(conn.execute("select count(*) from temporal_memory_links").fetchone()[0])
    raw_rows = int(conn.execute("select count(*) from memory_facts").fetchone()[0])
    retrievals = int(conn.execute("select count(*) from memory_retrieval_events").fetchone()[0])
    validated = int(conn.execute("select count(*) from temporal_memories where last_validated_at is not null").fetchone()[0])
    positive_utility = int(conn.execute("select count(*) from temporal_memories where utility_score > 0.05").fetchone()[0])
    negative_utility = int(conn.execute("select count(*) from temporal_memories where utility_score < -0.05").fetchone()[0])
    latest_retrievals = [
        {
            "created_at": row[0], "agent_name": row[1], "selected_count": row[2],
            "namespace_counts": _decode(row[3], {}),
        }
        for row in conn.execute(
            "select created_at, agent_name, selected_count, namespace_counts_json "
            "from memory_retrieval_events order by id desc limit 12"
        )
    ]
    return {
        "status": "ok",
        "raw_legacy_rows": raw_rows,
        "temporal_memory_rows": total,
        "active_memories": by_status.get("active", 0),
        "provisional_memories": by_status.get("provisional", 0),
        "superseded_memories": by_status.get("superseded", 0),
        "memory_links": links,
        "retrieval_events": retrievals,
        "validated_memories": validated,
        "positive_utility_memories": positive_utility,
        "negative_utility_memories": negative_utility,
        "legacy_to_active_compression_ratio": round(raw_rows / max(1, by_status.get("active", 0)), 2),
        "by_namespace": by_namespace,
        "by_memory_type": by_type,
        "latest_agent_retrievals": latest_retrievals,
        "last_evidence_refresh": _get_state(conn, "last_evidence_refresh", {}),
        "last_swarm_reflection": _get_state(conn, "last_swarm_reflection", {}),
        "last_graphiti_retrieval": _get_state(conn, "last_graphiti_retrieval", {}),
        "graphiti": graphiti_status(settings),
        "retrieval_mode": settings.get("agent_memory", {}).get("retrieval_mode", "hybrid_temporal_fts_graph"),
    }


def write_memory_reports(conn: sqlite3.Connection, settings: dict | None = None) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    summary = memory_system_summary(conn, settings)
    REPORT_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    memories = [
        _row_to_memory(row)
        for row in conn.execute(
            "select * from temporal_memories where status in ('active','provisional') "
            "order by importance desc, abs(outcome_score) desc, last_seen_at desc limit 200"
        )
    ]
    with GRAPHITI_EXPORT.open("w", encoding="utf-8") as handle:
        for item in memories:
            handle.write(json.dumps(item, sort_keys=True, default=str) + "\n")
    lines = [
        "# Temporal Agent Memory",
        "",
        f"- Active memories: `{summary['active_memories']}`",
        f"- Provisional memories: `{summary['provisional_memories']}`",
        f"- Superseded temporal versions: `{summary['superseded_memories']}`",
        f"- Provenance links: `{summary['memory_links']}`",
        f"- Memories validated through downstream use: `{summary['validated_memories']}`",
        f"- Positive / negative utility memories: `{summary['positive_utility_memories']}` / `{summary['negative_utility_memories']}`",
        f"- Legacy raw rows retained for audit: `{summary['raw_legacy_rows']}`",
        f"- Legacy-to-active compression: `{summary['legacy_to_active_compression_ratio']}x`",
        f"- Retrieval mode: `{summary['retrieval_mode']}`",
        f"- Graphiti: `{summary['graphiti']}`",
        "",
        "## Active Memory By Namespace",
        "",
    ]
    for namespace, count in sorted(summary["by_namespace"].items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- `{namespace}`: `{count}`")
    lines.extend(["", "## Highest-Value Memories", ""])
    for item in memories[:50]:
        lines.append(
            f"- `{item['namespace']}` `{item['fact_type']}` `{item['subject']}`: "
            f"{item['summary'][:240]} importance={item['importance']:.2f} "
            f"outcome={item['outcome_score']:.2f} utility={item['utility_score']:.2f} "
            f"observations={item['observation_count']}"
        )
    MEMORY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
