"""Cost-aware model router with LiteLLM optional support.

Model spend is allowed only when RADAR_USE_LITELLM=1 and the selected provider
has credentials in the environment. Otherwise the router logs a no-cost fallback.
OpenAI GPT-5.x tiers prefer the native Responses API so reasoning effort,
verbosity, structured JSON, and prompt caching are explicit.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import sqlite3
import uuid
from dataclasses import dataclass

from autonomous_cost_guard import autonomous_paid_attempt_status, claim_autonomous_paid_attempt
from storage import connect, record_llm_cost_event


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "llm_config.example.yaml"
COST_LOG_DEFERRED_PATH = ROOT / "runs" / "llm_cost_events_deferred.jsonl"
QUOTA_STATE_PATH = ROOT / "runs" / "llm_quota_state.json"
MAX_GLOBAL_BUDGET_USD = 25.0
MAX_GLOBAL_CALLS = 10

PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "azure": "AZURE_API_KEY",
    "cohere": "COHERE_API_KEY",
}


@dataclass
class ModelResult:
    text: str
    model_name: str
    model_tier: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    status: str
    provider: str = ""
    api: str = ""
    reasoning_effort: str | None = None
    reasoning_mode: str | None = None
    verbosity: str | None = None
    operation: str | None = None
    prompt_cache_key: str | None = None
    frontier_escalation_reason: str | None = None
    structured_json: bool = False
    max_output_tokens: int | None = None
    stop_reason: str | None = None
    event_id: str | None = None
    created_at: str | None = None


def load_llm_config(path: pathlib.Path = CONFIG_PATH) -> dict:
    raw = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            return yaml.safe_load(raw)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Cannot parse LLM config {path}: {exc}") from exc


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


def _cost_usd(prompt_tokens: int, completion_tokens: int, tier_cfg: dict) -> float:
    return (
        (prompt_tokens / 1_000_000.0) * float(tier_cfg.get("input_cost_per_1m", 0.0))
        + (completion_tokens / 1_000_000.0) * float(tier_cfg.get("output_cost_per_1m", 0.0))
    )


def _fallback_response(agent_name: str, prompt: str) -> str:
    return json.dumps(
        {
            "action": "propose_hunter_directive",
            "priority": 55,
            "title": f"{agent_name} fallback recommendation",
            "rationale": "No configured LiteLLM model call was made; fallback agent recommends continued evidence collection and route discovery.",
            "market_key": "fallback_llm_bridge",
            "directive": "continue_low_cost_research",
            "evidence": {"agent": agent_name, "prompt_chars": len(prompt), "mode": "fallback"},
            "proposed_change": "Keep deterministic radar running and configure LiteLLM/local model for richer inference.",
        },
        sort_keys=True,
    )


def _provider_key_env(model_name: str) -> str | None:
    if "/" not in model_name:
        return "OPENAI_API_KEY"
    provider = model_name.split("/", 1)[0].lower()
    if provider in {"ollama", "vllm", "sglang", "local"}:
        return None
    return PROVIDER_KEY_ENV.get(provider)


def _provider_name(model_name: str) -> str:
    if "/" not in model_name:
        return "openai"
    return model_name.split("/", 1)[0].lower()


def _provider_model_name(model_name: str) -> str:
    provider = _provider_name(model_name)
    if provider == "openai" and "/" in model_name:
        return model_name.split("/", 1)[1]
    return model_name


def _provider_ready(model_name: str) -> tuple[bool, str]:
    key_name = _provider_key_env(model_name)
    if key_name is None:
        return True, "provider_no_key_required"
    if os.environ.get(key_name):
        return True, f"provider_key_present:{key_name}"
    return False, f"fallback_missing_provider_key:{key_name}"


def _model_credentials_locked() -> bool:
    """Return True when operations deliberately prohibit all provider calls."""

    values = {
        str(os.environ.get("RADAR_MODEL_CREDENTIAL_LOCK") or "").strip().lower(),
        str(os.environ.get("RADAR_MODELS_DISABLED") or "").strip().lower(),
    }
    locked = bool(values & {"1", "true", "yes", "locked", "disabled"})
    research_override = (
        str(os.environ.get("RADAR_PROCESS_ROLE") or "").strip().lower() == "research_one_shot"
        and str(os.environ.get("RADAR_RESEARCH_MODEL_OVERRIDE") or "").strip().lower()
        in {"1", "true", "yes"}
    )
    return locked and not research_override


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _read_quota_state() -> dict:
    try:
        value = json.loads(QUOTA_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _quota_circuit_status() -> dict | None:
    state = _read_quota_state()
    next_probe = str(state.get("next_probe_at") or "")
    if not next_probe:
        return None
    try:
        parsed = dt.datetime.fromisoformat(next_probe.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    if _utc_now() >= parsed:
        return None
    return state


def _write_quota_state(state: dict) -> None:
    QUOTA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = QUOTA_STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, QUOTA_STATE_PATH)


def _mark_quota_failure(status: str) -> None:
    lowered = status.lower()
    if "insufficient_quota" not in lowered and "credit_balance" not in lowered and "429" not in lowered:
        return
    previous = _read_quota_state()
    failures = int(previous.get("consecutive_failures") or 0) + 1
    cooldown_minutes = min(30, 5 * (2 ** min(3, failures - 1)))
    now = _utc_now()
    _write_quota_state(
        {
            "status": "quota_circuit_open",
            "consecutive_failures": failures,
            "opened_at": previous.get("opened_at") or now.isoformat(),
            "last_failure_at": now.isoformat(),
            "next_probe_at": (now + dt.timedelta(minutes=cooldown_minutes)).isoformat(),
            "cooldown_minutes": cooldown_minutes,
            "last_error": status[:1000],
        }
    )


def _clear_quota_state() -> None:
    try:
        QUOTA_STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def _is_sqlite_locked(exc: BaseException) -> bool:
    return "database is locked" in str(exc).lower()


def _needs_schema_init(exc: BaseException) -> bool:
    return "no such table" in str(exc).lower()


def _paid_attempt_sql() -> str:
    return """
        (
            status = 'model_call'
            or status like 'model_call:%'
            or status = 'model_call_reserved'
            or (event_id is not null and status like 'fallback_error:%')
        )
    """


def _normalise_event_timestamp(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _deferred_event_id(path: pathlib.Path, line_number: int, raw: str) -> str:
    material = f"{path.resolve()}:{line_number}:{raw}".encode("utf-8")
    return "deferred-" + hashlib.sha256(material).hexdigest()


_COST_EVENT_COLUMNS = (
    "event_id",
    "created_at",
    "agent_name",
    "model_tier",
    "model_name",
    "provider",
    "api",
    "reasoning_effort",
    "verbosity",
    "operation",
    "prompt_cache_key",
    "frontier_escalation_reason",
    "structured_json",
    "prompt_tokens",
    "completion_tokens",
    "estimated_cost_usd",
    "status",
)
_RESERVATION_IDENTITY_INDEXES = tuple(range(14))


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _cost_payload_values(payload: dict, *, event_id: str, created_at: str) -> tuple:
    if "estimated_cost_usd" not in payload:
        raise ValueError("deferred cost is missing")
    prompt_tokens = int(payload.get("prompt_tokens", 0) or 0)
    completion_tokens = int(payload.get("completion_tokens", 0) or 0)
    estimated_cost = float(payload["estimated_cost_usd"])
    status = str(payload.get("status") or "").strip()
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("deferred token count is negative")
    if not math.isfinite(estimated_cost) or estimated_cost < 0:
        raise ValueError("deferred cost is invalid")
    if not status:
        raise ValueError("deferred status is missing")
    return (
        event_id,
        created_at,
        str(payload.get("agent_name") or "unknown"),
        str(payload.get("model_tier") or "unknown"),
        str(payload.get("model_name") or "unknown"),
        str(payload.get("provider") or ""),
        str(payload.get("api") or ""),
        _optional_text(payload.get("reasoning_effort")),
        _optional_text(payload.get("verbosity")),
        _optional_text(payload.get("operation")),
        _optional_text(payload.get("prompt_cache_key")),
        _optional_text(payload.get("frontier_escalation_reason")),
        1 if bool(payload.get("structured_json", False)) else 0,
        prompt_tokens,
        completion_tokens,
        estimated_cost,
        status,
    )


def _deferred_report_aliases(report: dict) -> dict:
    report["line_count"] = int(report.get("read", 0) or 0)
    for key in ("invalid", "pending", "reserved", "conflicting", "reconciled"):
        report[f"{key}_count"] = int(report.get(key, 0) or 0)
    return report


def _load_deferred_cost_records(source: pathlib.Path) -> tuple[dict, list[dict]]:
    report = {
        "status": "deferred_cost_log_missing",
        "source_path": str(source.resolve()),
        "source_exists": False,
        "source_digest": hashlib.sha256(b"").hexdigest(),
        "source_size_bytes": 0,
        "read": 0,
        "invalid": 0,
        "pending": 0,
        "reserved": 0,
        "conflicting": 0,
        "reconciled": 0,
        "inferred_timestamps": 0,
        "unique_event_count": 0,
        "expected_cost_usd": 0.0,
        "event_ids_digest": hashlib.sha256(b"").hexdigest(),
        "complete": True,
    }
    if not source.exists():
        return _deferred_report_aliases(report), []
    report["source_exists"] = True
    try:
        with source.open("rb") as handle:
            source_bytes = handle.read()
            file_mtime = dt.datetime.fromtimestamp(
                os.fstat(handle.fileno()).st_mtime,
                tz=dt.timezone.utc,
            )
        report["source_digest"] = hashlib.sha256(source_bytes).hexdigest()
        report["source_size_bytes"] = len(source_bytes)
        text = source_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        report["status"] = "deferred_cost_log_unreadable"
        report["invalid"] = 1
        report["complete"] = False
        return _deferred_report_aliases(report), []

    records: list[dict] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        report["read"] += 1
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("event is not an object")
            event_id = str(payload.get("event_id") or "").strip()
            if not event_id:
                event_id = _deferred_event_id(source, line_number, raw)
            created_at = _normalise_event_timestamp(payload.get("created_at"))
            inferred_timestamp = created_at is None
            if inferred_timestamp:
                created_at = (file_mtime + dt.timedelta(microseconds=line_number)).isoformat()
                report["inferred_timestamps"] += 1
            values = _cost_payload_values(payload, event_id=event_id, created_at=created_at)
        except (TypeError, ValueError, json.JSONDecodeError, OverflowError):
            report["invalid"] += 1
            continue
        records.append(
            {
                "line_number": line_number,
                "event_id": event_id,
                "values": values,
                "inferred_timestamp": inferred_timestamp,
            }
        )

    event_ids = [str(record["event_id"]) for record in records]
    unique_values: dict[str, tuple] = {}
    for record in records:
        unique_values.setdefault(str(record["event_id"]), tuple(record["values"]))
    report["unique_event_count"] = len(unique_values)
    report["expected_cost_usd"] = float(sum(float(values[15]) for values in unique_values.values()))
    report["event_ids_digest"] = hashlib.sha256(
        json.dumps(event_ids, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    report["complete"] = report["invalid"] == 0 and not records
    report["status"] = (
        "deferred_cost_log_invalid"
        if report["invalid"]
        else "deferred_cost_log_reconciled"
        if not records
        else "deferred_cost_log_pending"
    )
    return _deferred_report_aliases(report), records


def _row_cost_values(row: sqlite3.Row | tuple) -> tuple:
    if isinstance(row, sqlite3.Row):
        return tuple(row[column] for column in _COST_EVENT_COLUMNS)
    return tuple(row)


def _reservation_matches(existing: tuple, expected: tuple) -> bool:
    return str(existing[16]) == "model_call_reserved" and all(
        existing[index] == expected[index] for index in _RESERVATION_IDENTITY_INDEXES
    )


def _classify_deferred_cost_records(
    conn: sqlite3.Connection,
    report: dict,
    records: list[dict],
    *,
    include_event_ids: bool,
) -> dict:
    result = dict(report)
    event_ids = [str(record["event_id"]) for record in records]
    expected_by_id: dict[str, tuple] = {}
    source_conflicts: set[str] = set()
    for record in records:
        event_id = str(record["event_id"])
        values = tuple(record["values"])
        previous = expected_by_id.get(event_id)
        if previous is not None and previous != values:
            source_conflicts.add(event_id)
        else:
            expected_by_id[event_id] = values

    category_ids: dict[str, list[str]] = {
        "pending": [],
        "reserved": [],
        "conflicting": [],
        "reconciled": [],
    }
    select_sql = f"select {','.join(_COST_EVENT_COLUMNS)} from llm_cost_events where event_id=?"
    for record in records:
        event_id = str(record["event_id"])
        if event_id in source_conflicts:
            category = "conflicting"
        else:
            existing_row = conn.execute(select_sql, (event_id,)).fetchone()
            if existing_row is None:
                category = "pending"
            else:
                existing = _row_cost_values(existing_row)
                expected_values = list(record["values"])
                if record.get("inferred_timestamp"):
                    # A legacy line has no timestamp of its own.  Once replayed,
                    # its database timestamp is the durable anchor; appending a
                    # new line changes file mtime and must not rewrite history.
                    expected_values[1] = existing[1]
                    record["values"] = tuple(expected_values)
                expected = tuple(expected_values)
                if existing == expected:
                    category = "reconciled"
                elif _reservation_matches(existing, expected):
                    category = "reserved"
                else:
                    category = "conflicting"
        result[category] = int(result.get(category, 0) or 0) + 1
        category_ids[category].append(event_id)

    result["complete"] = (
        int(result.get("invalid", 0) or 0) == 0
        and int(result.get("pending", 0) or 0) == 0
        and int(result.get("reserved", 0) or 0) == 0
        and int(result.get("conflicting", 0) or 0) == 0
    )
    if result["invalid"]:
        result["status"] = "deferred_cost_log_invalid"
    elif result["conflicting"]:
        result["status"] = "deferred_cost_log_conflict"
    elif result["pending"] or result["reserved"]:
        result["status"] = "deferred_cost_log_pending"
    elif result.get("source_exists"):
        result["status"] = "deferred_cost_log_reconciled"
    else:
        result["status"] = "deferred_cost_log_missing"

    if include_event_ids:
        result["event_ids"] = event_ids
        for category, values in category_ids.items():
            result[f"{category}_event_ids"] = list(dict.fromkeys(values))
        result["expected_events"] = [
            {
                "line_number": int(record["line_number"]),
                "event_id": str(record["event_id"]),
                "cost": float(record["values"][15]),
                "estimated_cost_usd": float(record["values"][15]),
                "status": str(record["values"][16]),
            }
            for record in records
        ]
    return _deferred_report_aliases(result)


def deferred_cost_reconciliation_status(
    conn: sqlite3.Connection | None = None,
    path: pathlib.Path | None = None,
    *,
    include_event_ids: bool = False,
) -> dict:
    """Read-only, exact reconciliation status for the append-only cost ledger."""

    source = pathlib.Path(path) if path is not None else COST_LOG_DEFERRED_PATH
    report, records = _load_deferred_cost_records(source)
    if not report.get("source_exists") or report.get("invalid"):
        if include_event_ids:
            report["event_ids"] = [str(record["event_id"]) for record in records]
            report["pending_event_ids"] = []
            report["reserved_event_ids"] = []
            report["conflicting_event_ids"] = []
            report["reconciled_event_ids"] = []
            report["expected_events"] = []
        return _deferred_report_aliases(report)

    owns_connection = conn is None
    working = conn
    try:
        if working is None:
            working = connect(initialize=False)
        columns = {str(row[1]) for row in working.execute("pragma table_info(llm_cost_events)")}
        if not set(_COST_EVENT_COLUMNS).issubset(columns):
            report["status"] = "cost_event_schema_missing"
            report["invalid"] = max(1, int(report.get("invalid", 0) or 0))
            report["complete"] = False
            return _deferred_report_aliases(report)
        return _classify_deferred_cost_records(
            working,
            report,
            records,
            include_event_ids=include_event_ids,
        )
    except (OSError, sqlite3.Error):
        report["status"] = "deferred_cost_reconciliation_failed"
        report["invalid"] = max(1, int(report.get("invalid", 0) or 0))
        report["complete"] = False
        return _deferred_report_aliases(report)
    finally:
        if owns_connection and working is not None:
            working.close()


def replay_deferred_cost_events(
    conn: sqlite3.Connection | None = None,
    path: pathlib.Path | None = None,
) -> dict:
    """Replay the append-only fallback ledger without duplicating events.

    Old rows predate event IDs and timestamps.  Their IDs are derived from the
    immutable file location/line and their timestamps from the file mtime, so
    replays are deterministic and conservatively remain in the relevant cost
    window.  The source file is intentionally never truncated.
    """

    source = pathlib.Path(path) if path is not None else COST_LOG_DEFERRED_PATH
    source_report, records = _load_deferred_cost_records(source)
    report = {
        **source_report,
        "inserted": 0,
        "finalized": 0,
        "skipped": 0,
        "event_ids": [str(record["event_id"]) for record in records],
        "inserted_event_ids": [],
        "finalized_event_ids": [],
        "skipped_event_ids": [],
    }
    if not source_report.get("source_exists"):
        report["post_verification"] = dict(source_report)
        return _deferred_report_aliases(report)

    owns_connection = conn is None
    working = conn
    try:
        if working is None:
            working = connect(initialize=False)
        before = _classify_deferred_cost_records(
            working,
            source_report,
            records,
            include_event_ids=True,
        )
        report.update(before)
        if int(before.get("invalid", 0) or 0) > 0 or int(before.get("conflicting", 0) or 0) > 0:
            report["post_verification"] = dict(before)
            return _deferred_report_aliases(report)
        select_sql = f"select {','.join(_COST_EVENT_COLUMNS)} from llm_cost_events where event_id=?"
        for record in records:
            values = tuple(record["values"])
            event_id = str(record["event_id"])
            existing = working.execute(
                select_sql,
                (event_id,),
            ).fetchone()
            if existing is None:
                working.execute(
                    """
                    insert into llm_cost_events (
                        event_id,created_at,agent_name,model_tier,model_name,provider,api,
                        reasoning_effort,verbosity,operation,prompt_cache_key,
                        frontier_escalation_reason,structured_json,prompt_tokens,
                        completion_tokens,estimated_cost_usd,status
                    ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    values,
                )
                report["inserted"] += 1
                report["inserted_event_ids"].append(event_id)
                continue
            existing_values = _row_cost_values(existing)
            if existing_values == values:
                report["skipped"] += 1
                report["skipped_event_ids"].append(event_id)
                continue
            if not _reservation_matches(existing_values, values):
                raise ValueError(f"deferred cost event conflict:{event_id}")
            updated = working.execute(
                """
                update llm_cost_events set
                    created_at=?,agent_name=?,model_tier=?,model_name=?,provider=?,api=?,
                    reasoning_effort=?,verbosity=?,operation=?,prompt_cache_key=?,
                    frontier_escalation_reason=?,structured_json=?,prompt_tokens=?,
                    completion_tokens=?,estimated_cost_usd=?,status=?
                where event_id=? and status='model_call_reserved'
                """,
                (
                    values[1], values[2], values[3], values[4], values[5], values[6],
                    values[7], values[8], values[9], values[10], values[11], values[12],
                    values[13], values[14], values[15], values[16], values[0],
                ),
            )
            if updated.rowcount != 1:
                raise ValueError(f"deferred reservation changed during replay:{event_id}")
            report["finalized"] += 1
            report["finalized_event_ids"].append(event_id)
        working.commit()
        post_verification = deferred_cost_reconciliation_status(
            working,
            source,
            include_event_ids=True,
        )
        report.update(
            {
                key: value
                for key, value in post_verification.items()
                if key not in {"event_ids", "expected_events"}
            }
        )
        report["post_verification"] = post_verification
        report["status"] = (
            "deferred_cost_log_replayed"
            if post_verification.get("complete")
            else "deferred_cost_replay_incomplete"
        )
        report["complete"] = bool(post_verification.get("complete"))
        return _deferred_report_aliases(report)
    except (OSError, sqlite3.Error, ValueError):
        attempted_inserted = int(report.get("inserted", 0) or 0)
        attempted_finalized = int(report.get("finalized", 0) or 0)
        if working is not None:
            try:
                working.rollback()
            except sqlite3.Error:
                pass
        report["attempted_inserted"] = attempted_inserted
        report["attempted_finalized"] = attempted_finalized
        report["inserted"] = 0
        report["finalized"] = 0
        report["inserted_event_ids"] = []
        report["finalized_event_ids"] = []
        post_verification = deferred_cost_reconciliation_status(
            working,
            source,
            include_event_ids=True,
        ) if working is not None else None
        report["post_verification"] = post_verification
        if post_verification and int(post_verification.get("conflicting", 0) or 0) > 0:
            report.update(post_verification)
            report["status"] = "deferred_cost_log_conflict"
        else:
            report["status"] = "deferred_cost_replay_failed"
            report["invalid"] = max(1, int(report.get("invalid", 0) or 0))
        report["complete"] = False
        return _deferred_report_aliases(report)
    finally:
        if owns_connection and working is not None:
            working.close()


