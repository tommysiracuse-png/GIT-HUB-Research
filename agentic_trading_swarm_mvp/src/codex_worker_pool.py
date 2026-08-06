#!/usr/bin/env python3
"""Concurrent repository-aware Codex workers with serialized Git promotion.

The radar database remains the source of truth for recommendations and owner
tasks.  A small, separate SQLite database owns only scheduling, leases, and
asynchronous verification so long coding turns do not contend with radar writes.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from typing import Any

from adapter_implementation_owner import run_once as run_adapter_owner
from autonomous_builder import run_autonomous_builder
from code_evolution import process_code_change_recommendation
from codex_coordination import (
    ACTIVE_TASK_STATUSES,
    CLAIMABLE_TASK_STATUSES,
    acquire_resource_lease,
    claim_task,
    claim_verification_job,
    complete_task,
    connect as connect_coordination,
    coordination_summary,
    enqueue_task,
    enqueue_verification_job,
    finish_verification_job,
    heartbeat_worker,
    peer_work_context,
    record_migration,
    reconcile_duplicate_tasks,
    release_resource_lease,
    renew_task_lease,
    requeue_task,
    set_task_work_identity,
)
from evolution.contracts import CandidateRelease
from evolution.worktree import cleanup_worktree, current_commit, repo_root, run_git, update_champion_latest
from market_activation_owner import run_once as run_activation_owner
from self_improvement import run_auto_improvement
from settings import load_settings
from storage import (
    RUNS_DIR,
    code_evolution_by_status,
    connect,
    get_code_evolution_proposal,
    update_code_evolution_proposal,
)
from strategy_implementation_owner import run_once as run_strategy_owner


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_JSON = RUNS_DIR / "codex_worker_pool.json"
REPORT_MD = RUNS_DIR / "codex_worker_pool.md"
SUPERVISOR_HEARTBEAT = RUNS_DIR / "codex_worker_pool_heartbeat.json"

RETRYABLE_STATUSES = {
    "queued_concurrent_worker",
    "implementation_paused",
    "patch_generation_timeout",
    "patch_generation_failed",
    "patch_generation_unavailable_retry_later",
    "blocked_model_quota",
    "queued_probation_limit",
    "promotion_overlap_requires_repair",
    "main_promotion_lease_timeout",
    "repairing_post_promotion",
}
PENDING_VERIFICATION_STATUSES = {"promoted_pending_verification"}
PROPOSAL_QUEUE_STATUSES = sorted(RETRYABLE_STATUSES | {"proposed"})


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _runtime_heartbeat(stop: threading.Event, settings: dict) -> None:
    interval = max(5, int(_cfg(settings).get("worker_heartbeat_seconds", 30)))
    while not stop.is_set():
        try:
            prior = json.loads(SUPERVISOR_HEARTBEAT.read_text(encoding="utf-8")) if SUPERVISOR_HEARTBEAT.exists() else {}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            prior = {}
        payload = {
            **(prior if isinstance(prior, dict) else {}),
            "supervisor_pid": int(os.environ.get("CODEX_WORKER_POOL_SUPERVISOR_PID") or prior.get("supervisor_pid") or os.getppid()),
            "status": "running_pool_iteration",
            "last_updated_at_utc": _utc_now(),
            "project_root": str(ROOT),
        }
        temporary = SUPERVISOR_HEARTBEAT.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, SUPERVISOR_HEARTBEAT)
        except OSError:
            pass
        stop.wait(interval)


def _cfg(settings: dict) -> dict[str, Any]:
    defaults = {
        "enabled": True,
        "coordination_db": "runs/codex_coordination.sqlite",
        "max_workers": 3,
        "max_verifiers": 2,
        "task_lease_seconds": 2700,
        "worker_heartbeat_seconds": 30,
        "promotion_lease_seconds": 180,
        "verification_timeout_seconds": 900,
    "queue_batch_size": 100,
    "max_quick_task_hops": 8,
    "quick_task_seconds": 5,
        "defer_full_regression": True,
        "keep_repairing_after_verification_failure": True,
        "worker_roles": [
            {"worker_id": "strategy-codex", "preferred_lanes": ["strategy"]},
            {"worker_id": "market-codex", "preferred_lanes": ["adapter"]},
            {"worker_id": "system-codex", "preferred_lanes": ["general"]},
        ],
    }
    return {**defaults, **(settings.get("codex_worker_pool") or {})}


def coordination_db_path(settings: dict) -> pathlib.Path:
    raw = pathlib.Path(str(_cfg(settings)["coordination_db"])).expanduser()
    return raw.resolve() if raw.is_absolute() else (ROOT / raw).resolve()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone() is not None


def _coordination_has_open_kind(coord: sqlite3.Connection, source_kind: str) -> bool:
    statuses = sorted(CLAIMABLE_TASK_STATUSES | ACTIVE_TASK_STATUSES)
    placeholders = ",".join("?" for _ in statuses)
    return coord.execute(
        f"select 1 from codex_tasks where source_kind=? and status in ({placeholders}) limit 1",
        (source_kind, *statuses),
    ).fetchone() is not None


def _enqueue_owner_turn(
    coord: sqlite3.Connection,
    *,
    source_kind: str,
    source_id: str,
    lane: str,
    priority: int,
    payload: dict[str, Any] | None = None,
    work_fingerprint: str | None = None,
    work_scope: str | None = None,
) -> dict[str, Any] | None:
    if _coordination_has_open_kind(coord, source_kind):
        return None
    return enqueue_task(
        coord,
        source_kind,
        source_id,
        lane=lane,
        priority=priority,
        payload={"lane": lane, "source_id": source_id, **(payload or {})},
        reactivate_terminal=True,
        work_fingerprint=work_fingerprint,
        work_scope=work_scope,
    )


def _proposal_lane(row: dict[str, Any]) -> str:
    payload = row.get("payload") or {}
    category = str(row.get("category") or "unknown")
    source = str(payload.get("agent_name") or row.get("source_agent") or "").lower()
    if "strategy" in source or category == "strategy_lab_promotion":
        return "strategy"
    if "adapter" in source or category in {"public_data_adapter", "parser_improvement", "scanner_expansion"}:
        return "adapter"
    if "activation" in source or category == "runtime_pipeline_integration":
        return "activation"
    return "general"


_WORK_STOPWORDS = {
    "a", "add", "an", "and", "as", "at", "candidate", "candidates", "change",
    "for", "from", "in", "into", "of", "on", "only", "paper", "spot", "spots",
    "the", "through", "to", "with", "fill", "fills", "trade", "trades",
}


def _nested(payload: dict[str, Any], key: str) -> Any:
    if key in payload and payload.get(key) not in (None, ""):
        return payload.get(key)
    code_change = payload.get("code_change")
    if isinstance(code_change, dict) and code_change.get(key) not in (None, ""):
        return code_change.get(key)
    return None


def _flatten_work_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_work_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_work_text(item) for item in value)
    return str(value or "")


def _normalize_work_tokens(value: Any) -> list[str]:
    text = _flatten_work_text(value).lower()
    replacements = (
        (r"\b(?:cost[-_\s]*(?:swallowed|negative)|non[-_\s]*positive[-_\s]*net[-_\s]*edge|net[-_\s]*edge[-_\s]*(?:after[-_\s]*)?costs?)\b", " net_edge_cost "),
        (r"\bmean[-_\s]*reversion\b", " mean_reversion "),
        (r"\b(?:gate|gated|guard|guardrail|shadow|stop|quarantine|exclude|block|filter|cap|tighten)(?:d|ing|s)?\b", " admission_policy "),
        (r"\b(?:decayed|decaying|decay)\b", " decay "),
        (r"\b(?:paper[-_\s]*)?(?:filled|fills?|candidates?)\b", " "),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    tokens = re.findall(r"[a-z0-9]+(?:_[a-z0-9]+)*", text)
    normalized = {token for token in tokens if token not in _WORK_STOPWORDS and len(token) > 1}
    if "okx" in normalized and "basis" in normalized and "decay" in normalized:
        normalized.difference_update({"basis", "decay", "mean_reversion", "regime"})
        normalized.add("okx_basis_decay")
    if "frontier" in normalized and (
        "net_edge_cost" in normalized or {"cost", "edge"}.issubset(normalized)
    ):
        normalized.difference_update({"cost", "edge", "negative", "swallowed", "nonpositive"})
        normalized.add("frontier_net_edge_cost")
    return sorted(normalized)


def _proposal_work_identity(
    row: dict[str, Any], radar: sqlite3.Connection | None = None
) -> tuple[str, str]:
    """Return a stable, readable identity for semantically equivalent code work."""

    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    code_change = payload.get("code_change") if isinstance(payload.get("code_change"), dict) else {}
    proposal_id = str(row.get("proposal_id") or "")

    activation = code_change.get("activation_contract")
    if isinstance(activation, dict):
        adapter_id = str(activation.get("adapter_id") or "unknown").lower()
        surface = str(activation.get("market_surface") or payload.get("market_key") or "unknown").lower()
        scope = f"market_activation:{adapter_id}:{surface}"
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope

    if radar is not None and proposal_id and _table_exists(radar, "strategy_owner_tasks"):
        owner = radar.execute(
            "select task_id from strategy_owner_tasks where code_proposal_id=? limit 1",
            (proposal_id,),
        ).fetchone()
        if owner:
            scope = f"strategy_owner:{str(owner['task_id']).lower()}"
            return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope

    adapter_spec_id = _nested(payload, "adapter_spec_id")
    if adapter_spec_id not in (None, ""):
        scope = f"adapter_spec:{adapter_spec_id}"
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope

    market_key = str(payload.get("market_key") or payload.get("signal_key") or "").lower()
    target_tokens = _normalize_work_tokens(market_key)
    target = "_".join(target_tokens[:10]) or str(row.get("category") or "general").lower()
    title_tokens = _normalize_work_tokens(row.get("title") or payload.get("title"))
    if len(title_tokens) < 2:
        title_tokens = _normalize_work_tokens(payload.get("proposed_change") or payload.get("rationale"))[:12]
    scope = f"semantic:{target}:{'_'.join(title_tokens[:14]) or 'unspecified'}"
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope


def _owner_work_identity(source_kind: str, row: sqlite3.Row) -> tuple[str, str]:
    if source_kind == "adapter_owner_turn":
        scope = f"adapter_spec:{row['id']}"
    elif source_kind == "strategy_owner_turn":
        scope = f"strategy_owner:{str(row['task_id']).lower()}"
    elif source_kind == "activation_owner_turn":
        scope = (
            f"market_activation:{str(row['adapter_id'] or 'unknown').lower()}:"
            f"{str(row['market_surface'] or 'unknown').lower()}"
        )
    else:
        scope = f"{source_kind}:{str(row[0]).lower()}"
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope


def _backfill_work_identities(
    radar: sqlite3.Connection, coord: sqlite3.Connection, *, limit: int = 1000
) -> int:
    rows = coord.execute(
        """
        select task_id,source_id from codex_tasks
        where source_kind='code_evolution_proposal'
          and status<>'superseded_duplicate'
          and (work_fingerprint is null or work_fingerprint='')
        order by updated_at desc limit ?
        """,
        (int(limit),),
    ).fetchall()
    updated = 0
    for task in rows:
        proposal = get_code_evolution_proposal(radar, str(task["source_id"]))
        if not proposal:
            continue
        fingerprint, scope = _proposal_work_identity(proposal, radar)
        updated += int(
            set_task_work_identity(
                coord,
                str(task["task_id"]),
                work_fingerprint=fingerprint,
                work_scope=scope,
            )
        )
    owner_rows = coord.execute(
        """
        select task_id,source_kind,source_id from codex_tasks
        where source_kind in ('strategy_owner_turn','adapter_owner_turn','activation_owner_turn','general_owner_turn')
          and status<>'superseded_duplicate'
          and (work_fingerprint is null or work_fingerprint='')
        order by updated_at desc limit ?
        """,
        (int(limit),),
    ).fetchall()
    for task in owner_rows:
        source_kind = str(task["source_kind"])
        source_id = str(task["source_id"])
        owner_row = None
        if source_kind == "strategy_owner_turn" and _table_exists(radar, "strategy_owner_tasks"):
            owner_row = radar.execute(
                "select task_id,priority,code_proposal_id from strategy_owner_tasks where task_id=?",
                (source_id,),
            ).fetchone()
        elif source_kind == "adapter_owner_turn" and _table_exists(radar, "adapter_specs"):
            owner_row = radar.execute(
                "select id,priority,title,market_key from adapter_specs where id=?",
                (source_id,),
            ).fetchone()
        elif source_kind == "activation_owner_turn" and _table_exists(radar, "market_activation_tasks"):
            owner_row = radar.execute(
                """
                select task_id,priority,adapter_id,market_surface,venue
                from market_activation_tasks where task_id=?
                """,
                (source_id,),
            ).fetchone()
        if owner_row is not None:
            fingerprint, scope = _owner_work_identity(source_kind, owner_row)
        else:
            scope = f"{source_kind}:{source_id.lower()}"
            fingerprint = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]
        updated += int(
            set_task_work_identity(
                coord,
                str(task["task_id"]),
                work_fingerprint=fingerprint,
                work_scope=scope,
            )
        )
    return updated


def _reconcile_promoted_commits(
    radar: sqlite3.Connection, coord: sqlite3.Connection, settings: dict
) -> dict[str, Any]:
    """Recover metadata when Git promotion won the race with a locked radar DB."""

    root = repo_root(ROOT)
    if root is None:
        return {"reconciled": 0, "reason": "ambiguous_repo_root"}
    history = run_git(
        ["log", "--format=%H%x09%s", f"-{int(_cfg(settings).get('promotion_reconcile_log_limit', 500))}"],
        root,
        timeout=30,
    )
    if history["returncode"] != 0:
        return {"reconciled": 0, "reason": "git_history_unavailable"}
    promoted_by_subject: dict[str, str] = {}
    for line in str(history.get("stdout") or "").splitlines():
        commit, separator, subject = line.partition("\t")
        if separator and subject.startswith("Autonomous candidate "):
            promoted_by_subject[subject.removeprefix("Autonomous candidate ").strip()] = commit.strip()

    rows = code_evolution_by_status(radar, PROPOSAL_QUEUE_STATUSES, limit=1000)
    reconciled: list[dict[str, Any]] = []
    for row in rows:
        proposal_id = str(row.get("proposal_id") or "")
        promoted_commit = promoted_by_subject.get(proposal_id)
        if not promoted_commit:
            continue
        parent = run_git(["rev-parse", f"{promoted_commit}^"], root, timeout=30)
        parent_commit = str(parent.get("stdout") or "").strip() if parent["returncode"] == 0 else None
        evaluation = dict(row.get("evaluation") or {})
        evaluation["promotion_reconciliation"] = {
            "at": _utc_now(),
            "reason": "main_commit_found_after_interrupted_status_write",
            "promoted_commit": promoted_commit,
        }
        update_code_evolution_proposal(
            radar,
            proposal_id,
            status="promoted_pending_verification",
            parent_commit=parent_commit,
            candidate_commit=promoted_commit,
            evaluation=evaluation,
            promotion_reason="Reconciled exact autonomous commit after interrupted database status write.",
        )
        work_fingerprint, work_scope = _proposal_work_identity(row, radar)
        task = enqueue_task(
            coord,
            "code_evolution_proposal",
            proposal_id,
            lane=_proposal_lane(row),
            priority=int(row.get("priority") or 0),
            payload={"proposal_id": proposal_id, "category": row.get("category"), "title": row.get("title")},
            reactivate_terminal=True,
            work_fingerprint=work_fingerprint,
            work_scope=work_scope,
        )
        if str(task.get("status") or "") != "superseded_duplicate":
            complete_task(
                coord,
                str(task["task_id"]),
                status="promoted_pending_verification",
                result={"reconciled_commit": promoted_commit},
            )
            enqueue_verification_job(
                coord,
                str(task["task_id"]),
                priority=int(row.get("priority") or 0),
                payload={"proposal_id": proposal_id, "promoted_commit": promoted_commit},
            )
        source_id = str(row.get("source_recommendation_id") or "")
        if source_id.startswith("strategy-owner:") and source_id.endswith(":code"):
            owner_task_id = source_id[len("strategy-owner:") : -len(":code")]
            if _table_exists(radar, "strategy_owner_tasks"):
                radar.execute(
                    """
                    update strategy_owner_tasks
                    set code_proposal_id=?, status='promoted_to_runtime', claimed_by=null, claimed_pid=null,
                        lease_expires_at=null, next_retry_at=null, updated_at=?
                    where task_id=? and status not in ('completed','promoted_to_code','retired_bad_evidence')
                    """,
                    (proposal_id, _utc_now(), owner_task_id),
                )
                radar.commit()
        reconciled.append({"proposal_id": proposal_id, "promoted_commit": promoted_commit})
    return {"reconciled": len(reconciled), "items": reconciled}


def sync_available_work(radar: sqlite3.Connection, coord: sqlite3.Connection, settings: dict) -> dict[str, Any]:
    """Copy durable executable work into the low-contention coordination queue."""

    cfg = _cfg(settings)
    reconciliation = _reconcile_promoted_commits(radar, coord, settings)
    identities_backfilled = _backfill_work_identities(radar, coord)
    queued: list[dict[str, Any]] = []
    duplicate_proposals: list[dict[str, Any]] = []
    for row in code_evolution_by_status(radar, PROPOSAL_QUEUE_STATUSES, limit=int(cfg["queue_batch_size"])):
        payload = row.get("payload") or {}
        category = str(row.get("category") or "unknown")
        lane = _proposal_lane(row)
        work_fingerprint, work_scope = _proposal_work_identity(row, radar)
        item = enqueue_task(
            coord,
            "code_evolution_proposal",
            str(row["proposal_id"]),
            lane=lane,
            priority=int(row.get("priority") or 0),
            payload={
                "proposal_id": row["proposal_id"],
                "category": category,
                "title": row.get("title"),
                "work_scope": work_scope,
            },
            reactivate_terminal=True,
            work_fingerprint=work_fingerprint,
            work_scope=work_scope,
        )
        if str(item.get("status") or "") == "superseded_duplicate":
            duplicate = {
                "proposal_id": str(row["proposal_id"]),
                "canonical_task_id": item.get("canonical_task_id"),
                "work_fingerprint": work_fingerprint,
                "work_scope": work_scope,
            }
            evaluation = dict(row.get("evaluation") or {})
            evaluation["coordination_deduplication"] = {"at": _utc_now(), **duplicate}
            update_code_evolution_proposal(
                radar,
                str(row["proposal_id"]),
                status="superseded_duplicate",
                evaluation=evaluation,
            )
            duplicate_proposals.append(duplicate)
        else:
            queued.append(item)

    if _table_exists(radar, "strategy_owner_tasks"):
        row = radar.execute(
            """
            select task_id,priority,code_proposal_id from strategy_owner_tasks
            where status in ('queued','analyzing','coding','implementation_paused','waiting_quota',
                             'waiting_network','promote_candidate')
              and (next_retry_at is null or next_retry_at<=?)
            order by case when status='implementation_paused' then 0 else 1 end,priority desc,updated_at asc limit 1
            """,
            (_utc_now(),),
        ).fetchone()
        if row:
            work_fingerprint, work_scope = _owner_work_identity("strategy_owner_turn", row)
            item = _enqueue_owner_turn(
                coord, source_kind="strategy_owner_turn", source_id=str(row["task_id"]),
                lane="strategy", priority=int(row["priority"] or 90),
                payload={"title": f"Strategy owner {row['task_id']}", "work_scope": work_scope},
                work_fingerprint=work_fingerprint, work_scope=work_scope,
            )
            if item:
                queued.append(item)

    if _table_exists(radar, "adapter_specs"):
        row = radar.execute(
            """
            select id,priority,title,market_key from adapter_specs
            where status in ('open','planned','proposed','implementation_queued','implementation_queued_retry')
            order by priority desc,created_at asc limit 1
            """
        ).fetchone()
        if row:
            work_fingerprint, work_scope = _owner_work_identity("adapter_owner_turn", row)
            item = _enqueue_owner_turn(
                coord, source_kind="adapter_owner_turn", source_id=str(row["id"]),
                lane="adapter", priority=int(row["priority"] or 80),
                payload={"title": row["title"], "market_key": row["market_key"], "work_scope": work_scope},
                work_fingerprint=work_fingerprint, work_scope=work_scope,
            )
            if item:
                queued.append(item)

    if _table_exists(radar, "market_activation_tasks"):
        row = radar.execute(
            """
            select task_id,priority,adapter_id,market_surface,venue from market_activation_tasks
            where status in ('queued','waiting_source','needs_data_repair','needs_runtime_repair','implementation_paused')
              and (next_retry_at is null or next_retry_at<=?)
            order by priority desc,updated_at asc limit 1
            """,
            (_utc_now(),),
        ).fetchone()
        if row:
            work_fingerprint, work_scope = _owner_work_identity("activation_owner_turn", row)
            item = _enqueue_owner_turn(
                coord, source_kind="activation_owner_turn", source_id=str(row["task_id"]),
                lane="activation", priority=int(row["priority"] or 85),
                payload={
                    "title": f"Activate {row['adapter_id']} for {row['market_surface']}",
                    "venue": row["venue"],
                    "work_scope": work_scope,
                },
                work_fingerprint=work_fingerprint, work_scope=work_scope,
            )
            if item:
                queued.append(item)

    if _table_exists(radar, "llm_recommendations"):
        row = radar.execute(
            """
            select recommendation_id,coalesce(json_extract(payload_json,'$.priority'),50) as priority
            from llm_recommendations
            where status='accepted' order by priority desc,created_at asc limit 1
            """
        ).fetchone()
        if row:
            item = _enqueue_owner_turn(
                coord, source_kind="general_owner_turn", source_id=str(row["recommendation_id"]),
                lane="general", priority=int(row["priority"] or 70),
            )
            if item:
                queued.append(item)

    reconciled_duplicates = reconcile_duplicate_tasks(coord)
    for duplicate in reconciled_duplicates:
        if duplicate.get("source_kind") != "code_evolution_proposal":
            continue
        proposal_id = str(duplicate.get("source_id") or "")
        proposal = get_code_evolution_proposal(radar, proposal_id)
        if not proposal or str(proposal.get("status") or "") not in PROPOSAL_QUEUE_STATUSES:
            continue
        evaluation = dict(proposal.get("evaluation") or {})
        evaluation["coordination_deduplication"] = {
            "at": _utc_now(),
            "canonical_task_id": duplicate.get("canonical_task_id"),
            "canonical_source_id": duplicate.get("canonical_source_id"),
            "work_fingerprint": duplicate.get("work_fingerprint"),
            "work_scope": duplicate.get("work_scope"),
        }
        update_code_evolution_proposal(
            radar,
            proposal_id,
            status="superseded_duplicate",
            evaluation=evaluation,
        )
        duplicate_proposals.append({"proposal_id": proposal_id, **duplicate})

    record_migration(
        coord,
        "radar_backlog_sync",
        {
            "checked_at": _utc_now(),
            "queued_or_refreshed": len(queued),
            "identities_backfilled": identities_backfilled,
            "duplicates_suppressed": len(duplicate_proposals),
        },
    )
    return {
        "queued_or_refreshed": len(queued),
        "tasks": queued,
        "identities_backfilled": identities_backfilled,
        "duplicates_suppressed": len(duplicate_proposals),
        "duplicate_tasks": duplicate_proposals[:50],
        "promotion_reconciliation": reconciliation,
    }


def _worker_settings(
    settings: dict,
    worker_id: str,
    coordination_context: dict[str, Any] | None = None,
) -> dict:
    output = copy.deepcopy(settings)
    output["_codex_worker_execute"] = True
    output["_codex_worker_id"] = worker_id
    output.setdefault("codex_repo_agent", {})["parallel_sessions_enabled"] = True
    output["codex_repo_agent"]["coordination_context"] = coordination_context or {}
    if _cfg(settings).get("defer_full_regression", True):
        output.setdefault("code_evolution", {})["run_full_regression"] = False
    output.setdefault("self_improvement", {})["process_code_changes_in_radar_loop"] = True
    return output


def _proposal_result(result: Any) -> tuple[str | None, str | None]:
    if isinstance(result, list):
        for item in result:
            status, proposal_id = _proposal_result(item)
            if status:
                return status, proposal_id
        return None, None
    if not isinstance(result, dict):
        return None, None
    if result.get("artifact_type") == "code_evolution":
        return str(result.get("status") or ""), str(result.get("proposal_id") or "") or None
    for key in (
        "artifacts", "consumed", "last_cycle", "handled", "self_improvement", "autonomous_builder",
        "strategy_implementation_owner", "adapter_implementation_owner", "market_activation_owner",
    ):
        status, proposal_id = _proposal_result(result.get(key))
        if status:
            return status, proposal_id
    if result.get("proposal_status"):
        return str(result.get("proposal_status") or ""), str(result.get("proposal_id") or "") or None
    return str(result.get("status") or "") or None, str(result.get("proposal_id") or "") or None


def _run_code_proposal(radar: sqlite3.Connection, task: dict, settings: dict) -> Any:
    proposal_id = str((task.get("payload") or {}).get("proposal_id") or task.get("source_id") or "")
    row = get_code_evolution_proposal(radar, proposal_id)
    if not row:
        return {"status": "missing_code_evolution_proposal", "proposal_id": proposal_id}
    proposal_payload = dict(row.get("payload") or {})
    coordination_context = (settings.get("codex_repo_agent") or {}).get("coordination_context")
    if coordination_context:
        proposal_payload["coordination_context"] = coordination_context
    rec = {
        "recommendation_id": row.get("source_recommendation_id") or f"coordination:{proposal_id}",
        "title": row.get("title"),
        "priority": row.get("priority"),
        "payload": proposal_payload,
    }
    return process_code_change_recommendation(radar, rec, settings)


def _proposal_model_usage(proposal_id: str | None) -> dict[str, Any]:
    if not proposal_id:
        return {}
    with closing(connect()) as radar:
        row = get_code_evolution_proposal(radar, proposal_id)
    safety = (row or {}).get("safety") or {}
    generation = safety.get("codex_repo_agent") or safety.get("patch_generation") or {}
    event = generation.get("event_summary") if isinstance(generation.get("event_summary"), dict) else {}
    return {
        "model": generation.get("model_name") or generation.get("model"),
        "reasoning_effort": generation.get("reasoning_effort"),
        "usage": event.get("usage") or generation.get("usage") or {},
        "estimated_cost_usd": generation.get("estimated_cost_usd"),
        "session_id": generation.get("session_id"),
        "resumed": bool(generation.get("resumed")),
    }


def _dispatch(task: dict, settings: dict) -> Any:
    with closing(connect()) as radar:
        kind = str(task.get("source_kind") or "")
        if kind == "code_evolution_proposal":
            return _run_code_proposal(radar, task, settings)
        if kind == "strategy_owner_turn":
            return run_strategy_owner(
                radar, settings, execute_turn=True, cycle_id=f"pool:{task['task_id']}",
                scheduler={"mode": "concurrent_codex_pool"},
            )
        if kind == "adapter_owner_turn":
            return run_adapter_owner(radar, settings)
        if kind == "activation_owner_turn":
            return run_activation_owner(
                radar, settings, execute_turn=True, cycle_id=f"pool:{task['task_id']}",
                scheduler={"mode": "concurrent_codex_pool"},
            )
        if kind == "general_owner_turn":
            improvement = run_auto_improvement(radar, settings, include_code_changes=True)
            code_attempted = any(
                str(item.get("task_type") or "") == "code_change"
                for item in (improvement.get("consumed") or []) if isinstance(item, dict)
            )
            builder = {"status": "deferred_for_targeted_code_attempt"} if code_attempted else run_autonomous_builder(
                settings=settings, conn=radar, force=False
            )
            return {"status": "completed", "self_improvement": improvement, "autonomous_builder": builder}
        return {"status": "unsupported_coordination_task", "source_kind": kind}


def _lease_heartbeat(stop: threading.Event, db_path: pathlib.Path, task: dict, worker_id: str, cfg: dict) -> None:
    interval = max(5, int(cfg.get("worker_heartbeat_seconds", 30)))
    while not stop.wait(interval):
        try:
            with closing(connect_coordination(db_path)) as coord:
                heartbeat_worker(
                    coord, worker_id, preferred_lane=str(task.get("lane") or "general"), pid=os.getpid(),
                    state="coding", details={"task_id": task.get("task_id"), "source_kind": task.get("source_kind")},
                )
                renew_task_lease(
                    coord, str(task["task_id"]), worker_id, str(task["claim_token"]),
                    lease_seconds=int(cfg.get("task_lease_seconds", 2700)), state="coding",
                )
        except sqlite3.Error:
            continue


def _run_one_worker_task(worker: dict, settings: dict) -> dict[str, Any]:
    cfg = _cfg(settings)
    worker_id = str(worker.get("worker_id") or f"codex-{uuid.uuid4().hex[:8]}")
    preferences = list(worker.get("preferred_lanes") or ["general"])
    minute = int(time.time() // 60)
    if len(preferences) > 1 and minute % 3 == 2:
        preferred = str(preferences[1 + ((minute // 3) % (len(preferences) - 1))])
    else:
        preferred = str(preferences[0] if preferences else "general")
    db_path = coordination_db_path(settings)
    with closing(connect_coordination(db_path)) as coord:
        heartbeat_worker(coord, worker_id, preferred_lane=preferred, pid=os.getpid(), state="claiming")
        task = claim_task(
            coord, worker_id, preferred_lane=preferred, pid=os.getpid(),
            lease_seconds=int(cfg.get("task_lease_seconds", 2700)),
        )
        if task:
            task["coordination_context"] = peer_work_context(
                coord, task_id=str(task.get("task_id") or ""), limit=16
            )
    if not task:
        with closing(connect_coordination(db_path)) as coord:
            heartbeat_worker(coord, worker_id, preferred_lane=preferred, pid=os.getpid(), state="idle")
        return {"worker_id": worker_id, "status": "idle"}

    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_lease_heartbeat, args=(stop, db_path, task, worker_id, cfg), daemon=True
    )
    heartbeat.start()
    started = time.monotonic()
    try:
        result = _dispatch(
            task,
            _worker_settings(settings, worker_id, task.get("coordination_context")),
        )
        status, proposal_id = _proposal_result(result)
        elapsed = round(time.monotonic() - started, 3)
        model_usage = _proposal_model_usage(proposal_id)
        if status == "implementation_paused" and proposal_id:
            with closing(connect()) as radar:
                paused = get_code_evolution_proposal(radar, proposal_id)
                evaluation = dict((paused or {}).get("evaluation") or {})
                if str(evaluation.get("reason") or "") == "promotion_overlap_requires_repair":
                    repair = _prepare_repair_worktree(paused or {}, _release_from_row(paused or {}))
                    evaluation["overlap_repair_worktree"] = repair
                    update_code_evolution_proposal(
                        radar, proposal_id, evaluation=evaluation,
                        parent_commit=repair.get("parent_commit") if repair.get("prepared") else None,
                    )
                    if repair.get("prepared"):
                        radar.execute(
                            "update code_evolution_proposals set candidate_commit=null where proposal_id=?",
                            (proposal_id,),
                        )
                        radar.commit()
        with closing(connect_coordination(db_path)) as coord:
            if status in PENDING_VERIFICATION_STATUSES:
                enqueue_verification_job(
                    coord, str(task["task_id"]), priority=int(task.get("priority") or 0),
                    payload={"proposal_id": proposal_id, "worker_id": worker_id},
                )
                complete_task(
                    coord, str(task["task_id"]), worker_id=worker_id, claim_token=str(task["claim_token"]),
                    status="promoted_pending_verification", result={"elapsed_seconds": elapsed, "result": result},
                )
            elif status in RETRYABLE_STATUSES:
                requeue_task(
                    coord, str(task["task_id"]), worker_id=worker_id, claim_token=str(task["claim_token"]),
                    delay_seconds=60, error=status,
                )
            else:
                complete_task(
                    coord, str(task["task_id"]), worker_id=worker_id, claim_token=str(task["claim_token"]),
                    status="completed", result={"elapsed_seconds": elapsed, "result": result},
                )
            heartbeat_worker(
                coord, worker_id, preferred_lane=preferred, pid=os.getpid(), state="idle",
                details={
                    "last_task_id": task["task_id"], "last_status": status,
                    "elapsed_seconds": elapsed, "model_usage": model_usage,
                },
            )
        return {
            "worker_id": worker_id, "status": status or "completed", "proposal_id": proposal_id,
            "task_id": task["task_id"], "source_kind": task["source_kind"], "elapsed_seconds": elapsed,
            "model_usage": model_usage,
        }
    except Exception as exc:  # noqa: BLE001 - durable queue must survive arbitrary owner failures.
        elapsed = round(time.monotonic() - started, 3)
        with closing(connect_coordination(db_path)) as coord:
            requeue_task(
                coord, str(task["task_id"]), worker_id=worker_id, claim_token=str(task["claim_token"]),
                delay_seconds=120, error=f"{type(exc).__name__}:{str(exc)[:500]}",
            )
            heartbeat_worker(
                coord, worker_id, preferred_lane=preferred, pid=os.getpid(), state="error",
                details={"task_id": task["task_id"], "error": str(exc)[:500]},
            )
        return {"worker_id": worker_id, "status": "requeued_after_exception", "error": str(exc)[:1000], "elapsed_seconds": elapsed}
    finally:
        stop.set()
        heartbeat.join(timeout=2)


def run_worker_once(worker: dict, settings: dict) -> dict[str, Any]:
    """Keep a lane productive when owner bookkeeping finishes almost immediately."""

    cfg = _cfg(settings)
    max_hops = max(1, int(cfg.get("max_quick_task_hops", 8)))
    quick_seconds = max(0.1, float(cfg.get("quick_task_seconds", 5)))
    completed: list[dict[str, Any]] = []
    for _index in range(max_hops):
        result = _run_one_worker_task(worker, settings)
        if result.get("status") == "idle":
            if not completed:
                return result
            final = dict(completed[-1])
            final["tasks_processed_this_turn"] = len(completed)
            final["quick_handoffs"] = len(completed)
            final["drained_to_idle"] = True
            return final
        completed.append(result)
        status = str(result.get("status") or "")
        elapsed = float(result.get("elapsed_seconds") or 0.0)
        if (
            elapsed > quick_seconds
            or status in PENDING_VERIFICATION_STATUSES
            or status in RETRYABLE_STATUSES
            or status in {"requeued_after_exception", "worker_exception"}
        ):
            break
    final = dict(completed[-1]) if completed else {"status": "idle"}
    final["tasks_processed_this_turn"] = len(completed)
    final["quick_handoffs"] = max(0, len(completed) - 1)
    return final


def _release_from_row(row: dict) -> CandidateRelease | None:
    evaluation = row.get("evaluation") if isinstance(row.get("evaluation"), dict) else {}
    metadata = evaluation.get("release") if isinstance(evaluation.get("release"), dict) else {}
    worktree = str(row.get("worktree_path") or metadata.get("worktree_path") or "")
    app_worktree = str(metadata.get("app_worktree_path") or "")
    if not worktree or not app_worktree:
        return None
    return CandidateRelease(
        proposal_id=str(row.get("proposal_id") or ""), parent_commit=str(row.get("parent_commit") or metadata.get("parent_commit") or ""),
        branch_name=str(row.get("branch_name") or metadata.get("branch_name") or ""), worktree_path=worktree,
        app_worktree_path=app_worktree, candidate_commit=row.get("candidate_commit"), status=str(row.get("status") or ""),
    )


def _run_full_regression(app_root: pathlib.Path, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
            cwd=str(app_root), capture_output=True, text=True, timeout=timeout, check=False,
        )
        return {
            "passed": completed.returncode == 0, "returncode": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-8000:], "stderr_tail": (completed.stderr or "")[-8000:],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "passed": False, "returncode": 124, "reason": "full_regression_timeout",
            "stdout_tail": str(exc.stdout or "")[-8000:], "stderr_tail": str(exc.stderr or "")[-8000:],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }


def _acquire_promotion_lease(coord: sqlite3.Connection, worker_id: str, cfg: dict, details: dict) -> dict | None:
    deadline = time.monotonic() + int(cfg.get("promotion_lease_seconds", 180))
    while time.monotonic() < deadline:
        lease = acquire_resource_lease(
            coord, "main_promotion", worker_id, pid=os.getpid(),
            lease_seconds=int(cfg.get("promotion_lease_seconds", 180)), details=details,
        )
        if lease:
            return lease
        time.sleep(0.25)
    return None


def _sync_verified_runtime(root: pathlib.Path | None, promoted_commit: str) -> dict[str, Any]:
    """Tag the verified commit while pushing the current, possibly newer, runtime head."""

    if root is None:
        return {"ok": False, "reason": "ambiguous_repo_root"}
    runtime_head = current_commit(root)
    if not runtime_head or not promoted_commit:
        return {"ok": False, "reason": "missing_runtime_or_promoted_commit"}
    ancestor = run_git(["merge-base", "--is-ancestor", promoted_commit, runtime_head], root, timeout=30)
    if ancestor["returncode"] != 0:
        return {
            "ok": False,
            "reason": "verified_commit_not_in_current_runtime",
            "runtime_head": runtime_head,
            "ancestor_check": ancestor,
        }
    champion = update_champion_latest(root, promoted_commit)
    push = None
    tag_push = None
    if champion.get("ok"):
        push = run_git(["push", "origin", f"{runtime_head}:refs/heads/main"], root, timeout=120)
        if push["returncode"] == 0:
            tag_push = run_git(["push", "--force", "origin", "refs/tags/champion/latest"], root, timeout=120)
    return {
        "ok": bool(champion.get("ok") and push and push.get("returncode") == 0),
        "runtime_head": runtime_head,
        "verified_commit": promoted_commit,
        "champion": champion,
        "push": push,
        "tag_push": tag_push,
    }


def _cleanup_verified_worktree(
    release: CandidateRelease | None,
    *,
    db_path: pathlib.Path,
    worker_id: str,
    cfg: dict,
    proposal_id: str,
) -> dict[str, Any]:
    if release is None:
        return {"ok": True, "reason": "no_candidate_worktree"}
    with closing(connect_coordination(db_path)) as coord:
        lease = _acquire_promotion_lease(
            coord, worker_id, cfg, {"proposal_id": proposal_id, "stage": "worktree_cleanup"}
        )
        if not lease:
            return {"ok": False, "reason": "worktree_cleanup_lease_timeout"}
        try:
            return cleanup_worktree(release, ROOT)
        finally:
            release_resource_lease(coord, "main_promotion", worker_id, str(lease.get("lease_token") or ""))


def _prepare_repair_worktree(row: dict, release: CandidateRelease | None) -> dict[str, Any]:
    if release is None or not pathlib.Path(release.worktree_path).exists():
        return {"prepared": False, "reason": "candidate_worktree_missing"}
    root = repo_root(ROOT)
    if root is None:
        return {"prepared": False, "reason": "ambiguous_repo_root"}
    head = current_commit(root)
    reset = run_git(["reset", "--hard", str(head or "main")], pathlib.Path(release.worktree_path), timeout=120)
    if reset["returncode"] != 0:
        return {"prepared": False, "reason": "repair_worktree_reset_failed", "reset": reset}
    return {"prepared": True, "parent_commit": head, "reset": reset}


def run_verifier_once(index: int, settings: dict) -> dict[str, Any]:
    cfg = _cfg(settings)
    worker_id = f"verifier-{index + 1}"
    db_path = coordination_db_path(settings)
    with closing(connect_coordination(db_path)) as coord:
        job = claim_verification_job(
            coord, worker_id, pid=os.getpid(), lease_seconds=int(cfg.get("verification_timeout_seconds", 900)) + 120,
        )
    if not job:
        return {"worker_id": worker_id, "status": "idle"}
    proposal_id = str((job.get("payload") or {}).get("proposal_id") or "")
    if str(job.get("verification_kind") or "") == "git_sync":
        promoted_commit = str((job.get("payload") or {}).get("promoted_commit") or "")
        with closing(connect_coordination(db_path)) as coord:
            lease = _acquire_promotion_lease(
                coord, worker_id, cfg, {"proposal_id": proposal_id, "stage": "git_sync"}
            )
            sync_result = {"ok": False, "reason": "main_promotion_lease_timeout"}
            if lease:
                try:
                    sync_result = _sync_verified_runtime(repo_root(ROOT), promoted_commit)
                finally:
                    release_resource_lease(coord, "main_promotion", worker_id, str(lease.get("lease_token") or ""))
            passed = bool(sync_result.get("ok"))
            finish_verification_job(
                coord, str(job["job_id"]), worker_id=worker_id, claim_token=str(job["claim_token"]),
                status="synced" if passed else "requeued", result=sync_result,
                task_status="verified" if passed else "promoted_pending_verification",
                requeue_after_seconds=None if passed else 60,
            )
        cleanup = None
        if passed:
            with closing(connect()) as radar:
                row = get_code_evolution_proposal(radar, proposal_id)
                if row:
                    evaluation = dict(row.get("evaluation") or {})
                    evaluation["runtime_sync"] = sync_result
                    update_code_evolution_proposal(
                        radar, proposal_id, status="promoted", evaluation=evaluation,
                        promotion_reason="Focused gates, asynchronous full regression, and runtime sync passed.",
                    )
            cleanup = _cleanup_verified_worktree(
                _release_from_row(row) if row else None,
                db_path=db_path, worker_id=worker_id, cfg=cfg, proposal_id=proposal_id,
            )
        return {
            "worker_id": worker_id, "status": "git_synced" if passed else "git_sync_retry",
            "proposal_id": proposal_id, "cleanup": cleanup,
        }
    with closing(connect()) as radar:
        row = get_code_evolution_proposal(radar, proposal_id)
    if not row:
        with closing(connect_coordination(db_path)) as coord:
            finish_verification_job(
                coord, str(job["job_id"]), worker_id=worker_id, claim_token=str(job["claim_token"]),
                status="failed_missing_proposal", result={"proposal_id": proposal_id}, task_status="archived_failed",
            )
        return {"worker_id": worker_id, "status": "failed_missing_proposal", "proposal_id": proposal_id}

    release = _release_from_row(row)
    app_root = pathlib.Path(release.app_worktree_path) if release else ROOT
    verification = _run_full_regression(app_root, int(cfg.get("verification_timeout_seconds", 900)))
    evaluation = dict(row.get("evaluation") or {})
    evaluation["async_full_verification"] = {**verification, "checked_at": _utc_now(), "worker_id": worker_id}
    if verification.get("passed"):
        promotion = evaluation.get("promotion") if isinstance(evaluation.get("promotion"), dict) else {}
        promoted_commit = str(promotion.get("promoted_commit") or row.get("candidate_commit") or "")
        with closing(connect_coordination(db_path)) as coord:
            lease = _acquire_promotion_lease(coord, worker_id, cfg, {"proposal_id": proposal_id, "stage": "verification"})
            sync_result = {"ok": False, "reason": "main_promotion_lease_timeout"}
            if lease:
                try:
                    sync_result = _sync_verified_runtime(repo_root(ROOT), promoted_commit)
                finally:
                    release_resource_lease(coord, "main_promotion", worker_id, str(lease.get("lease_token") or ""))
            evaluation["async_full_verification"]["runtime_sync"] = sync_result
            if not sync_result.get("ok"):
                finish_verification_job(
                    coord, str(job["job_id"]), worker_id=worker_id, claim_token=str(job["claim_token"]),
                    status="verified_pending_sync", result=evaluation["async_full_verification"],
                    task_status="promoted_pending_verification",
                )
                enqueue_verification_job(
                    coord, str(job["task_id"]), verification_kind="git_sync",
                    priority=int(job.get("priority") or 0),
                    payload={"proposal_id": proposal_id, "promoted_commit": promoted_commit},
                )
                with closing(connect()) as radar:
                    update_code_evolution_proposal(
                        radar, proposal_id, status="promoted_pending_verification", evaluation=evaluation,
                        promotion_reason="Full regression passed; runtime Git sync is retrying.",
                    )
                return {
                    "worker_id": worker_id, "status": "verification_passed_sync_queued",
                    "proposal_id": proposal_id,
                }
            with closing(connect()) as radar:
                update_code_evolution_proposal(
                    radar, proposal_id, status="promoted", evaluation=evaluation,
                    promotion_reason="Focused gates and asynchronous full regression passed.",
                )
            finish_verification_job(
                coord, str(job["job_id"]), worker_id=worker_id, claim_token=str(job["claim_token"]),
                status="verified", result=evaluation["async_full_verification"], task_status="verified",
            )
        cleanup = _cleanup_verified_worktree(
            release, db_path=db_path, worker_id=worker_id, cfg=cfg, proposal_id=proposal_id
        )
        return {"worker_id": worker_id, "status": "verified", "proposal_id": proposal_id, "cleanup": cleanup}

    repair = _prepare_repair_worktree(row, release)
    evaluation["reason"] = "async_full_regression_failed_repair_required"
    evaluation["repair_worktree"] = repair
    with closing(connect()) as radar:
        update_code_evolution_proposal(
            radar, proposal_id, status="implementation_paused", tests={**(row.get("tests") or {}), "async_full_regression": verification},
            evaluation=evaluation, parent_commit=repair.get("parent_commit") if repair.get("prepared") else None,
        )
        if repair.get("prepared"):
            radar.execute(
                "update code_evolution_proposals set candidate_commit=null where proposal_id=?", (proposal_id,)
            )
            radar.commit()
    with closing(connect_coordination(db_path)) as coord:
        finish_verification_job(
            coord, str(job["job_id"]), worker_id=worker_id, claim_token=str(job["claim_token"]),
            status="failed_needs_repair", result={**verification, "repair": repair}, task_status="repairing_post_promotion",
        )
        requeue_task(
            coord, str(job["task_id"]), delay_seconds=30,
            error="async_full_regression_failed_repair_required", status="requeued",
        )
    return {"worker_id": worker_id, "status": "repairing_post_promotion", "proposal_id": proposal_id, "verification": verification}


def _write_report(report: dict[str, Any]) -> dict[str, Any]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    summary = report.get("summary") or {}
    lines = [
        "# Concurrent Codex Worker Pool", "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Workers: `{len(summary.get('workers') or [])}`",
        f"- Queue depth: `{summary.get('queue_depth', 0)}`",
        f"- Task statuses: `{summary.get('task_statuses', {})}`",
        f"- Verification statuses: `{summary.get('verification_statuses', {})}`",
        f"- Worker results: `{report.get('worker_results', [])}`",
        f"- Verification results: `{report.get('verification_results', [])}`",
    ]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def pool_summary(settings: dict | None = None) -> dict[str, Any]:
    effective = settings or load_settings(None)
    try:
        with closing(connect_coordination(coordination_db_path(effective))) as coord:
            return coordination_summary(coord)
    except sqlite3.Error as exc:
        return {"status": "coordination_database_error", "error": str(exc)}


def run_once(settings: dict) -> dict[str, Any]:
    cfg = _cfg(settings)
    if not cfg.get("enabled", True):
        return _write_report({"generated_at": _utc_now(), "status": "disabled"})
    if settings.get("allow_live_trading"):
        return _write_report({"generated_at": _utc_now(), "status": "blocked_live_trading"})
    db_path = coordination_db_path(settings)
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(target=_runtime_heartbeat, args=(heartbeat_stop, settings), daemon=True)
    heartbeat_thread.start()
    try:
        try:
            with closing(connect()) as radar, closing(connect_coordination(db_path)) as coord:
                sync = sync_available_work(radar, coord, settings)
        except sqlite3.Error as exc:
            return _write_report({
                "generated_at": _utc_now(), "status": "database_busy_retry_later", "error": str(exc)
            })

        workers = list(cfg.get("worker_roles") or [])[: int(cfg.get("max_workers", 3))]
        while len(workers) < int(cfg.get("max_workers", 3)):
            workers.append({"worker_id": f"general-codex-{len(workers) + 1}", "preferred_lanes": ["general"]})
        verifier_count = max(0, int(cfg.get("max_verifiers", 2)))
        worker_results: list[dict[str, Any]] = []
        verification_results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, len(workers) + verifier_count)) as executor:
            futures = {executor.submit(run_worker_once, worker, settings): "worker" for worker in workers}
            futures.update({executor.submit(run_verifier_once, index, settings): "verifier" for index in range(verifier_count)})
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = {"status": "worker_exception", "error": f"{type(exc).__name__}:{str(exc)[:1000]}"}
                (verification_results if futures[future] == "verifier" else worker_results).append(result)
        with closing(connect_coordination(db_path)) as coord:
            summary = coordination_summary(coord)
        return _write_report({
            "generated_at": _utc_now(), "status": "ok", "live_trading_allowed": False,
            "sync": sync, "worker_results": worker_results, "verification_results": verification_results,
            "summary": summary,
        })
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the concurrent repository-aware Codex worker pool.")
    parser.add_argument("--config", type=pathlib.Path, default=None)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    for index in range(max(1, args.iterations)):
        report = run_once(settings)
        print(
            f"Codex worker pool status={report.get('status')} "
            f"queue={((report.get('summary') or {}).get('queue_depth', 0))} "
            f"workers={len(report.get('worker_results') or [])}"
        )
        if index < args.iterations - 1:
            time.sleep(max(1, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
