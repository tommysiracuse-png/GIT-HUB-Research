"""Durable, low-contention coordination primitives for Codex worker pools.

This module intentionally owns a small SQLite database separate from the radar
database.  It provides the minimum durable state needed for independent coding
workers to claim work, serialize named resources such as ``main_promotion``,
and hand completed candidates to asynchronous verification.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
DEFAULT_DB_PATH = RUNS_DIR / "codex_coordination.sqlite"
SQLITE_BUSY_TIMEOUT_MS = 15_000
SCHEMA_VERSION = 2

CLAIMABLE_TASK_STATUSES = frozenset({"queued", "requeued"})
ACTIVE_TASK_STATUSES = frozenset(
    {
        "claimed",
        "coding",
        "focused_tests",
        "candidate_committed",
        "promoted_pending_verification",
        "verifying",
        "repairing",
    }
)
CLAIMABLE_VERIFICATION_STATUSES = frozenset({"queued", "requeued"})
DEDUPLICATION_BLOCKING_STATUSES = frozenset(
    set(CLAIMABLE_TASK_STATUSES)
    | set(ACTIVE_TASK_STATUSES)
    | {"promoted", "verified", "repairing_post_promotion"}
)


def utc_now() -> str:
    """Return a sortable UTC timestamp with an explicit offset."""

    return dt.datetime.now(dt.timezone.utc).isoformat()


def _utc_after(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(0.0, seconds))).isoformat()


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=repr)


def _decode_json(value: Any) -> Any:
    if value in (None, ""):
        return {}
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return {"raw": str(value)}


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for field in ("payload_json", "result_json", "details_json", "value_json"):
        if field in result:
            result[field.removesuffix("_json")] = _decode_json(result.pop(field))
    return result


def _is_memory_db(db_path: pathlib.Path | str) -> bool:
    return str(db_path) == ":memory:"


def connect(db_path: pathlib.Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open and initialize the dedicated coordination database."""

    if not _is_memory_db(db_path):
        pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"pragma busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    if not _is_memory_db(db_path):
        conn.execute("pragma journal_mode = wal")
    conn.execute("pragma synchronous = normal")
    conn.execute("pragma foreign_keys = on")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the coordination schema. Safe to call on every connection."""

    conn.executescript(
        """
        create table if not exists coordination_metadata (
            key text primary key,
            value_json text not null,
            updated_at text not null
        );

        create table if not exists codex_tasks (
            task_id text primary key,
            source_kind text not null,
            source_id text not null,
            lane text not null,
            status text not null,
            priority integer not null default 0,
            payload_json text not null default '{}',
            work_fingerprint text,
            work_scope text,
            canonical_task_id text,
            created_at text not null,
            updated_at text not null,
            retry_at text,
            claimed_by text,
            claim_pid integer,
            claim_token text,
            claimed_at text,
            lease_expires_at text,
            claim_count integer not null default 0,
            completed_at text,
            result_json text,
            last_error text,
            unique(source_kind, source_id)
        );
        create index if not exists idx_codex_tasks_claim
            on codex_tasks(status, retry_at, lane, priority desc, created_at);
        create index if not exists idx_codex_tasks_lease
            on codex_tasks(status, lease_expires_at);

        create table if not exists codex_workers (
            worker_id text primary key,
            preferred_lane text not null,
            pid integer,
            state text not null,
            started_at text not null,
            heartbeat_at text not null,
            details_json text not null default '{}'
        );
        create index if not exists idx_codex_workers_heartbeat
            on codex_workers(heartbeat_at);

        create table if not exists codex_resource_leases (
            resource_name text primary key,
            owner_worker_id text not null,
            owner_pid integer,
            lease_token text not null,
            acquired_at text not null,
            updated_at text not null,
            lease_expires_at text not null,
            details_json text not null default '{}'
        );
        create index if not exists idx_codex_resource_leases_expiry
            on codex_resource_leases(lease_expires_at);

        create table if not exists codex_verification_jobs (
            job_id text primary key,
            task_id text not null references codex_tasks(task_id),
            verification_kind text not null,
            status text not null,
            priority integer not null default 0,
            payload_json text not null default '{}',
            created_at text not null,
            updated_at text not null,
            retry_at text,
            claimed_by text,
            claim_pid integer,
            claim_token text,
            claimed_at text,
            lease_expires_at text,
            completed_at text,
            result_json text,
            last_error text,
            unique(task_id, verification_kind)
        );
        create index if not exists idx_codex_verification_claim
            on codex_verification_jobs(status, retry_at, priority desc, created_at);
        create index if not exists idx_codex_verification_lease
            on codex_verification_jobs(status, lease_expires_at);

        create table if not exists codex_coordination_events (
            event_id integer primary key autoincrement,
            occurred_at text not null,
            entity_kind text not null,
            entity_id text not null,
            event_type text not null,
            details_json text not null default '{}'
        );
        create index if not exists idx_codex_coordination_events_entity
            on codex_coordination_events(entity_kind, entity_id, occurred_at);
        """
    )
    task_columns = {
        str(row["name"])
        for row in conn.execute("pragma table_info(codex_tasks)").fetchall()
    }
    for name, sql_type in (
        ("work_fingerprint", "text"),
        ("work_scope", "text"),
        ("canonical_task_id", "text"),
    ):
        if name not in task_columns:
            conn.execute(f"alter table codex_tasks add column {name} {sql_type}")
    conn.execute(
        "create index if not exists idx_codex_tasks_work "
        "on codex_tasks(work_fingerprint,status,priority desc,created_at)"
    )
    now = utc_now()
    conn.execute(
        """
        insert into coordination_metadata(key, value_json, updated_at)
        values('schema_version', ?, ?)
        on conflict(key) do update set value_json=excluded.value_json, updated_at=excluded.updated_at
        """,
        (_json({"version": SCHEMA_VERSION}), now),
    )
    conn.commit()


def _begin(conn: sqlite3.Connection) -> None:
    conn.execute("begin immediate")


def _event(
    conn: sqlite3.Connection,
    entity_kind: str,
    entity_id: str,
    event_type: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        insert into codex_coordination_events(occurred_at, entity_kind, entity_id, event_type, details_json)
        values(?,?,?,?,?)
        """,
        (utc_now(), entity_kind, entity_id, event_type, _json(dict(details or {}))),
    )


