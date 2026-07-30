"""Cross-queue semantic identity for autonomous recommendations."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any


TERMINAL_IMPLEMENTED_PREFIXES = ("implemented", "promoted", "superseded", "resolved")
REOPEN_FIELDS = (
    "strategy_version",
    "signal_version",
    "admission_stage",
    "blocker_code",
    "outcome_window_end",
    "performance_regime",
)
GENERIC_TERMS = {
    "add", "build", "create", "develop", "improve", "investigate", "llm", "paper",
    "propose", "recommendation", "research", "system", "test", "the", "this", "with",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_text(item)}" for key, item in sorted(value.items()))
    if isinstance(value, (list, tuple, set)):
        return " ".join(_text(item) for item in value)
    return str(value or "")


def _normalized(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9][a-z0-9_.:/+-]*", _text(value).lower()))


def _terms(value: Any) -> set[str]:
    return {
        token.strip("._:/+-")
        for token in _normalized(value).split()
        if len(token.strip("._:/+-")) > 2 and token.strip("._:/+-") not in GENERIC_TERMS
    }


def _first(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return _normalized(value)
    return ""


def descriptor_from_recommendation(payload: Mapping[str, Any], topic_type: str) -> dict[str, Any]:
    evidence = _json_object(payload.get("evidence"))
    code_change = _json_object(payload.get("code_change"))
    strategy_logic = _json_object(payload.get("strategy_logic"))
    identity = {
        "topic_type": _normalized(topic_type),
        "market_key": _first(payload, "market_key", "route_key", "venue", "market_surface"),
        "signal_key": _first(payload, "signal_key", "signal_family"),
        "strategy_lineage": _first(
            payload,
            "strategy_lab_id",
            "strategy_lineage",
            "variant_id",
            "strategy_version",
        ),
        "admission_stage": _first(payload, "admission_stage", "current_stage"),
        "blocker_code": _first(payload, "blocker_code", "route_blocker", "route_key"),
        "intent": _first(
            code_change,
            "expected_behavior_change",
            "change_category",
            "implementation_mode",
        ) or _first(payload, "intended_behavior", "proposed_change", "hypothesis", "recommended_next_action", "action"),
        "surface_type": _first(payload, "surface_type_classified", "trade_type", "market_surface"),
        "direction": _first(payload, "direction"),
    }
    semantic_text = " ".join(
        filter(
            None,
            (
                _first(payload, "title"),
                _first(payload, "proposed_change", "rationale", "hypothesis"),
                _normalized(strategy_logic),
                identity["intent"],
            ),
        )
    )
    evidence_markers = {
        key: _normalized(payload.get(key) or evidence.get(key))
        for key in REOPEN_FIELDS
        if payload.get(key) not in (None, "") or evidence.get(key) not in (None, "")
    }
    return {
        **identity,
        "semantic_text": semantic_text,
        "semantic_terms": sorted(_terms(semantic_text)),
        "evidence_markers": evidence_markers,
    }


def topic_key(descriptor: Mapping[str, Any]) -> str:
    stable = {
        key: descriptor.get(key) or ""
        for key in (
            "market_key", "signal_key", "strategy_lineage", "admission_stage", "blocker_code",
            "intent", "surface_type", "direction",
        )
    }
    stable["semantic_terms"] = list(descriptor.get("semantic_terms") or [])
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def evidence_digest(evidence: Any) -> str:
    raw = json.dumps(evidence or {}, sort_keys=True, default=repr, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    # Admission stage and blocker are evidence dimensions: changing them may reopen
    # the same topic, not create a separate identity.
    scoped = ("market_key", "signal_key", "strategy_lineage", "surface_type", "direction")
    comparable = [(left.get(key), right.get(key)) for key in scoped if left.get(key) and right.get(key)]
    if comparable and any(a != b for a, b in comparable):
        return 0.0
    a = set(left.get("semantic_terms") or [])
    b = set(right.get("semantic_terms") or [])
    if not a or not b:
        return 0.0
    lexical = len(a & b) / len(a | b)
    intent_bonus = 0.15 if left.get("intent") and left.get("intent") == right.get("intent") else 0.0
    scope_bonus = min(0.15, 0.05 * len(comparable))
    return min(1.0, lexical + intent_bonus + scope_bonus)


def _materially_new(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    old = _json_object(previous.get("evidence_markers"))
    new = _json_object(current.get("evidence_markers"))
    return any(new.get(key) and new.get(key) != old.get(key) for key in REOPEN_FIELDS)


@dataclasses.dataclass(frozen=True)
class TopicClaim:
    topic_key: str
    created: bool
    duplicate: bool
    reopened: bool
    canonical_table: str | None
    canonical_row_id: str | None


def claim_topic(
    conn: sqlite3.Connection,
    *,
    payload: Mapping[str, Any],
    topic_type: str,
    priority: int,
    evidence: Any = None,
    source_ref: str | None = None,
) -> TopicClaim:
    descriptor = descriptor_from_recommendation(payload, topic_type)
    exact_key = topic_key(descriptor)
    rows = conn.execute(
        """
        select topic_key, status, canonical_table, canonical_row_id, descriptor_json,
               source_refs_json, occurrence_count, reopen_count
        from recommendation_topics
        where status not like 'archived%'
        order by updated_at desc
        limit 2000
        """
    ).fetchall()
    match = None
    for row in rows:
        prior = json.loads(row["descriptor_json"] or "{}")
        if row["topic_key"] == exact_key or _similarity(prior, descriptor) >= 0.55:
            match = row
            break
    now = utc_now()
    digest = evidence_digest(evidence)
    if match is None:
        refs = [source_ref] if source_ref else []
        conn.execute(
            """
            insert into recommendation_topics (
                topic_key, created_at, updated_at, topic_type, status, priority,
                descriptor_json, evidence_digest, evidence_json, source_refs_json,
                occurrence_count, reopen_count
            ) values (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, 1, 0)
            """,
            (
                exact_key, now, now, topic_type, int(priority), json.dumps(descriptor, sort_keys=True),
                digest, json.dumps(evidence or {}, sort_keys=True, default=repr), json.dumps(refs),
            ),
        )
        if source_ref:
            conn.execute(
                "insert or ignore into recommendation_topic_sources (source_ref, topic_key, created_at) values (?, ?, ?)",
                (source_ref, exact_key, now),
            )
        return TopicClaim(exact_key, True, False, False, None, None)

    key = str(match["topic_key"])
    prior_descriptor = json.loads(match["descriptor_json"] or "{}")
    terminal = str(match["status"] or "").startswith(TERMINAL_IMPLEMENTED_PREFIXES)
    reopened = terminal and _materially_new(prior_descriptor, descriptor)
    refs = json.loads(match["source_refs_json"] or "[]")
    if source_ref and source_ref not in refs:
        refs.append(source_ref)
    status = "open" if reopened else str(match["status"])
    conn.execute(
        """
        update recommendation_topics
        set updated_at = ?, topic_type = ?, status = ?, priority = max(priority, ?),
            descriptor_json = ?, evidence_digest = ?, evidence_json = ?, source_refs_json = ?,
            occurrence_count = occurrence_count + 1,
            reopen_count = reopen_count + ?
        where topic_key = ?
        """,
        (
            now, topic_type, status, int(priority), json.dumps(descriptor, sort_keys=True), digest,
            json.dumps(evidence or {}, sort_keys=True, default=repr), json.dumps(refs),
            1 if reopened else 0, key,
        ),
    )
    if source_ref:
        conn.execute(
            "insert or ignore into recommendation_topic_sources (source_ref, topic_key, created_at) values (?, ?, ?)",
            (source_ref, key, now),
        )
    return TopicClaim(
        key, False, not reopened, reopened,
        match["canonical_table"], match["canonical_row_id"],
    )


def bind_artifact(conn: sqlite3.Connection, topic_key_value: str, table: str, row_id: Any) -> None:
    conn.execute(
        """
        update recommendation_topics
        set canonical_table = coalesce(canonical_table, ?),
            canonical_row_id = coalesce(canonical_row_id, ?), updated_at = ?
        where topic_key = ?
        """,
        (table, str(row_id), utc_now(), topic_key_value),
    )


def set_topic_status(
    conn: sqlite3.Connection,
    topic_key_value: str,
    status: str,
    *,
    implemented_category: str | None = None,
    implementation_commit: str | None = None,
) -> None:
    conn.execute(
        """
        update recommendation_topics
        set status = ?, updated_at = ?,
            implemented_category = coalesce(?, implemented_category),
            implementation_commit = coalesce(?, implementation_commit)
        where topic_key = ?
        """,
        (status, utc_now(), implemented_category, implementation_commit, topic_key_value),
    )


def registry_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        select count(*) topics,
               coalesce(sum(occurrence_count - 1), 0) duplicates_suppressed,
               coalesce(sum(reopen_count), 0) reopened,
               sum(case when canonical_row_id is not null then 1 else 0 end) bound_topics
        from recommendation_topics
        """
    ).fetchone()
    return dict(row) if row else {"topics": 0, "duplicates_suppressed": 0, "reopened": 0, "bound_topics": 0}


