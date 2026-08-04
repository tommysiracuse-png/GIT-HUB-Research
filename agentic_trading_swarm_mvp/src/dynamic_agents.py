"""Persistent, trigger-aware agents for the collaborative LLM swarm."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from typing import Any, Iterable

from storage import RUNS_DIR, link_recommendation_artifact, utc_now


REPORT_JSON = RUNS_DIR / "dynamic_agents_latest.json"
REPORT_MD = RUNS_DIR / "dynamic_agents_report.md"
DEFAULT_ALLOWED_ACTIONS = [
    "propose_build_task", "propose_growth_experiment", "propose_hunter_directive",
    "request_data_source", "request_market_adapter", "request_red_team",
    "propose_signal_variant", "propose_diagnostic_hypothesis",
    "propose_strategy_lab_experiment", "propose_code_change", "spawn_agent",
]
VALID_MODEL_TIERS = {"fast", "standard", "frontier"}
VALID_NAMESPACES = {
    "outcomes", "strategies", "markets", "routes", "code", "policies",
    "recommendations", "system",
}

BOOTSTRAP_AGENT_SPECS = [
    {
        "name": "global_market_expansion_specialist",
        "objective": (
            "Continuously turn global market discoveries and stalled admission stages into "
            "new priceable markets, complete public adapters, and paper-testable market surfaces."
        ),
        "parent_agent_id": "market_scout",
        "triggers": {
            "any_packet_paths": ["expansion_map", "frontier_crypto_venues"],
            "cooldown_minutes": 180,
        },
        "evidence_inputs": [
            "expansion_map", "frontier_crypto_venues", "improvement_tasks",
            "growth_experiments", "route_intelligence",
        ],
        "memory_policy": {
            "namespaces": ["markets", "routes", "recommendations", "outcomes"],
            "keywords": ["global discovery", "market admission", "adapter", "new venue"],
            "retrieval_limit": 36,
        },
        "model_tier": "standard",
        "allowed_actions": [
            "request_market_adapter", "propose_growth_experiment", "propose_code_change", "spawn_agent",
        ],
        "success_measure": {
            "primary": "new_markets_reaching_paper_eligible",
            "supporting": ["verified_market_coverage", "adapter_promotions", "reliable_outcomes"],
        },
        "metadata": {"bootstrap_seed": True, "bootstrap_key": "global_market_expansion", "version": 1},
    },
    {
        "name": "novel_strategy_discovery_specialist",
        "objective": (
            "Invent reusable observation-native trading hypotheses, diagnose why experiments produce no candidates, "
            "and advance genuinely novel Strategy Lab programs toward reliable paper evidence."
        ),
        "parent_agent_id": "strategy_lab",
        "triggers": {
            "any_packet_paths": ["strategy_lab", "horizon_outcomes"],
            "cooldown_minutes": 120,
        },
        "evidence_inputs": [
            "strategy_lab", "horizon_outcomes", "contextual_stats", "market_discovery",
            "signal_redesign", "current_cycle_agent_outputs",
        ],
        "memory_policy": {
            "namespaces": ["strategies", "outcomes", "markets", "recommendations"],
            "keywords": ["strategy lab", "observation program", "promotion", "reliable outcome"],
            "retrieval_limit": 40,
        },
        "model_tier": "standard",
        "allowed_actions": [
            "propose_strategy_lab_experiment", "propose_diagnostic_hypothesis", "propose_code_change", "spawn_agent",
        ],
        "success_measure": {
            "primary": "novel_strategies_with_reliable_labels",
            "supporting": ["paper_candidates_generated", "promoted_strategy_plugins", "positive_paired_uplift"],
        },
        "metadata": {"bootstrap_seed": True, "bootstrap_key": "novel_strategy_discovery", "version": 1},
    },
    {
        "name": "code_evolution_reliability_specialist",
        "objective": (
            "Diagnose recurring autonomous coding failures, improve useful promotion throughput, and propose "
            "repository-integrated repairs without weakening paper-only safety or auditability."
        ),
        "parent_agent_id": "build_planner",
        "triggers": {"any_packet_paths": ["code_evolution"], "cooldown_minutes": 60},
        "evidence_inputs": [
            "code_evolution", "self_improvement", "repository_grounding",
            "improvement_tasks", "current_cycle_critiques",
        ],
        "memory_policy": {
            "namespaces": ["code", "recommendations", "outcomes", "system"],
            "keywords": ["code evolution", "implementation paused", "test failure", "promotion"],
            "retrieval_limit": 32,
        },
        "model_tier": "fast",
        "allowed_actions": ["propose_code_change", "propose_diagnostic_hypothesis", "spawn_agent"],
        "success_measure": {
            "primary": "useful_merge_rate",
            "supporting": ["repair_success_rate", "cost_per_kept_change", "post_promotion_health"],
        },
        "metadata": {"bootstrap_seed": True, "bootstrap_key": "code_evolution_reliability", "version": 1},
    },
    {
        "name": "market_data_quality_specialist",
        "objective": (
            "Continuously identify weak, stale, malformed, or under-enriched public market data and turn "
            "repeated quality failures into adapter, parser, and admission-stage repairs."
        ),
        "parent_agent_id": "cross_market_researcher",
        "triggers": {
            "any_packet_paths": ["frontier_crypto_venues", "expansion_map"],
            "cooldown_minutes": 120,
        },
        "evidence_inputs": [
            "frontier_crypto_venues", "crypto_venue_health", "expansion_map",
            "contextual_stats", "improvement_tasks",
        ],
        "memory_policy": {
            "namespaces": ["markets", "outcomes", "system", "recommendations"],
            "keywords": ["data quality", "depth parser", "stale", "degraded", "normalization"],
            "retrieval_limit": 36,
        },
        "model_tier": "fast",
        "allowed_actions": [
            "request_market_adapter", "propose_diagnostic_hypothesis", "propose_code_change", "spawn_agent",
        ],
        "success_measure": {
            "primary": "verified_quality_coverage",
            "supporting": ["parser_recovery_rate", "new_depth_snapshots", "admission_stage_advances"],
        },
        "metadata": {"bootstrap_seed": True, "bootstrap_key": "market_data_quality", "version": 1},
    },
]


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _hash(value: Any, length: int = 24) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()[:length]


def _safe_name(value: Any) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", str(value or "dynamic_specialist").strip().lower()).strip("_")
    return (name or "dynamic_specialist")[:64]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone())


def ensure_dynamic_agent_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists agent_specs (
            agent_id text primary key,
            canonical_hash text not null unique,
            name text not null,
            objective text not null,
            primary_parent_agent_id text,
            parent_ids_json text not null default '[]',
            trigger_json text not null,
            evidence_inputs_json text not null,
            memory_policy_json text not null,
            model_tier text not null,
            allowed_actions_json text not null,
            success_measure_json text not null,
            status text not null,
            generation integer not null default 1,
            source_recommendation_id text,
            created_at text not null,
            updated_at text not null,
            activated_at text,
            activation_cycle_id text,
            last_evaluated_at text,
            last_run_at text,
            last_trigger_matched integer not null default 0,
            last_trigger_reason text,
            runs_count integer not null default 0,
            successful_runs integer not null default 0,
            total_cost_usd real not null default 0,
            merged_count integer not null default 0,
            metadata_json text not null default '{}'
        )
        """
    )
    conn.execute(
        """
        create table if not exists agent_lineage (
            parent_agent_id text not null,
            child_agent_id text not null,
            created_at text not null,
            source_recommendation_id text,
            primary key(parent_agent_id, child_agent_id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists agent_runs (
            run_id text primary key,
            agent_id text not null,
            cycle_id text not null,
            started_at text not null,
            completed_at text,
            duration_ms integer not null default 0,
            status text not null,
            trigger_match_json text not null,
            memory_ids_json text not null,
            model_json text not null,
            recommendation_json text not null,
            recommendation_id text,
            action text,
            priority integer,
            estimated_cost_usd real not null default 0,
            code_proposal_id text,
            strategy_lab_id text,
            outcome_json text not null default '{}',
            unique(agent_id, cycle_id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists agent_spawn_candidates (
            candidate_id text primary key,
            created_at text not null,
            updated_at text not null,
            objective_cluster text not null,
            parent_agent_id text not null,
            evidence_json text not null,
            proposed_spec_json text not null,
            trigger_replay_json text not null default '{}',
            overlap_score real not null default 0,
            status text not null,
            source_cycle_id text,
            resulting_agent_id text,
            source_recommendation_id text
        )
        """
    )
    columns = {row["name"] for row in conn.execute("pragma table_info(agent_specs)").fetchall()}
    for column, ddl in (
        ("last_trigger_fingerprint", "text"),
        ("code_promotions_count", "integer not null default 0"),
        ("strategy_materializations_count", "integer not null default 0"),
        ("paper_trades_count", "integer not null default 0"),
        ("reliable_outcomes_count", "integer not null default 0"),
    ):
        if column not in columns:
            conn.execute(f"alter table agent_specs add column {column} {ddl}")
    conn.execute("create index if not exists idx_agent_specs_status on agent_specs(status, last_run_at)")
    conn.execute("create index if not exists idx_agent_runs_agent_time on agent_runs(agent_id, started_at)")
    conn.execute("create index if not exists idx_agent_runs_recommendation on agent_runs(recommendation_id)")
    conn.execute("create index if not exists idx_agent_lineage_child on agent_lineage(child_agent_id)")
    conn.commit()


