"""Persistent owner for Strategy Lab contracts, evidence, and code promotion."""

from __future__ import annotations

import datetime as dt
import ctypes
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import uuid
from collections import Counter
from typing import Any

from codex_repo_agent import run_structured_codex_turn
from code_evolution import process_code_change_recommendation
from storage import (
    ROOT,
    RUNS_DIR,
    link_recommendation_artifact,
    update_llm_recommendation_status,
)
from strategy_lab import ingest_strategy_lab_recommendation
from temporal_memory import retrieve_role_memories, upsert_memory_fact, upsert_memory_link


REPORT_JSON = RUNS_DIR / "strategy_implementation_owner.json"
REPORT_MD = RUNS_DIR / "strategy_implementation_owner.md"
ACTIVE_TASK_STATUSES = {
    "queued", "claimed", "analyzing", "contract_validated", "coding", "host_validation",
    "promoted_to_runtime", "active_testing", "monitoring_evidence", "waiting_data",
    "waiting_route", "waiting_quota", "waiting_network", "implementation_paused",
    "promote_candidate",
}
CLAIMABLE_STATUSES = {
    "queued", "analyzing", "contract_validated", "coding", "waiting_data", "waiting_route",
    "waiting_quota", "waiting_network", "implementation_paused", "promote_candidate",
}
TERMINAL_STATUSES = {"completed", "promoted_to_code", "retired_bad_evidence", "superseded_duplicate"}
DECISIONS = {
    "materialize_experiment", "implement_code", "wait_for_data", "wait_for_route",
    "monitor_evidence", "retire", "completed",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(value: Any) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value or _utc_now()).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _pid_alive(pid: Any) -> bool:
    try:
        numeric = int(pid or 0)
        if numeric <= 0:
            return False
        if os.name == "nt":
            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, numeric)
            if not process:
                return False
            try:
                exit_code = ctypes.c_ulong()
                return bool(ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))) and exit_code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        os.kill(numeric, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _cfg(settings: dict) -> dict:
    defaults = {
        "enabled": True,
        "lease_seconds": 2400,
        "retry_backoff_seconds": 300,
        "memory_context_chars": 70000,
        "memory_retrieval_limit": 80,
        "salvage_invalid_backlog": True,
        "salvage_limit_per_cycle": 12,
        "stalled_testing_hours": 24,
        "task_worktree_dir": str(RUNS_DIR / "strategy_owner_worktrees"),
        "report_limit": 100,
    }
    return {**defaults, **(settings.get("strategy_implementation_owner") or {})}


def _priority(value: Any, default: int = 80) -> int:
    labels = {"critical": 100, "urgent": 95, "high": 90, "medium": 60, "low": 35}
    if isinstance(value, str) and value.lower() in labels:
        return labels[value.lower()]
    try:
        return max(1, min(100, int(float(value))))
    except (TypeError, ValueError):
        return default


def _normal_text(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values).lower()
    tokens = re.findall(r"[a-z][a-z0-9_]{2,}", text)
    stop = {"strategy", "experiment", "paper", "market", "trade", "propose", "candidate", "test", "using"}
    return " ".join(sorted({token for token in tokens if token not in stop})[:80])


def _dedupe_key(payload: dict, experiment: dict | None = None) -> str:
    contract = payload.get("strategy_lab_experiment") if isinstance(payload.get("strategy_lab_experiment"), dict) else {}
    logic = contract.get("strategy_logic") or payload.get("strategy_logic") or (experiment or {}).get("strategy_logic") or {}
    hypothesis = contract.get("hypothesis") or payload.get("hypothesis") or payload.get("rationale") or (experiment or {}).get("hypothesis")
    material = {
        "logic": logic,
        "hypothesis_terms": _normal_text(hypothesis, payload.get("title")),
        "market": payload.get("market_key") or payload.get("signal_key"),
    }
    return "strategy-owner:" + hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def _task_id(dedupe_key: str) -> str:
    return "strategy-owner-" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:20]


def _row_dict(row: sqlite3.Row | None) -> dict:
    if not row:
        return {}
    item = dict(row)
    for source, target, default in (
        ("acceptance_json", "acceptance", {}),
        ("dependency_json", "dependencies", {}),
        ("memory_ids_json", "memory_ids", []),
        ("last_error_json", "last_error", {}),
        ("last_result_json", "last_result", {}),
        ("recovery_journal_json", "recovery_journal", []),
    ):
        item[target] = _json(item.get(source), default)
    return item


def enqueue_recommendation(conn: sqlite3.Connection, rec: dict, settings: dict) -> dict:
    """Create the durable downstream artifact before a recommendation is handled."""

    payload = dict(rec.get("payload") or {})
    dedupe = _dedupe_key(payload)
    task_id = _task_id(dedupe)
    now = _utc_now()
    hypothesis = str(
        (payload.get("strategy_lab_experiment") or {}).get("hypothesis")
        if isinstance(payload.get("strategy_lab_experiment"), dict)
        else ""
    ) or str(payload.get("hypothesis") or payload.get("rationale") or rec.get("rationale") or rec.get("title") or "")
    existing = conn.execute(
        "select task_id, status from strategy_owner_tasks where dedupe_key = ?",
        (dedupe,),
    ).fetchone()
    conn.execute(
        """
        insert into strategy_owner_tasks (
            task_id, created_at, updated_at, dedupe_key, objective_type, priority, status,
            strategy_lab_id, strategy_lab_version, source_recommendation_id, hypothesis,
            acceptance_json, dependency_json
        ) values (?, ?, ?, ?, 'materialize_hypothesis', ?, 'queued', ?, ?, ?, ?, ?, ?)
        on conflict(dedupe_key) do update set
            updated_at = excluded.updated_at,
            priority = max(strategy_owner_tasks.priority, excluded.priority),
            source_recommendation_id = coalesce(strategy_owner_tasks.source_recommendation_id, excluded.source_recommendation_id)
        """,
        (
            task_id, now, now, dedupe, _priority(payload.get("priority", rec.get("priority"))),
            (payload.get("strategy_lab_experiment") or {}).get("strategy_lab_id")
            if isinstance(payload.get("strategy_lab_experiment"), dict) else None,
            (payload.get("strategy_lab_experiment") or {}).get("version")
            if isinstance(payload.get("strategy_lab_experiment"), dict) else None,
            rec.get("recommendation_id"), hypothesis[:8000],
            json.dumps(payload.get("acceptance_criteria") or {}, sort_keys=True),
            json.dumps({"source_payload": payload}, sort_keys=True, default=str),
        ),
    )
    action_status = "linked_existing" if existing else "created"
    effective_task_id = str(existing["task_id"] if existing else task_id)
    link_recommendation_artifact(
        conn,
        rec.get("recommendation_id"),
        "strategy_owner_task",
        effective_task_id,
        "owned_by",
        {"dedupe_key": dedupe, "action_status": action_status},
    )
    if rec.get("recommendation_id"):
        update_llm_recommendation_status(
            conn,
            str(rec["recommendation_id"]),
            "linked_existing_task" if existing else "owner_queued",
        )
    else:
        conn.commit()
    return {
        "action_status": action_status,
        "artifact": "strategy_owner_task",
        "task_id": effective_task_id,
        "status": str(existing["status"] if existing else "queued"),
    }


