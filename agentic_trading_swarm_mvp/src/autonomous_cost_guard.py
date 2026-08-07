"""Persisted daily gate for autonomous paid-model work.

Only code running inside an explicit autonomous scope is gated. Manual tools,
offline tests, and the separately budgeted concurrent Codex worker pool remain
unscoped unless their caller deliberately opts in.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import os
import pathlib
import sqlite3
import uuid
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_LEDGER_PATH = ROOT / "runs" / "autonomous_paid_attempts.sqlite"

_SCOPE_ENV = "RADAR_AUTONOMOUS_PAID_SCOPE"
_SCOPE_ID_ENV = "RADAR_AUTONOMOUS_PAID_SCOPE_ID"
_GUARD_ENABLED_ENV = "RADAR_AUTONOMOUS_PAID_GUARD_ENABLED"
_DAILY_LIMIT_ENV = "RADAR_AUTONOMOUS_PAID_DAILY_LIMIT"
_LEDGER_PATH_ENV = "RADAR_AUTONOMOUS_PAID_LEDGER"
_SCOPE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "radar_autonomous_paid_scope",
    default=None,
)


def _guard_config(settings: dict) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "enabled": True,
        "daily_paid_attempt_limit": 10,
        "ledger_path": str(DEFAULT_LEDGER_PATH),
    }
    configured = settings.get("autonomous_cost_guard") or {}
    cfg = {**defaults, **configured}
    path = pathlib.Path(str(cfg.get("ledger_path") or DEFAULT_LEDGER_PATH)).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    cfg["ledger_path"] = str(path.resolve())
    cfg["daily_paid_attempt_limit"] = max(0, int(cfg.get("daily_paid_attempt_limit", 10)))
    cfg["enabled"] = bool(cfg.get("enabled", True))
    return cfg


def _scope_from_environment() -> dict[str, Any] | None:
    if os.environ.get(_SCOPE_ENV) != "1":
        return None
    try:
        limit = max(0, int(os.environ.get(_DAILY_LIMIT_ENV, "10")))
    except ValueError:
        limit = 0
    path = pathlib.Path(os.environ.get(_LEDGER_PATH_ENV) or DEFAULT_LEDGER_PATH).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return {
        "scope_id": os.environ.get(_SCOPE_ID_ENV) or "inherited-autonomous-scope",
        "enabled": os.environ.get(_GUARD_ENABLED_ENV, "1") == "1",
        "daily_paid_attempt_limit": limit,
        "ledger_path": str(path.resolve()),
        "inherited": True,
    }


def current_autonomous_scope() -> dict[str, Any] | None:
    scope = _SCOPE.get()
    if scope is None:
        scope = _scope_from_environment()
    return dict(scope) if scope is not None else None


@contextmanager
def autonomous_paid_scope(
    settings: dict,
    *,
    source: str,
    scope_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Mark a call tree (and its subprocesses) as autonomous paid work."""

    cfg = _guard_config(settings)
    scope = {
        **cfg,
        "source": str(source),
        "scope_id": str(scope_id or f"{source}:{uuid.uuid4()}"),
        "inherited": False,
    }
    token = _SCOPE.set(scope)
    inherited_values = {
        _SCOPE_ENV: "1",
        _SCOPE_ID_ENV: str(scope["scope_id"]),
        _GUARD_ENABLED_ENV: "1" if scope["enabled"] else "0",
        _DAILY_LIMIT_ENV: str(scope["daily_paid_attempt_limit"]),
        _LEDGER_PATH_ENV: str(scope["ledger_path"]),
    }
    previous = {name: os.environ.get(name) for name in inherited_values}
    os.environ.update(inherited_values)
    try:
        yield dict(scope)
    finally:
        _SCOPE.reset(token)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def autonomous_paid_scope_for(source: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a function whose first argument is the active settings dict."""

    def decorate(callback: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(callback)
        def guarded(settings: dict, *args: Any, **kwargs: Any) -> Any:
            with autonomous_paid_scope(settings, source=source):
                return callback(settings, *args, **kwargs)

        return guarded

    return decorate


def _connect(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=5000")
    conn.execute(
        """
        create table if not exists autonomous_paid_attempts (
            attempt_id text primary key,
            created_at text not null,
            day_utc text not null,
            scope_id text not null,
            source text not null,
            agent_name text,
            operation text not null,
            metadata_json text not null
        )
        """
    )
    conn.execute(
        "create index if not exists idx_autonomous_paid_attempts_day on autonomous_paid_attempts(day_utc)"
    )
    return conn


def _unguarded_status(reason: str) -> dict[str, Any]:
    return {
        "allowed": True,
        "guarded": False,
        "claimed": False,
        "status": reason,
    }


def autonomous_paid_attempt_status() -> dict[str, Any]:
    """Read remaining capacity without reserving it; failures are fail-closed."""

    scope = current_autonomous_scope()
    if scope is None:
        return _unguarded_status("manual_or_unscoped")
    if not scope.get("enabled", True):
        return _unguarded_status("autonomous_cost_guard_disabled")

    limit = max(0, int(scope.get("daily_paid_attempt_limit", 10)))
    day_utc = dt.datetime.now(dt.timezone.utc).date().isoformat()
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(pathlib.Path(str(scope["ledger_path"])))
        row = conn.execute(
            "select count(*) as used from autonomous_paid_attempts where day_utc=?",
            (day_utc,),
        ).fetchone()
        used = int(row["used"] or 0)
    except (OSError, sqlite3.Error) as exc:
        return {
            "allowed": False,
            "guarded": True,
            "claimed": False,
            "status": "autonomous_cost_guard_unavailable",
            "reason": f"autonomous_cost_guard_unavailable:{type(exc).__name__}",
            "daily_paid_attempt_limit": limit,
        }
    finally:
        if conn is not None:
            conn.close()

    allowed = used < limit
    return {
        "allowed": allowed,
        "guarded": True,
        "claimed": False,
        "status": "autonomous_paid_attempt_capacity" if allowed else "autonomous_daily_paid_attempt_limit",
        "reason": (
            "autonomous_paid_attempt_budget_available"
            if allowed
            else f"autonomous_daily_paid_attempt_budget_exhausted:{used}/{limit}"
        ),
        "day_utc": day_utc,
        "used": used,
        "remaining": max(0, limit - used),
        "daily_paid_attempt_limit": limit,
        "ledger_path": str(scope["ledger_path"]),
    }


def claim_autonomous_paid_attempt(
    *,
    agent_name: str,
    operation: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically reserve one autonomous paid attempt for the current UTC day."""

    scope = current_autonomous_scope()
    if scope is None:
        return _unguarded_status("manual_or_unscoped")
    if not scope.get("enabled", True):
        return _unguarded_status("autonomous_cost_guard_disabled")

    limit = max(0, int(scope.get("daily_paid_attempt_limit", 10)))
    now = dt.datetime.now(dt.timezone.utc)
    day_utc = now.date().isoformat()
    path = pathlib.Path(str(scope["ledger_path"]))
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        conn.execute("begin immediate")
        row = conn.execute(
            "select count(*) as used from autonomous_paid_attempts where day_utc=?",
            (day_utc,),
        ).fetchone()
        used = int(row["used"] or 0)
        if used >= limit:
            conn.commit()
            return {
                "allowed": False,
                "guarded": True,
                "claimed": False,
                "status": "autonomous_daily_paid_attempt_limit",
                "reason": f"autonomous_daily_paid_attempt_budget_exhausted:{used}/{limit}",
                "day_utc": day_utc,
                "used": used,
                "remaining": 0,
                "daily_paid_attempt_limit": limit,
                "ledger_path": str(path),
            }

        attempt_id = str(uuid.uuid4())
        conn.execute(
            """
            insert into autonomous_paid_attempts (
                attempt_id,created_at,day_utc,scope_id,source,agent_name,operation,metadata_json
            ) values (?,?,?,?,?,?,?,?)
            """,
            (
                attempt_id,
                now.isoformat(),
                day_utc,
                str(scope.get("scope_id") or "autonomous"),
                str(scope.get("source") or "inherited_autonomous_process"),
                str(agent_name or "unknown"),
                str(operation or "model_call"),
                json.dumps(metadata or {}, sort_keys=True, default=str),
            ),
        )
        conn.commit()
        return {
            "allowed": True,
            "guarded": True,
            "claimed": True,
            "status": "autonomous_paid_attempt_claimed",
            "reason": "autonomous_paid_attempt_budget_claimed",
            "attempt_id": attempt_id,
            "day_utc": day_utc,
            "used": used + 1,
            "remaining": max(0, limit - used - 1),
            "daily_paid_attempt_limit": limit,
            "ledger_path": str(path),
        }
    except (OSError, sqlite3.Error) as exc:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        return {
            "allowed": False,
            "guarded": True,
            "claimed": False,
            "status": "autonomous_cost_guard_unavailable",
            "reason": f"autonomous_cost_guard_unavailable:{type(exc).__name__}",
            "daily_paid_attempt_limit": limit,
            "ledger_path": str(path),
        }
    finally:
        if conn is not None:
            conn.close()