def _normalize_trigger(value: Any) -> dict:
    trigger = dict(value) if isinstance(value, dict) else {}
    normalized = {
        "always": bool(trigger.get("always", False)),
        "any_packet_paths": sorted({str(x).strip() for x in trigger.get("any_packet_paths", []) if str(x).strip()}),
        "all_packet_paths": sorted({str(x).strip() for x in trigger.get("all_packet_paths", []) if str(x).strip()}),
        "any_terms": sorted({str(x).strip().lower() for x in trigger.get("any_terms", []) if str(x).strip()}),
        "all_terms": sorted({str(x).strip().lower() for x in trigger.get("all_terms", []) if str(x).strip()}),
        "conditions": [x for x in trigger.get("conditions", []) if isinstance(x, dict)],
        "cooldown_minutes": max(0.0, float(trigger.get("cooldown_minutes", 0) or 0)),
    }
    if not any(normalized[k] for k in ("always", "any_packet_paths", "all_packet_paths", "any_terms", "all_terms", "conditions")):
        normalized["always"] = True
    return normalized


def _normalize_memory_policy(value: Any) -> dict:
    policy = dict(value) if isinstance(value, dict) else {}
    namespaces = [
        str(x).strip().lower() for x in policy.get("namespaces", [])
        if str(x).strip().lower() in VALID_NAMESPACES
    ]
    keywords = policy.get("keywords", [])
    if isinstance(keywords, str):
        keywords = [keywords]
    return {
        "namespaces": list(dict.fromkeys(namespaces)) or ["outcomes", "strategies", "markets", "recommendations"],
        "keywords": [str(x).strip()[:240] for x in keywords if str(x).strip()][:20],
        "retrieval_limit": max(1, min(80, int(policy.get("retrieval_limit", 24) or 24))),
    }