ARTIFACT_QUERIES = {
    "improvement_tasks": (
        "select id, priority, title, rationale, status from improvement_tasks where status = 'open' order by id",
        lambda row: {"title": row["title"], "rationale": row["rationale"], "action": "improvement_task"},
    ),
    "growth_experiments": (
        "select id, priority, signal_key, hypothesis, action, evidence_json, status from growth_experiments where status = 'open' order by id",
        lambda row: {
            "signal_key": row["signal_key"], "title": row["hypothesis"], "hypothesis": row["hypothesis"],
            "proposed_change": row["action"], "action": "growth_experiment",
            "evidence": _json_object(row["evidence_json"]),
        },
    ),
    "market_hunter_directives": (
        "select id, priority, market_key, directive, rationale, evidence_json, status from market_hunter_directives where status = 'open' order by id",
        lambda row: {
            "market_key": row["market_key"], "title": row["directive"], "rationale": row["rationale"],
            "action": "hunter_directive", "evidence": _json_object(row["evidence_json"]),
        },
    ),
    "adapter_specs": (
        "select id, priority, market_key, title, spec_json, evidence_json, status from adapter_specs where status = 'open' order by id",
        lambda row: {
            "market_key": row["market_key"], "title": row["title"], "action": "adapter_spec",
            "proposed_change": _json_object(row["spec_json"]), "evidence": _json_object(row["evidence_json"]),
        },
    ),
    "route_probe_tasks": (
        "select id, priority, market_key, route_key, probe_type, rationale, evidence_json, status from route_probe_tasks where status = 'open' order by id",
        lambda row: {
            "market_key": row["market_key"], "route_key": row["route_key"], "title": row["probe_type"],
            "rationale": row["rationale"], "action": "route_probe", "evidence": _json_object(row["evidence_json"]),
        },
    ),
}