def _window_usage(
    conn: sqlite3.Connection,
    *,
    now: dt.datetime,
    agent_name: str | None = None,
) -> dict:
    day_utc = now.date().isoformat()
    rolling_start = (now - dt.timedelta(hours=24)).isoformat()
    agent_clause = " and agent_name = ?" if agent_name else ""
    day_params: tuple[object, ...] = (day_utc, agent_name) if agent_name else (day_utc,)
    rolling_params: tuple[object, ...] = (rolling_start, agent_name) if agent_name else (rolling_start,)
    paid_attempt = _paid_attempt_sql()
    utc_row = conn.execute(
        f"""
        select coalesce(sum(estimated_cost_usd), 0) as cost,
               coalesce(sum(case when {paid_attempt} then 1 else 0 end), 0) as calls
        from llm_cost_events
        where date(created_at) = ?{agent_clause}
        """,
        day_params,
    ).fetchone()
    rolling_row = conn.execute(
        f"""
        select coalesce(sum(estimated_cost_usd), 0) as cost,
               coalesce(sum(case when {paid_attempt} then 1 else 0 end), 0) as calls
        from llm_cost_events
        where julianday(created_at) >= julianday(?){agent_clause}
        """,
        rolling_params,
    ).fetchone()
    return {
        "utc_day": {"cost_usd": float(utc_row["cost"] or 0.0), "calls": int(utc_row["calls"] or 0)},
        "rolling_24h": {
            "cost_usd": float(rolling_row["cost"] or 0.0),
            "calls": int(rolling_row["calls"] or 0),
        },
    }


