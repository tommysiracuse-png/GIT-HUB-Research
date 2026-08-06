"""Persistent owner for turning public adapter research into paper evidence.

The adapter owner finishes when an adapter implementation is promoted.  This
owner starts there and owns the remaining runtime path: real prices, admission,
Strategy Lab handoff, paper candidates, trades, and reliable outcomes.  It does
not apply one generic signal to every adapter; a repository-aware Codex session
repairs each surface inside the existing code-evolution release pipeline.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import uuid
from collections import Counter
from typing import Any

from adapter_runtime import REPORT_JSON as ADAPTER_REPORT_JSON
from code_evolution import (
    preflight_proposal,
    process_code_change_recommendation,
    validate_strict_recommendation_schema,
)
from market_admission import STAGE_INDEX
from storage import RUNS_DIR, add_llm_recommendation
from strategy_implementation_owner import enqueue_recommendation as enqueue_strategy_recommendation
from temporal_memory import upsert_memory_fact, upsert_memory_link


REPORT_JSON = RUNS_DIR / "market_activation_owner.json"
REPORT_MD = RUNS_DIR / "market_activation_owner.md"

TERMINAL_STATUSES = {
    "completed_paper_evaluated",
    "completed_reference_feature",
    "reference_only_validated",
    "retired_unavailable",
}
CODE_SUCCESS_STATUSES = {
    "candidate_committed",
    "promoted",
    "promoted_pending_verification",
    "verified",
    "workspace_applied_probation",
    "workspace_kept",
    "kept",
}
CODE_PAUSED_STATUSES = {
    "queued_concurrent_worker",
    "implementation_paused",
    "patch_generation_timeout",
    "patch_generation_failed",
    "patch_generation_unavailable_retry_later",
}
CODE_CLAIMABLE_STATUSES = {
    "queued",
    "waiting_source",
    "needs_data_repair",
    "needs_runtime_repair",
    "implementation_paused",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _cfg(settings: dict) -> dict:
    defaults = {
        "enabled": True,
        "max_new_tasks_per_cycle": 100,
        "max_strategy_handoffs_per_cycle": 20,
        "lease_seconds": 2400,
        "retry_backoff_seconds": 300,
        "runtime_verification_scans": 3,
        "minimum_price_observations": 1,
        "trade_evidence_lookback_rows": 10000,
        "report_limit": 100,
    }
    return {**defaults, **(settings.get("market_activation_owner") or {})}


def _task_id(adapter_id: str, surface: str) -> tuple[str, str]:
    dedupe = f"market-activation:{adapter_id}:{surface}"
    digest = hashlib.sha256(dedupe.encode("utf-8")).hexdigest()[:20]
    return f"market-activation-{digest}", dedupe


def _task_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if not row:
        return {}
    item = dict(row)
    for source, target, default in (
        ("evidence_json", "evidence", {}),
        ("acceptance_json", "acceptance", {}),
        ("last_result_json", "last_result", {}),
        ("last_error_json", "last_error", {}),
    ):
        item[target] = _json(item.get(source), default)
    return item


def _runtime_report() -> dict[str, Any]:
    try:
        payload = json.loads(ADAPTER_REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _surface_rows(adapter: dict[str, Any]) -> list[tuple[str, int]]:
    surfaces = adapter.get("market_surfaces")
    if isinstance(surfaces, dict) and surfaces:
        return [(str(surface), int(count or 0)) for surface, count in surfaces.items()]
    return [(str(adapter.get("market_type") or "unknown"), int(adapter.get("observation_count") or 0))]


def _initial_status(adapter: dict[str, Any]) -> str:
    if int(adapter.get("candidate_count") or 0) > 0:
        return "monitoring_runtime"
    source_status = str(adapter.get("source_status") or "unknown")
    if source_status in {"blocked", "unavailable"} and int(adapter.get("observation_count") or 0) <= 0:
        return "waiting_source"
    return "queued"


def _priority(adapter: dict[str, Any]) -> int:
    if int(adapter.get("candidate_count") or 0) > 0:
        return 96
    if int(adapter.get("price_observation_count") or 0) > 0:
        return 94
    if int(adapter.get("observation_count") or 0) > 0:
        return 90
    if str(adapter.get("source_status") or "") == "degraded":
        return 86
    return 76


def _acceptance(adapter_id: str, venue: str, surface: str) -> dict[str, Any]:
    return {
        "adapter_id": adapter_id,
        "venue": venue,
        "market_surface": surface,
        "required_chain": [
            "registered_runtime_adapter",
            "real_public_price_observation",
            "normalized_market_admission",
            "strategy_lab_handoff_or_surface_candidate",
            "paper_trade",
            "reliable_outcome",
        ],
        "allowed_terminal_alternative": (
            "Reference-only is valid only when the source cannot publish a defensible price and its fields are "
            "actually consumed by a named Strategy Lab feature or experiment. A report-only adapter is not complete."
        ),
        "paper_execution": "Use normal paper routing when feasible and synthetic_research_paper otherwise.",
        "no_generic_strategy": "Do not apply one universal momentum or long/short rule to unrelated surfaces.",
    }


def sync_backlog(conn: sqlite3.Connection, settings: dict) -> dict[str, Any]:
    """Create one durable activation objective per adapter market surface."""

    cfg = _cfg(settings)
    report = _runtime_report()
    adapters = list(report.get("adapters") or [])
    created = 0
    refreshed = 0
    now = _utc_now()
    for adapter in adapters[: int(cfg["max_new_tasks_per_cycle"])]:
        adapter_id = str(adapter.get("adapter_id") or "").strip()
        venue = str(adapter.get("venue") or "unknown").strip().upper()
        if not adapter_id:
            continue
        for surface, surface_observations in _surface_rows(adapter):
            task_id, dedupe = _task_id(adapter_id, surface)
            existing = conn.execute(
                "select status from market_activation_tasks where task_id=?",
                (task_id,),
            ).fetchone()
            evidence = {
                "runtime_report_generated_at": report.get("generated_at"),
                "adapter": adapter,
                "surface_observation_count": surface_observations,
            }
            conn.execute(
                """
                insert into market_activation_tasks(
                    task_id,created_at,updated_at,dedupe_key,adapter_id,venue,market_surface,
                    objective_type,priority,status,source_adapter_spec_id,evidence_json,acceptance_json
                ) values(?,?,?,?,?,?,?,'activate_market_surface',?,?,?,?,?)
                on conflict(dedupe_key) do update set
                    updated_at=excluded.updated_at,
                    priority=max(market_activation_tasks.priority, excluded.priority),
                    source_adapter_spec_id=coalesce(market_activation_tasks.source_adapter_spec_id, excluded.source_adapter_spec_id),
                    evidence_json=excluded.evidence_json,
                    acceptance_json=excluded.acceptance_json
                """,
                (
                    task_id,
                    now,
                    now,
                    dedupe,
                    adapter_id,
                    venue,
                    surface,
                    _priority(adapter),
                    _initial_status(adapter),
                    adapter.get("adapter_spec_id"),
                    json.dumps(evidence, sort_keys=True, default=str),
                    json.dumps(_acceptance(adapter_id, venue, surface), sort_keys=True),
                ),
            )
            if existing is None:
                upsert_memory_fact(
                    conn,
                    "market_activation",
                    task_id,
                    "owns_adapter_surface_until_paper_evidence",
                    (
                        f"Adapter {adapter_id} at {venue} exposes {surface_observations} observations for {surface}. "
                        f"It currently emits {int(adapter.get('candidate_count') or 0)} candidates and has source "
                        f"status {adapter.get('source_status')}. The activation task remains responsible for real "
                        "prices, Strategy Lab lineage, paper trades, and reliable outcomes."
                    ),
                    0.95,
                    "market_activation_owner",
                    {"adapter_id": adapter_id, "venue": venue, "market_surface": surface},
                    namespace="market_expansion",
                    source_id=task_id,
                    importance=0.9,
                    tags=["market_activation", adapter_id, venue, surface],
                    commit=False,
                )
            created += int(existing is None)
            refreshed += int(existing is not None)
    conn.commit()
    return {
        "runtime_report_available": bool(report),
        "runtime_adapter_count": len(adapters),
        "tasks_created": created,
        "tasks_refreshed": refreshed,
    }


def _admission_metrics(conn: sqlite3.Connection, task: dict[str, Any]) -> dict[str, Any]:
    rows = conn.execute(
        """
        select admission_key,current_stage,highest_stage,health_status,blocker_code,
               eligible_scans,stalled_eligible_scans,details_json
        from market_admission_states
        where upper(venue)=? and market_surface=?
        """,
        (str(task["venue"]).upper(), task["market_surface"]),
    ).fetchall()
    matched = []
    for row in rows:
        item = dict(row)
        details = _json(item.pop("details_json"), {})
        stored_adapter = str(details.get("adapter_id") or "")
        if stored_adapter and stored_adapter != task["adapter_id"]:
            continue
        item["details"] = details
        matched.append(item)
    by_stage = Counter(str(row["current_stage"]) for row in matched)
    by_blocker = Counter(str(row["blocker_code"]) for row in matched if row.get("blocker_code"))
    return {
        "state_count": len(matched),
        "by_stage": dict(by_stage),
        "by_blocker": dict(by_blocker),
        "priceable_count": sum(STAGE_INDEX.get(str(row["current_stage"]), 0) >= STAGE_INDEX["priceable"] for row in matched),
        "highest_priceable_count": sum(STAGE_INDEX.get(str(row["highest_stage"]), 0) >= STAGE_INDEX["priceable"] for row in matched),
        "strategy_candidate_count": sum(STAGE_INDEX.get(str(row["current_stage"]), 0) >= STAGE_INDEX["strategy_candidate"] for row in matched),
        "paper_eligible_count": sum(STAGE_INDEX.get(str(row["highest_stage"]), 0) >= STAGE_INDEX["paper_eligible"] for row in matched),
        "paper_evaluated_count": sum(str(row["highest_stage"]) == "paper_evaluated" for row in matched),
        "eligible_scans": max([int(row.get("eligible_scans") or 0) for row in matched], default=0),
        "stalled_eligible_scans": max([int(row.get("stalled_eligible_scans") or 0) for row in matched], default=0),
        "sample_admission_keys": [str(row["admission_key"]) for row in matched[:10]],
    }


def _recent_trade_evidence(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select id,opened_at,status,strategy_lab_id,venue,candidate_json
        from paper_trades order by id desc limit ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["candidate"] = _json(item.pop("candidate_json"), {})
        output.append(item)
    return output


def _matching_trades(task: dict[str, Any], recent_trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched = []
    created_at = _parse_time(task.get("created_at"))
    for item in recent_trades:
        if str(item.get("venue") or "").upper() != str(task["venue"]).upper():
            continue
        opened_at = _parse_time(item.get("opened_at"))
        if created_at and opened_at and opened_at < created_at:
            continue
        candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
        adapter_id = str(candidate.get("adapter_id") or candidate.get("source_adapter_id") or "")
        surface = str(candidate.get("market_surface") or candidate.get("proxy_surface") or candidate.get("trade_type") or "")
        if adapter_id == task["adapter_id"] or (surface == task["market_surface"] and not adapter_id):
            matched.append(dict(item))
    return matched


def _strategy_metrics(conn: sqlite3.Connection, task: dict[str, Any]) -> dict[str, Any]:
    owner_task = None
    if task.get("strategy_owner_task_id"):
        owner_task = conn.execute(
            "select task_id,status,strategy_lab_id,last_result_json from strategy_owner_tasks where task_id=?",
            (task["strategy_owner_task_id"],),
        ).fetchone()
    experiments = []
    rows = conn.execute(
        """
        select strategy_lab_id,status,compile_status,source_surface,data_requirements_json,evaluation_json
        from strategy_lab_experiments
        where created_at>=? and (source_surface=? or data_requirements_json like ?)
        order by updated_at desc
        """,
        (task["created_at"], task["market_surface"], f'%"adapter_id": "{task["adapter_id"]}"%'),
    ).fetchall()
    for row in rows:
        item = dict(row)
        requirements = _json(item.pop("data_requirements_json"), {})
        if item.get("source_surface") == task["market_surface"] or requirements.get("adapter_id") == task["adapter_id"]:
            item["data_requirements"] = requirements
            item["evaluation"] = _json(item.pop("evaluation_json"), {})
            experiments.append(item)
    return {
        "strategy_owner_task": dict(owner_task) if owner_task else None,
        "experiments": experiments[:20],
        "experiment_count": len(experiments),
        "active_experiment_count": sum(
            str(row.get("status")) in {"active_testing", "needs_more_evidence", "promote_candidate"}
            for row in experiments
        ),
        "compiled_experiment_count": sum(str(row.get("compile_status")) == "compiled" for row in experiments),
    }


def _metrics(
    conn: sqlite3.Connection,
    task: dict[str, Any],
    recent_trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    admission = _admission_metrics(conn, task)
    strategy = _strategy_metrics(conn, task)
    trades = _matching_trades(task, recent_trades or [])
    trade_ids = [int(row["id"]) for row in trades]
    reliable = 0
    if trade_ids:
        placeholders = ",".join("?" for _ in trade_ids)
        reliable = int(
            conn.execute(
                f"select count(*) from paper_trade_outcomes where measurement_status='valid' and trade_id in ({placeholders})",
                trade_ids,
            ).fetchone()[0]
        )
    return {
        "admission": admission,
        "strategy": strategy,
        "paper_trade_count": len(trades),
        "open_paper_trade_count": sum(str(row.get("status")) == "open" for row in trades),
        "reliable_outcome_count": reliable,
        "strategy_lab_ids": sorted({str(row.get("strategy_lab_id")) for row in trades if row.get("strategy_lab_id")}),
    }


def _runtime_adapter(task: dict[str, Any]) -> dict[str, Any]:
    evidence = task.get("evidence") if isinstance(task.get("evidence"), dict) else {}
    adapter = evidence.get("adapter")
    return adapter if isinstance(adapter, dict) else {}


def _paper_diagnostic_pass(
    task: dict[str, Any],
    metrics: dict[str, Any],
    settings: dict,
    recent_trades: list[dict[str, Any]],
    recommendation_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = _cfg(settings)
    adapter = _runtime_adapter(task)
    evidence = task.get("evidence") if isinstance(task.get("evidence"), dict) else {}
    report_generated_at = _parse_time(evidence.get("runtime_report_generated_at"))
    now = dt.datetime.now(dt.timezone.utc)
    age_seconds = (
        round((now - report_generated_at).total_seconds(), 3)
        if report_generated_at is not None
        else None
    )
    freshness_threshold_seconds = max(
        300,
        int(cfg.get("retry_backoff_seconds", 300)),
        int(cfg.get("runtime_verification_scans", 3)) * 60,
    )
    matched_trades = _matching_trades(task, recent_trades)
    schema_valid = False
    schema_reason = "not_evaluated"
    preflight: dict[str, Any] = {}
    if isinstance(recommendation_payload, dict):
        schema_valid, schema_reason = validate_strict_recommendation_schema(recommendation_payload)
        preflight = preflight_proposal(recommendation_payload, settings)
    price_observation_count = int(adapter.get("price_observation_count") or 0)
    observation_count = int(adapter.get("observation_count") or 0)
    candidate_count = int(adapter.get("candidate_count") or 0)
    paper_trade_count = int(metrics.get("paper_trade_count") or 0)
    reliable_outcome_count = int(metrics.get("reliable_outcome_count") or 0)
    telemetry_complete = all(
        (
            report_generated_at is not None,
            observation_count >= 0,
            price_observation_count >= 0,
            candidate_count >= 0,
            schema_valid,
        )
    )
    return {
        "captured_at": _utc_now(),
        "paper_only": True,
        "telemetry_complete": telemetry_complete,
        "data_freshness": {
            "runtime_report_generated_at": evidence.get("runtime_report_generated_at"),
            "age_seconds": age_seconds,
            "freshness_threshold_seconds": freshness_threshold_seconds,
            "status": (
                "fresh"
                if age_seconds is not None and age_seconds <= freshness_threshold_seconds
                else "stale" if age_seconds is not None else "unknown"
            ),
            "source_status": adapter.get("source_status"),
        },
        "signal_values": {
            "observation_count": observation_count,
            "price_observation_count": price_observation_count,
            "candidate_count": candidate_count,
            "priceable_count": int((metrics.get("admission") or {}).get("priceable_count") or 0),
            "strategy_candidate_count": int((metrics.get("admission") or {}).get("strategy_candidate_count") or 0),
            "paper_trade_count": paper_trade_count,
            "reliable_outcome_count": reliable_outcome_count,
        },
        "decision_thresholds": {
            "minimum_price_observations": int(cfg.get("minimum_price_observations", 1)),
            "runtime_verification_scans": int(cfg.get("runtime_verification_scans", 3)),
            "required_chain": list((task.get("acceptance") or {}).get("required_chain") or []),
        },
        "simulated_positions": {
            "paper_trade_count": paper_trade_count,
            "open_paper_trade_count": int(metrics.get("open_paper_trade_count") or 0),
            "closed_paper_trade_count": sum(str(item.get("status") or "") == "closed" for item in matched_trades),
            "reliable_outcome_count": reliable_outcome_count,
            "strategy_lab_ids": list(metrics.get("strategy_lab_ids") or []),
            "recent_trade_ids": [int(item["id"]) for item in matched_trades[:10] if item.get("id") is not None],
        },
        "json_schema_validation": {
            "valid": schema_valid,
            "reason": schema_reason,
            "quality_scorecard": (
                (preflight.get("quality_scorecard") or {})
                if isinstance(preflight, dict)
                else {}
            ),
        },
    }


def _desired_status(task: dict[str, Any], metrics: dict[str, Any]) -> str:
    if int(metrics["reliable_outcome_count"]) > 0:
        return "completed_paper_evaluated"
    if int(metrics["paper_trade_count"]) > 0:
        return "paper_trading"
    strategy = metrics["strategy"]
    if int(strategy["active_experiment_count"]) > 0:
        return "active_testing"
    owner_task = strategy.get("strategy_owner_task") or {}
    owner_status = str(owner_task.get("status") or "")
    if owner_status == "waiting_data":
        return "needs_data_repair"
    if owner_status == "waiting_route":
        return "needs_runtime_repair"
    if owner_task:
        return "strategy_handoff"
    adapter = _runtime_adapter(task)
    if int(adapter.get("candidate_count") or 0) > 0:
        return "monitoring_runtime"
    if int(metrics["admission"]["priceable_count"]) > 0:
        return "needs_strategy_handoff"
    if str(task.get("status") or "") in {
        "coding",
        "implementation_paused",
        "needs_data_repair",
        "needs_runtime_repair",
    }:
        return str(task["status"])
    if str(adapter.get("source_status") or "") in {"blocked", "unavailable"} and int(adapter.get("observation_count") or 0) <= 0:
        return "waiting_source"
    if task.get("code_proposal_id"):
        return "deployed_waiting_runtime"
    return "queued"


def _reconcile_tasks(
    conn: sqlite3.Connection,
    settings: dict,
    recent_trades: list[dict[str, Any]],
) -> dict[str, int]:
    changed = Counter()
    rows = conn.execute(
        "select * from market_activation_tasks where status not in (%s)" % ",".join("?" for _ in TERMINAL_STATUSES),
        sorted(TERMINAL_STATUSES),
    ).fetchall()
    now = _utc_now()
    verification_scans = int(_cfg(settings)["runtime_verification_scans"])
    for raw in rows:
        task = _task_dict(raw)
        if task["status"] == "waiting_source" and str(_runtime_adapter(task).get("source_status") or "") == "blocked":
            continue
        metrics = _metrics(conn, task, recent_trades)
        desired = _desired_status(task, metrics)
        current = str(task["status"])
        if current == "coding" and _lease_available(task):
            desired = "implementation_paused"
        if current == "deployed_waiting_runtime" and desired in {"queued", "deployed_waiting_runtime"}:
            prior_metrics = (task.get("last_result") or {}).get("runtime_metrics") or {}
            baseline_scans = int((prior_metrics.get("admission") or {}).get("eligible_scans") or 0)
            new_scans = max(0, int(metrics["admission"]["eligible_scans"]) - baseline_scans)
            if new_scans >= verification_scans:
                desired = "needs_runtime_repair"
        if desired != current:
            changed[f"{current}->{desired}"] += 1
            upsert_memory_fact(
                conn,
                "market_activation",
                task["task_id"],
                "activation_status_changed",
                (
                    f"{task['adapter_id']} {task['venue']} {task['market_surface']} moved from {current} to {desired}. "
                    f"Current admission={metrics['admission']['by_stage']}; Strategy Lab experiments="
                    f"{metrics['strategy']['experiment_count']}; paper trades={metrics['paper_trade_count']}; "
                    f"reliable outcomes={metrics['reliable_outcome_count']}."
                ),
                0.95,
                "market_activation_owner",
                {"from_status": current, "to_status": desired, "runtime_metrics": metrics},
                namespace="market_expansion",
                source_id=f"{task['task_id']}:{current}:{desired}",
                importance=0.95,
                outcome_score=1.0 if desired in TERMINAL_STATUSES else 0.0,
                tags=["market_activation", task["adapter_id"], desired],
                commit=False,
            )
        completed = now if desired in TERMINAL_STATUSES else None
        result = dict(task.get("last_result") or {})
        result["runtime_metrics"] = metrics
        experiments = metrics["strategy"].get("experiments") or []
        strategy_lab_id = str(experiments[0].get("strategy_lab_id")) if experiments else None
        admission_keys = metrics["admission"].get("sample_admission_keys") or []
        conn.execute(
            """
            update market_activation_tasks
            set status=?,updated_at=?,completed_at=coalesce(?,completed_at),last_result_json=?,
                strategy_lab_id=coalesce(?,strategy_lab_id),
                source_admission_key=coalesce(?,source_admission_key),
                claimed_pid=null,lease_expires_at=null
            where task_id=?
            """,
            (
                desired,
                now,
                completed,
                json.dumps(result, sort_keys=True, default=str),
                strategy_lab_id,
                str(admission_keys[0]) if admission_keys else None,
                task["task_id"],
            ),
        )
    conn.commit()
    return dict(changed)


def _strategy_recommendation(task: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    adapter = _runtime_adapter(task)
    rec_id = f"market-activation:{task['task_id']}:strategy"
    title = f"Invent and test a strategy for {task['venue']} {task['market_surface']}"
    rationale = (
        f"Adapter {task['adapter_id']} now supplies real priceable observations for "
        f"{task['market_surface']}, but no independent strategy lineage is paper testing them. "
        "Use the actual observed fields and related-market evidence to invent a surface-specific, repeatable hypothesis."
    )
    payload = {
        "action": "propose_strategy_lab_experiment",
        "agent_name": "market_activation_owner",
        "priority": max(90, int(task["priority"])),
        "title": title,
        "rationale": rationale,
        "market_key": f"paper|{task['venue']}|{task['market_surface']}",
        "evidence": {
            "adapter_id": task["adapter_id"],
            "venue": task["venue"],
            "market_surface": task["market_surface"],
            "available_fields": adapter.get("available_fields") or [],
            "sample_instruments": adapter.get("sample_instruments") or [],
            "admission": metrics["admission"],
            "docs_url": adapter.get("docs_url"),
        },
        "strategy_lab_experiment": {
            "experiment_type": "observation_program",
            "hypothesis": rationale,
            "source_surface": task["market_surface"],
            "permitted_target_surface": [task["market_surface"]],
            "data_requirements": {
                "adapter_id": task["adapter_id"],
                "venue": task["venue"],
                "market_surface": task["market_surface"],
                "available_fields": adapter.get("available_fields") or [],
                "sample_instruments": adapter.get("sample_instruments") or [],
            },
        },
        "acceptance_criteria": {
            "compile": "A valid observation program compiles against fields this adapter actually emits.",
            "runtime": "Qualifying observations produce Strategy Lab candidates with this adapter lineage.",
            "paper": "Candidates reach normal or synthetic paper execution and reliable outcomes.",
        },
    }
    return {
        "recommendation_id": rec_id,
        "title": title,
        "rationale": rationale,
        "priority": payload["priority"],
        "payload": payload,
    }


def _create_strategy_handoffs(
    conn: sqlite3.Connection,
    settings: dict,
    recent_trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    limit = int(_cfg(settings)["max_strategy_handoffs_per_cycle"])
    rows = conn.execute(
        """
        select * from market_activation_tasks
        where status='needs_strategy_handoff' and strategy_owner_task_id is null
        order by priority desc,created_at asc limit ?
        """,
        (limit,),
    ).fetchall()
    created = []
    for raw in rows:
        task = _task_dict(raw)
        metrics = _metrics(conn, task, recent_trades)
        if int(metrics["admission"]["priceable_count"]) < int(_cfg(settings)["minimum_price_observations"]):
            continue
        rec = _strategy_recommendation(task, metrics)
        payload = rec["payload"]
        add_llm_recommendation(
            conn,
            rec["recommendation_id"],
            payload["action"],
            payload["title"],
            payload["rationale"],
            payload,
        )
        artifact = enqueue_strategy_recommendation(conn, rec, settings)
        if artifact.get("task_id"):
            upsert_memory_link(
                conn,
                "market_activation_task",
                task["task_id"],
                "hands_off_strategy_invention_to",
                "strategy_owner_task",
                str(artifact["task_id"]),
                evidence={"adapter_id": task["adapter_id"], "market_surface": task["market_surface"]},
            )
        conn.execute(
            """
            update market_activation_tasks
            set strategy_owner_task_id=?,status='strategy_handoff',updated_at=?,last_result_json=?
            where task_id=?
            """,
            (
                artifact.get("task_id"),
                _utc_now(),
                json.dumps({"strategy_handoff": artifact, "runtime_metrics": metrics}, sort_keys=True, default=str),
                task["task_id"],
            ),
        )
        conn.commit()
        created.append({"task_id": task["task_id"], **artifact})
    return created


def _lease_available(task: dict[str, Any]) -> bool:
    expires = _parse_time(task.get("lease_expires_at"))
    return not task.get("claimed_pid") or expires is None or expires <= dt.datetime.now(dt.timezone.utc)


def _select_code_task(conn: sqlite3.Connection) -> dict[str, Any] | None:
    rows = conn.execute(
        "select * from market_activation_tasks where status in (%s) order by priority desc,updated_at asc"
        % ",".join("?" for _ in CODE_CLAIMABLE_STATUSES),
        sorted(CODE_CLAIMABLE_STATUSES),
    ).fetchall()
    now = dt.datetime.now(dt.timezone.utc)
    for raw in rows:
        task = _task_dict(raw)
        retry_at = _parse_time(task.get("next_retry_at"))
        if retry_at and retry_at > now:
            continue
        if _lease_available(task):
            return task
    return None


def _code_recommendation(
    task: dict[str, Any],
    metrics: dict[str, Any],
    settings: dict,
    recent_trades: list[dict[str, Any]],
    attempt: int,
) -> dict[str, Any]:
    adapter = _runtime_adapter(task)
    rec_id = f"market-activation:{task['task_id']}:code:{attempt}"
    acceptance = task.get("acceptance") or _acceptance(task["adapter_id"], task["venue"], task["market_surface"])
    proposed = (
        f"Own adapter {task['adapter_id']} for {task['venue']} {task['market_surface']} through actual paper testing. "
        "Inspect the repository and current adapter implementation. Repair the narrow missing runtime links instead of "
        "adding another report or discovery record. Obtain defensible public prices when available, preserve provenance, "
        "normalize the emitted fields, and connect the surface to the existing market-admission and Strategy Lab systems. "
        "If route execution is unavailable, use the existing synthetic research paper path. If the source is genuinely "
        "reference-only, either find a public priceable companion/proxy or wire the reference fields into a named Strategy "
        "Lab feature; never fabricate a price. If the runtime evidence is still incomplete, first preserve a paper-only "
        "diagnostic pass that records data freshness, signal values, decision thresholds, simulated positions, and JSON-schema "
        "validation results before changing the paper strategy. Add focused tests and leave unrelated strategies unchanged."
    )
    code_change = {
        "change_category": "runtime_pipeline_integration",
        "implementation_mode": "runtime_active",
        "expected_files": [],
        "tests_to_run": [
            "python -m unittest tests.test_public_market_adapters",
            "python -m unittest tests.test_strategy_lab",
        ],
        "paper_testable_surface": f"paper:{task['venue']}:{task['market_surface']}:{task['adapter_id']}",
        "behavioral_gate": (
            "A normal radar cycle must advance this exact surface toward Strategy Lab candidates and paper evidence, "
            "not merely register an adapter or write a report."
        ),
        "rollback_criteria": (
            "Revert if public-adapter discovery, Strategy Lab propagation, paper-only safety, or the full regression fails."
        ),
        "activation_contract": acceptance,
    }
    payload = {
        "action": "propose_code_change",
        "agent_name": "market_activation_owner",
        "title": f"Activate {task['adapter_id']} through paper testing"[:180],
        "priority": int(task["priority"]),
        "market_key": f"paper|{task['venue']}|{task['market_surface']}",
        "rationale": proposed,
        "proposed_change": proposed,
        "evidence": {
            "source": "market_activation_owner",
            "market_activation_task_id": task["task_id"],
            "adapter_id": task["adapter_id"],
            "adapter_runtime": adapter,
            "admission": metrics["admission"],
            "strategy": metrics["strategy"],
            "route_evidence": {
                "paper_mode": True,
                "synthetic_research_available": True,
                "live_trading": False,
            },
            "quality_evidence": {
                "price_observation_count": adapter.get("price_observation_count", 0),
                "available_fields": adapter.get("available_fields") or [],
                "source_status": adapter.get("source_status"),
                "capability_gap": adapter.get("capability_gap"),
            },
        },
        "paper_testable_surface": code_change["paper_testable_surface"],
        "behavioral_gate": code_change["behavioral_gate"],
        "rollback_criteria": code_change["rollback_criteria"],
        "frontier_escalation_reason": (
            "A public adapter already exists but has not produced paper evidence; repository-aware implementation is required."
        ),
        "change_category": code_change["change_category"],
        "implementation_mode": code_change["implementation_mode"],
        "code_change": code_change,
    }
    payload["evidence"]["paper_diagnostic_pass"] = _paper_diagnostic_pass(
        task,
        metrics,
        settings,
        recent_trades,
        recommendation_payload=payload,
    )
    return {
        "recommendation_id": rec_id,
        "title": payload["title"],
        "priority": payload["priority"],
        "payload": payload,
    }


def _run_code_turn(
    conn: sqlite3.Connection,
    task: dict[str, Any],
    settings: dict,
    cycle_id: str,
    recent_trades: list[dict[str, Any]],
) -> dict[str, Any]:
    cfg = _cfg(settings)
    metrics = _metrics(conn, task, recent_trades)
    last_result = dict(task.get("last_result") or {})
    stored_rec = last_result.get("active_code_recommendation")
    if task["status"] == "implementation_paused" and isinstance(stored_rec, dict):
        recommendation = stored_rec
        attempt = int(task.get("attempt_count") or 1)
        payload = recommendation.get("payload") if isinstance(recommendation.get("payload"), dict) else {}
        if payload:
            payload = dict(payload)
            payload.setdefault("evidence", {})
            if isinstance(payload["evidence"], dict):
                payload["evidence"]["paper_diagnostic_pass"] = _paper_diagnostic_pass(
                    task,
                    metrics,
                    settings,
                    recent_trades,
                    recommendation_payload=payload,
                )
            recommendation = {**recommendation, "payload": payload}
    else:
        attempt = int(task.get("attempt_count") or 0) + 1
        recommendation = _code_recommendation(task, metrics, settings, recent_trades, attempt)
    diagnostic = (
        (((recommendation.get("payload") or {}).get("evidence") or {}).get("paper_diagnostic_pass"))
        if isinstance(recommendation, dict)
        else None
    )
    started = _utc_now()
    run_id = str(uuid.uuid4())
    lease = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(cfg["lease_seconds"]))).isoformat()
    last_result["active_code_recommendation"] = recommendation
    last_result["runtime_metrics"] = metrics
    if isinstance(diagnostic, dict):
        last_result["paper_diagnostic_pass"] = diagnostic
    conn.execute(
        """
        update market_activation_tasks
        set status='coding',claimed_pid=?,lease_expires_at=?,attempt_count=?,updated_at=?,last_result_json=?
        where task_id=?
        """,
        (os.getpid(), lease, attempt, started, json.dumps(last_result, sort_keys=True, default=str), task["task_id"]),
    )
    conn.execute(
        """
        insert into market_activation_runs(run_id,task_id,cycle_id,started_at,status_before,result_json)
        values(?,?,?,?,?, '{}')
        """,
        (run_id, task["task_id"], cycle_id, started, task["status"]),
    )
    conn.commit()
    try:
        artifacts = process_code_change_recommendation(conn, recommendation, settings)
        artifact = artifacts[0] if artifacts else {}
        proposal_status = str(artifact.get("status") or "no_artifact")
        proposal_id = artifact.get("proposal_id")
        if proposal_id:
            upsert_memory_link(
                conn,
                "market_activation_task",
                task["task_id"],
                "implemented_by",
                "code_evolution_proposal",
                str(proposal_id),
                evidence={"attempt": attempt, "proposal_status": proposal_status},
            )
        if proposal_status in CODE_SUCCESS_STATUSES:
            status = "deployed_waiting_runtime"
            retry_at = None
        elif proposal_status in CODE_PAUSED_STATUSES:
            status = "implementation_paused"
            retry_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(cfg["retry_backoff_seconds"]))).isoformat()
        elif "quota" in proposal_status or "unavailable" in proposal_status:
            status = "implementation_paused"
            retry_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(cfg["retry_backoff_seconds"]))).isoformat()
        else:
            status = "needs_runtime_repair"
            retry_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(cfg["retry_backoff_seconds"]))).isoformat()
        result = {
            "artifacts": artifacts,
            "proposal_status": proposal_status,
            "runtime_metrics": metrics,
            "paper_diagnostic_pass": diagnostic if isinstance(diagnostic, dict) else None,
            "active_code_recommendation": recommendation if status == "implementation_paused" else None,
        }
        conn.execute(
            """
            update market_activation_tasks
            set status=?,code_proposal_id=coalesce(?,code_proposal_id),claimed_pid=null,lease_expires_at=null,
                next_retry_at=?,updated_at=?,last_result_json=?
            where task_id=?
            """,
            (status, proposal_id, retry_at, _utc_now(), json.dumps(result, sort_keys=True, default=str), task["task_id"]),
        )
        conn.execute(
            """
            update market_activation_runs
            set completed_at=?,status_after=?,decision=?,code_proposal_id=?,result_json=?
            where run_id=?
            """,
            (_utc_now(), status, proposal_status, proposal_id, json.dumps(result, sort_keys=True, default=str), run_id),
        )
        conn.commit()
        return {
            "status": status,
            "task_id": task["task_id"],
            "proposal_id": proposal_id,
            "proposal_status": proposal_status,
            "consumed_writer": True,
        }
    except Exception as exc:  # noqa: BLE001 - preserve the durable task after worker/process failure.
        retry_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(cfg["retry_backoff_seconds"]))).isoformat()
        error = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        conn.execute(
            """
            update market_activation_tasks
            set status='implementation_paused',claimed_pid=null,lease_expires_at=null,next_retry_at=?,
                updated_at=?,last_error_json=? where task_id=?
            """,
            (retry_at, _utc_now(), json.dumps(error, sort_keys=True), task["task_id"]),
        )
        conn.execute(
            """
            update market_activation_runs
            set completed_at=?,status_after='implementation_paused',decision='owner_exception',error_json=?
            where run_id=?
            """,
            (_utc_now(), json.dumps(error, sort_keys=True), run_id),
        )
        conn.commit()
        return {"status": "implementation_paused", "task_id": task["task_id"], "error": error, "consumed_writer": True}


def summary(conn: sqlite3.Connection, limit: int = 100) -> dict[str, Any]:
    tables = {str(row[0]) for row in conn.execute("select name from sqlite_master where type='table'")}
    if "market_activation_tasks" not in tables:
        return {"enabled": False, "status": "not_initialized"}
    by_status = dict(conn.execute("select status,count(*) from market_activation_tasks group by status").fetchall())
    rows = conn.execute(
        "select * from market_activation_tasks order by priority desc,updated_at desc limit ?",
        (int(limit),),
    ).fetchall()
    tasks = [_task_dict(row) for row in rows]
    funnel = {
        "tracked_surfaces": sum(int(value) for value in by_status.values()),
        "strategy_handoffs": int(by_status.get("strategy_handoff", 0)) + int(by_status.get("active_testing", 0)),
        "active_testing": int(by_status.get("active_testing", 0)),
        "paper_trading": int(by_status.get("paper_trading", 0)),
        "paper_evaluated": int(by_status.get("completed_paper_evaluated", 0)),
    }
    run_counts = dict(conn.execute("select coalesce(decision,'unknown'),count(*) from market_activation_runs group by decision").fetchall())
    return {
        "enabled": True,
        "by_status": by_status,
        "funnel": funnel,
        "runs_by_decision": run_counts,
        "tasks": tasks,
    }


def _write_report(report: dict[str, Any]) -> dict[str, Any]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    state = report.get("summary") or {}
    lines = [
        "# Market Activation Owner",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Runtime adapters: `{(report.get('sync') or {}).get('runtime_adapter_count', 0)}`",
        f"- Tasks by status: `{state.get('by_status', {})}`",
        f"- Conversion funnel: `{state.get('funnel', {})}`",
        f"- Strategy handoffs this cycle: `{len(report.get('strategy_handoffs') or [])}`",
        f"- Writer consumed: `{bool(report.get('consumed_writer'))}`",
    ]
    cycle = report.get("last_cycle") or {}
    if cycle:
        lines.extend(["", "## Last Cycle", "", f"- `{cycle}`"])
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run_once(
    conn: sqlite3.Connection,
    settings: dict,
    *,
    execute_turn: bool = True,
    cycle_id: str | None = None,
    scheduler: dict | None = None,
) -> dict[str, Any]:
    cfg = _cfg(settings)
    if not cfg.get("enabled", True):
        return _write_report({"generated_at": _utc_now(), "status": "disabled", "consumed_writer": False})
    cycle_id = cycle_id or str(uuid.uuid4())
    sync = sync_backlog(conn, settings)
    recent_trades = _recent_trade_evidence(conn, int(cfg["trade_evidence_lookback_rows"]))
    transitions = _reconcile_tasks(conn, settings, recent_trades)
    handoffs = _create_strategy_handoffs(conn, settings, recent_trades)
    last_cycle: dict[str, Any] = {"status": "monitor_only", "consumed_writer": False}
    if execute_turn:
        task = _select_code_task(conn)
        if task:
            last_cycle = _run_code_turn(conn, task, settings, cycle_id, recent_trades)
        else:
            last_cycle = {"status": "no_eligible_activation_task", "consumed_writer": False}
    state = summary(conn, limit=int(cfg["report_limit"]))
    return _write_report(
        {
            "generated_at": _utc_now(),
            "status": "ok",
            "sync": sync,
            "transitions": transitions,
            "strategy_handoffs": handoffs,
            "last_cycle": last_cycle,
            "consumed_writer": bool(last_cycle.get("consumed_writer")),
            "scheduler": scheduler or {},
            "summary": state,
        }
    )