def backfill_open_artifacts(conn: sqlite3.Connection, limit: int = 2000) -> dict[str, int]:
    """Register open legacy artifacts and supersede semantic duplicates without deletion."""
    counters = {"scanned": 0, "registered": 0, "superseded": 0, "already_registered": 0}
    for table, (query, payload_builder) in ARTIFACT_QUERIES.items():
        for row in conn.execute(query).fetchall():
            if counters["scanned"] >= limit:
                return counters
            source_ref = f"{table}:{row['id']}"
            counters["scanned"] += 1
            prior = conn.execute(
                "select topic_key from recommendation_topic_sources where source_ref = ?",
                (source_ref,),
            ).fetchone()
            if prior:
                counters["already_registered"] += 1
                continue
            payload = payload_builder(row)
            claim = claim_topic(
                conn,
                payload=payload,
                topic_type=table,
                priority=int(row["priority"] or 0),
                evidence=payload.get("evidence"),
                source_ref=source_ref,
            )
            if claim.duplicate and claim.canonical_row_id:
                conn.execute(
                    f"update {table} set status = ? where id = ? and status = 'open'",
                    (f"superseded_by_topic_{claim.topic_key[:16]}", int(row["id"])),
                )
                counters["superseded"] += 1
            else:
                bind_artifact(conn, claim.topic_key, table, row["id"])
                counters["registered"] += 1
    return counters