def record_migration(
    conn: sqlite3.Connection, migration_name: str, details: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Record an idempotent import/migration marker and return its metadata."""

    key = f"migration:{migration_name}"
    now = utc_now()
    _begin(conn)
    try:
        existing = conn.execute(
            "select value_json, updated_at from coordination_metadata where key=?", (key,)
        ).fetchone()
        if existing is None:
            value = {"name": migration_name, "details": dict(details or {}), "recorded_at": now}
            conn.execute(
                "insert into coordination_metadata(key,value_json,updated_at) values(?,?,?)",
                (key, _json(value), now),
            )
            _event(conn, "migration", migration_name, "recorded", value)
        else:
            value = _decode_json(existing["value_json"])
        conn.commit()
        return {"key": key, "value": value, "created": existing is None}
    except BaseException:
        conn.rollback()
        raise


def migration_metadata(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "select key,value_json from coordination_metadata where key like 'migration:%' order by key"
    ).fetchall()
    return {str(row["key"])[10:]: _decode_json(row["value_json"]) for row in rows}


def enqueue_task(
    conn: sqlite3.Connection,
    source_kind: str,
    source_id: str | int,
    *,
    lane: str = "general",
    priority: int = 0,
    payload: Mapping[str, Any] | None = None,
    status: str = "queued",
    reactivate_terminal: bool = False,
    work_fingerprint: str | None = None,
    work_scope: str | None = None,
) -> dict[str, Any]:
    """Idempotently enqueue a task and collapse equivalent work across sources."""

    source_id_text = str(source_id)
    fingerprint = str(work_fingerprint or "").strip() or None
    scope = str(work_scope or "").strip() or None
    now = utc_now()
    _begin(conn)
    try:
        existing = conn.execute(
            "select * from codex_tasks where source_kind=? and source_id=?",
            (source_kind, source_id_text),
        ).fetchone()
        canonical = None
        if fingerprint:
            statuses = tuple(sorted(DEDUPLICATION_BLOCKING_STATUSES))
            canonical = conn.execute(
                """
                select * from codex_tasks
                where work_fingerprint=? and status in (%s)
                  and not (source_kind=? and source_id=?)
                order by case
                    when status in ('verified','promoted') then 0
                    when status in ('promoted_pending_verification','verifying','candidate_committed') then 1
                    when status in ('claimed','coding','focused_tests','repairing','repairing_post_promotion') then 2
                    else 3 end,
                    priority desc, created_at asc, task_id asc
                limit 1
                """ % ",".join("?" for _ in statuses),
                (fingerprint, *statuses, source_kind, source_id_text),
            ).fetchone()
        if canonical is not None and not (
            existing is not None and str(existing["status"]) in ACTIVE_TASK_STATUSES
        ):
            result_payload = {
                "reason": "equivalent_work_already_owned",
                "canonical_task_id": str(canonical["task_id"]),
                "canonical_source_kind": str(canonical["source_kind"]),
                "canonical_source_id": str(canonical["source_id"]),
                "work_scope": scope or str(canonical["work_scope"] or ""),
            }
            if existing is None:
                task_id = uuid.uuid4().hex
                conn.execute(
                    """
                    insert into codex_tasks(
                        task_id,source_kind,source_id,lane,status,priority,payload_json,
                        work_fingerprint,work_scope,canonical_task_id,created_at,updated_at,
                        completed_at,result_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        task_id, source_kind, source_id_text, lane, "superseded_duplicate",
                        int(priority), _json(dict(payload or {})), fingerprint, scope,
                        canonical["task_id"], now, now, now, _json(result_payload),
                    ),
                )
                duplicate_id = task_id
            else:
                duplicate_id = str(existing["task_id"])
                conn.execute(
                    """
                    update codex_tasks
                    set lane=?, priority=max(priority,?), payload_json=?, status='superseded_duplicate',
                        work_fingerprint=?, work_scope=?, canonical_task_id=?, completed_at=?,
                        result_json=?, retry_at=null, claimed_by=null, claim_pid=null,
                        claim_token=null, claimed_at=null, lease_expires_at=null, updated_at=?
                    where task_id=?
                    """,
                    (
                        lane, int(priority), _json(dict(payload or _decode_json(existing["payload_json"]))),
                        fingerprint, scope, canonical["task_id"], now, _json(result_payload), now,
                        duplicate_id,
                    ),
                )
            _event(conn, "task", duplicate_id, "superseded_duplicate", result_payload)
            result = conn.execute(
                "select * from codex_tasks where task_id=?", (duplicate_id,)
            ).fetchone()
            conn.commit()
            return _row_dict(result) or {}
        if existing is not None:
            # New evidence may improve the priority/payload but cannot steal a live claim.
            next_status = str(existing["status"])
            if reactivate_terminal and next_status not in CLAIMABLE_TASK_STATUSES | ACTIVE_TASK_STATUSES:
                next_status = status
            updates = {
                "lane": lane or existing["lane"],
                "priority": max(int(existing["priority"]), int(priority)),
                "payload_json": _json(dict(payload or _decode_json(existing["payload_json"]))),
                "status": next_status,
                "work_fingerprint": fingerprint or existing["work_fingerprint"],
                "work_scope": scope or existing["work_scope"],
                "updated_at": now,
            }
            conn.execute(
                """
                update codex_tasks
                set lane=:lane, priority=:priority, payload_json=:payload_json, status=:status,
                    work_fingerprint=:work_fingerprint, work_scope=:work_scope,
                    canonical_task_id=case when :status in ('queued','requeued') then null else canonical_task_id end,
                    completed_at=case when :status in ('queued','requeued') then null else completed_at end,
                    retry_at=case when :status in ('queued','requeued') then null else retry_at end,
                    updated_at=:updated_at
                where task_id=:task_id
                """,
                {**updates, "task_id": existing["task_id"]},
            )
            result = conn.execute(
                "select * from codex_tasks where task_id=?", (existing["task_id"],)
            ).fetchone()
            _event(conn, "task", existing["task_id"], "enqueue_deduplicated", {"source_kind": source_kind})
        else:
            task_id = uuid.uuid4().hex
            conn.execute(
                """
                insert into codex_tasks(
                    task_id,source_kind,source_id,lane,status,priority,payload_json,
                    work_fingerprint,work_scope,created_at,updated_at
                ) values(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id, source_kind, source_id_text, lane, status, int(priority),
                    _json(dict(payload or {})), fingerprint, scope, now, now,
                ),
            )
            result = conn.execute("select * from codex_tasks where task_id=?", (task_id,)).fetchone()
            _event(conn, "task", task_id, "enqueued", {"source_kind": source_kind, "lane": lane})
        conn.commit()
        return _row_dict(result) or {}
    except BaseException:
        conn.rollback()
        raise


def heartbeat_worker(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    preferred_lane: str,
    pid: int | None = None,
    state: str = "idle",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Register or refresh a worker's durable heartbeat."""

    now = utc_now()
    conn.execute(
        """
        insert into codex_workers(worker_id,preferred_lane,pid,state,started_at,heartbeat_at,details_json)
        values(?,?,?,?,?,?,?)
        on conflict(worker_id) do update set
            preferred_lane=excluded.preferred_lane, pid=excluded.pid, state=excluded.state,
            heartbeat_at=excluded.heartbeat_at, details_json=excluded.details_json
        """,
        (worker_id, preferred_lane, pid, state, now, now, _json(dict(details or {}))),
    )
    conn.commit()
    return _row_dict(conn.execute("select * from codex_workers where worker_id=?", (worker_id,)).fetchone()) or {}


def set_task_work_identity(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    work_fingerprint: str,
    work_scope: str,
) -> bool:
    """Backfill a canonical identity for work queued before schema version 2."""

    updated = conn.execute(
        """
        update codex_tasks set work_fingerprint=?,work_scope=?,updated_at=?
        where task_id=?
        """,
        (work_fingerprint, work_scope, utc_now(), task_id),
    ).rowcount
    conn.commit()
    return updated == 1


def reconcile_duplicate_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Collapse claimable tasks that share work already owned by a peer."""

    blocking = tuple(sorted(DEDUPLICATION_BLOCKING_STATUSES))
    claimable = tuple(sorted(CLAIMABLE_TASK_STATUSES))
    fingerprints = conn.execute(
        """
        select work_fingerprint from codex_tasks
        where work_fingerprint is not null and work_fingerprint<>'' and status in (%s)
        group by work_fingerprint having count(*)>1
        """ % ",".join("?" for _ in blocking),
        blocking,
    ).fetchall()
    superseded: list[dict[str, Any]] = []
    _begin(conn)
    try:
        now = utc_now()
        for fingerprint_row in fingerprints:
            fingerprint = str(fingerprint_row["work_fingerprint"])
            rows = conn.execute(
                """
                select * from codex_tasks where work_fingerprint=? and status in (%s)
                order by case
                    when status in ('verified','promoted') then 0
                    when status in ('promoted_pending_verification','verifying','candidate_committed') then 1
                    when status in ('claimed','coding','focused_tests','repairing','repairing_post_promotion') then 2
                    else 3 end,
                    priority desc,created_at asc,task_id asc
                """ % ",".join("?" for _ in blocking),
                (fingerprint, *blocking),
            ).fetchall()
            if len(rows) < 2:
                continue
            canonical = rows[0]
            for duplicate in rows[1:]:
                if str(duplicate["status"]) not in CLAIMABLE_TASK_STATUSES:
                    continue
                result = {
                    "reason": "equivalent_work_reconciled",
                    "canonical_task_id": str(canonical["task_id"]),
                    "canonical_source_kind": str(canonical["source_kind"]),
                    "canonical_source_id": str(canonical["source_id"]),
                    "work_scope": str(canonical["work_scope"] or ""),
                }
                updated = conn.execute(
                    """
                    update codex_tasks set status='superseded_duplicate',canonical_task_id=?,
                        completed_at=?,updated_at=?,retry_at=null,result_json=?,last_error=null
                    where task_id=? and status in (%s)
                    """ % ",".join("?" for _ in claimable),
                    (
                        canonical["task_id"], now, now, _json(result), duplicate["task_id"],
                        *claimable,
                    ),
                ).rowcount
                if not updated:
                    continue
                _event(conn, "task", str(duplicate["task_id"]), "superseded_duplicate", result)
                superseded.append(
                    {
                        "task_id": str(duplicate["task_id"]),
                        "source_kind": str(duplicate["source_kind"]),
                        "source_id": str(duplicate["source_id"]),
                        "canonical_task_id": str(canonical["task_id"]),
                        "canonical_source_id": str(canonical["source_id"]),
                        "work_fingerprint": fingerprint,
                        "work_scope": str(canonical["work_scope"] or ""),
                    }
                )
        conn.commit()
        return superseded
    except BaseException:
        conn.rollback()
        raise


def peer_work_context(
    conn: sqlite3.Connection,
    *,
    task_id: str | None = None,
    limit: int = 16,
) -> dict[str, Any]:
    """Return compact shared work memory for a coding session."""

    active = conn.execute(
        """
        select task_id,source_kind,source_id,lane,status,claimed_by,
               work_fingerprint,work_scope,payload_json
        from codex_tasks
        where task_id<>coalesce(?, '') and status in (%s)
        order by updated_at desc limit ?
        """ % ",".join("?" for _ in ACTIVE_TASK_STATUSES),
        (task_id, *sorted(ACTIVE_TASK_STATUSES), int(limit)),
    ).fetchall()
    recent = conn.execute(
        """
        select task_id,source_kind,source_id,lane,status,null as claimed_by,
               work_fingerprint,work_scope,payload_json
        from codex_tasks
        where task_id<>coalesce(?, '')
          and status in ('promoted','verified','promoted_pending_verification')
        order by updated_at desc limit ?
        """,
        (task_id, int(limit)),
    ).fetchall()

    def compact(row: sqlite3.Row) -> dict[str, Any]:
        payload = _decode_json(row["payload_json"])
        return {
            "task_id": str(row["task_id"]),
            "source_kind": str(row["source_kind"]),
            "source_id": str(row["source_id"]),
            "lane": str(row["lane"]),
            "status": str(row["status"]),
            "worker": str(row["claimed_by"] or ""),
            "work_fingerprint": str(row["work_fingerprint"] or ""),
            "work_scope": str(row["work_scope"] or ""),
            "title": str(payload.get("title") or ""),
        }

    return {
        "active_peer_work": [compact(row) for row in active],
        "recent_completed_work": [compact(row) for row in recent],
    }


def _default_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_reclaimable(
    *,
    expires_at: str | None,
    pid: int | None,
    now: dt.datetime,
    pid_alive: Callable[[int | None], bool],
) -> bool:
    expiry = _parse_time(expires_at)
    return bool(expiry and expiry <= now and not pid_alive(pid))


def reclaim_stale_tasks(
    conn: sqlite3.Connection,
    *,
    pid_alive: Callable[[int | None], bool] = _default_pid_alive,
) -> int:
    """Return dead, expired worker claims to the queue without touching live owners."""

    now_dt = dt.datetime.now(dt.timezone.utc)
    now = now_dt.isoformat()
    _begin(conn)
    try:
        rows = conn.execute(
            """
            select task_id,claim_pid from codex_tasks
            where status in (%s) and lease_expires_at is not null and lease_expires_at <= ?
            """ % ",".join("?" for _ in ACTIVE_TASK_STATUSES),
            (*sorted(ACTIVE_TASK_STATUSES), now),
        ).fetchall()
        reclaimed = 0
        for row in rows:
            if not _is_reclaimable(
                expires_at=conn.execute(
                    "select lease_expires_at from codex_tasks where task_id=?", (row["task_id"],)
                ).fetchone()[0],
                pid=row["claim_pid"],
                now=now_dt,
                pid_alive=pid_alive,
            ):
                continue
            conn.execute(
                """
                update codex_tasks set status='requeued', retry_at=?, updated_at=?, claimed_by=null,
                    claim_pid=null, claim_token=null, claimed_at=null, lease_expires_at=null,
                    last_error='stale_worker_lease_reclaimed'
                where task_id=?
                """,
                (now, now, row["task_id"]),
            )
            _event(conn, "task", row["task_id"], "stale_reclaimed", {})
            reclaimed += 1
        conn.commit()
        return reclaimed
    except BaseException:
        conn.rollback()
        raise


def claim_task(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    preferred_lane: str,
    pid: int | None = None,
    lease_seconds: float = 900,
    pid_alive: Callable[[int | None], bool] = _default_pid_alive,
) -> dict[str, Any] | None:
    """Atomically claim a preferred-lane task, falling back to work stealing."""

    reclaim_stale_tasks(conn, pid_alive=pid_alive)
    now = utc_now()
    token = uuid.uuid4().hex
    _begin(conn)
    try:
        statuses = tuple(sorted(CLAIMABLE_TASK_STATUSES))
        active_peer_statuses = tuple(
            sorted(DEDUPLICATION_BLOCKING_STATUSES - CLAIMABLE_TASK_STATUSES)
        )
        row = conn.execute(
            """
            select t.* from codex_tasks t
            where t.status in (%s) and (t.retry_at is null or t.retry_at <= ?)
              and (
                t.work_fingerprint is null or t.work_fingerprint='' or not exists (
                    select 1 from codex_tasks peer
                    where peer.task_id<>t.task_id
                      and peer.work_fingerprint=t.work_fingerprint
                      and peer.status in (%s)
                )
              )
            order by case when t.lane=? then 0 else 1 end,
                     t.priority desc, t.created_at asc, t.task_id asc
            limit 1
            """ % (
                ",".join("?" for _ in statuses),
                ",".join("?" for _ in active_peer_statuses),
            ),
            (*statuses, now, *active_peer_statuses, preferred_lane),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        updated = conn.execute(
            """
            update codex_tasks
            set status='claimed', claimed_by=?, claim_pid=?, claim_token=?, claimed_at=?,
                lease_expires_at=?, updated_at=?, claim_count=claim_count+1, retry_at=null
            where task_id=? and status in (%s)
            """ % ",".join("?" for _ in statuses),
            (worker_id, pid, token, now, _utc_after(lease_seconds), now, row["task_id"], *statuses),
        ).rowcount
        if updated != 1:
            conn.rollback()
            return None
        if row["work_fingerprint"]:
            duplicate_rows = conn.execute(
                """
                select task_id,source_kind,source_id from codex_tasks
                where task_id<>? and work_fingerprint=? and status in (%s)
                """ % ",".join("?" for _ in statuses),
                (row["task_id"], row["work_fingerprint"], *statuses),
            ).fetchall()
            for duplicate in duplicate_rows:
                duplicate_result = {
                    "reason": "equivalent_work_claimed_by_peer",
                    "canonical_task_id": str(row["task_id"]),
                    "canonical_source_kind": str(row["source_kind"]),
                    "canonical_source_id": str(row["source_id"]),
                }
                conn.execute(
                    """
                    update codex_tasks set status='superseded_duplicate', canonical_task_id=?,
                        completed_at=?, updated_at=?, retry_at=null, result_json=?, last_error=null
                    where task_id=?
                    """,
                    (row["task_id"], now, now, _json(duplicate_result), duplicate["task_id"]),
                )
                _event(
                    conn,
                    "task",
                    str(duplicate["task_id"]),
                    "superseded_duplicate",
                    duplicate_result,
                )
        result = conn.execute("select * from codex_tasks where task_id=?", (row["task_id"],)).fetchone()
        _event(
            conn,
            "task",
            row["task_id"],
            "claimed",
            {"worker_id": worker_id, "preferred_lane": preferred_lane, "work_stolen": row["lane"] != preferred_lane},
        )
        conn.commit()
        return _row_dict(result)
    except BaseException:
        conn.rollback()
        raise


def renew_task_lease(
    conn: sqlite3.Connection,
    task_id: str,
    worker_id: str,
    claim_token: str,
    *,
    lease_seconds: float = 900,
    state: str = "coding",
) -> bool:
    now = utc_now()
    updated = conn.execute(
        """
        update codex_tasks set status=?, lease_expires_at=?, updated_at=?
        where task_id=? and claimed_by=? and claim_token=? and status in (%s)
        """ % ",".join("?" for _ in ACTIVE_TASK_STATUSES),
        (state, _utc_after(lease_seconds), now, task_id, worker_id, claim_token, *sorted(ACTIVE_TASK_STATUSES)),
    ).rowcount
    conn.commit()
    return updated == 1


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    worker_id: str | None = None,
    claim_token: str | None = None,
    status: str = "completed",
    result: Mapping[str, Any] | None = None,
) -> bool:
    """Complete a claimed task. Owner information protects against stale workers."""

    now = utc_now()
    where = ["task_id=?"]
    params: list[Any] = [task_id]
    if worker_id is not None:
        where.append("claimed_by=?")
        params.append(worker_id)
    if claim_token is not None:
        where.append("claim_token=?")
        params.append(claim_token)
    conn.execute("begin immediate")
    try:
        updated = conn.execute(
            """
            update codex_tasks set status=?, completed_at=?, updated_at=?, result_json=?,
                claimed_by=null, claim_pid=null, claim_token=null, lease_expires_at=null
            where %s
            """ % " and ".join(where),
            (status, now, now, _json(dict(result or {})), *params),
        ).rowcount
        if updated:
            _event(conn, "task", task_id, "completed", {"status": status})
        conn.commit()
        return updated == 1
    except BaseException:
        conn.rollback()
        raise


def requeue_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    worker_id: str | None = None,
    claim_token: str | None = None,
    delay_seconds: float = 0,
    error: str | None = None,
    status: str = "requeued",
) -> bool:
    """Release a task for a future owner while retaining its source and history."""

    where = ["task_id=?"]
    params: list[Any] = [task_id]
    if worker_id is not None:
        where.append("claimed_by=?")
        params.append(worker_id)
    if claim_token is not None:
        where.append("claim_token=?")
        params.append(claim_token)
    now = utc_now()
    _begin(conn)
    try:
        updated = conn.execute(
            """
            update codex_tasks set status=?, retry_at=?, updated_at=?, last_error=?,
                claimed_by=null, claim_pid=null, claim_token=null, claimed_at=null, lease_expires_at=null
            where %s
            """ % " and ".join(where),
            (status, _utc_after(delay_seconds), now, error, *params),
        ).rowcount
        if updated:
            _event(conn, "task", task_id, "requeued", {"status": status, "error": error})
        conn.commit()
        return updated == 1
    except BaseException:
        conn.rollback()
        raise


def acquire_resource_lease(
    conn: sqlite3.Connection,
    resource_name: str,
    worker_id: str,
    *,
    pid: int | None = None,
    lease_seconds: float = 60,
    details: Mapping[str, Any] | None = None,
    pid_alive: Callable[[int | None], bool] = _default_pid_alive,
) -> dict[str, Any] | None:
    """Acquire a named atomic resource lease, for example ``main_promotion``."""

    now_dt = dt.datetime.now(dt.timezone.utc)
    now = now_dt.isoformat()
    _begin(conn)
    try:
        current = conn.execute(
            "select * from codex_resource_leases where resource_name=?", (resource_name,)
        ).fetchone()
        can_take = current is None or current["owner_worker_id"] == worker_id
        if current is not None and not can_take:
            can_take = _is_reclaimable(
                expires_at=current["lease_expires_at"],
                pid=current["owner_pid"],
                now=now_dt,
                pid_alive=pid_alive,
            )
        if not can_take:
            conn.commit()
            return None
        token = current["lease_token"] if current and current["owner_worker_id"] == worker_id else uuid.uuid4().hex
        conn.execute(
            """
            insert into codex_resource_leases(
                resource_name,owner_worker_id,owner_pid,lease_token,acquired_at,updated_at,lease_expires_at,details_json
            ) values(?,?,?,?,?,?,?,?)
            on conflict(resource_name) do update set
                owner_worker_id=excluded.owner_worker_id, owner_pid=excluded.owner_pid,
                lease_token=excluded.lease_token, updated_at=excluded.updated_at,
                lease_expires_at=excluded.lease_expires_at, details_json=excluded.details_json
            """,
            (resource_name, worker_id, pid, token, now, now, _utc_after(lease_seconds), _json(dict(details or {}))),
        )
        _event(conn, "resource", resource_name, "lease_acquired", {"worker_id": worker_id})
        result = conn.execute(
            "select * from codex_resource_leases where resource_name=?", (resource_name,)
        ).fetchone()
        conn.commit()
        return _row_dict(result)
    except BaseException:
        conn.rollback()
        raise


def renew_resource_lease(
    conn: sqlite3.Connection,
    resource_name: str,
    worker_id: str,
    lease_token: str,
    *,
    lease_seconds: float = 60,
) -> bool:
    updated = conn.execute(
        """
        update codex_resource_leases set updated_at=?, lease_expires_at=?
        where resource_name=? and owner_worker_id=? and lease_token=?
        """,
        (utc_now(), _utc_after(lease_seconds), resource_name, worker_id, lease_token),
    ).rowcount
    conn.commit()
    return updated == 1


def release_resource_lease(
    conn: sqlite3.Connection, resource_name: str, worker_id: str, lease_token: str
) -> bool:
    _begin(conn)
    try:
        deleted = conn.execute(
            "delete from codex_resource_leases where resource_name=? and owner_worker_id=? and lease_token=?",
            (resource_name, worker_id, lease_token),
        ).rowcount
        if deleted:
            _event(conn, "resource", resource_name, "lease_released", {"worker_id": worker_id})
        conn.commit()
        return deleted == 1
    except BaseException:
        conn.rollback()
        raise


def enqueue_verification_job(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    verification_kind: str = "full_regression",
    priority: int = 0,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Idempotently enqueue follow-up verification for a promoted candidate."""

    now = utc_now()
    _begin(conn)
    try:
        if conn.execute("select 1 from codex_tasks where task_id=?", (task_id,)).fetchone() is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        existing = conn.execute(
            "select * from codex_verification_jobs where task_id=? and verification_kind=?",
            (task_id, verification_kind),
        ).fetchone()
        if existing is not None:
            previous_payload = _decode_json(existing["payload_json"])
            next_payload = dict(payload or previous_payload)
            changed_revision = next_payload != previous_payload
            if changed_revision and str(existing["status"]) not in CLAIMABLE_VERIFICATION_STATUSES | {"claimed"}:
                conn.execute(
                    """
                    update codex_verification_jobs set status='queued', priority=?, payload_json=?, result_json=null,
                        claimed_by=null, claim_pid=null, claim_token=null, claimed_at=null,
                        lease_expires_at=null, retry_at=null, completed_at=null, updated_at=?
                    where job_id=?
                    """,
                    (max(int(existing["priority"]), int(priority)), _json(next_payload), now, existing["job_id"]),
                )
                _event(conn, "verification", existing["job_id"], "reactivated_for_new_revision", {})
            else:
                conn.execute(
                    """
                    update codex_verification_jobs set priority=?, payload_json=?, updated_at=?
                    where job_id=?
                    """,
                    (max(int(existing["priority"]), int(priority)), _json(next_payload), now, existing["job_id"]),
                )
            result = conn.execute(
                "select * from codex_verification_jobs where job_id=?", (existing["job_id"],)
            ).fetchone()
            _event(conn, "verification", existing["job_id"], "enqueue_deduplicated", {})
        else:
            job_id = uuid.uuid4().hex
            conn.execute(
                """
                insert into codex_verification_jobs(
                    job_id,task_id,verification_kind,status,priority,payload_json,created_at,updated_at
                ) values(?,?,?,?,?,?,?,?)
                """,
                (job_id, task_id, verification_kind, "queued", int(priority), _json(dict(payload or {})), now, now),
            )
            result = conn.execute(
                "select * from codex_verification_jobs where job_id=?", (job_id,)
            ).fetchone()
            _event(conn, "verification", job_id, "enqueued", {"task_id": task_id})
        conn.commit()
        return _row_dict(result) or {}
    except BaseException:
        conn.rollback()
        raise


def claim_verification_job(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    pid: int | None = None,
    lease_seconds: float = 1800,
) -> dict[str, Any] | None:
    """Atomically claim the highest-priority due verification job."""

    now = utc_now()
    token = uuid.uuid4().hex
    _begin(conn)
    try:
        statuses = tuple(sorted(CLAIMABLE_VERIFICATION_STATUSES))
        row = conn.execute(
            """
            select * from codex_verification_jobs
            where status in (%s) and (retry_at is null or retry_at <= ?)
            order by priority desc, created_at asc, job_id asc limit 1
            """ % ",".join("?" for _ in statuses),
            (*statuses, now),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            """
            update codex_verification_jobs set status='claimed', claimed_by=?, claim_pid=?, claim_token=?,
                claimed_at=?, lease_expires_at=?, updated_at=?, retry_at=null
            where job_id=?
            """,
            (worker_id, pid, token, now, _utc_after(lease_seconds), now, row["job_id"]),
        )
        result = conn.execute(
            "select * from codex_verification_jobs where job_id=?", (row["job_id"],)
        ).fetchone()
        _event(conn, "verification", row["job_id"], "claimed", {"worker_id": worker_id})
        conn.commit()
        return _row_dict(result)
    except BaseException:
        conn.rollback()
        raise


def finish_verification_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    worker_id: str,
    claim_token: str,
    status: str,
    result: Mapping[str, Any] | None = None,
    task_status: str | None = None,
    requeue_after_seconds: float | None = None,
) -> bool:
    """Finish or requeue a claimed verification job and optionally transition its task."""

    now = utc_now()
    _begin(conn)
    try:
        job = conn.execute("select * from codex_verification_jobs where job_id=?", (job_id,)).fetchone()
        if job is None or job["claimed_by"] != worker_id or job["claim_token"] != claim_token:
            conn.rollback()
            return False
        retry_at = _utc_after(requeue_after_seconds) if requeue_after_seconds is not None else None
        completed_at = None if requeue_after_seconds is not None else now
        conn.execute(
            """
            update codex_verification_jobs set status=?, result_json=?, completed_at=?, retry_at=?,
                updated_at=?, claimed_by=null, claim_pid=null, claim_token=null, lease_expires_at=null
            where job_id=?
            """,
            (status, _json(dict(result or {})), completed_at, retry_at, now, job_id),
        )
        if task_status:
            conn.execute(
                "update codex_tasks set status=?, updated_at=? where task_id=?",
                (task_status, now, job["task_id"]),
            )
        _event(conn, "verification", job_id, "finished", {"status": status, "task_status": task_status})
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def coordination_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a JSON-serializable snapshot suitable for reports and state packets."""

    status_rows = conn.execute("select status,count(*) as count from codex_tasks group by status").fetchall()
    verification_rows = conn.execute(
        "select status,count(*) as count from codex_verification_jobs group by status"
    ).fetchall()
    worker_rows = conn.execute("select * from codex_workers order by worker_id").fetchall()
    lane_rows = conn.execute(
        "select lane,status,count(*) as count from codex_tasks group by lane,status order by lane,status"
    ).fetchall()
    source_rows = conn.execute(
        "select source_kind,status,count(*) as count from codex_tasks group by source_kind,status order by source_kind,status"
    ).fetchall()
    promotion_count = conn.execute(
        "select count(*) from codex_tasks where status in ('promoted','promoted_pending_verification','verified')"
    ).fetchone()[0]
    repair_count = conn.execute("select count(*) from codex_tasks where status like 'repairing%' ").fetchone()[0]
    queue_depth = conn.execute(
        "select count(*) from codex_tasks where status in ('queued','requeued') and (retry_at is null or retry_at<=?)",
        (utc_now(),),
    ).fetchone()[0]
    duplicate_count = conn.execute(
        "select count(*) from codex_tasks where status='superseded_duplicate'"
    ).fetchone()[0]
    shared_work = peer_work_context(conn, limit=12)
    return {
        "generated_at": utc_now(),
        "schema_version": SCHEMA_VERSION,
        "queue_depth": int(queue_depth),
        "task_statuses": {str(row["status"]): int(row["count"]) for row in status_rows},
        "tasks_by_lane": {
            lane: {
                str(row["status"]): int(row["count"])
                for row in lane_rows if str(row["lane"]) == lane
            }
            for lane in sorted({str(row["lane"]) for row in lane_rows})
        },
        "tasks_by_source": {
            source: {
                str(row["status"]): int(row["count"])
                for row in source_rows if str(row["source_kind"]) == source
            }
            for source in sorted({str(row["source_kind"]) for row in source_rows})
        },
        "verification_statuses": {str(row["status"]): int(row["count"]) for row in verification_rows},
        "promotions": int(promotion_count),
        "repairs": int(repair_count),
        "deduplicated_tasks": int(duplicate_count),
        "shared_work": shared_work,
        "workers": [_row_dict(row) for row in worker_rows],
        "migrations": migration_metadata(conn),
    }