def _experiment_payload(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    for key in (
        "strategy_logic_json", "original_strategy_logic_json", "compiled_strategy_logic_json",
        "compile_diagnostics_json", "data_requirements_json", "risk_gates_json",
        "promotion_rules_json", "evaluation_json", "novelty_details_json",
    ):
        item[key.removesuffix("_json")] = _json(item.get(key), {})
    return item


def _enqueue_experiment_repair(conn: sqlite3.Connection, experiment: dict, objective_type: str) -> bool:
    payload = {
        "title": f"Repair Strategy Lab {experiment.get('strategy_lab_id')}",
        "hypothesis": experiment.get("hypothesis"),
        "market_key": experiment.get("strategy_lab_id"),
        "strategy_logic": experiment.get("original_strategy_logic") or experiment.get("strategy_logic"),
    }
    dedupe = _dedupe_key(payload, experiment)
    task_id = _task_id(dedupe)
    now = _utc_now()
    cursor = conn.execute(
        """
        insert or ignore into strategy_owner_tasks (
            task_id, created_at, updated_at, dedupe_key, objective_type, priority, status,
            strategy_lab_id, strategy_lab_version, source_recommendation_id, hypothesis,
            acceptance_json, dependency_json
        ) values (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, '{}', ?)
        """,
        (
            task_id, now, now, dedupe, objective_type,
            95 if experiment.get("status") == "promote_candidate" else 84,
            experiment.get("strategy_lab_id"), experiment.get("version"),
            experiment.get("source_recommendation_id"), str(experiment.get("hypothesis") or "")[:8000],
            json.dumps({"source_experiment": experiment}, sort_keys=True, default=str),
        ),
    )
    conn.commit()
    return bool(cursor.rowcount)


def _salvage_score(experiment: dict) -> tuple[float, str]:
    status_rank = {
        "promote_candidate": 400.0,
        "needs_data": 300.0,
        "needs_contract_revision": 290.0,
        "quarantined_surface_policy": 280.0,
        "needs_more_evidence": 260.0,
        "active_testing": 250.0,
        "proposed": 180.0,
        "rejected_invalid": 100.0,
    }
    evaluation = experiment.get("evaluation") if isinstance(experiment.get("evaluation"), dict) else {}
    diagnostics = experiment.get("compile_diagnostics") if isinstance(experiment.get("compile_diagnostics"), dict) else {}
    evidence_count = max(
        float(evaluation.get("valid_label_count") or 0),
        float((evaluation.get("outcomes") or {}).get("count") or 0) if isinstance(evaluation.get("outcomes"), dict) else 0,
        float(diagnostics.get("source_candidate_count") or 0),
        float(diagnostics.get("scope_match_count") or 0) * 2,
    )
    repairable = 25.0 if diagnostics.get("nearest_candidates") or diagnostics.get("missing_features") else 0.0
    return status_rank.get(str(experiment.get("status")), 0.0) + min(100.0, evidence_count) + repairable, str(experiment.get("updated_at") or "")


def _needs_zero_output_diagnosis(experiment: dict) -> bool:
    """Return true when a compiled experiment sees its universe but emits nothing."""

    if str(experiment.get("status") or "") != "needs_more_evidence":
        return False
    evaluation = experiment.get("evaluation") if isinstance(experiment.get("evaluation"), dict) else {}
    diagnostic = evaluation.get("generation_diagnostic") if isinstance(evaluation.get("generation_diagnostic"), dict) else {}
    feasibility = diagnostic.get("feasibility") if isinstance(diagnostic.get("feasibility"), dict) else {}
    universe_matches = int(
        feasibility.get("universe_match_count")
        or diagnostic.get("universe_match_count")
        or 0
    )
    generated = int(diagnostic.get("generated_candidate_count") or 0)
    return universe_matches > 0 and generated == 0


def _backfill_artifact_lifecycle(conn: sqlite3.Connection) -> dict:
    """Repair historical recommendation states and build explicit artifact lineage."""

    linked = 0
    reopened = 0
    status_repaired = 0
    sources = (
        (
            "strategy_lab_experiments", "source_recommendation_id", "strategy_lab_experiment",
            "strategy_lab_id", "materialized_as",
        ),
        (
            "code_evolution_proposals", "source_recommendation_id", "code_evolution_proposal",
            "proposal_id", "materialized_as",
        ),
        (
            "strategy_owner_tasks", "source_recommendation_id", "strategy_owner_task",
            "task_id", "owned_by",
        ),
        (
            "agent_specs", "source_recommendation_id", "agent_spec",
            "agent_id", "spawned_as",
        ),
    )
    tables = {str(row[0]) for row in conn.execute("select name from sqlite_master where type='table'")}
    for table, source_column, artifact_type, id_column, relationship in sources:
        if table not in tables:
            continue
        rows = conn.execute(
            f"select {source_column} as recommendation_id,{id_column} as artifact_id from {table} "
            f"where {source_column} is not null and trim({source_column}) != ''"
        ).fetchall()
        for row in rows:
            existing = conn.execute(
                """select 1 from recommendation_artifact_links where recommendation_id=?
                   and artifact_type=? and artifact_id=? and relationship=?""",
                (str(row["recommendation_id"]), artifact_type, str(row["artifact_id"]), relationship),
            ).fetchone()
            if existing is None:
                link_recommendation_artifact(
                    conn, row["recommendation_id"], artifact_type, row["artifact_id"], relationship,
                    {"backfilled": True},
                )
                linked += 1

    rows = conn.execute(
        """
        select recommendation_id,status from llm_recommendations
        where action='propose_strategy_lab_experiment'
          and status in ('auto_executed','owner_queued','linked_existing_task')
        """
    ).fetchall()
    for row in rows:
        artifacts = conn.execute(
            """select artifact_type from recommendation_artifact_links
               where recommendation_id=? order by updated_at desc""",
            (row["recommendation_id"],),
        ).fetchall()
        kinds = {str(item["artifact_type"]) for item in artifacts}
        if "strategy_lab_experiment" in kinds:
            desired = "experiment_materialized"
        elif "strategy_owner_task" in kinds:
            desired = "owner_queued"
        else:
            desired = "accepted"
            reopened += int(row["status"] != desired)
        if row["status"] != desired:
            conn.execute(
                "update llm_recommendations set status=? where recommendation_id=?",
                (desired, row["recommendation_id"]),
            )
            status_repaired += 1
    conn.commit()
    return {"artifact_links_backfilled": linked, "statuses_repaired": status_repaired, "recommendations_reopened": reopened}


def _consolidate_duplicate_owner_tasks(conn: sqlite3.Connection) -> dict:
    """Collapse queued repair work by canonical program structure, preserving every lineage edge."""

    rows = conn.execute(
        """
        select t.task_id,t.priority,t.created_at,t.source_recommendation_id,t.strategy_lab_id,
               e.novelty_signature,e.compiled_strategy_logic_json,e.original_strategy_logic_json,
               e.source_surface,e.hypothesis
        from strategy_owner_tasks t
        join strategy_lab_experiments e on e.strategy_lab_id=t.strategy_lab_id
        where t.status='queued' and t.code_proposal_id is null and t.codex_session_id is null
        order by t.priority desc,t.created_at asc
        """
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        signature = str(row["novelty_signature"] or "").strip()
        if not signature:
            logic = _json(row["compiled_strategy_logic_json"], {}) or _json(row["original_strategy_logic_json"], {})
            material = {
                "logic": logic,
                "surface": str(row["source_surface"] or ""),
                "hypothesis_terms": _normal_text(row["hypothesis"]) if not logic else "",
            }
            signature = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        groups.setdefault(signature, []).append(row)
    superseded = 0
    for items in groups.values():
        if len(items) < 2:
            continue
        canonical = items[0]
        for duplicate in items[1:]:
            conn.execute(
                """update strategy_owner_tasks set status='superseded_duplicate',completed_at=?,updated_at=?,
                   last_result_json=? where task_id=?""",
                (
                    _utc_now(), _utc_now(),
                    json.dumps({"canonical_task_id": canonical["task_id"], "reason": "canonical_program_structure"}, sort_keys=True),
                    duplicate["task_id"],
                ),
            )
            if duplicate["source_recommendation_id"]:
                link_recommendation_artifact(
                    conn, duplicate["source_recommendation_id"], "strategy_owner_task",
                    canonical["task_id"], "deduplicated_into",
                    {"superseded_task_id": duplicate["task_id"]},
                )
                update_llm_recommendation_status(conn, duplicate["source_recommendation_id"], "linked_existing_task")
            superseded += 1
    conn.commit()
    return {"canonical_groups": sum(1 for items in groups.values() if len(items) > 1), "tasks_superseded": superseded}


def sync_backlog(conn: sqlite3.Connection, settings: dict) -> dict:
    cfg = _cfg(settings)
    queued_recommendations = 0
    conn.execute(
        """
        create table if not exists llm_recommendations (
            recommendation_id text primary key, created_at text not null, action text not null,
            title text not null, rationale text not null, payload_json text not null, status text not null
        )
        """
    )
    lifecycle = _backfill_artifact_lifecycle(conn)
    consolidation = _consolidate_duplicate_owner_tasks(conn)
    rows = conn.execute(
        """
        select recommendation_id, created_at, action, title, rationale, payload_json, status
        from llm_recommendations
        where action = 'propose_strategy_lab_experiment'
          and status in ('accepted', 'owner_queued')
        order by created_at asc
        limit 500
        """
    ).fetchall()
    for row in rows:
        rec = dict(row)
        rec["payload"] = _json(rec.pop("payload_json"), {})
        artifact = enqueue_recommendation(conn, rec, settings)
        queued_recommendations += artifact.get("action_status") == "created"

    salvaged = 0
    if cfg.get("salvage_invalid_backlog", True):
        experiment_rows = conn.execute(
            """
            select * from strategy_lab_experiments
            where status in (
                'rejected_invalid', 'needs_data', 'needs_contract_revision',
                'quarantined_surface_policy', 'proposed', 'promote_candidate',
                'needs_more_evidence'
            )
               or (status = 'active_testing' and updated_at <= ?)
            order by case status when 'promote_candidate' then 0 when 'needs_data' then 1 else 2 end,
                     updated_at desc
            limit 500
            """,
            ((_parse_iso(_utc_now()) - dt.timedelta(hours=float(cfg["stalled_testing_hours"]))).isoformat(),),
        ).fetchall()
        seen: set[str] = set()
        ranked_experiments = sorted(
            (_experiment_payload(raw) for raw in experiment_rows),
            key=_salvage_score,
            reverse=True,
        )
        for experiment in ranked_experiments:
            if (
                str(experiment.get("status") or "") == "needs_more_evidence"
                and not _needs_zero_output_diagnosis(experiment)
            ):
                continue
            signature = _dedupe_key({}, experiment)
            if signature in seen:
                continue
            seen.add(signature)
            objective = {
                "rejected_invalid": "repair_invalid_contract",
                "needs_data": "add_missing_strategy_features",
                "needs_contract_revision": "repair_runtime_contract",
                "quarantined_surface_policy": "repair_surface_contract",
                "proposed": "materialize_hypothesis",
                "promote_candidate": "promote_proven_experiment",
                "needs_more_evidence": "diagnose_zero_output",
            }.get(str(experiment.get("status")), "diagnose_zero_output")
            salvaged += _enqueue_experiment_repair(conn, experiment, objective)
            if salvaged >= int(cfg.get("salvage_limit_per_cycle", 12)):
                break
    return {
        "recommendations_queued": queued_recommendations,
        "historical_experiments_salvaged": salvaged,
        "artifact_lifecycle": lifecycle,
        "duplicate_consolidation": consolidation,
    }


def _reclaim_dead_leases(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "select task_id, claimed_pid, status from strategy_owner_tasks where claimed_by is not null"
    ).fetchall()
    reclaimed = 0
    for row in rows:
        if _pid_alive(row["claimed_pid"]):
            continue
        task = conn.execute("select codex_pid from strategy_owner_tasks where task_id = ?", (row["task_id"],)).fetchone()
        if task and _pid_alive(task["codex_pid"]):
            try:
                os.kill(int(task["codex_pid"]), 15)
            except OSError:
                pass
        status = "implementation_paused" if row["status"] in {"coding", "host_validation", "claimed"} else row["status"]
        conn.execute(
            """
            update strategy_owner_tasks
            set status = ?, claimed_by = null, claimed_pid = null, codex_pid = null, lease_expires_at = null,
                heartbeat_at = ?, updated_at = ?
            where task_id = ?
            """,
            (status, _utc_now(), _utc_now(), row["task_id"]),
        )
        reclaimed += 1
    if reclaimed:
        conn.commit()
    return reclaimed


def claim_task(conn: sqlite3.Connection, settings: dict) -> dict | None:
    _reclaim_dead_leases(conn)
    now = _utc_now()
    lease_until = (_parse_iso(now) + dt.timedelta(seconds=int(_cfg(settings)["lease_seconds"]))).isoformat()
    conn.execute("begin immediate")
    row = conn.execute(
        """
        select * from strategy_owner_tasks
        where status in ({})
          and claimed_by is null
          and (next_retry_at is null or next_retry_at <= ?)
        order by case when status = 'implementation_paused' then 0 else 1 end,
                 priority desc, updated_at asc
        limit 1
        """.format(",".join("?" for _ in CLAIMABLE_STATUSES)),
        (*sorted(CLAIMABLE_STATUSES), now),
    ).fetchone()
    if not row:
        conn.commit()
        return None
    task_id = row["task_id"]
    conn.execute(
        """
        update strategy_owner_tasks
        set claimed_by = 'strategy_implementation_owner', claimed_pid = ?, status = 'claimed',
            lease_expires_at = ?, heartbeat_at = ?, updated_at = ?, attempt_count = attempt_count + 1
        where task_id = ? and claimed_by is null
        """,
        (os.getpid(), lease_until, now, now, task_id),
    )
    conn.commit()
    return _row_dict(conn.execute("select * from strategy_owner_tasks where task_id = ?", (task_id,)).fetchone())


def _ensure_worktree(task: dict, settings: dict) -> tuple[pathlib.Path | None, str | None, str | None]:
    if task.get("worktree_path") and pathlib.Path(task["worktree_path"]).exists():
        path = pathlib.Path(task["worktree_path"])
        app_path = path / ROOT.relative_to(pathlib.Path(_git_root(ROOT)))
        return app_path, task.get("branch_name"), None
    git_root = pathlib.Path(_git_root(ROOT))
    base = pathlib.Path(str(_cfg(settings)["task_worktree_dir"])).resolve()
    base.mkdir(parents=True, exist_ok=True)
    suffix = hashlib.sha256(task["task_id"].encode("utf-8")).hexdigest()[:12]
    branch = f"strategy-owner/{suffix}"
    path = base / suffix
    if path.exists():
        app_path = path / ROOT.relative_to(git_root)
        return app_path, branch, None
    check = subprocess.run(["git", "branch", "--list", branch], cwd=git_root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    command = ["git", "worktree", "add"]
    if not check.stdout.strip():
        command.extend(["-b", branch])
    command.extend([str(path), branch if check.stdout.strip() else "HEAD"])
    completed = subprocess.run(command, cwd=git_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if completed.returncode != 0:
        return None, branch, (completed.stderr or completed.stdout)[-2000:]
    return path / ROOT.relative_to(git_root), branch, None


def _git_root(path: pathlib.Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=path,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    return completed.stdout.strip() or str(path)


def _source_context(conn: sqlite3.Connection, task: dict) -> dict:
    recommendation = {}
    if task.get("source_recommendation_id"):
        row = conn.execute(
            "select * from llm_recommendations where recommendation_id = ?",
            (task["source_recommendation_id"],),
        ).fetchone()
        if row:
            recommendation = dict(row)
            recommendation["payload"] = _json(recommendation.pop("payload_json"), {})
    experiment = {}
    if task.get("strategy_lab_id"):
        row = conn.execute(
            "select * from strategy_lab_experiments where strategy_lab_id = ?",
            (task["strategy_lab_id"],),
        ).fetchone()
        experiment = _experiment_payload(row) if row else {}
    trades = []
    outcomes = []
    if task.get("strategy_lab_id"):
        trades = [dict(row) for row in conn.execute(
            """
            select id, opened_at, closed_at, venue, inst_id, direction, trade_type, status, pnl_bps,
                   strategy_lab_id, strategy_lab_version
            from paper_trades where strategy_lab_id = ? order by id desc limit 100
            """, (task["strategy_lab_id"],),
        ).fetchall()]
        outcomes = [dict(row) for row in conn.execute(
            """
            select o.* from paper_trade_outcomes o join paper_trades t on t.id = o.trade_id
            where t.strategy_lab_id = ? order by o.id desc limit 200
            """, (task["strategy_lab_id"],),
        ).fetchall()]
    code_rows = [dict(row) for row in conn.execute(
        """
        select proposal_id, status, title, changed_files_json, tests_json, evaluation_json,
               candidate_commit, branch_name, worktree_path, updated_at
        from code_evolution_proposals
        where source_recommendation_id = ? or proposal_id = ?
        order by updated_at desc limit 20
        """, (task.get("source_recommendation_id"), task.get("code_proposal_id")),
    ).fetchall()]
    return {
        "task": task,
        "recommendation": recommendation,
        "experiment": experiment,
        "paper_trades": trades,
        "reliable_outcomes": [row for row in outcomes if row.get("measurement_status") == "valid"],
        "all_recent_outcomes": outcomes,
        "code_attempts": code_rows,
    }


def _memory_context(conn: sqlite3.Connection, task: dict, settings: dict, chain: dict, cycle_id: str) -> tuple[list[dict], str]:
    cfg = _cfg(settings)
    packet_path = RUNS_DIR / "llm_state_packet.json"
    packet = _json(packet_path.read_text(encoding="utf-8") if packet_path.exists() else "{}", {})
    packet = {**packet, "strategy_owner_active_task": chain}
    policy = {
        "namespaces": ["strategies", "outcomes", "code", "recommendations", "markets", "routes"],
        "keywords": [
            "strategy implementation owner hypothesis compile candidate outcome promotion code test failure",
            str(task.get("hypothesis") or ""), str(task.get("strategy_lab_id") or ""),
        ],
        "retrieval_limit": int(cfg["memory_retrieval_limit"]),
    }
    memories = retrieve_role_memories(
        conn, packet, "strategy_implementation_owner", settings,
        cycle_id=cycle_id, policy_override=policy,
    )
    budget = int(cfg["memory_context_chars"])
    selected: list[dict] = []
    used = len(json.dumps(chain, default=str))
    for memory in memories:
        size = len(json.dumps(memory, default=str))
        if selected and used + size > budget:
            break
        selected.append(memory)
        used += size
    return selected, hashlib.sha256(json.dumps({"chain": chain, "memory": selected}, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _decision_schema() -> dict:
    # Strict Responses API schemas cannot admit free-form objects. Keep the
    # decision envelope strict and carry flexible strategy contracts as JSON
    # strings; the deterministic validator decodes and validates them below.
    nullable_json = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": sorted(DECISIONS)},
            "rationale": {"type": "string"},
            "strategy_experiment": nullable_json,
            "code_goal": nullable_json,
            "dependencies": {"type": "string"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "tests_to_run": {"type": "array", "items": {"type": "string"}},
            "blocker": nullable_json,
            "memory_note": {"type": "string"},
        },
        "required": [
            "decision", "rationale", "strategy_experiment", "code_goal", "dependencies",
            "acceptance_criteria", "tests_to_run", "blocker", "memory_note",
        ],
    }


def _decode_decision_payload(decision: dict) -> dict:
    decoded = dict(decision or {})
    expected = {
        "strategy_experiment": (dict, type(None)),
        "code_goal": (dict, type(None)),
        "dependencies": (list,),
        "blocker": (dict, type(None)),
    }
    for key, allowed_types in expected.items():
        value = decoded.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid_{key}_json:{exc.msg}") from exc
        if not isinstance(value, allowed_types):
            raise ValueError(f"invalid_{key}_type:{type(value).__name__}")
        decoded[key] = value
    return decoded


def _prompt(task: dict, chain: dict, memories: list[dict]) -> str:
    return "\n\n".join(
        [
            "You are the persistent Strategy Implementation Owner for a paper-only autonomous trading research system.",
            "Own this objective through a concrete runtime artifact. Inspect the repository and current Strategy Lab contracts. This turn is analysis/contract design only: do not edit files. Return exactly the requested JSON schema.",
            "The strategy_experiment, code_goal, dependencies, and blocker fields are JSON-encoded strings. Encode objects/arrays as valid compact JSON strings; use null only for nullable fields and use '[]' for no dependencies.",
            "Prefer a general reusable observation_program over a one-off instrument rule. Preserve novelty. Use current available features when possible. If a required feature is missing, choose implement_code and define an end-to-end code goal. Do not invent broker writes or live trading.",
            "A materialize_experiment decision must include a complete strategy_experiment compatible with strategy_lab.ingest_strategy_lab_recommendation: strategy_lab_id, version, experiment_type='market_strategy', hypothesis, source_surface, permitted_target_surface, strategy_logic, data_requirements, risk_gates, and promotion_rules. Surface values must be explicit exact market contexts; missing metadata is quarantined.",
            "An observation program must define a universe, calculated_features, entry_expression, invalidation_expression, direction_logic, edge_formula, and score_formula using supported market feature snapshots. Candidate filters must be broad reusable strategies, not one-off symbols.",
            "TASK CHAIN\n" + json.dumps(chain, sort_keys=True, default=str)[:50000],
            "RELEVANT TEMPORAL MEMORY\n" + json.dumps(memories, sort_keys=True, default=str)[:30000],
        ]
    )


def _journal(task: dict, entry: dict) -> list[dict]:
    journal = list(task.get("recovery_journal") or [])
    journal.append({"at": _utc_now(), **entry})
    return journal[-100:]


def _release_claim(conn: sqlite3.Connection, task_id: str, *, status: str, result: dict, error: dict | None = None, retry: bool = False) -> None:
    retry_at = None
    if retry:
        retry_at = (_parse_iso(_utc_now()) + dt.timedelta(seconds=300)).isoformat()
    conn.execute(
        """
        update strategy_owner_tasks
        set status = ?, updated_at = ?, completed_at = case when ? in ('completed','promoted_to_code','retired_bad_evidence') then ? else completed_at end,
            claimed_by = null, claimed_pid = null, codex_pid = null, lease_expires_at = null, heartbeat_at = ?,
            next_retry_at = ?, last_result_json = ?, last_error_json = ?,
            codex_session_id = case when ? in ('active_testing','monitoring_evidence','promoted_to_runtime','completed','promoted_to_code','retired_bad_evidence') then null else codex_session_id end
        where task_id = ?
        """,
        (status, _utc_now(), status, _utc_now(), _utc_now(), retry_at, json.dumps(result, sort_keys=True, default=str), json.dumps(error or {}, sort_keys=True, default=str), status, task_id),
    )
    conn.commit()


def _record_run(conn: sqlite3.Connection, task: dict, cycle_id: str, before: str, after: str, result: dict) -> None:
    conn.execute(
        """
        insert into strategy_owner_runs (
            run_id, task_id, cycle_id, started_at, completed_at, status_before, status_after,
            decision, codex_session_id, worktree_path, memory_ids_json, context_hash,
            model_json, result_json, error_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()), task["task_id"], cycle_id, result.get("started_at") or _utc_now(), _utc_now(),
            before, after, (result.get("decision") or {}).get("decision"), result.get("session_id"),
            task.get("worktree_path"), json.dumps(task.get("memory_ids") or []), task.get("memory_context_hash"),
            json.dumps({"model": result.get("model"), "reasoning_effort": result.get("reasoning_effort")}),
            json.dumps(result, sort_keys=True, default=str), json.dumps({"reason": result.get("reason"), "stderr_tail": result.get("stderr_tail")}, sort_keys=True),
        ),
    )
    conn.commit()


def _record_memory(conn: sqlite3.Connection, task: dict, result: dict, status: str) -> None:
    memory = upsert_memory_fact(
        conn,
        "strategy_owner_outcome",
        task["task_id"],
        status,
        json.dumps(result.get("decision") or result, sort_keys=True, default=str),
        0.9 if status in {"active_testing", "promoted_to_runtime", "completed"} else 0.65,
        "strategy_implementation_owner",
        {
            "memory_summary": (
                f"Strategy owner task {task['task_id']} for {task.get('hypothesis')} reached {status}. "
                f"Decision: {(result.get('decision') or {}).get('decision')}. "
                f"Rationale: {(result.get('decision') or {}).get('rationale')}."
            ),
            "strategy_lab_id": task.get("strategy_lab_id"),
            "recommendation_id": task.get("source_recommendation_id"),
        },
        namespace="strategies",
        memory_type="episodic",
        source_id=task["task_id"],
        importance=0.85,
        outcome_score=0.7 if status in {"active_testing", "promoted_to_runtime", "completed"} else -0.25 if status == "retired_bad_evidence" else 0.0,
        tags=["strategy-owner", status],
    )
    upsert_memory_link(conn, "memory", memory["memory_id"], "describes", "strategy_owner_task", task["task_id"])
    if task.get("source_recommendation_id"):
        upsert_memory_link(conn, "recommendation", task["source_recommendation_id"], "owned_by", "strategy_owner_task", task["task_id"])
    conn.commit()


def _handle_materialize(
    conn: sqlite3.Connection,
    task: dict,
    decision: dict,
    settings: dict | None = None,
) -> tuple[str, dict]:
    experiment = decision.get("strategy_experiment")
    if not isinstance(experiment, dict):
        return "analyzing", {"validation_error": "materialize_experiment_missing_strategy_experiment"}
    rec = {
        "recommendation_id": task.get("source_recommendation_id") or f"owner:{task['task_id']}",
        "source_agent": "strategy_implementation_owner",
        "payload": {
            "action": "propose_strategy_lab_experiment",
            "agent_name": "strategy_implementation_owner",
            "strategy_lab_experiment": experiment,
        },
    }
    artifacts = ingest_strategy_lab_recommendation(conn, rec, settings)
    created = next((item for item in artifacts if item.get("action_status") == "created"), None)
    if not created:
        return "analyzing", {"validation_error": artifacts}
    strategy_id = created.get("strategy_lab_id")
    conn.execute(
        """
        update strategy_owner_tasks
        set strategy_lab_id = ?, strategy_lab_version = ?, status = 'active_testing', updated_at = ?
        where task_id = ?
        """,
        (strategy_id, experiment.get("version", 1), _utc_now(), task["task_id"]),
    )
    conn.commit()
    if task.get("source_recommendation_id"):
        link_recommendation_artifact(
            conn,
            task["source_recommendation_id"],
            "strategy_lab_experiment",
            strategy_id,
            "materialized_as",
            {"strategy_owner_task_id": task["task_id"]},
        )
        update_llm_recommendation_status(
            conn,
            task["source_recommendation_id"],
            "experiment_materialized",
        )
    upsert_memory_link(conn, "strategy_owner_task", task["task_id"], "materialized", "strategy_lab_experiment", str(strategy_id))
    conn.commit()
    return "active_testing", {"artifacts": artifacts, "strategy_lab_id": strategy_id}


def _handle_code(conn: sqlite3.Connection, task: dict, decision: dict, settings: dict, session_id: str | None) -> tuple[str, dict]:
    goal = decision.get("code_goal")
    if not isinstance(goal, dict):
        return "analyzing", {"validation_error": "implement_code_missing_code_goal"}
    recommendation_id = f"strategy-owner:{task['task_id']}:code"
    task_worktree = pathlib.Path(str(task.get("worktree_path") or ""))
    source_git_root = pathlib.Path(_git_root(ROOT))
    app_worktree = task_worktree / ROOT.relative_to(source_git_root)
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=task_worktree,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    ).stdout.strip()
    payload = {
        "action": "propose_code_change",
        "agent_name": "strategy_implementation_owner",
        "priority": task.get("priority", 90),
        "title": str(goal.get("title") or f"Implement strategy owner task {task['task_id']}")[:180],
        "rationale": goal.get("goal") or goal.get("summary") or decision.get("rationale"),
        "proposed_change": goal,
        "evidence": {"strategy_owner_task_id": task["task_id"], "strategy_lab_id": task.get("strategy_lab_id")},
        "change_category": "strategy_lab_promotion" if task.get("objective_type") == "promote_proven_experiment" else "paper_signal_logic",
        "implementation_mode": "runtime_active",
        "strategy_owner_codex_session_id": session_id,
        "strategy_owner_release": {
            "parent_commit": parent,
            "branch_name": task.get("branch_name"),
            "worktree_path": str(task_worktree),
            "app_worktree_path": str(app_worktree),
            "status": "implementing",
        },
        "code_change": {
            "change_category": "strategy_lab_promotion" if task.get("objective_type") == "promote_proven_experiment" else "paper_signal_logic",
            "implementation_mode": "runtime_active",
            "strategy_owner_codex_session_id": session_id,
            "tests_to_run": decision.get("tests_to_run") or [],
            "rollback_criteria": "Revert if paper-only safety, Strategy Lab propagation, tests, or radar health fail.",
        },
    }
    artifacts = process_code_change_recommendation(
        conn,
        {"recommendation_id": recommendation_id, "title": payload["title"], "priority": payload["priority"], "payload": payload},
        settings,
    )
    artifact = artifacts[0] if artifacts else {}
    proposal_id = artifact.get("proposal_id")
    proposal_status = str(artifact.get("status") or "no_artifact")
    conn.execute(
        "update strategy_owner_tasks set code_proposal_id = ?, codex_session_id = ?, updated_at = ? where task_id = ?",
        (proposal_id, session_id, _utc_now(), task["task_id"]),
    )
    conn.commit()
    if proposal_id:
        link_recommendation_artifact(
            conn,
            task.get("source_recommendation_id"),
            "code_evolution_proposal",
            proposal_id,
            "implemented_by",
            {"strategy_owner_task_id": task["task_id"]},
        )
        upsert_memory_link(conn, "strategy_owner_task", task["task_id"], "implemented_by", "code_evolution_proposal", str(proposal_id))
        conn.commit()
    if proposal_status in {"promoted", "candidate_committed", "workspace_kept", "kept"}:
        if task.get("objective_type") == "promote_proven_experiment":
            if task.get("strategy_lab_id"):
                conn.execute(
                    "update strategy_lab_experiments set status='promoted_to_code', updated_at=? where strategy_lab_id=?",
                    (_utc_now(), task["strategy_lab_id"]),
                )
                conn.commit()
            return "promoted_to_code", {"artifacts": artifacts, "proposal_status": proposal_status}
        if not task.get("strategy_lab_id"):
            return "analyzing", {
                "artifacts": artifacts,
                "proposal_status": proposal_status,
                "next_action": "resume_same_task_and_materialize_experiment",
            }
        return "promoted_to_runtime", {"artifacts": artifacts, "proposal_status": proposal_status}
    if proposal_status in {"patch_generation_unavailable_retry_later", "blocked_model_quota"}:
        return "waiting_quota", {"artifacts": artifacts, "proposal_status": proposal_status}
    if proposal_status in {"implementation_paused", "codex_writer_busy", "queued_probation_limit"}:
        return "implementation_paused", {"artifacts": artifacts, "proposal_status": proposal_status}
    return "coding", {"artifacts": artifacts, "proposal_status": proposal_status}


def monitor_tasks(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "select * from strategy_owner_tasks where status not in ('completed','promoted_to_code','retired_bad_evidence','superseded_duplicate')"
    ).fetchall()
    transitions = []
    for raw in rows:
        task = _row_dict(raw)
        experiment = conn.execute(
            "select status, compile_status, evaluation_json from strategy_lab_experiments where strategy_lab_id = ?",
            (task.get("strategy_lab_id"),),
        ).fetchone() if task.get("strategy_lab_id") else None
        proposal = conn.execute(
            "select status from code_evolution_proposals where proposal_id = ?",
            (task.get("code_proposal_id"),),
        ).fetchone() if task.get("code_proposal_id") else None
        proposal_status = str(proposal["status"] if proposal else "")
        proposal_promoted = proposal_status in {
            "promoted", "candidate_committed", "workspace_kept", "kept",
        }
        if not experiment:
            if proposal_promoted and task["status"] in {
                "implementation_paused", "waiting_data", "coding", "promoted_to_runtime",
            }:
                next_status = "analyzing"
                conn.execute(
                    "update strategy_owner_tasks set status=?, claimed_by=null, claimed_pid=null, next_retry_at=null, updated_at=? where task_id=?",
                    (next_status, _utc_now(), task["task_id"]),
                )
                transitions.append({
                    "task_id": task["task_id"], "from": task["status"], "to": next_status,
                    "reason": "promoted_dependency_ready_for_materialization",
                })
            continue
        experiment_status = str(experiment["status"])
        compile_status = str(experiment["compile_status"] or "")
        evaluation = _json(experiment["evaluation_json"], {})
        generation_diagnostic = (
            evaluation.get("generation_diagnostic")
            if isinstance(evaluation.get("generation_diagnostic"), dict)
            else {}
        )
        runtime_contract_mismatch = generation_diagnostic.get("runtime_contract_mismatch")
        if (
            experiment_status == "needs_contract_revision"
            and isinstance(runtime_contract_mismatch, dict)
            and runtime_contract_mismatch.get("repairable")
        ):
            canonical = conn.execute(
                """
                select task_id from strategy_owner_tasks
                where strategy_lab_id = ?
                  and status not in ('completed','promoted_to_code','retired_bad_evidence','superseded_duplicate')
                order by priority desc, created_at asc limit 1
                """,
                (task.get("strategy_lab_id"),),
            ).fetchone()
            if canonical and str(canonical["task_id"]) != str(task["task_id"]):
                conn.execute(
                    """
                    update strategy_owner_tasks
                    set status='superseded_duplicate', completed_at=?, updated_at=?,
                        claimed_by=null, claimed_pid=null
                    where task_id=?
                    """,
                    (_utc_now(), _utc_now(), task["task_id"]),
                )
                transitions.append(
                    {
                        "task_id": task["task_id"],
                        "from": task["status"],
                        "to": "superseded_duplicate",
                        "reason": "canonical_runtime_contract_repair_exists",
                    }
                )
                continue
            dependencies = dict(task.get("dependencies") or {})
            dependencies["runtime_contract_mismatch"] = runtime_contract_mismatch
            conn.execute(
                """
                update strategy_owner_tasks
                set status='analyzing', objective_type='repair_runtime_contract', priority=max(priority,99),
                    dependency_json=?, next_retry_at=null, claimed_by=null, claimed_pid=null, updated_at=?
                where task_id=?
                """,
                (json.dumps(dependencies, sort_keys=True), _utc_now(), task["task_id"]),
            )
            transitions.append(
                {
                    "task_id": task["task_id"],
                    "from": task["status"],
                    "to": "analyzing",
                    "reason": "repairable_runtime_contract_mismatch",
                    "objective_type": "repair_runtime_contract",
                }
            )
            continue
        zero_output_needs_owner = (
            str(task.get("objective_type") or "") == "diagnose_zero_output"
            and _needs_zero_output_diagnosis(
                {"status": experiment_status, "evaluation": evaluation}
            )
            and not (
                str(task.get("status") or "") == "monitoring_evidence"
                and int(task.get("attempt_count") or 0) > 0
            )
        )
        if zero_output_needs_owner:
            next_status = "analyzing"
        elif (
            experiment_status == "needs_contract_revision"
            and isinstance(generation_diagnostic.get("relaxed_child"), dict)
            and generation_diagnostic["relaxed_child"].get("strategy_lab_id")
        ):
            next_status = "monitoring_evidence"
        else:
            next_status = {
                "promote_candidate": "promote_candidate",
                "promotion_queued": "coding",
                "promoted_to_code": "completed",
                "retired_bad_evidence": "retired_bad_evidence",
                "retired_no_activity": "retired_bad_evidence",
                "needs_data": "waiting_data",
                "needs_route": "waiting_route",
                "needs_contract_revision": "analyzing",
                "quarantined_surface_policy": "analyzing",
                "proposed": "analyzing",
            }.get(
                experiment_status,
                "monitoring_evidence" if compile_status == "compiled" else task["status"],
            )
        priority_needs_raise = zero_output_needs_owner and int(task.get("priority") or 0) < 96
        if next_status != task["status"] or priority_needs_raise:
            conn.execute(
                """update strategy_owner_tasks
                   set status = ?, priority = case when ? then max(priority,96) else priority end,
                       updated_at = ?, completed_at = case when ? in ('completed','retired_bad_evidence') then ? else completed_at end
                   where task_id = ?""",
                (next_status, zero_output_needs_owner, _utc_now(), next_status, _utc_now(), task["task_id"]),
            )
            transitions.append({"task_id": task["task_id"], "from": task["status"], "to": next_status})
    if transitions:
        conn.commit()
    return {"checked": len(rows), "transitions": transitions}


def process_one(conn: sqlite3.Connection, settings: dict, *, cycle_id: str) -> dict:
    task = claim_task(conn, settings)
    if not task:
        return {"status": "no_eligible_strategy_task"}
    before = str(task.get("status") or "claimed")
    chain = _source_context(conn, task)
    memories, context_hash = _memory_context(conn, task, settings, chain, cycle_id)
    memory_ids = [item.get("memory_id") for item in memories if item.get("memory_id")]
    worktree, branch, worktree_error = _ensure_worktree(task, settings)
    if worktree is None:
        result = {"status": "implementation_paused", "reason": "strategy_owner_worktree_failed", "error": worktree_error}
        _release_claim(conn, task["task_id"], status="implementation_paused", result=result, error=result, retry=True)
        _record_run(conn, task, cycle_id, before, "implementation_paused", result)
        return result
    conn.execute(
        """
        update strategy_owner_tasks set status='analyzing', worktree_path=?, branch_name=?,
            memory_ids_json=?, memory_context_hash=?, heartbeat_at=?, updated_at=? where task_id=?
        """,
        (str(worktree.parent), branch, json.dumps(memory_ids), context_hash, _utc_now(), _utc_now(), task["task_id"]),
    )
    conn.commit()
    task.update({"worktree_path": str(worktree.parent), "branch_name": branch, "memory_ids": memory_ids, "memory_context_hash": context_hash})
    def record_codex_pid(pid: int) -> None:
        conn.execute(
            "update strategy_owner_tasks set codex_pid=?, heartbeat_at=?, updated_at=? where task_id=?",
            (int(pid), _utc_now(), _utc_now(), task["task_id"]),
        )
        conn.commit()

    result = run_structured_codex_turn(
        task_id=task["task_id"], prompt=_prompt(task, chain, memories), output_schema=_decision_schema(),
        worktree_root=worktree, settings=settings, runs_dir=RUNS_DIR,
        session_id=str(task.get("codex_session_id") or "") or None,
        process_started=record_codex_pid,
    )
    conn.execute(
        "update strategy_owner_tasks set codex_session_id=?, recovery_journal_json=?, updated_at=? where task_id=?",
        (result.get("session_id"), json.dumps(_journal(task, {"status": result.get("status"), "reason": result.get("reason")})), _utc_now(), task["task_id"]),
    )
    conn.commit()
    if result.get("status") != "completed":
        reason = str(result.get("reason") or result.get("stderr_tail") or "codex_turn_incomplete").lower()
        status = "waiting_quota" if any(term in reason for term in ("quota", "429", "budget")) else "waiting_network" if any(term in reason for term in ("connect", "network", "dns")) else "implementation_paused"
        _release_claim(conn, task["task_id"], status=status, result=result, error={"reason": reason}, retry=True)
        _record_run(conn, task, cycle_id, before, status, result)
        _record_memory(conn, task, result, status)
        return {"status": status, "task_id": task["task_id"], "codex": result}

    try:
        decision = _decode_decision_payload(result.get("decision") or {})
    except ValueError as exc:
        result["reason"] = str(exc)
        result["decision_validation_error"] = str(exc)
        _release_claim(
            conn,
            task["task_id"],
            status="implementation_paused",
            result=result,
            error={"reason": str(exc)},
            retry=True,
        )
        _record_run(conn, task, cycle_id, before, "implementation_paused", result)
        _record_memory(conn, task, result, "implementation_paused")
        return {"status": "implementation_paused", "task_id": task["task_id"], "codex": result}
    choice = str(decision.get("decision") or "")
    if choice == "materialize_experiment":
        status, handled = _handle_materialize(conn, task, decision, settings)
    elif choice == "implement_code":
        status, handled = _handle_code(conn, task, decision, settings, result.get("session_id"))
    elif choice == "wait_for_data":
        status, handled = "waiting_data", {"blocker": decision.get("blocker"), "dependencies": decision.get("dependencies")}
    elif choice == "wait_for_route":
        status, handled = "waiting_route", {"blocker": decision.get("blocker"), "dependencies": decision.get("dependencies")}
    elif choice == "monitor_evidence":
        status, handled = "monitoring_evidence", {"reason": decision.get("rationale")}
    elif choice == "retire":
        status, handled = "retired_bad_evidence", {"reason": decision.get("rationale")}
    elif choice == "completed":
        status, handled = "completed", {"reason": decision.get("rationale")}
    else:
        status, handled = "analyzing", {"validation_error": f"unsupported_decision:{choice}"}
    final = {**result, "handled": handled}
    _release_claim(conn, task["task_id"], status=status, result=final, retry=status in {"analyzing", "coding", "implementation_paused", "waiting_quota", "waiting_network"})
    _record_run(conn, task, cycle_id, before, status, final)
    _record_memory(conn, task, final, status)
    return {"status": status, "task_id": task["task_id"], "decision": choice, **handled}


def summary(conn: sqlite3.Connection, limit: int = 100) -> dict:
    by_status = dict(conn.execute("select status, count(*) from strategy_owner_tasks group by status").fetchall())
    rows = [_row_dict(row) for row in conn.execute(
        "select * from strategy_owner_tasks order by priority desc, updated_at desc limit ?", (int(limit),)
    ).fetchall()]
    run_counts = dict(conn.execute("select coalesce(decision,'unknown'), count(*) from strategy_owner_runs group by decision").fetchall())
    metrics = conn.execute(
        """
        select count(distinct t.task_id) as tasks_with_trades,
               count(p.id) as paper_trades,
               sum(case when o.measurement_status='valid' then 1 else 0 end) as valid_labels
        from strategy_owner_tasks t
        left join paper_trades p on p.strategy_lab_id=t.strategy_lab_id
        left join paper_trade_outcomes o on o.trade_id=p.id
        """
    ).fetchone()
    run_metrics = conn.execute(
        """
        select count(*) as run_count,
               sum(case when codex_session_id is not null then 1 else 0 end) as codex_turns,
               sum(case when json_extract(result_json, '$.resumed') = 1 then 1 else 0 end) as resumed_turns,
               sum(estimated_cost_usd) as estimated_cost_usd
        from strategy_owner_runs
        """
    ).fetchone()
    artifact_metrics = conn.execute(
        """
        select sum(case when strategy_lab_id is not null then 1 else 0 end) as experiments,
               sum(case when code_proposal_id is not null then 1 else 0 end) as code_proposals,
               sum(case when status in ('completed','promoted_to_code') then 1 else 0 end) as completed,
               sum(case when status='retired_bad_evidence' then 1 else 0 end) as retired
        from strategy_owner_tasks
        """
    ).fetchone()
    blockers = Counter()
    for task in rows:
        blocker = (task.get("last_result") or {}).get("blocker") or (task.get("last_error") or {}).get("reason")
        if blocker:
            blockers[str(blocker)[:240]] += 1
    return {
        "enabled": True,
        "by_status": by_status,
        "total_tasks": sum(by_status.values()),
        "decision_counts": run_counts,
        "tasks_with_trades": int(metrics[0] or 0),
        "paper_trades": int(metrics[1] or 0),
        "valid_labels": int(metrics[2] or 0),
        "experiment_artifacts": int(artifact_metrics[0] or 0),
        "code_proposals": int(artifact_metrics[1] or 0),
        "completed_tasks": int(artifact_metrics[2] or 0),
        "retired_tasks": int(artifact_metrics[3] or 0),
        "codex_turns": int(run_metrics[1] or 0),
        "resumed_codex_turns": int(run_metrics[2] or 0),
        "estimated_cost_usd": round(float(run_metrics[3] or 0), 6),
        "exact_blockers": dict(blockers.most_common(20)),
        "tasks": rows,
    }


def write_report(conn: sqlite3.Connection, *, cycle: dict | None = None, scheduler: dict | None = None, settings: dict | None = None) -> dict:
    report = {
        "generated_at": _utc_now(),
        "status": "ok",
        "summary": summary(conn, int(_cfg(settings or {}).get("report_limit", 100))),
        "last_cycle": cycle or {},
        "scheduler": scheduler or {},
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    lines = [
        "# Strategy Implementation Owner", "",
        f"- Generated: `{report['generated_at']}`",
        f"- Tasks: `{report['summary']['total_tasks']}`",
        f"- Paper trades: `{report['summary']['paper_trades']}`",
        f"- Reliable labels: `{report['summary']['valid_labels']}`",
        f"- Last cycle: `{(cycle or {}).get('status')}`",
        "", "## Lifecycle", "",
    ]
    for status, count in sorted(report["summary"]["by_status"].items()):
        lines.append(f"- `{status}`: {count}")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run_once(conn: sqlite3.Connection, settings: dict, *, execute_turn: bool = True, cycle_id: str | None = None, scheduler: dict | None = None) -> dict:
    if not _cfg(settings).get("enabled", True):
        return write_report(conn, cycle={"status": "disabled"}, scheduler=scheduler, settings=settings)
    sync = sync_backlog(conn, settings)
    monitored = monitor_tasks(conn)
    cycle = process_one(conn, settings, cycle_id=cycle_id or str(uuid.uuid4())) if execute_turn else {"status": "monitor_only"}
    cycle["backlog_sync"] = sync
    cycle["monitoring"] = monitored
    return write_report(conn, cycle=cycle, scheduler=scheduler, settings=settings)