def _spent_today(agent_name: str | None = None) -> float:
    try:
        with connect(initialize=False) as conn:
            usage = _window_usage(conn, now=_utc_now(), agent_name=agent_name)
    except sqlite3.OperationalError as exc:
        if _is_sqlite_locked(exc):
            return float("inf")
        if not _needs_schema_init(exc):
            raise
        with connect() as conn:
            usage = _window_usage(conn, now=_utc_now(), agent_name=agent_name)
    return float(usage["utc_day"]["cost_usd"])


def _locked_budget_limit(
    cfg: dict,
    key: str,
    default: float | int,
    maximum: float,
) -> float:
    raw = cfg.get(key, default)
    if raw is None:
        raise ValueError(f"{key} cannot be null in the bounded paid profile")
    value = float(raw)
    if not math.isfinite(value) or value < 0 or value > maximum:
        raise ValueError(f"{key} must be within 0..{maximum}")
    return value


def _locked_call_limit(cfg: dict, key: str, default: int, maximum: int) -> int:
    raw = cfg.get(key, default)
    if raw is None:
        raise ValueError(f"{key} cannot be null in the bounded paid profile")
    numeric = float(raw)
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError(f"{key} must be a whole number")
    value = int(numeric)
    if value < 0 or value > maximum:
        raise ValueError(f"{key} must be within 0..{maximum}")
    return value