def normalize_agent_spec(raw: Any, *, source_agent: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("agent_spec must be an object")
    objective = str(raw.get("objective") or raw.get("role") or "").strip()
    if len(objective) < 12:
        raise ValueError("agent_spec.objective must describe a durable specialty")
    triggers = _normalize_trigger(raw.get("triggers") or raw.get("trigger"))
    evidence_inputs = raw.get("evidence_inputs") or []
    if isinstance(evidence_inputs, str):
        evidence_inputs = [evidence_inputs]
    evidence_inputs = [str(x).strip()[:180] for x in evidence_inputs if str(x).strip()][:30]
    memory_policy = _normalize_memory_policy(raw.get("memory_policy"))
    model_tier = str(raw.get("model_tier") or "fast").strip().lower()
    if model_tier not in VALID_MODEL_TIERS:
        model_tier = "fast"
    allowed = raw.get("allowed_actions") or raw.get("allowed_outputs") or ["propose_diagnostic_hypothesis"]
    if isinstance(allowed, str):
        allowed = [allowed]
    allowed_actions = [str(x) for x in allowed if str(x) in DEFAULT_ALLOWED_ACTIONS]
    if not allowed_actions:
        allowed_actions = ["propose_diagnostic_hypothesis"]
    success_measure = raw.get("success_measure") or raw.get("success_metrics") or {"accepted_recommendations": 1}
    if not isinstance(success_measure, dict):
        success_measure = {"description": str(success_measure)[:500]}
    name = _safe_name(raw.get("name") or raw.get("agent_name") or objective[:48])
    parent = str(raw.get("parent_agent_id") or source_agent or "build_planner").strip()
    canonical = {
        "objective": re.sub(r"\s+", " ", objective).strip().lower(),
        "triggers": triggers,
        "evidence_inputs": sorted(set(evidence_inputs)),
        "memory_policy": memory_policy,
        "model_tier": model_tier,
        "allowed_actions": sorted(set(allowed_actions)),
        "success_measure": success_measure,
    }
    return {
        "name": name, "objective": objective[:4000], "parent_agent_id": parent[:128],
        "triggers": triggers, "evidence_inputs": evidence_inputs,
        "memory_policy": memory_policy, "model_tier": model_tier,
        "allowed_actions": list(dict.fromkeys(allowed_actions)),
        "success_measure": success_measure, "canonical_hash": _hash(canonical, 40),
        "metadata": dict(raw.get("metadata") or {}) if isinstance(raw.get("metadata"), dict) else {},
    }


def _generation_for_parent(conn: sqlite3.Connection, parent_agent_id: str) -> int:
    row = conn.execute("select generation from agent_specs where agent_id=?", (parent_agent_id,)).fetchone()
    return int(row[0]) + 1 if row else 1


def register_agent_spec(
    conn: sqlite3.Connection,
    raw_spec: dict,
    *,
    source_recommendation_id: str | None = None,
    source_agent: str | None = None,
) -> dict:
    ensure_dynamic_agent_schema(conn)
    spec = normalize_agent_spec(raw_spec, source_agent=source_agent)
    existing = conn.execute("select * from agent_specs where canonical_hash=?", (spec["canonical_hash"],)).fetchone()
    parent = spec["parent_agent_id"]
    now = utc_now()
    if existing:
        item = dict(existing)
        parents = set(_decode(item.get("parent_ids_json"), []))
        if parent:
            parents.add(parent)
        conn.execute(
            "update agent_specs set parent_ids_json=?, merged_count=merged_count+1, updated_at=? where agent_id=?",
            (_json(sorted(parents)), now, item["agent_id"]),
        )
        if parent and parent != item["agent_id"]:
            conn.execute(
                "insert or ignore into agent_lineage(parent_agent_id, child_agent_id, created_at, source_recommendation_id) values(?,?,?,?)",
                (parent, item["agent_id"], now, source_recommendation_id),
            )
        conn.commit()
        return {"status": "merged_exact_duplicate", "agent_id": item["agent_id"], "canonical_hash": spec["canonical_hash"]}
    agent_id = f"agent_{spec['canonical_hash'][:20]}"
    generation = _generation_for_parent(conn, parent)
    conn.execute(
        """
        insert into agent_specs (
            agent_id, canonical_hash, name, objective, primary_parent_agent_id, parent_ids_json,
            trigger_json, evidence_inputs_json, memory_policy_json, model_tier,
            allowed_actions_json, success_measure_json, status, generation,
            source_recommendation_id, created_at, updated_at, metadata_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
        """,
        (
            agent_id, spec["canonical_hash"], spec["name"], spec["objective"], parent or None,
            _json([parent] if parent else []), _json(spec["triggers"]), _json(spec["evidence_inputs"]),
            _json(spec["memory_policy"]), spec["model_tier"], _json(spec["allowed_actions"]),
            _json(spec["success_measure"]), generation, source_recommendation_id, now, now,
            _json(spec["metadata"]),
        ),
    )
    if parent and parent != agent_id:
        conn.execute(
            "insert or ignore into agent_lineage(parent_agent_id, child_agent_id, created_at, source_recommendation_id) values(?,?,?,?)",
            (parent, agent_id, now, source_recommendation_id),
        )
    conn.commit()
    return {"status": "created", "agent_id": agent_id, "canonical_hash": spec["canonical_hash"], "generation": generation}


def ingest_spawn_agent_recommendation(conn: sqlite3.Connection, item: dict, *, recommendation_id: str | None = None) -> dict:
    spec = item.get("agent_spec") if isinstance(item.get("agent_spec"), dict) else {}
    source_agent = str(item.get("dynamic_agent_id") or item.get("agent_name") or "build_planner")
    recommendation_id = recommendation_id or hashlib.sha256(json.dumps(item, sort_keys=True).encode("utf-8")).hexdigest()
    result = register_agent_spec(conn, spec, source_recommendation_id=recommendation_id, source_agent=source_agent)
    candidate_id = str(
        ((item.get("evidence") or {}).get("spawn_candidate_id") if isinstance(item.get("evidence"), dict) else "")
        or ((spec.get("metadata") or {}).get("spawn_candidate_id") if isinstance(spec.get("metadata"), dict) else "")
    )
    if candidate_id:
        conn.execute(
            """
            update agent_spawn_candidates set status='spawned', resulting_agent_id=?,
                source_recommendation_id=?, updated_at=? where candidate_id=?
            """,
            (result.get("agent_id"), recommendation_id, utc_now(), candidate_id),
        )
    link_recommendation_artifact(
        conn, recommendation_id, "agent_spec", result.get("agent_id"), "spawned_as",
        {"spawn_candidate_id": candidate_id, "registration_status": result.get("status")},
    )
    conn.commit()
    return result


def bootstrap_seed_agents(conn: sqlite3.Connection, settings: dict) -> dict:
    """Register the first specialist generation through the normal persistent-agent contract."""

    cfg = settings.get("dynamic_agents", {})
    if not cfg.get("bootstrap_seed_agents", False):
        return {"status": "disabled", "configured": 0, "created": 0, "existing": 0, "superseded": 0}
    configured = cfg.get("bootstrap_agent_specs")
    raw_specs = configured if isinstance(configured, list) and configured else BOOTSTRAP_AGENT_SPECS
    result = {"status": "ok", "configured": len(raw_specs), "created": 0, "existing": 0, "superseded": 0, "agent_ids": []}
    ensure_dynamic_agent_schema(conn)
    for raw in raw_specs:
        if not isinstance(raw, dict):
            continue
        spec = normalize_agent_spec(raw, source_agent=str(raw.get("parent_agent_id") or "build_planner"))
        current = conn.execute(
            "select agent_id from agent_specs where canonical_hash=?",
            (spec["canonical_hash"],),
        ).fetchone()
        if current:
            result["existing"] += 1
            result["agent_ids"].append(str(current["agent_id"]))
            continue
        bootstrap_key = str((spec.get("metadata") or {}).get("bootstrap_key") or "")
        if bootstrap_key:
            for row in conn.execute(
                "select agent_id, metadata_json from agent_specs where name=? and status='active'",
                (spec["name"],),
            ).fetchall():
                metadata = _decode(row["metadata_json"], {})
                if metadata.get("bootstrap_seed") and str(metadata.get("bootstrap_key") or "") == bootstrap_key:
                    conn.execute(
                        "update agent_specs set status='superseded', updated_at=? where agent_id=?",
                        (utc_now(), row["agent_id"]),
                    )
                    result["superseded"] += 1
        registered = register_agent_spec(
            conn,
            raw,
            source_recommendation_id=f"bootstrap_agent:{bootstrap_key or spec['canonical_hash']}",
            source_agent=spec["parent_agent_id"],
        )
        result["created"] += int(registered.get("status") == "created")
        result["agent_ids"].append(str(registered["agent_id"]))
    conn.commit()
    return result


def _row_to_spec(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    for source, target, default in (
        ("parent_ids_json", "parent_ids", []), ("trigger_json", "triggers", {}),
        ("evidence_inputs_json", "evidence_inputs", []), ("memory_policy_json", "memory_policy", {}),
        ("allowed_actions_json", "allowed_actions", []), ("success_measure_json", "success_measure", {}),
        ("metadata_json", "metadata", {}),
    ):
        item[target] = _decode(item.pop(source, None), default)
    return item


def _packet_value(packet: dict, path: str) -> Any:
    current: Any = packet
    for part in str(path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _compare(value: Any, operator: str, expected: Any) -> bool:
    if operator == "exists": return value is not None
    if operator == "truthy": return bool(value)
    if operator == "eq": return value == expected
    if operator == "ne": return value != expected
    if operator == "contains": return str(expected).lower() in str(value).lower()
    if operator == "count_gte": return isinstance(value, (dict, list, tuple, set, str)) and len(value) >= int(expected)
    try:
        left, right = float(value), float(expected)
    except (TypeError, ValueError):
        return False
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}.get(operator, False)


def _stable_evidence(value: Any) -> Any:
    volatile = {
        "generated_at", "updated_at", "created_at", "checked_at", "heartbeat_at",
        "last_seen_at", "last_checked_at", "cycle_id", "scan_id",
    }
    if isinstance(value, dict):
        return {
            key: _stable_evidence(item)
            for key, item in sorted(value.items())
            if str(key).lower() not in volatile
        }
    if isinstance(value, list):
        return [_stable_evidence(item) for item in value]
    return value


def _term_evidence(value: Any, terms: set[str], *, path: str = "packet", limit: int = 80) -> list[dict]:
    """Return bounded packet fragments that actually caused a term trigger to match."""

    matches: list[dict] = []

    def visit(item: Any, current_path: str) -> None:
        if len(matches) >= limit:
            return
        if isinstance(item, dict):
            if current_path != path:
                compact = _json(_stable_evidence(item))
                matched = sorted(term for term in terms if term in compact.lower())
                if matched:
                    matches.append({"path": current_path, "terms": matched, "value": compact[:1200]})
                    if len(matches) >= limit:
                        return
            for key, child in sorted(item.items(), key=lambda pair: str(pair[0])):
                if str(key).lower() in {
                    "generated_at", "updated_at", "created_at", "checked_at", "heartbeat_at",
                    "last_seen_at", "last_checked_at", "cycle_id", "scan_id",
                }:
                    continue
                visit(child, f"{current_path}.{key}")
            return
        if isinstance(item, list):
            for index, child in enumerate(item[:100]):
                visit(child, f"{current_path}[{index}]")
            return
        text = str(item or "")
        lowered = text.lower()
        matched = sorted(term for term in terms if term in lowered)
        if matched:
            matches.append({"path": current_path, "terms": matched, "value": text[:500]})

    visit(value, path)
    return matches


def _trigger_fingerprint(trigger: dict, packet: dict) -> str:
    paths = list(trigger.get("any_packet_paths") or []) + list(trigger.get("all_packet_paths") or [])
    paths.extend(
        str(item.get("path") or "")
        for item in trigger.get("conditions") or []
        if isinstance(item, dict) and item.get("path")
    )
    evidence = {path: _stable_evidence(_packet_value(packet, path)) for path in sorted(set(paths))}
    if not evidence:
        terms = set(trigger.get("any_terms") or []) | set(trigger.get("all_terms") or [])
        evidence = {"term_matches": _term_evidence(packet, {str(term).lower() for term in terms})}
    return _hash(evidence, 40)


def evaluate_agent_trigger(
    spec: dict,
    packet: dict,
    now: dt.datetime | None = None,
    *,
    require_evidence_delta: bool = False,
) -> dict:
    trigger = spec.get("triggers") or {}
    now = now or dt.datetime.now(dt.timezone.utc)
    cooldown = float(trigger.get("cooldown_minutes", 0) or 0)
    if cooldown and spec.get("last_run_at"):
        try:
            previous = dt.datetime.fromisoformat(str(spec["last_run_at"]).replace("Z", "+00:00"))
            if previous.tzinfo is None: previous = previous.replace(tzinfo=dt.timezone.utc)
            remaining = cooldown - (now - previous).total_seconds() / 60.0
            if remaining > 0:
                return {"matched": False, "reason": "cooldown", "remaining_minutes": round(remaining, 2)}
        except ValueError:
            pass
    checks: list[tuple[str, bool]] = []
    any_paths, all_paths = trigger.get("any_packet_paths") or [], trigger.get("all_packet_paths") or []
    if any_paths: checks.append(("any_packet_paths", any(bool(_packet_value(packet, p)) for p in any_paths)))
    if all_paths: checks.append(("all_packet_paths", all(bool(_packet_value(packet, p)) for p in all_paths)))
    packet_text = _json(packet).lower()
    any_terms, all_terms = trigger.get("any_terms") or [], trigger.get("all_terms") or []
    if any_terms: checks.append(("any_terms", any(term in packet_text for term in any_terms)))
    if all_terms: checks.append(("all_terms", all(term in packet_text for term in all_terms)))
    for index, condition in enumerate(trigger.get("conditions") or []):
        checks.append((f"condition_{index}", _compare(
            _packet_value(packet, str(condition.get("path") or "")),
            str(condition.get("operator") or "exists"), condition.get("value"))))
    matched = bool(trigger.get("always")) or (bool(checks) and all(result for _name, result in checks))
    fingerprint = _trigger_fingerprint(trigger, packet)
    if (
        matched
        and require_evidence_delta
        and not trigger.get("always")
        and spec.get("last_run_at")
        and str(spec.get("last_trigger_fingerprint") or "") == fingerprint
    ):
        return {
            "matched": False,
            "reason": "evidence_unchanged",
            "failed_checks": [],
            "evidence_fingerprint": fingerprint,
        }
    return {
        "matched": matched, "reason": "matched" if matched else "trigger_not_matched",
        "failed_checks": [name for name, result in checks if not result],
        "evidence_fingerprint": fingerprint,
    }


def _memory_pressure_percent() -> float | None:
    try:
        import psutil  # type: ignore
        return float(psutil.virtual_memory().percent)
    except Exception:
        return None


def adaptive_concurrency(conn: sqlite3.Connection, settings: dict) -> dict:
    cfg = settings.get("dynamic_agents", {})
    base = max(1, int(cfg.get("adaptive_concurrency", 8)))
    minimum = max(1, min(base, int(cfg.get("minimum_concurrency", 1))))
    current, reasons = base, []
    rows = conn.execute("select status, duration_ms, model_json from agent_runs order by started_at desc limit 32").fetchall()
    status_text = " ".join(str(r["status"] or "") + " " + str(r["model_json"] or "") for r in rows).lower()
    if sum(status_text.count(token) for token in ("quota", "rate_limit", "429")) >= 2:
        current, reasons = max(minimum, current // 2), ["recent_model_quota_pressure"]
    durations = [int(r["duration_ms"] or 0) for r in rows if int(r["duration_ms"] or 0) > 0]
    if durations and sum(durations) / len(durations) > float(cfg.get("latency_backoff_seconds", 120)) * 1000:
        current, reasons = max(minimum, current - 2), [*reasons, "recent_agent_latency"]
    if "database_busy" in status_text:
        current, reasons = max(minimum, current - 2), [*reasons, "recent_database_pressure"]
    memory_percent = _memory_pressure_percent()
    if memory_percent is not None and memory_percent >= float(cfg.get("memory_backoff_percent", 85)):
        current, reasons = max(minimum, current // 2), [*reasons, "host_memory_pressure"]
    return {"configured": base, "effective": current, "reasons": reasons or ["normal"], "memory_percent": memory_percent}


def runtime_agent(spec: dict) -> dict:
    return {
        "name": f"dynamic__{spec['agent_id']}", "display_name": spec["name"], "role": spec["objective"],
        "default_action": spec["allowed_actions"][0], "base_tier": spec["model_tier"],
        "standard_escalation_reason": "Persistent specialist evidence justifies standard reasoning.",
        "frontier_escalation_reason": "Persistent specialist objective requires frontier reasoning.",
        "dynamic_agent_id": spec["agent_id"], "parent_ids": spec["parent_ids"],
        "generation": spec["generation"], "allowed_actions": spec["allowed_actions"],
        "memory_policy": spec["memory_policy"], "evidence_inputs": spec["evidence_inputs"],
        "success_measure": spec["success_measure"],
    }


def _objective_tokens(text: Any) -> set[str]:
    stop = {"agent", "specialist", "continuously", "paper", "system", "current", "using", "into"}
    return {
        token for token in re.findall(r"[a-z][a-z0-9_]{2,}", str(text or "").lower())
        if token not in stop
    }


def _objective_overlap(left: Any, right: Any) -> float:
    a, b = _objective_tokens(left), _objective_tokens(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def _recommendation_gap_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Discover recurring work clusters without assuming a fixed market or asset class."""

    if not _table_exists(conn, "llm_recommendations"):
        return []
    rows = conn.execute(
        """
        select action,title,rationale,payload_json,status from llm_recommendations
        where status in (
            'accepted','owner_queued','linked_existing_task','implementation_paused',
            'patch_generation_unavailable_retry_later','blocked_model_quota'
        )
        order by created_at desc limit 1000
        """
    ).fetchall()
    groups: dict[str, dict] = {}
    for row in rows:
        action = str(row["action"] or "propose_diagnostic_hypothesis")
        payload = _decode(row["payload_json"], {})
        market_key = str(payload.get("market_key") or payload.get("signal_key") or "").strip().lower()
        subject_tokens = sorted(_objective_tokens(" ".join((str(row["title"] or ""), str(row["rationale"] or "")))))
        subject = market_key or "_".join(subject_tokens[:5]) or "unclassified"
        cluster_key = f"recommendation:{action}:{subject}"[:300]
        group = groups.setdefault(cluster_key, {
            "action": action, "subject": subject, "count": 0, "titles": [], "statuses": set(),
        })
        group["count"] += 1
        group["statuses"].add(str(row["status"] or ""))
        if row["title"] and str(row["title"]) not in group["titles"]:
            group["titles"].append(str(row["title"])[:240])

    result = []
    for cluster_key, group in groups.items():
        if group["count"] < 2:
            continue
        action = group["action"]
        if action == "propose_strategy_lab_experiment":
            parent, paths = "strategy_lab", ["strategy_lab", "horizon_outcomes"]
            allowed = ["propose_strategy_lab_experiment", "propose_diagnostic_hypothesis", "propose_code_change", "spawn_agent"]
        elif action in {"request_market_adapter", "request_data_source", "propose_growth_experiment"}:
            parent, paths = "market_scout", ["expansion_map", "market_discovery"]
            allowed = ["request_market_adapter", "request_data_source", "propose_growth_experiment", "propose_code_change", "spawn_agent"]
        elif action == "propose_code_change":
            parent, paths = "build_planner", ["code_evolution", "self_improvement"]
            allowed = ["propose_code_change", "propose_diagnostic_hypothesis", "spawn_agent"]
        else:
            parent, paths = "red_team", ["self_improvement", "contextual_stats"]
            allowed = [action] if action in DEFAULT_ALLOWED_ACTIONS else ["propose_diagnostic_hypothesis"]
            allowed.extend(item for item in ("propose_code_change", "spawn_agent") if item not in allowed)
        examples = "; ".join(group["titles"][:3])
        result.append({
            "cluster": "recurring_" + _hash(cluster_key, 16),
            "count": int(group["count"]),
            "priority": min(94, 72 + int(group["count"])),
            "parent_agent_id": parent,
            "name": _safe_name(f"{action}_{group['subject']}_specialist"),
            "objective": (
                f"Own the recurring {action} work for {group['subject']}; consolidate its evidence, "
                f"remove repeated dead ends, and advance it to measurable runtime artifacts. "
                f"Representative requests: {examples}"
            )[:4000],
            "triggers": {"any_packet_paths": paths, "cooldown_minutes": 60},
            "evidence_inputs": list(dict.fromkeys([*paths, "improvement_tasks", "current_cycle_agent_outputs"])),
            "allowed_actions": list(dict.fromkeys(allowed)),
            "success_measure": {"primary": "recurring_recommendations_advanced_to_measurable_artifacts"},
        })
    return result


def _spawn_gap_candidates(conn: sqlite3.Connection) -> list[dict]:
    gaps: list[dict] = []
    if _table_exists(conn, "strategy_lab_experiments"):
        count = int(conn.execute(
            """
            select count(*) from strategy_lab_experiments
            where status in (
                'rejected_invalid','quarantined_surface_policy','needs_contract_revision','needs_data'
            )
            """
        ).fetchone()[0])
        if count:
            gaps.append({
                "cluster": "strategy_contract_feasibility",
                "count": count,
                "priority": 96,
                "parent_agent_id": "strategy_lab",
                "name": "strategy_contract_feasibility_specialist",
                "objective": (
                    "Continuously diagnose impossible, quarantined, or data-starved Strategy Lab contracts; "
                    "calibrate them against observed feature distributions and advance feasible paper experiments."
                ),
                "triggers": {"any_packet_paths": ["strategy_lab"], "cooldown_minutes": 60},
                "evidence_inputs": ["strategy_lab", "horizon_outcomes", "contextual_stats", "current_cycle_agent_outputs"],
                "allowed_actions": ["propose_strategy_lab_experiment", "propose_diagnostic_hypothesis", "propose_code_change", "spawn_agent"],
                "success_measure": {"primary": "starved_strategies_reaching_paper_candidates"},
            })
    if _table_exists(conn, "code_evolution_proposals"):
        count = int(conn.execute(
            """
            select count(*) from code_evolution_proposals
            where status in (
                'implementation_paused','discarded_test_failure','discarded_patch_apply_failure',
                'invalid_patch_format','no_changed_files'
            )
            """
        ).fetchone()[0])
        if count:
            gaps.append({
                "cluster": "code_promotion_recovery",
                "count": count,
                "priority": 88,
                "parent_agent_id": "build_planner",
                "name": "code_promotion_recovery_specialist",
                "objective": "Recover recurring autonomous coding failures by tracing paused proposals through repository-aware implementation and promotion.",
                "triggers": {"any_packet_paths": ["code_evolution"], "cooldown_minutes": 60},
                "evidence_inputs": ["code_evolution", "repository_grounding", "self_improvement"],
                "allowed_actions": ["propose_code_change", "propose_diagnostic_hypothesis", "spawn_agent"],
                "success_measure": {"primary": "paused_proposals_recovered_to_promoted_commits"},
            })
    if _table_exists(conn, "market_admission_states"):
        count = int(conn.execute(
            "select count(*) from market_admission_states where current_stage not in ('paper_eligible','paper_evaluated')"
        ).fetchone()[0])
        if count:
            gaps.append({
                "cluster": "market_admission_advancement",
                "count": count,
                "priority": 84,
                "parent_agent_id": "market_scout",
                "name": "market_admission_advancement_specialist",
                "objective": "Advance globally discovered markets from reachable public data through normalization, quality verification, strategy candidates, and paper eligibility.",
                "triggers": {"any_packet_paths": ["expansion_map"], "cooldown_minutes": 90},
                "evidence_inputs": ["expansion_map", "market_discovery", "route_intelligence", "improvement_tasks"],
                "allowed_actions": ["request_market_adapter", "propose_growth_experiment", "propose_code_change", "spawn_agent"],
                "success_measure": {"primary": "markets_advancing_to_paper_eligible"},
            })
    gaps.extend(_recommendation_gap_candidates(conn))
    return sorted(gaps, key=lambda item: (item["priority"], item["count"]), reverse=True)


def prepare_agent_architect(
    conn: sqlite3.Connection,
    packet: dict,
    settings: dict,
    cycle_id: str,
) -> dict:
    cfg = settings.get("dynamic_agents", {})
    if not cfg.get("agent_architect_enabled", True):
        return {"status": "disabled"}
    minimum = int(cfg.get("spawn_cluster_min_count", 3))
    overlap_limit = float(cfg.get("spawn_objective_overlap_threshold", 0.82))
    existing = [dict(row) for row in conn.execute("select agent_id, objective from agent_specs where status='active'")]
    now = utc_now()
    observed = []
    for gap in _spawn_gap_candidates(conn):
        if int(gap["count"]) < minimum:
            continue
        overlap = max((_objective_overlap(gap["objective"], row["objective"]) for row in existing), default=0.0)
        candidate_id = "spawn_" + _hash({"cluster": gap["cluster"], "parent": gap["parent_agent_id"]}, 24)
        previous = conn.execute(
            "select evidence_json, status from agent_spawn_candidates where candidate_id=?",
            (candidate_id,),
        ).fetchone()
        evidence = _decode(previous["evidence_json"], {}) if previous else {}
        cycles = list(evidence.get("observed_cycle_ids") or [])
        if cycle_id not in cycles:
            cycles.append(cycle_id)
        cycles = cycles[-12:]
        spec = {
            "name": gap["name"], "objective": gap["objective"],
            "parent_agent_id": gap["parent_agent_id"], "triggers": gap["triggers"],
            "evidence_inputs": gap["evidence_inputs"],
            "memory_policy": {
                "namespaces": ["strategies", "outcomes", "recommendations", "code"],
                "keywords": sorted(_objective_tokens(gap["objective"]))[:16],
                "retrieval_limit": 48,
            },
            "model_tier": "standard", "allowed_actions": gap["allowed_actions"],
            "success_measure": gap["success_measure"],
            "metadata": {"spawn_candidate_id": candidate_id, "objective_cluster": gap["cluster"]},
        }
        ready = len(cycles) >= 2 and overlap < overlap_limit
        status = "ready" if ready else "duplicate_covered" if overlap >= overlap_limit else "observing"
        evidence.update({
            "count": gap["count"], "priority": gap["priority"],
            "observed_cycle_ids": cycles, "packet_paths_present": [
                path for path in gap["triggers"].get("any_packet_paths", []) if _packet_value(packet, path) is not None
            ],
        })
        conn.execute(
            """
            insert into agent_spawn_candidates(
                candidate_id,created_at,updated_at,objective_cluster,parent_agent_id,
                evidence_json,proposed_spec_json,trigger_replay_json,overlap_score,status,source_cycle_id
            ) values(?,?,?,?,?,?,?,?,?,?,?)
            on conflict(candidate_id) do update set
                updated_at=excluded.updated_at,evidence_json=excluded.evidence_json,
                proposed_spec_json=excluded.proposed_spec_json,
                trigger_replay_json=excluded.trigger_replay_json,
                overlap_score=excluded.overlap_score,status=case
                    when agent_spawn_candidates.status='spawned' then 'spawned' else excluded.status end,
                source_cycle_id=excluded.source_cycle_id
            """,
            (
                candidate_id, now, now, gap["cluster"], gap["parent_agent_id"],
                _json(evidence), _json(spec), _json({"matched_cycles": len(cycles), "required": 2}),
                overlap, status, cycle_id,
            ),
        )
        observed.append({"candidate_id": candidate_id, "status": status, "overlap_score": round(overlap, 3), **gap})
    conn.commit()
    ready_rows = conn.execute(
        "select * from agent_spawn_candidates where status='ready' order by updated_at asc limit 1"
    ).fetchone()
    ready_candidate = dict(ready_rows) if ready_rows else None
    if ready_candidate:
        ready_candidate["evidence"] = _decode(ready_candidate.pop("evidence_json"), {})
        ready_candidate["proposed_spec"] = _decode(ready_candidate.pop("proposed_spec_json"), {})
        ready_candidate["trigger_replay"] = _decode(ready_candidate.pop("trigger_replay_json"), {})
    return {"status": "ready", "observed": observed, "spawn_candidate": ready_candidate}


def architect_recommendation(dynamic_cycle: dict) -> dict | None:
    candidate = (dynamic_cycle.get("agent_architect") or {}).get("spawn_candidate")
    if not isinstance(candidate, dict):
        return None
    evidence = dict(candidate.get("evidence") or {})
    evidence["spawn_candidate_id"] = candidate["candidate_id"]
    return {
        "action": "spawn_agent",
        "priority": int(evidence.get("priority") or 80),
        "title": f"Create {candidate['objective_cluster']} specialist",
        "rationale": (
            f"The unresolved {candidate['objective_cluster']} cluster persisted across "
            f"{len(evidence.get('observed_cycle_ids') or [])} cycles and is not covered by an equivalent agent."
        ),
        "market_key": candidate["objective_cluster"],
        "evidence": evidence,
        "proposed_change": "Register the persistent specialist for the next evolution cycle.",
        "agent_spec": candidate["proposed_spec"],
        "agent_name": "agent_architect",
        "parse_status": "deterministic_valid",
        "model": {"status": "deterministic", "tier": "none", "estimated_cost_usd": 0.0},
    }


def prepare_dynamic_agent_cycle(conn: sqlite3.Connection, packet: dict, settings: dict, cycle_id: str) -> dict:
    ensure_dynamic_agent_schema(conn)
    if not settings.get("dynamic_agents", {}).get("enabled", True):
        return {"status": "disabled", "matched_agents": [], "evaluated": [], "concurrency": {"configured": 0, "effective": 0}}
    bootstrap = bootstrap_seed_agents(conn, settings)
    architect = prepare_agent_architect(conn, packet, settings, cycle_id)
    rows = conn.execute("select * from agent_specs where status='active' order by generation, created_at").fetchall()
    matched, evaluated = [], []
    now = utc_now()
    for row in rows:
        spec = _row_to_spec(row)
        trigger = evaluate_agent_trigger(
            spec,
            packet,
            require_evidence_delta=bool(settings.get("dynamic_agents", {}).get("evidence_delta_triggers", True)),
        )
        conn.execute(
            """update agent_specs set last_evaluated_at=?, last_trigger_matched=?, last_trigger_reason=?,
            last_trigger_fingerprint=case when ? then ? else last_trigger_fingerprint end,
            activated_at=coalesce(activated_at, ?), activation_cycle_id=coalesce(activation_cycle_id, ?)
            where agent_id=?""",
            (
                now, int(trigger["matched"]), trigger["reason"], int(trigger["matched"]),
                trigger.get("evidence_fingerprint"), now, cycle_id, spec["agent_id"],
            ),
        )
        evaluated.append({"agent_id": spec["agent_id"], "name": spec["name"], **trigger})
        if trigger["matched"]:
            agent = runtime_agent(spec)
            agent["trigger_match"] = trigger
            matched.append(agent)
    conn.commit()
    return {
        "status": "ready", "matched_agents": matched, "evaluated": evaluated,
        "active_count": len(rows), "matched_count": len(matched), "dormant_count": len(rows) - len(matched),
        "concurrency": adaptive_concurrency(conn, settings), "bootstrap": bootstrap,
        "agent_architect": architect,
    }


def build_dynamic_memory_contexts(
    conn: sqlite3.Connection, packet: dict, agents: Iterable[dict], settings: dict, cycle_id: str,
) -> dict[str, list[dict]]:
    from temporal_memory import retrieve_role_memories
    return {
        agent["name"]: retrieve_role_memories(
            conn, packet, agent["name"], settings, cycle_id=cycle_id,
            policy_override=agent.get("memory_policy"),
        )
        for agent in agents
    }


def dynamic_run_id(agent_id: str, cycle_id: str) -> str:
    return f"agent_run_{_hash({'agent_id': agent_id, 'cycle_id': cycle_id}, 24)}"


def decorate_dynamic_recommendation(agent: dict, rec: dict, cycle_id: str) -> dict:
    if not agent.get("dynamic_agent_id"):
        return rec
    rec["dynamic_agent_id"] = agent["dynamic_agent_id"]
    rec["dynamic_agent_run_id"] = dynamic_run_id(agent["dynamic_agent_id"], cycle_id)
    rec["agent_lineage"] = {"parent_agent_ids": list(agent.get("parent_ids") or []), "generation": int(agent.get("generation") or 1)}
    rec.setdefault("evidence", {})["dynamic_agent_objective"] = agent.get("role")
    return rec


def _recommendation_id(rec: dict) -> str:
    return hashlib.sha256(json.dumps(rec, sort_keys=True).encode("utf-8")).hexdigest()


def record_dynamic_agent_runs(conn: sqlite3.Connection, state: dict, cycle: dict, cycle_id: str) -> dict:
    ensure_dynamic_agent_schema(conn)
    agents = {a["dynamic_agent_id"]: a for a in cycle.get("matched_agents", [])}
    trigger_by_id = {r["agent_id"]: r for r in cycle.get("evaluated", [])}
    recorded = 0
    for output in state.get("agent_outputs", []) or []:
        if not isinstance(output, dict): continue
        rec = output.get("recommendation") if isinstance(output.get("recommendation"), dict) else {}
        agent_id = str(rec.get("dynamic_agent_id") or "")
        if not agent_id or agent_id not in agents: continue
        model = output.get("model") if isinstance(output.get("model"), dict) else {}
        trace = next((r for r in state.get("graph_trace", []) or [] if isinstance(r, dict) and r.get("dynamic_agent_id") == agent_id), {})
        elapsed = int(trace.get("elapsed_ms") or 0)
        completed = dt.datetime.now(dt.timezone.utc)
        started = completed - dt.timedelta(milliseconds=elapsed)
        accepted, model_status = bool(output.get("accepted")), str(model.get("status") or "")
        status = "recommended" if accepted else "rejected"
        if any(t in model_status.lower() for t in ("quota", "429", "rate_limit")): status = "model_unavailable"
        recommendation_id = _recommendation_id(rec) if rec else None
        try: priority = max(1, min(100, int(float(rec.get("priority")))))
        except (TypeError, ValueError): priority = None
        previous = conn.execute(
            "select status, estimated_cost_usd from agent_runs where agent_id=? and cycle_id=?",
            (agent_id, cycle_id),
        ).fetchone()
        conn.execute(
            """insert into agent_runs (
            run_id, agent_id, cycle_id, started_at, completed_at, duration_ms, status,
            trigger_match_json, memory_ids_json, model_json, recommendation_json,
            recommendation_id, action, priority, estimated_cost_usd
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(agent_id, cycle_id) do update set
            completed_at=excluded.completed_at, duration_ms=excluded.duration_ms, status=excluded.status,
            memory_ids_json=excluded.memory_ids_json, model_json=excluded.model_json,
            recommendation_json=excluded.recommendation_json, recommendation_id=excluded.recommendation_id,
            action=excluded.action, priority=excluded.priority, estimated_cost_usd=excluded.estimated_cost_usd""",
            (dynamic_run_id(agent_id, cycle_id), agent_id, cycle_id, started.isoformat(), completed.isoformat(),
             elapsed, status, _json(trigger_by_id.get(agent_id, {})), _json(output.get("memory_ids") or []),
             _json(model), _json(rec), recommendation_id, rec.get("action"), priority,
             float(model.get("estimated_cost_usd") or 0.0)),
        )
        conn.execute(
            """update agent_specs set last_run_at=?, runs_count=runs_count+?,
            successful_runs=successful_runs+?, total_cost_usd=total_cost_usd+?, updated_at=? where agent_id=?""",
            (
                completed.isoformat(),
                0 if previous else 1,
                int(accepted) - int(bool(previous and previous["status"] == "recommended")),
                float(model.get("estimated_cost_usd") or 0.0) - float(previous["estimated_cost_usd"] or 0.0) if previous else float(model.get("estimated_cost_usd") or 0.0),
                completed.isoformat(),
                agent_id,
            ),
        )
        conn.execute(
            """insert or ignore into temporal_memory_links(
            source_type, source_id, relation, target_type, target_id, first_seen_at,
            last_seen_at, confidence, evidence_json
            ) values('agent_spec', ?, 'executed_as', 'agent_run', ?, ?, ?, 1.0, ?)""",
            (agent_id, dynamic_run_id(agent_id, cycle_id), completed.isoformat(), completed.isoformat(), _json({"cycle_id": cycle_id})),
        )
        recorded += 1
    for output in state.get("agent_outputs", []) or []:
        if not isinstance(output, dict):
            continue
        rec = output.get("recommendation") if isinstance(output.get("recommendation"), dict) else {}
        upstream_runs = (rec.get("evidence") or {}).get("upstream_dynamic_agent_runs", []) if isinstance(rec.get("evidence"), dict) else []
        if not upstream_runs:
            continue
        downstream_id = _recommendation_id(rec)
        for run_id in upstream_runs:
            row = conn.execute("select outcome_json from agent_runs where run_id=?", (str(run_id),)).fetchone()
            if not row:
                continue
            outcome = _decode(row[0], {})
            ids = list(outcome.get("downstream_recommendation_ids") or [])
            if downstream_id not in ids:
                ids.append(downstream_id)
            outcome["downstream_recommendation_ids"] = ids
            conn.execute("update agent_runs set outcome_json=? where run_id=?", (_json(outcome), str(run_id)))
    conn.commit()
    return {"recorded": recorded, "matched": len(agents)}


def _artifact_links(conn: sqlite3.Connection, run: dict) -> dict:
    recommendation_id, links = run.get("recommendation_id"), {}
    recommendation_ids = [recommendation_id] if recommendation_id else []
    recommendation_ids.extend(
        str(item) for item in (run.get("outcome") or {}).get("downstream_recommendation_ids", []) if str(item)
    )
    recommendation_ids = list(dict.fromkeys(recommendation_ids))
    if not recommendation_ids: return links
    placeholders = ",".join("?" for _ in recommendation_ids)
    if _table_exists(conn, "recommendation_artifact_links"):
        rows = conn.execute(
            f"""select recommendation_id, artifact_type, artifact_id, relationship,
                       metadata_json, updated_at
                from recommendation_artifact_links
                where recommendation_id in ({placeholders})
                order by updated_at desc""",
            recommendation_ids,
        ).fetchall()
        if rows:
            links["artifact_lineage"] = [
                {**dict(row), "metadata": _decode(row["metadata_json"], {})}
                for row in rows
            ]
            task_ids = [row["artifact_id"] for row in rows if row["artifact_type"] == "strategy_owner_task"]
            if task_ids and _table_exists(conn, "strategy_owner_tasks"):
                task_placeholders = ",".join("?" for _ in task_ids)
                task_rows = conn.execute(
                    f"select task_id,status,strategy_lab_id,code_proposal_id from strategy_owner_tasks where task_id in ({task_placeholders})",
                    task_ids,
                ).fetchall()
                links["strategy_owner_tasks"] = [dict(row) for row in task_rows]
    if _table_exists(conn, "llm_recommendations"):
        rows = conn.execute(
            f"select recommendation_id, status from llm_recommendations where recommendation_id in ({placeholders})",
            recommendation_ids,
        ).fetchall()
        if rows: links["recommendations"] = [dict(row) for row in rows]
    if _table_exists(conn, "code_evolution_proposals"):
        rows = conn.execute(
            f"select proposal_id, status, candidate_commit from code_evolution_proposals where source_recommendation_id in ({placeholders}) order by created_at desc",
            recommendation_ids,
        ).fetchall()
        if rows: links["code_proposals"] = [dict(r) for r in rows]
    if _table_exists(conn, "strategy_lab_experiments"):
        rows = conn.execute(
            f"select strategy_lab_id, status, evaluation_json from strategy_lab_experiments where source_recommendation_id in ({placeholders}) order by created_at desc",
            recommendation_ids,
        ).fetchall()
        strategies = []
        for row in rows:
            item = dict(row)
            item["evaluation"] = _decode(item.pop("evaluation_json", None), {})
            if _table_exists(conn, "paper_trades"):
                metrics = conn.execute(
                    """select count(*) as trades, sum(case when status='closed' then 1 else 0 end) as closed,
                    avg(case when status='closed' then pnl_bps end) as avg_pnl_bps
                    from paper_trades where strategy_lab_id=?""", (item["strategy_lab_id"],),
                ).fetchone()
                item["paper_outcomes"] = dict(metrics) if metrics else {}
            strategies.append(item)
        if strategies: links["strategy_experiments"] = strategies
    return links


def reconcile_dynamic_agent_artifacts(conn: sqlite3.Connection) -> int:
    """Attach downstream artifacts after inbox ingestion has materialized them."""
    updated = 0
    rows = conn.execute(
        "select run_id, recommendation_id, outcome_json from agent_runs where recommendation_id is not null order by started_at desc limit 500"
    ).fetchall()
    for row in rows:
        recommendation_ids = [str(row["recommendation_id"])]
        recommendation_ids.extend(
            str(item) for item in _decode(row["outcome_json"], {}).get("downstream_recommendation_ids", []) if str(item)
        )
        recommendation_ids = list(dict.fromkeys(recommendation_ids))
        placeholders = ",".join("?" for _ in recommendation_ids)
        code_proposal_id = None
        strategy_lab_id = None
        if _table_exists(conn, "code_evolution_proposals"):
            match = conn.execute(
                f"select proposal_id from code_evolution_proposals where source_recommendation_id in ({placeholders}) order by created_at desc limit 1",
                recommendation_ids,
            ).fetchone()
            code_proposal_id = match[0] if match else None
        if _table_exists(conn, "strategy_lab_experiments"):
            match = conn.execute(
                f"select strategy_lab_id from strategy_lab_experiments where source_recommendation_id in ({placeholders}) order by created_at desc limit 1",
                recommendation_ids,
            ).fetchone()
            strategy_lab_id = match[0] if match else None
        if _table_exists(conn, "recommendation_artifact_links"):
            linked = conn.execute(
                f"""select artifact_type, artifact_id from recommendation_artifact_links
                    where recommendation_id in ({placeholders})
                    order by updated_at desc""",
                recommendation_ids,
            ).fetchall()
            if not code_proposal_id:
                code_proposal_id = next(
                    (row["artifact_id"] for row in linked if row["artifact_type"] == "code_evolution_proposal"),
                    None,
                )
            if not strategy_lab_id:
                strategy_lab_id = next(
                    (row["artifact_id"] for row in linked if row["artifact_type"] == "strategy_lab_experiment"),
                    None,
                )
        result = conn.execute(
            "update agent_runs set code_proposal_id=?, strategy_lab_id=? where run_id=?",
            (code_proposal_id, strategy_lab_id, row["run_id"]),
        )
        updated += int(result.rowcount or 0)
    conn.commit()
    for agent in conn.execute("select agent_id from agent_specs").fetchall():
        agent_id = str(agent["agent_id"])
        promoted = int(conn.execute(
            """select count(distinct p.proposal_id) from agent_runs r
               join code_evolution_proposals p on p.proposal_id=r.code_proposal_id
               where r.agent_id=? and p.status in ('promoted','kept','workspace_kept','candidate_committed')""",
            (agent_id,),
        ).fetchone()[0]) if _table_exists(conn, "code_evolution_proposals") else 0
        materialized = int(conn.execute(
            "select count(distinct strategy_lab_id) from agent_runs where agent_id=? and strategy_lab_id is not null",
            (agent_id,),
        ).fetchone()[0])
        trades = int(conn.execute(
            """select count(*) from paper_trades where strategy_lab_id in
               (select strategy_lab_id from agent_runs where agent_id=? and strategy_lab_id is not null)""",
            (agent_id,),
        ).fetchone()[0]) if _table_exists(conn, "paper_trades") else 0
        outcomes = int(conn.execute(
            """select count(*) from paper_trade_outcomes o join paper_trades p on p.id=o.trade_id
               where o.measurement_status='valid' and p.strategy_lab_id in
               (select strategy_lab_id from agent_runs where agent_id=? and strategy_lab_id is not null)""",
            (agent_id,),
        ).fetchone()[0]) if _table_exists(conn, "paper_trade_outcomes") else 0
        conn.execute(
            """update agent_specs set code_promotions_count=?, strategy_materializations_count=?,
               paper_trades_count=?, reliable_outcomes_count=? where agent_id=?""",
            (promoted, materialized, trades, outcomes, agent_id),
        )
    conn.commit()
    return updated


def dynamic_agent_summary(conn: sqlite3.Connection, limit: int = 100) -> dict:
    ensure_dynamic_agent_schema(conn)
    reconciled = reconcile_dynamic_agent_artifacts(conn)
    counts = {str(r[0]): int(r[1]) for r in conn.execute("select status, count(*) from agent_specs group by status").fetchall()}
    specs = [_row_to_spec(r) for r in conn.execute(
        "select * from agent_specs order by last_trigger_matched desc, generation, created_at limit ?", (limit,),
    ).fetchall()]
    latest_runs = []
    for row in conn.execute("select * from agent_runs order by started_at desc limit ?", (limit,)).fetchall():
        item = dict(row)
        for source, target, default in (
            ("trigger_match_json", "trigger_match", {}), ("memory_ids_json", "memory_ids", []),
            ("model_json", "model", {}), ("recommendation_json", "recommendation", {}),
            ("outcome_json", "outcome", {}),
        ):
            item[target] = _decode(item.pop(source, None), default)
        item["downstream"] = _artifact_links(conn, item)
        latest_runs.append(item)
    lineage = [dict(r) for r in conn.execute(
        "select parent_agent_id, child_agent_id, created_at, source_recommendation_id from agent_lineage order by created_at desc limit ?",
        (limit * 2,),
    ).fetchall()]
    total_cost = float(conn.execute("select coalesce(sum(estimated_cost_usd),0) from agent_runs").fetchone()[0])
    persistent_count = sum(counts.values())
    currently_triggered = int(conn.execute(
        "select count(*) from agent_specs where status='active' and last_trigger_matched=1"
    ).fetchone()[0])
    dormant_count = int(conn.execute(
        "select count(*) from agent_specs where status='active' and last_trigger_matched=0"
    ).fetchone()[0])
    spawn_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute("select status,count(*) from agent_spawn_candidates group by status").fetchall()
    }
    spawn_candidates = []
    for row in conn.execute(
        "select * from agent_spawn_candidates order by updated_at desc limit ?", (limit,)
    ).fetchall():
        item = dict(row)
        item["evidence"] = _decode(item.pop("evidence_json", None), {})
        item["proposed_spec"] = _decode(item.pop("proposed_spec_json", None), {})
        item["trigger_replay"] = _decode(item.pop("trigger_replay_json", None), {})
        spawn_candidates.append(item)
    outcomes = conn.execute(
        """select coalesce(sum(code_promotions_count),0),
                  coalesce(sum(strategy_materializations_count),0),
                  coalesce(sum(paper_trades_count),0),
                  coalesce(sum(reliable_outcomes_count),0)
           from agent_specs"""
    ).fetchone()
    return {
        "generated_at": utc_now(), "counts_by_status": counts, "persistent_agents": persistent_count,
        "currently_triggered": currently_triggered,
        "dormant": dormant_count,
        "total_runs": int(conn.execute("select count(*) from agent_runs").fetchone()[0]),
        "total_estimated_cost_usd": round(total_cost, 6), "agents": specs,
        "lineage": lineage, "latest_runs": latest_runs, "artifact_links_reconciled": reconciled,
        "spawn_candidates_by_status": spawn_counts,
        "spawn_candidates": spawn_candidates,
        "outcome_totals": {
            "code_promotions": int(outcomes[0] or 0),
            "strategy_materializations": int(outcomes[1] or 0),
            "paper_trades": int(outcomes[2] or 0),
            "reliable_outcomes": int(outcomes[3] or 0),
        },
    }


def write_dynamic_agent_reports(conn: sqlite3.Connection, settings: dict | None = None) -> dict:
    summary = dynamic_agent_summary(conn, limit=int((settings or {}).get("dynamic_agents", {}).get("report_limit", 100)))
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    lines = [
        "# Dynamic Agents", "", f"Generated: `{summary['generated_at']}`",
        f"- Persistent agents: `{summary['persistent_agents']}`",
        f"- Triggered on latest evaluation: `{summary['currently_triggered']}`",
        f"- Dormant on latest evaluation: `{summary['dormant']}`",
        f"- Recorded runs: `{summary['total_runs']}`",
        f"- Estimated model cost: `${summary['total_estimated_cost_usd']:.4f}`", "", "## Agents",
    ]
    for spec in summary["agents"][:50]:
        state = "active" if spec.get("last_trigger_matched") else "dormant"
        lines.append(
            f"- `{spec['agent_id']}` {spec['name']} generation={spec['generation']} state={state} "
            f"runs={spec['runs_count']} cost=${float(spec['total_cost_usd'] or 0):.4f} "
            f"code={int(spec.get('code_promotions_count') or 0)} strategies={int(spec.get('strategy_materializations_count') or 0)} "
            f"trades={int(spec.get('paper_trades_count') or 0)} labels={int(spec.get('reliable_outcomes_count') or 0)} "
            f"parents={spec['parent_ids']}"
        )
    lines.extend(["", "## Agent Architect", ""])
    for status, count in sorted(summary["spawn_candidates_by_status"].items()):
        lines.append(f"- `{status}`: {count}")
    lines.extend(["", "## Recent Runs"])
    for run in summary["latest_runs"][:50]:
        downstream = run.get("downstream") or {}
        lines.append(
            f"- `{run['run_id']}` agent=`{run['agent_id']}` status=`{run['status']}` action=`{run.get('action')}` "
            f"cost=${float(run.get('estimated_cost_usd') or 0):.4f} code={len(downstream.get('code_proposals') or [])} "
            f"strategies={len(downstream.get('strategy_experiments') or [])}"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