def cost_budget_status(
    cfg: dict | None = None,
    *,
    agent_name: str | None = None,
    replay_deferred: bool = False,
) -> dict:
    """Return authoritative usage without mutating the deferred cost ledger.

    ``replay_deferred`` remains accepted for older callers, but replay is only
    authorized by the audited maintenance workflow.  A pending ledger therefore
    fails closed even when the legacy flag is true.
    """

    effective_cfg = cfg or load_llm_config()
    try:
        with connect(initialize=False) as conn:
            reconciliation = deferred_cost_reconciliation_status(conn)
            replay = {
                **reconciliation,
                "legacy_replay_requested": bool(replay_deferred),
                "mutation_performed": False,
            }
            if not bool(reconciliation.get("complete")):
                reconciliation_status = str(
                    reconciliation.get("status")
                    or "deferred_cost_reconciliation_incomplete"
                )
                reason = (
                    f"explicit_maintenance_required:{reconciliation_status}"
                    if replay_deferred
                    else reconciliation_status
                )
                return {
                    "allowed": False,
                    "status": "cost_budget_unavailable",
                    "reason": reason,
                    "deferred_replay": replay,
                }
            now = _utc_now()
            global_usage = _window_usage(conn, now=now)
            agent_usage = _window_usage(conn, now=now, agent_name=agent_name)
    except (OSError, sqlite3.Error) as exc:
        return {
            "allowed": False,
            "status": "cost_budget_unavailable",
            "reason": f"cost_budget_unavailable:{type(exc).__name__}",
        }
    agent_cfg = (
        (effective_cfg.get("agents") or {}).get(agent_name, {})
        if agent_name
        else {}
    )
    usage = {"global": global_usage, "agent": agent_usage}
    reason = _budget_reason(
        usage,
        cfg=effective_cfg,
        agent_cfg=agent_cfg,
        estimated_call_cost=0.0,
        require_positive_headroom=True,
    )
    try:
        day_budget = _locked_budget_limit(
            effective_cfg,
            "daily_budget_usd",
            MAX_GLOBAL_BUDGET_USD,
            MAX_GLOBAL_BUDGET_USD,
        )
        rolling_budget = _locked_budget_limit(
            effective_cfg,
            "rolling_24h_budget_usd",
            day_budget,
            MAX_GLOBAL_BUDGET_USD,
        )
        day_calls = _locked_call_limit(
            effective_cfg,
            "daily_call_limit",
            MAX_GLOBAL_CALLS,
            MAX_GLOBAL_CALLS,
        )
        rolling_calls = _locked_call_limit(
            effective_cfg,
            "rolling_24h_call_limit",
            day_calls,
            MAX_GLOBAL_CALLS,
        )
        agent_day_budget = _locked_budget_limit(
            agent_cfg,
            "daily_budget_usd",
            day_budget,
            day_budget,
        )
        agent_rolling_budget = _locked_budget_limit(
            agent_cfg,
            "rolling_24h_budget_usd",
            min(agent_day_budget, rolling_budget),
            rolling_budget,
        )
        agent_day_calls = _locked_call_limit(
            agent_cfg,
            "daily_call_limit",
            day_calls,
            day_calls,
        )
        agent_rolling_calls = _locked_call_limit(
            agent_cfg,
            "rolling_24h_call_limit",
            min(agent_day_calls, rolling_calls),
            rolling_calls,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return {
            "allowed": False,
            "status": "cost_budget_unavailable",
            "reason": f"cost_limit_config_invalid:{type(exc).__name__}",
            "usage": global_usage,
            "agent_usage": agent_usage,
            "deferred_replay": replay,
        }
    return {
        "allowed": reason is None,
        "status": "cost_budget_available" if reason is None else "cost_budget_exhausted",
        "reason": reason,
        "usage": global_usage,
        "agent_usage": agent_usage,
        "limits": {
            "utc_day_cost_usd": day_budget,
            "rolling_24h_cost_usd": rolling_budget,
            "utc_day_calls": day_calls,
            "rolling_24h_calls": rolling_calls,
            "agent_utc_day_cost_usd": agent_day_budget,
            "agent_rolling_24h_cost_usd": agent_rolling_budget,
            "agent_utc_day_calls": agent_day_calls,
            "agent_rolling_24h_calls": agent_rolling_calls,
        },
        "deferred_replay": replay,
    }


def _budget_reason(
    usage: dict,
    *,
    cfg: dict,
    agent_cfg: dict,
    estimated_call_cost: float,
    require_positive_headroom: bool = False,
) -> str | None:
    try:
        estimated_call_cost = float(estimated_call_cost)
    except (TypeError, ValueError, OverflowError):
        return "estimated_call_cost_invalid"
    if not math.isfinite(estimated_call_cost) or estimated_call_cost < 0:
        return "estimated_call_cost_invalid"
    try:
        global_day_budget = _locked_budget_limit(
            cfg,
            "daily_budget_usd",
            MAX_GLOBAL_BUDGET_USD,
            MAX_GLOBAL_BUDGET_USD,
        )
        global_rolling_budget = _locked_budget_limit(
            cfg,
            "rolling_24h_budget_usd",
            global_day_budget,
            MAX_GLOBAL_BUDGET_USD,
        )
        day_call_limit = _locked_call_limit(
            cfg,
            "daily_call_limit",
            MAX_GLOBAL_CALLS,
            MAX_GLOBAL_CALLS,
        )
        rolling_call_limit = _locked_call_limit(
            cfg,
            "rolling_24h_call_limit",
            day_call_limit,
            MAX_GLOBAL_CALLS,
        )
        agent_day_budget = _locked_budget_limit(
            agent_cfg,
            "daily_budget_usd",
            global_day_budget,
            global_day_budget,
        )
        agent_rolling_budget = _locked_budget_limit(
            agent_cfg,
            "rolling_24h_budget_usd",
            min(agent_day_budget, global_rolling_budget),
            global_rolling_budget,
        )
        agent_day_call_limit = _locked_call_limit(
            agent_cfg,
            "daily_call_limit",
            day_call_limit,
            day_call_limit,
        )
        agent_rolling_call_limit = _locked_call_limit(
            agent_cfg,
            "rolling_24h_call_limit",
            min(agent_day_call_limit, rolling_call_limit),
            rolling_call_limit,
        )
    except (TypeError, ValueError, OverflowError):
        return "cost_limit_config_invalid"
    day = usage["global"]["utc_day"]
    rolling = usage["global"]["rolling_24h"]
    if day["calls"] + 1 > day_call_limit:
        return f"global_utc_call_guard:{day['calls']}+1>{day_call_limit}"
    if rolling["calls"] + 1 > rolling_call_limit:
        return f"global_rolling_24h_call_guard:{rolling['calls']}+1>{rolling_call_limit}"
    if (
        global_day_budget == 0
        or day["cost_usd"] + estimated_call_cost > global_day_budget
        or require_positive_headroom and day["cost_usd"] >= global_day_budget
    ):
        return (
            f"global_utc_budget_guard:{day['cost_usd']:.6f}+"
            f"{estimated_call_cost:.6f}>{global_day_budget:.6f}"
        )
    if (
        global_rolling_budget == 0
        or rolling["cost_usd"] + estimated_call_cost > global_rolling_budget
        or require_positive_headroom and rolling["cost_usd"] >= global_rolling_budget
    ):
        return (
            f"global_rolling_24h_budget_guard:{rolling['cost_usd']:.6f}+"
            f"{estimated_call_cost:.6f}>{global_rolling_budget:.6f}"
        )
    agent_day = usage["agent"]["utc_day"]
    agent_rolling = usage["agent"]["rolling_24h"]
    if agent_day["calls"] + 1 > agent_day_call_limit:
        return f"agent_utc_call_guard:{agent_day['calls']}+1>{agent_day_call_limit}"
    if agent_rolling["calls"] + 1 > agent_rolling_call_limit:
        return (
            f"agent_rolling_24h_call_guard:{agent_rolling['calls']}+1>"
            f"{agent_rolling_call_limit}"
        )
    if (
        agent_day_budget == 0
        or agent_day["cost_usd"] + estimated_call_cost > agent_day_budget
        or require_positive_headroom and agent_day["cost_usd"] >= agent_day_budget
    ):
        return (
            f"agent_utc_budget_guard:{agent_day['cost_usd']:.6f}+"
            f"{estimated_call_cost:.6f}>{agent_day_budget:.6f}"
        )
    if (
        agent_rolling_budget == 0
        or agent_rolling["cost_usd"] + estimated_call_cost > agent_rolling_budget
        or require_positive_headroom
        and agent_rolling["cost_usd"] >= agent_rolling_budget
    ):
        return (
            f"agent_rolling_24h_budget_guard:{agent_rolling['cost_usd']:.6f}+"
            f"{estimated_call_cost:.6f}>{agent_rolling_budget:.6f}"
        )
    return None


def _budget_usage_for_call(conn: sqlite3.Connection, agent_name: str, now: dt.datetime) -> dict:
    return {
        "global": _window_usage(conn, now=now),
        "agent": _window_usage(conn, now=now, agent_name=agent_name),
    }


def _budget_allows_call(
    agent_name: str,
    cfg: dict,
    agent_cfg: dict,
    tier_cfg: dict,
    prompt_tokens: int,
    *,
    max_output_tokens: int | None = None,
) -> tuple[bool, str]:
    completion_tokens = int(
        max_output_tokens
        if max_output_tokens is not None
        else tier_cfg.get("estimated_completion_tokens", 1000)
    )
    estimated_call_cost = _cost_usd(prompt_tokens, completion_tokens, tier_cfg)
    try:
        with connect(initialize=False) as conn:
            reconciliation = deferred_cost_reconciliation_status(conn)
            if not bool(reconciliation.get("complete")):
                return False, str(
                    reconciliation.get("status") or "deferred_cost_reconciliation_incomplete"
                )
            usage = _budget_usage_for_call(conn, agent_name, _utc_now())
    except (OSError, sqlite3.Error) as exc:
        return False, f"cost_budget_unavailable:{type(exc).__name__}"
    reason = _budget_reason(usage, cfg=cfg, agent_cfg=agent_cfg, estimated_call_cost=estimated_call_cost)
    return (False, reason) if reason else (True, "budget_ok")


def _reserve_model_call(
    *,
    agent_name: str,
    cfg: dict,
    agent_cfg: dict,
    tier_name: str,
    tier_cfg: dict,
    model_name: str,
    prompt_tokens: int,
    max_output_tokens: int,
    provider: str,
    api: str,
    reasoning_effort: str | None,
    verbosity: str | None,
    operation: str,
    prompt_cache_key: str | None,
    frontier_escalation_reason: str | None,
    structured_json: bool,
) -> dict:
    """Atomically reserve one paid attempt under both cost windows."""

    now = _utc_now()
    event_id = str(uuid.uuid4())
    estimated_call_cost = _cost_usd(prompt_tokens, max_output_tokens, tier_cfg)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(initialize=False)
        reconciliation = deferred_cost_reconciliation_status(conn)
        if not bool(reconciliation.get("complete")):
            return {
                "allowed": False,
                "status": str(
                    reconciliation.get("status") or "deferred_cost_reconciliation_incomplete"
                ),
            }
        columns = {str(row[1]) for row in conn.execute("pragma table_info(llm_cost_events)").fetchall()}
        if "event_id" not in columns:
            return {"allowed": False, "status": "cost_event_id_schema_missing"}
        conn.execute("begin immediate")
        usage = _budget_usage_for_call(conn, agent_name, now)
        reason = _budget_reason(usage, cfg=cfg, agent_cfg=agent_cfg, estimated_call_cost=estimated_call_cost)
        if reason:
            conn.commit()
            return {"allowed": False, "status": reason, "usage": usage}
        conn.execute(
            """
            insert into llm_cost_events (
                event_id,created_at,agent_name,model_tier,model_name,provider,api,
                reasoning_effort,verbosity,operation,prompt_cache_key,
                frontier_escalation_reason,structured_json,prompt_tokens,
                completion_tokens,estimated_cost_usd,status
            ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                now.isoformat(),
                agent_name,
                tier_name,
                model_name,
                provider,
                api,
                reasoning_effort,
                verbosity,
                operation,
                prompt_cache_key,
                frontier_escalation_reason,
                1 if structured_json else 0,
                int(prompt_tokens),
                int(max_output_tokens),
                float(estimated_call_cost),
                "model_call_reserved",
            ),
        )
        conn.commit()
        return {
            "allowed": True,
            "status": "model_call_reserved",
            "event_id": event_id,
            "created_at": now.isoformat(),
            "reserved_cost_usd": estimated_call_cost,
            "usage": usage,
        }
    except (OSError, sqlite3.Error) as exc:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        return {"allowed": False, "status": f"cost_budget_unavailable:{type(exc).__name__}"}
    finally:
        if conn is not None:
            conn.close()


def _cancel_model_reservation(event_id: str, status: str) -> None:
    try:
        with connect(initialize=False) as conn:
            conn.execute(
                """
                update llm_cost_events
                set estimated_cost_usd=0, completion_tokens=0, status=?
                where event_id=? and status='model_call_reserved'
                """,
                (status, event_id),
            )
            conn.commit()
    except (OSError, sqlite3.Error):
        # A stranded reservation remains charged and counted, which is the
        # deliberate fail-closed behavior when cancellation cannot persist.
        return


def completion_preflight_status(
    agent_name: str,
    prompt: str,
    system: str = "",
    tier_override: str | None = None,
    *,
    max_output_tokens_override: int | None = None,
) -> dict:
    """Return model-call availability without making the model call."""

    cfg = load_llm_config()
    agent_cfg = cfg.get("agents", {}).get(agent_name, {"tier": "fast"})
    tier_name = tier_override or agent_cfg.get("tier", "fast")
    tier_cfg = cfg.get("tiers", {}).get(tier_name, cfg.get("tiers", {}).get("fast", {}))
    model_name = tier_cfg.get("model", "fallback")
    max_prompt_chars = int(tier_cfg.get("max_prompt_chars", 12000))
    prompt_tokens = estimate_tokens(system + prompt[:max_prompt_chars])
    provider = _provider_name(model_name)
    api = str(tier_cfg.get("api") or ("responses" if provider == "openai" else "litellm"))

    if _model_credentials_locked():
        return {
            "ok": False,
            "status": "credential_model_lock",
            "model_name": model_name,
            "model_tier": tier_name,
            "provider": provider,
            "api": api,
            "prompt_tokens": prompt_tokens,
        }

    if cfg.get("require_env_to_call_models", True) and os.environ.get("RADAR_USE_LITELLM") != "1":
        return {
            "ok": False,
            "status": "fallback_no_cost",
            "model_name": model_name,
            "model_tier": tier_name,
            "provider": provider,
            "api": api,
            "prompt_tokens": prompt_tokens,
        }

    ready, provider_status = _provider_ready(model_name)
    if not ready:
        return {
            "ok": False,
            "status": provider_status,
            "model_name": model_name,
            "model_tier": tier_name,
            "provider": provider,
            "api": api,
            "prompt_tokens": prompt_tokens,
        }

    quota_state = _quota_circuit_status()
    if quota_state:
        return {
            "ok": False,
            "status": f"quota_circuit_open_until:{quota_state.get('next_probe_at')}",
            "model_name": model_name,
            "model_tier": tier_name,
            "provider": provider,
            "api": api,
            "prompt_tokens": prompt_tokens,
        }

    allowed, budget_status = _budget_allows_call(
        agent_name,
        cfg,
        agent_cfg,
        tier_cfg,
        prompt_tokens,
        max_output_tokens=max_output_tokens_override,
    )
    if allowed:
        autonomous_status = autonomous_paid_attempt_status()
        if not autonomous_status.get("allowed", False):
            allowed = False
            budget_status = str(autonomous_status.get("reason") or autonomous_status.get("status"))
    return {
        "ok": bool(allowed),
        "status": budget_status,
        "model_name": model_name,
        "model_tier": tier_name,
        "provider": provider,
        "api": api,
        "prompt_tokens": prompt_tokens,
    }


def complete(
    agent_name: str,
    prompt: str,
    system: str = "",
    tier_override: str | None = None,
    operation: str | None = None,
    frontier_escalation_reason: str | None = None,
    reasoning_effort_override: str | None = None,
    structured_json: bool | None = None,
    max_output_tokens_override: int | None = None,
    timeout_seconds_override: float | None = None,
    tools: list[dict] | None = None,
) -> ModelResult:
    cfg = load_llm_config()
    agent_cfg = cfg.get("agents", {}).get(agent_name, {"tier": "fast"})
    tier_name = tier_override or agent_cfg.get("tier", "fast")
    tier_cfg = cfg.get("tiers", {}).get(tier_name, cfg.get("tiers", {}).get("fast", {}))
    model_name = tier_cfg.get("model", "fallback")
    max_prompt_chars = int(tier_cfg.get("max_prompt_chars", 12000))
    prompt = prompt[:max_prompt_chars]
    prompt_tokens = estimate_tokens(system + prompt)
    provider = _provider_name(model_name)
    api = str(tier_cfg.get("api") or ("responses" if provider == "openai" else "litellm"))
    reasoning_effort = reasoning_effort_override or tier_cfg.get("reasoning_effort")
    reasoning_mode = tier_cfg.get("reasoning_mode")
    verbosity = tier_cfg.get("verbosity")
    prompt_cache_key = tier_cfg.get("prompt_cache_key") or f"radar:{agent_name}:{tier_name}"
    prompt_cache_retention = tier_cfg.get("prompt_cache_retention")
    timeout_seconds = timeout_seconds_override or tier_cfg.get("timeout_seconds") or cfg.get("timeout_seconds")
    if provider == "openai" and _provider_model_name(model_name).startswith("gpt-5.") and prompt_cache_retention == "in_memory":
        prompt_cache_retention = "24h"
    structured_json_enabled = bool(tier_cfg.get("structured_json", False) if structured_json is None else structured_json)
    max_output_tokens = int(
        max_output_tokens_override
        or tier_cfg.get("max_output_tokens", tier_cfg.get("estimated_completion_tokens", 4000))
    )
    operation = operation or "llm_completion"

    if _model_credentials_locked():
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            0.0,
            "credential_model_lock",
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
            max_output_tokens=max_output_tokens,
        )
        _log(agent_name, result)
        return result

    use_litellm = os.environ.get("RADAR_USE_LITELLM") == "1"
    if cfg.get("require_env_to_call_models", True) and not use_litellm:
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            0.0,
            "fallback_no_cost",
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
            max_output_tokens=max_output_tokens,
        )
        _log(agent_name, result)
        return result

    ready, provider_status = _provider_ready(model_name)
    if not ready:
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            0.0,
            provider_status,
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
            max_output_tokens=max_output_tokens,
        )
        _log(agent_name, result)
        return result

    quota_state = _quota_circuit_status()
    if quota_state:
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            0.0,
            f"quota_circuit_open_until:{quota_state.get('next_probe_at')}",
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
            max_output_tokens=max_output_tokens,
        )
        _log(agent_name, result)
        return result

    reservation = _reserve_model_call(
        agent_name=agent_name,
        cfg=cfg,
        agent_cfg=agent_cfg,
        tier_name=tier_name,
        tier_cfg=tier_cfg,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        max_output_tokens=max_output_tokens,
        provider=provider,
        api=api,
        reasoning_effort=reasoning_effort,
        verbosity=verbosity,
        operation=operation,
        prompt_cache_key=prompt_cache_key,
        frontier_escalation_reason=frontier_escalation_reason,
        structured_json=structured_json_enabled,
    )
    if not reservation.get("allowed", False):
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            0.0,
            str(reservation.get("status") or "cost_budget_unavailable"),
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
            max_output_tokens=max_output_tokens,
        )
        _log(agent_name, result)
        return result

    event_id = str(reservation["event_id"])
    event_created_at = str(reservation["created_at"])
    autonomous_attempt = claim_autonomous_paid_attempt(
        agent_name=agent_name,
        operation=operation,
        metadata={
            "model_name": model_name,
            "model_tier": tier_name,
            "provider": provider,
            "api": api,
            "prompt_tokens": prompt_tokens,
        },
    )
    if not autonomous_attempt.get("allowed", False):
        _cancel_model_reservation(
            event_id,
            "model_call_cancelled_autonomous_guard",
        )
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            0.0,
            str(autonomous_attempt.get("reason") or autonomous_attempt.get("status")),
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
            max_output_tokens=max_output_tokens,
            event_id=event_id,
            created_at=event_created_at,
        )
        _log(agent_name, result)
        return result

    try:
        if provider == "openai" and api == "responses":
            response_payload = _complete_openai_responses(
                model_name=_provider_model_name(model_name),
                prompt=prompt,
                system=system,
                reasoning_effort=reasoning_effort,
                reasoning_mode=reasoning_mode,
                verbosity=verbosity,
                structured_json=structured_json_enabled,
                max_output_tokens=max_output_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
                timeout_seconds=timeout_seconds,
                tools=tools,
            )
            if len(response_payload) == 3:
                text, actual_prompt_tokens, completion_tokens = response_payload
                stop_reason = None
            else:
                text, actual_prompt_tokens, completion_tokens, stop_reason = response_payload
            prompt_tokens = actual_prompt_tokens or prompt_tokens
        else:
            response_payload = _complete_litellm(
                model_name=model_name,
                prompt=prompt,
                system=system,
                reasoning_effort=reasoning_effort,
                structured_json=structured_json_enabled,
                temperature=tier_cfg.get("temperature"),
                timeout_seconds=timeout_seconds,
            )
            if len(response_payload) == 2:
                text, completion_tokens = response_payload
                stop_reason = None
            else:
                text, completion_tokens, stop_reason = response_payload
        estimated_cost = _cost_usd(prompt_tokens, completion_tokens, tier_cfg)
        _clear_quota_state()
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            estimated_cost,
            f"model_call:{api}",
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
            max_output_tokens=max_output_tokens,
            stop_reason=stop_reason,
            event_id=event_id,
            created_at=event_created_at,
        )
        _log(agent_name, result)
        return result
    except Exception as exc:  # noqa: BLE001
        _mark_quota_failure(str(exc))
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            float(reservation.get("reserved_cost_usd", 0.0) or 0.0),
            f"fallback_error:{exc}",
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
            max_output_tokens=max_output_tokens,
            stop_reason="provider_error_cost_reserved_upper_bound",
            event_id=event_id,
            created_at=event_created_at,
        )
        _log(agent_name, result)
        return result


def _complete_openai_responses(
    model_name: str,
    prompt: str,
    system: str,
    reasoning_effort: str | None,
    reasoning_mode: str | None,
    verbosity: str | None,
    structured_json: bool,
    max_output_tokens: int,
    prompt_cache_key: str | None,
    prompt_cache_retention: str | None,
    timeout_seconds: float | None,
    tools: list[dict] | None = None,
) -> tuple[str, int | None, int, str | None]:
    from openai import OpenAI

    client = OpenAI(timeout=float(timeout_seconds)) if timeout_seconds else OpenAI()
    text_cfg: dict = {}
    if verbosity:
        text_cfg["verbosity"] = verbosity
    if structured_json:
        text_cfg["format"] = {"type": "json_object"}

    kwargs: dict = {
        "model": model_name,
        "input": [{"role": "user", "content": prompt}],
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    if system:
        kwargs["instructions"] = system
    reasoning: dict = {}
    if reasoning_effort:
        reasoning["effort"] = reasoning_effort
    if reasoning_mode:
        reasoning["mode"] = reasoning_mode
    if reasoning:
        kwargs["reasoning"] = reasoning
    if text_cfg:
        kwargs["text"] = text_cfg
    if prompt_cache_key:
        kwargs["prompt_cache_key"] = prompt_cache_key
    if prompt_cache_retention:
        kwargs["prompt_cache_retention"] = prompt_cache_retention
    if tools:
        kwargs["tools"] = tools

    response = client.responses.create(**kwargs)
    text = getattr(response, "output_text", None) or _extract_response_text(response)
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else None
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else estimate_tokens(text)
    stop_reason = getattr(response, "status", None)
    incomplete = getattr(response, "incomplete_details", None)
    if incomplete:
        reason = getattr(incomplete, "reason", None)
        if reason:
            stop_reason = str(reason)
    return text, input_tokens, output_tokens, stop_reason


def _extract_response_text(response: object) -> str:
    output = getattr(response, "output", None) or []
    parts: list[str] = []
    for item in output:
        content = getattr(item, "content", None) or []
        for block in content:
            for attr in ("text", "value", "content"):
                text = getattr(block, attr, None)
                if isinstance(text, str) and text:
                    parts.append(text)
                    break
    if parts:
        return "\n".join(parts)
    try:
        raw = response.model_dump()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(_walk_text_values(raw))


def _walk_text_values(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        text_type = str(value.get("type") or "")
        text_value = value.get("text")
        if isinstance(text_value, str) and text_value and "reasoning" not in text_type:
            found.append(text_value)
        for key, child in value.items():
            if key in {"usage", "reasoning"}:
                continue
            found.extend(_walk_text_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_text_values(child))
    elif isinstance(value, str) and value.startswith("diff --git "):
        found.append(value)
    return found


def _complete_litellm(
    model_name: str,
    prompt: str,
    system: str,
    reasoning_effort: str | None,
    structured_json: bool,
    temperature: float | None,
    timeout_seconds: float | None,
) -> tuple[str, int, str | None]:
    import litellm  # type: ignore

    kwargs: dict = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if temperature is not None:
        kwargs["temperature"] = float(temperature)
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if structured_json:
        kwargs["response_format"] = {"type": "json_object"}
    if timeout_seconds:
        kwargs["timeout"] = float(timeout_seconds)
    response = litellm.completion(**kwargs)
    text = response["choices"][0]["message"]["content"]
    finish_reason = response["choices"][0].get("finish_reason")
    return text, estimate_tokens(text), finish_reason


def _persist_cost_result(conn: sqlite3.Connection, agent_name: str, result: ModelResult) -> None:
    if not result.event_id:
        record_llm_cost_event(
            conn,
            agent_name,
            result.model_tier,
            result.model_name,
            result.prompt_tokens,
            result.completion_tokens,
            result.estimated_cost_usd,
            result.status,
            provider=result.provider,
            api=result.api,
            reasoning_effort=result.reasoning_effort,
            verbosity=result.verbosity,
            operation=result.operation,
            prompt_cache_key=result.prompt_cache_key,
            frontier_escalation_reason=result.frontier_escalation_reason,
            structured_json=result.structured_json,
        )
        return

    event_id = str(result.event_id)
    updated = conn.execute(
        """
        update llm_cost_events set
            agent_name=?,model_tier=?,model_name=?,provider=?,api=?,
            reasoning_effort=?,verbosity=?,operation=?,prompt_cache_key=?,
            frontier_escalation_reason=?,structured_json=?,prompt_tokens=?,
            completion_tokens=?,estimated_cost_usd=?,status=?
        where event_id=? and status='model_call_reserved'
        """,
        (
            agent_name,
            result.model_tier,
            result.model_name,
            result.provider,
            result.api,
            result.reasoning_effort,
            result.verbosity,
            result.operation,
            result.prompt_cache_key,
            result.frontier_escalation_reason,
            1 if result.structured_json else 0,
            int(result.prompt_tokens),
            int(result.completion_tokens),
            float(result.estimated_cost_usd),
            result.status,
            event_id,
        ),
    )
    if updated.rowcount:
        conn.commit()
        return
    existing = conn.execute(
        "select 1 from llm_cost_events where event_id=?",
        (event_id,),
    ).fetchone()
    if existing is not None:
        # An already-finalized event or an explicitly cancelled reservation is
        # immutable.  This makes retries and deferred replays idempotent.
        conn.commit()
        return
    created_at = _normalise_event_timestamp(result.created_at) or _utc_now().isoformat()
    conn.execute(
        """
        insert into llm_cost_events (
            event_id,created_at,agent_name,model_tier,model_name,provider,api,
            reasoning_effort,verbosity,operation,prompt_cache_key,
            frontier_escalation_reason,structured_json,prompt_tokens,
            completion_tokens,estimated_cost_usd,status
        ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            created_at,
            agent_name,
            result.model_tier,
            result.model_name,
            result.provider,
            result.api,
            result.reasoning_effort,
            result.verbosity,
            result.operation,
            result.prompt_cache_key,
            result.frontier_escalation_reason,
            1 if result.structured_json else 0,
            int(result.prompt_tokens),
            int(result.completion_tokens),
            float(result.estimated_cost_usd),
            result.status,
        ),
    )
    conn.commit()


def _log(agent_name: str, result: ModelResult) -> None:
    try:
        conn_ctx = connect(initialize=False)
    except sqlite3.OperationalError as exc:
        if _is_sqlite_locked(exc):
            _defer_cost_log(agent_name, result, reason="database_locked_on_connect")
            return
        conn_ctx = connect()
    try:
        with conn_ctx as conn:
            _persist_cost_result(conn, agent_name, result)
    except sqlite3.OperationalError as exc:
        if not _is_sqlite_locked(exc):
            if not _needs_schema_init(exc):
                raise
            with connect() as conn:
                _persist_cost_result(conn, agent_name, result)
            return
        _defer_cost_log(agent_name, result, reason="database_locked_on_insert")


def _defer_cost_log(agent_name: str, result: ModelResult, *, reason: str) -> None:
    COST_LOG_DEFERRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not result.event_id:
        result.event_id = str(uuid.uuid4())
    if not _normalise_event_timestamp(result.created_at):
        result.created_at = _utc_now().isoformat()
    payload = {
        "event_id": result.event_id,
        "created_at": result.created_at,
        "agent_name": agent_name,
        "model_tier": result.model_tier,
        "model_name": result.model_name,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "estimated_cost_usd": result.estimated_cost_usd,
        "status": result.status,
        "provider": result.provider,
        "api": result.api,
        "reasoning_effort": result.reasoning_effort,
        "verbosity": result.verbosity,
        "operation": result.operation,
        "prompt_cache_key": result.prompt_cache_key,
        "frontier_escalation_reason": result.frontier_escalation_reason,
        "structured_json": result.structured_json,
        "deferred_reason": reason,
    }
    with COST_LOG_DEFERRED_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")
