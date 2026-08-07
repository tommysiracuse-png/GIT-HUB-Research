"""Fail-closed state machine for bounded crypto paper expansion.

The radar loop owns market work.  This module only applies deterministic
quotas, snapshots safety counters, and advances a persisted campaign after a
cycle reports healthy evidence.  Research remains a separate one-shot process.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import sqlite3
import uuid
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
DEFERRED_COST_PATH = RUNS_DIR / "llm_cost_events_deferred.jsonl"
AUTONOMOUS_LEDGER_PATH = RUNS_DIR / "autonomous_paid_attempts.sqlite"
PHASES = ("burn_in", "measurement", "canary", "research")
RUN_STATUSES = ("running", "soft_paused", "hard_halted")
MAX_HEALTHY_CYCLE_CREDIT_SECONDS = 900.0
RECOVERY_CANARY_STRATEGY_LAB_ID = "recovery_okx_short_perp_long_spot_v1"
SAFETY_WATERMARK_VERSION = 1
PAID_RESEARCH_MAX_COST_USD = 25.0
PAID_RESEARCH_MAX_CALLS = 10
INTERCYCLE_INTEGER_COUNTERS = (
    "llm_cost_events",
    "paid_model_attempts",
    "agent_runs",
    "strategy_owner_runs",
    "codex_claims",
    "owner_task_claims",
    "llm_recommendations",
    "memory_facts",
    "temporal_memories",
    "temporal_memory_links",
    "memory_retrieval_events",
    "memory_system_state_updates",
    "live_orders",
    "nonpaper_fills",
    "deferred_cost_lines",
    "autonomous_attempts_today",
)
BOUNDED_STRATEGY_ROOT_STATUSES = frozenset(
    {
        "proposed",
        "active_testing",
        "needs_more_evidence",
        "needs_contract_revision",
        "needs_data",
        "needs_route",
        "promotion_candidate",
        "promote_candidate",
    }
)
DIRECT_CRYPTO_STRATEGY_SURFACES = frozenset(
    {"perp_funding_basis", "frontier_crypto_venue_map"}
)
DIRECT_CRYPTO_STRATEGY_DIRECTIONS = frozenset(
    {
        "long_frontier_spot",
        "short_frontier_spot",
        "long_frontier_perp",
        "short_frontier_perp",
        "funding_capture_long_perp",
        "funding_capture_short_perp",
        "long_perp_short_spot",
        "short_perp_long_spot",
        "basis_mean_reversion_long_perp",
        "basis_mean_reversion_short_perp",
    }
)


class CampaignError(RuntimeError):
    """Raised when campaign controls cannot be applied safely."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _config_hash(cfg: dict) -> str:
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _environment_count(name: str) -> int | None:
    value = os.environ.get(name)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_timestamp(value: object, fallback: dt.datetime) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _json_value(value: object, fallback: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, json.JSONDecodeError):
        return copy.deepcopy(fallback)
    return parsed


def _string_set(value: object, *, upper: bool = False, lower: bool = False) -> set[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return set()
    output = {str(item).strip() for item in value if str(item).strip()}
    if upper:
        return {item.upper() for item in output}
    if lower:
        return {item.lower() for item in output}
    return output


def _direct_crypto_venue_allowlist() -> set[str]:
    allowed = {"OKX", "OKX_SPOT", "OKX_PERP"}
    for name in ("frontier_crypto_venues.json", "frontier_crypto_venues.example.json"):
        path = ROOT / "config" / name
        if not path.exists():
            continue
        payload = _json_value(path.read_text(encoding="utf-8-sig"), {})
        for row in payload.get("venues", []) if isinstance(payload, dict) else []:
            if isinstance(row, dict) and row.get("enabled") is not False:
                venue = str(row.get("venue") or "").strip().upper()
                if venue:
                    allowed.add(venue)
        break
    return allowed


def _bounded_strategy_root_allowlist(
    conn: sqlite3.Connection,
    *,
    phase: str,
    configured: list[str],
    max_roots: int,
) -> list[str]:
    """Return only deterministic-canary and validated paid crypto roots.

    A blank recovery allowlist must never fall back to arbitrary experiments
    preserved in the historical database.  Paid roots are rechecked from the
    persisted contract before they are allowed into the radar process.
    """

    limit = max(0, int(max_roots))
    if limit == 0:
        return []
    if phase != "research":
        return [str(item) for item in configured if str(item).strip()][:limit]
    if not _table_exists(conn, "strategy_lab_experiments"):
        return []
    rows = conn.execute(
        """
        select strategy_lab_id,status,experiment_type,source_agent,source_surface,
               permitted_target_surfaces_json,strategy_logic_json,data_requirements_json,
               promotion_rules_json,created_at
        from strategy_lab_experiments
        where parent_strategy_lab_id is null
          and (strategy_lab_id=? or source_agent='paid_research_one_shot')
        order by case when strategy_lab_id=? then 0 else 1 end,
                 created_at asc,strategy_lab_id asc
        """,
        (RECOVERY_CANARY_STRATEGY_LAB_ID, RECOVERY_CANARY_STRATEGY_LAB_ID),
    ).fetchall()
    allowed: list[str] = []
    for row in rows:
        root_id = str(row["strategy_lab_id"] or "").strip()
        if not root_id or str(row["status"] or "") not in BOUNDED_STRATEGY_ROOT_STATUSES:
            continue
        source_agent = str(row["source_agent"] or "")
        if root_id == RECOVERY_CANARY_STRATEGY_LAB_ID:
            if source_agent != "deterministic_recovery_bootstrap":
                continue
        else:
            if source_agent != "paid_research_one_shot":
                continue
            if str(row["experiment_type"] or "") != "market_strategy":
                continue
            source_surface = str(row["source_surface"] or "").strip()
            target_surfaces = {
                str(item).strip()
                for item in _json_value(row["permitted_target_surfaces_json"], [])
                if str(item).strip()
            }
            logic = _json_value(row["strategy_logic_json"], {})
            requirements = _json_value(row["data_requirements_json"], {})
            trade_types = _string_set(logic.get("trade_types")) if isinstance(logic, dict) else set()
            asset_classes = _string_set(logic.get("asset_classes"), lower=True) if isinstance(logic, dict) else set()
            directions = _string_set(logic.get("directions"), lower=True) if isinstance(logic, dict) else set()
            venues = _string_set(logic.get("venues"), upper=True) if isinstance(logic, dict) else set()
            promotion_rules = _json_value(row["promotion_rules_json"], {})
            if (
                source_surface not in DIRECT_CRYPTO_STRATEGY_SURFACES
                or not target_surfaces
                or not target_surfaces.issubset(DIRECT_CRYPTO_STRATEGY_SURFACES)
                or not trade_types
                or not trade_types.issubset(DIRECT_CRYPTO_STRATEGY_SURFACES)
                or not asset_classes
                or not asset_classes.issubset({"crypto", "crypto_spot"})
                or not directions
                or not directions.issubset(DIRECT_CRYPTO_STRATEGY_DIRECTIONS)
                or not venues
                or not venues.issubset(_direct_crypto_venue_allowlist())
                or bool(logic.get("allow_any_surface"))
                or not isinstance(requirements, dict)
                or requirements.get("paper_only") is not True
                or str(requirements.get("route_status") or "").lower() != "standard"
                or not isinstance(promotion_rules, dict)
                or bool(promotion_rules)
            ):
                continue
        allowed.append(root_id)
        if len(allowed) >= limit:
            break
    return allowed


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?",
        (table,),
    ).fetchone()
    return row is not None


def _require_schema(conn: sqlite3.Connection) -> None:
    missing = [
        name
        for name in ("paper_expansion_campaign_state", "paper_expansion_campaign_cycles")
        if not _table_exists(conn, name)
    ]
    if missing:
        raise CampaignError("campaign_schema_missing:" + ",".join(missing))


def _campaign_config(settings: dict) -> dict:
    cfg = settings.get("paper_expansion") or {}
    if not bool(cfg.get("enabled", False)):
        return {}
    phases = cfg.get("phases") or {}
    missing = [phase for phase in PHASES if not isinstance(phases.get(phase), dict)]
    if missing:
        raise CampaignError("campaign_phase_config_missing:" + ",".join(missing))
    return cfg


def _assert_fail_closed_settings(settings: dict) -> None:
    reasons: list[str] = []
    if str(settings.get("mode") or "").lower() != "paper":
        reasons.append("mode_not_paper")
    if bool(settings.get("allow_live_trading", False)):
        reasons.append("live_trading_enabled")
    if float((settings.get("risk") or {}).get("max_live_notional_usd", 0.0) or 0.0) != 0.0:
        reasons.append("max_live_notional_nonzero")
    if float((settings.get("risk") or {}).get("paper_notional_usd", 0.0) or 0.0) != 100.0:
        reasons.append("paper_notional_not_100")
    operations = settings.get("operations") or {}
    if bool(operations.get("model_credentials_enabled", True)):
        reasons.append("radar_model_credentials_enabled")
    if not bool(operations.get("crypto_only", False)):
        reasons.append("crypto_only_disabled")
    cfg = settings.get("paper_expansion") or {}
    for key in (
        "direct_queue_allocation_multiplier",
        "reviewer_allocation_multiplier",
        "runtime_allocation_multiplier",
    ):
        if float(cfg.get(key, 0.0) or 0.0) != 1.0:
            reasons.append(f"{key}_not_1")
    if reasons:
        raise CampaignError("unsafe_campaign_settings:" + ",".join(reasons))


def _initial_state(campaign_id: str, now: dt.datetime, config_hash: str) -> dict:
    stamp = _iso(now)
    return {
        "campaign_id": campaign_id,
        "phase": "burn_in",
        "run_status": "running",
        "healthy_streak": 0,
        "phase_cycle_count": 0,
        "phase_healthy_cycles": 0,
        "total_cycle_count": 0,
        "phase_started_at": stamp,
        "phase_healthy_running_seconds": 0.0,
        "phase_clock_checkpoint_at": stamp,
        "updated_at": stamp,
        "config_hash": config_hash,
        "gate_evidence": {},
        "last_good_phase": "burn_in",
        "stop_reason": None,
        "hard_halt_reason": None,
        "last_health_status": None,
        "last_reasons": [],
        "last_completed_safety_watermark": None,
        "accumulated": {
            "direct_closes": 0,
            "reliable_direct_closes": 0,
            "timely_direct_closes": 0,
            "horizon_outcomes": 0,
            "timely_horizon_outcomes": 0,
            "exact_attributed_admission_keys": 0,
            "opportunity_lineage_records": 0,
            "opportunity_lineage_complete": 0,
            "order_lineage_records": 0,
            "order_lineage_complete": 0,
            "trade_lineage_records": 0,
            "trade_lineage_complete": 0,
            "synthetic_proxy_primary": 0,
            "canary_reliable_direct_labels": 0,
        },
        "operational_history": {
            "runtime_seconds": [],
            "peak_rss_mb": [],
            "db_footprint": [],
            "db_growth": [],
        },
    }


def _decode_state(row: sqlite3.Row | tuple, now: dt.datetime) -> dict:
    try:
        raw = row["state_json"]
    except (IndexError, TypeError):
        raw = row[-1]
    try:
        state = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    for key in (
        "campaign_id",
        "phase",
        "run_status",
        "healthy_streak",
        "phase_cycle_count",
        "total_cycle_count",
        "phase_started_at",
        "updated_at",
    ):
        try:
            state.setdefault(key, row[key])
        except (IndexError, TypeError):
            pass
    state.setdefault("phase_healthy_cycles", 0)
    # Legacy state did not distinguish wall age from healthy running time.
    # Start that clock from zero rather than granting unverifiable historical
    # uptime, and anchor future accounting at this load.
    state.setdefault("phase_healthy_running_seconds", 0.0)
    state.setdefault("phase_clock_checkpoint_at", _iso(now))
    state.setdefault("accumulated", {})
    state.setdefault("operational_history", {})
    state.setdefault("config_hash", None)
    state.setdefault("gate_evidence", {})
    state.setdefault("last_good_phase", "burn_in")
    state.setdefault("stop_reason", None)
    state.setdefault("last_reasons", [])
    state.setdefault("last_completed_safety_watermark", None)
    state.setdefault("updated_at", _iso(now))
    return state


def _load_or_create_state(
    conn: sqlite3.Connection,
    campaign_id: str,
    now: dt.datetime,
    config_hash: str,
) -> dict:
    commit_locally = not conn.in_transaction
    _require_schema(conn)
    row = conn.execute(
        "select * from paper_expansion_campaign_state where campaign_id=?",
        (campaign_id,),
    ).fetchone()
    if row is not None:
        state = _decode_state(row, now)
        if state.get("phase") not in PHASES or state.get("run_status") not in RUN_STATUSES:
            raise CampaignError("invalid_persisted_campaign_state")
        if state.get("config_hash") != config_hash:
            state["run_status"] = "hard_halted"
            state["healthy_streak"] = 0
            state["hard_halt_reason"] = "config_hash_changed"
            state["stop_reason"] = "config_hash_changed"
            state["updated_at"] = _iso(now)
            _persist_state(conn, state)
            if commit_locally:
                conn.commit()
        return state
    state = _initial_state(campaign_id, now, config_hash)
    inserted = conn.execute(
        """
        insert or ignore into paper_expansion_campaign_state (
            campaign_id,phase,run_status,healthy_streak,phase_cycle_count,
            total_cycle_count,phase_started_at,updated_at,state_json
        ) values (?,?,?,?,?,?,?,?,?)
        """,
        (
            campaign_id,
            state["phase"],
            state["run_status"],
            0,
            0,
            0,
            state["phase_started_at"],
            state["updated_at"],
            json.dumps(state, sort_keys=True),
        ),
    )
    if commit_locally:
        conn.commit()
    if inserted.rowcount == 0:
        row = conn.execute(
            "select * from paper_expansion_campaign_state where campaign_id=?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise CampaignError("campaign_state_creation_race")
        state = _decode_state(row, now)
        if state.get("phase") not in PHASES or state.get("run_status") not in RUN_STATUSES:
            raise CampaignError("invalid_persisted_campaign_state")
        if state.get("config_hash") != config_hash:
            state["run_status"] = "hard_halted"
            state["healthy_streak"] = 0
            state["hard_halt_reason"] = "config_hash_changed"
            state["stop_reason"] = "config_hash_changed"
            state["updated_at"] = _iso(now)
            _persist_state(conn, state)
            if commit_locally:
                conn.commit()
    return state


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int((row[0] if row is not None else 0) or 0)


def _optional_count(conn: sqlite3.Connection, table: str, sql: str) -> int:
    return _count(conn, sql) if _table_exists(conn, table) else 0


def _deferred_line_count(path: pathlib.Path = DEFERRED_COST_PATH) -> int:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except FileNotFoundError:
        return 0


def _deferred_cost_state(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"line_count": 0, "invalid_count": 0, "known": True}
    line_count = 0
    invalid_count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                line_count += 1
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        invalid_count += 1
                        continue
                    cost = float(payload["estimated_cost_usd"])
                    status = str(payload.get("status") or "").strip()
                    if not math.isfinite(cost) or cost < 0 or not status:
                        invalid_count += 1
                except (KeyError, TypeError, ValueError, OverflowError):
                    invalid_count += 1
                except json.JSONDecodeError:
                    invalid_count += 1
    except (OSError, UnicodeError):
        return {"line_count": -1, "invalid_count": -1, "known": False}
    return {
        "line_count": line_count,
        "invalid_count": invalid_count,
        "known": invalid_count == 0,
    }


def _autonomous_rows_digest(rows: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _autonomous_attempt_state(path: pathlib.Path, now: dt.datetime) -> dict:
    if not path.exists():
        return {
            "count": 0,
            "digest": _autonomous_rows_digest([]),
            "known": True,
        }
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                select attempt_id,created_at,day_utc,scope_id,source,agent_name,
                       operation,metadata_json
                from autonomous_paid_attempts
                order by attempt_id
                """
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return {"count": -1, "digest": None, "known": False}
    payload: list[dict] = []
    known = True
    for row in rows:
        item = dict(row)
        created_at = _strict_timestamp(item.get("created_at"))
        try:
            metadata = json.loads(str(item.get("metadata_json") or ""))
        except (TypeError, json.JSONDecodeError):
            metadata = None
        if (
            not str(item.get("attempt_id") or "")
            or created_at is None
            or str(item.get("day_utc") or "") != created_at.date().isoformat()
            or not isinstance(metadata, dict)
        ):
            known = False
        payload.append(item)
    digest = _autonomous_rows_digest(payload)
    return {"count": len(rows), "digest": digest, "known": known}


def _safety_snapshot(conn: sqlite3.Connection, cfg: dict, now: dt.datetime) -> dict:
    paid_predicate = """
        status='model_call' or status like 'model_call:%' or
        status='model_call_reserved' or
        (event_id is not null and status like 'fallback_error:%')
    """
    cost_ledger_known = True
    try:
        llm_rows = _count(conn, "select count(*) from llm_cost_events")
        llm_max_id = _count(conn, "select coalesce(max(id),0) from llm_cost_events")
        llm_sequence = _count(
            conn,
            "select coalesce(max(seq),0) from sqlite_sequence where name='llm_cost_events'",
        )
        llm_cost = conn.execute(
            "select coalesce(sum(estimated_cost_usd),0) from llm_cost_events"
        ).fetchone()[0]
        paid_attempts = _count(conn, f"select count(*) from llm_cost_events where {paid_predicate}")
        invalid_llm_timestamps = _count(
            conn,
            "select count(*) from llm_cost_events where julianday(created_at) is null",
        )
        invalid_llm_costs = _count(
            conn,
            """
            select count(*) from llm_cost_events
            where estimated_cost_usd is null
               or typeof(estimated_cost_usd) not in ('integer','real')
               or estimated_cost_usd < 0
               or estimated_cost_usd > 1.0e308
            """,
        )
        if invalid_llm_timestamps or invalid_llm_costs:
            cost_ledger_known = False
    except sqlite3.Error:
        llm_rows = -1
        llm_max_id = -1
        llm_sequence = -1
        llm_cost = -1.0
        paid_attempts = -1
        invalid_llm_timestamps = -1
        invalid_llm_costs = -1
        cost_ledger_known = False
    deferred = _deferred_cost_state(
        pathlib.Path(str(cfg.get("deferred_cost_path") or DEFERRED_COST_PATH))
    )
    autonomous_state = _autonomous_attempt_state(
        pathlib.Path(str(cfg.get("autonomous_ledger_path") or AUTONOMOUS_LEDGER_PATH)),
        now,
    )
    autonomous_attempts = int(autonomous_state.get("count", -1) or 0)
    cost_ledger_known = (
        cost_ledger_known
        and bool(deferred["known"])
        and bool(autonomous_state.get("known"))
        and autonomous_attempts >= 0
    )
    snapshot = {
        "cost_ledger_known": cost_ledger_known,
        "invalid_llm_timestamps": invalid_llm_timestamps,
        "invalid_llm_costs": invalid_llm_costs,
        "llm_cost_events": llm_rows,
        "llm_cost_event_max_id": llm_max_id,
        "llm_cost_event_sequence": llm_sequence,
        "llm_cost_usd": float(llm_cost or 0.0),
        "paid_model_attempts": paid_attempts,
        "agent_runs": _optional_count(conn, "agent_runs", "select count(*) from agent_runs"),
        "strategy_owner_runs": _optional_count(
            conn,
            "strategy_owner_runs",
            "select count(*) from strategy_owner_runs",
        ),
        "codex_claims": _optional_count(
            conn,
            "codex_coordination_events",
            "select count(*) from codex_coordination_events where event_type='claimed'",
        ),
        "owner_task_claims": _optional_count(
            conn,
            "strategy_owner_tasks",
            "select coalesce(sum(attempt_count),0) from strategy_owner_tasks",
        )
        + _optional_count(
            conn,
            "market_activation_tasks",
            "select coalesce(sum(attempt_count),0) from market_activation_tasks",
        ),
        "llm_recommendations": _optional_count(
            conn,
            "llm_recommendations",
            "select count(*) from llm_recommendations",
        ),
        "memory_facts": _optional_count(conn, "memory_facts", "select count(*) from memory_facts"),
        "temporal_memories": _optional_count(
            conn,
            "temporal_memories",
            "select count(*) from temporal_memories",
        ),
        "temporal_memory_links": _optional_count(
            conn,
            "temporal_memory_links",
            "select count(*) from temporal_memory_links",
        ),
        "memory_retrieval_events": _optional_count(
            conn,
            "memory_retrieval_events",
            "select count(*) from memory_retrieval_events",
        ),
        "memory_system_state_updates": _optional_count(
            conn,
            "memory_system_state",
            "select count(*) from memory_system_state",
        ),
        "pending_execution": _count(
            conn,
            "select count(*) from opportunities where decision='pending_execution'",
        ),
        "live_orders": _count(
            conn,
            "select count(*) from execution_orders where lower(mode)='live'",
        ),
        "nonpaper_fills": _count(
            conn,
            """
            select count(*) from execution_fills f
            join execution_orders o on o.id=f.order_id
            where lower(o.mode)<>'paper'
            """,
        ),
        "deferred_cost_lines": deferred["line_count"],
        "invalid_deferred_cost_lines": deferred["invalid_count"],
        "autonomous_attempts_today": autonomous_attempts,
        "autonomous_attempts_digest": autonomous_state.get("digest"),
    }
    return snapshot


def _strict_timestamp(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _snapshot_integer(snapshot: dict, key: str) -> int | None:
    value = snapshot.get(key)
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric != float(parsed) or parsed < 0:
        return None
    return parsed


def _snapshot_cost(snapshot: dict) -> float | None:
    value = snapshot.get("llm_cost_usd")
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _json_digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _authorized_paid_research_attribution(
    conn: sqlite3.Connection,
    state: dict,
    watermark: dict,
    before: dict,
    current: dict,
    deltas: dict[str, int],
    cost_delta: float,
    now: dt.datetime,
    cfg: dict,
) -> tuple[dict | None, list[str]]:
    """Match one inter-cycle model event to the isolated paid-research lease."""

    rejected: list[str] = []
    if str(state.get("phase") or "") != "research":
        rejected.append("phase_not_research")
    if str(state.get("run_status") or "") != "running":
        rejected.append("campaign_not_running")

    lease = state.get("last_paid_research_lease")
    if not isinstance(lease, dict) or not lease:
        rejected.append("successful_lease_missing")
        lease = {}
    lease_id = str(lease.get("lease_id") or "")
    if not lease_id:
        rejected.append("lease_id_missing")
    if lease_id == str(watermark.get("last_paid_research_lease_id") or ""):
        rejected.append("lease_already_watermarked")
    if str(lease.get("campaign_id") or "") != str(state.get("campaign_id") or ""):
        rejected.append("lease_campaign_mismatch")
    if str(lease.get("config_hash") or "") != str(state.get("config_hash") or ""):
        rejected.append("lease_config_mismatch")
    outcome = str(lease.get("outcome") or "")
    provider_outcome = str(lease.get("provider_outcome") or "")
    operation_outcome = str(lease.get("operation_outcome") or "")
    result_type = None
    if not provider_outcome or outcome != provider_outcome:
        rejected.append("lease_provider_outcome_mismatch")
    if provider_outcome.startswith("model_call:"):
        if operation_outcome == "completed":
            result_type = "success"
        elif operation_outcome == "downstream_failure":
            result_type = "downstream_failure"
        else:
            rejected.append("lease_operation_outcome_unclassified")
    elif provider_outcome.startswith("fallback_error:"):
        result_type = "known_provider_failure"
    else:
        rejected.append("lease_outcome_not_finalized_model_result")
    failure_category = str(lease.get("failure_category") or "")
    if result_type == "success" and failure_category:
        rejected.append("completed_lease_has_failure_category")
    if result_type == "downstream_failure" and not failure_category.startswith("downstream_"):
        rejected.append("downstream_failure_category_invalid")

    captured_at = _strict_timestamp(watermark.get("captured_at"))
    started_at = _strict_timestamp(lease.get("started_at"))
    completed_at = _strict_timestamp(lease.get("completed_at"))
    if captured_at is None:
        rejected.append("watermark_timestamp_invalid")
    if started_at is None:
        rejected.append("lease_started_at_invalid")
    if completed_at is None:
        rejected.append("lease_completed_at_invalid")
    if captured_at is not None and started_at is not None and started_at < captured_at:
        rejected.append("lease_started_before_watermark")
    if started_at is not None and completed_at is not None and completed_at < started_at:
        rejected.append("lease_completion_precedes_start")
    if completed_at is not None and completed_at > now:
        rejected.append("lease_completion_in_future")

    if deltas.get("llm_cost_events") != 1:
        rejected.append("cost_event_count_not_one")
    if deltas.get("paid_model_attempts") != 1:
        rejected.append("paid_attempt_count_not_one")
    if deltas.get("autonomous_attempts_today") != 1:
        rejected.append("autonomous_attempt_count_not_one")
    for key in INTERCYCLE_INTEGER_COUNTERS:
        if key in {
            "llm_cost_events",
            "paid_model_attempts",
            "autonomous_attempts_today",
        }:
            continue
        if deltas.get(key) != 0:
            rejected.append(f"unrelated_counter_changed:{key}")

    before_max_id = _snapshot_integer(before, "llm_cost_event_max_id")
    current_max_id = _snapshot_integer(current, "llm_cost_event_max_id")
    before_sequence = _snapshot_integer(before, "llm_cost_event_sequence")
    current_sequence = _snapshot_integer(current, "llm_cost_event_sequence")
    if None in {before_max_id, current_max_id, before_sequence, current_sequence}:
        rejected.append("cost_event_identity_unknown")
        rows: list[sqlite3.Row] = []
    else:
        rows = conn.execute(
            """
            select id,event_id,created_at,agent_name,model_tier,model_name,
                   provider,api,operation,structured_json,estimated_cost_usd,status
            from llm_cost_events
            where id>?
            order by id
            """,
            (before_max_id,),
        ).fetchall()
        if current_sequence - before_sequence != 1:
            rejected.append("cost_event_sequence_delta_not_one")
        if len(rows) != 1:
            rejected.append("new_cost_event_rows_not_one")

    event: dict = dict(rows[0]) if len(rows) == 1 else {}
    if event:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            rejected.append("cost_event_id_missing")
        if int(event.get("id") or 0) != current_max_id:
            rejected.append("cost_event_not_current_max")
        if int(event.get("id") or 0) != current_sequence:
            rejected.append("cost_event_not_current_sequence")
        if str(event.get("agent_name") or "") != "global_research_worker":
            rejected.append("cost_event_agent_mismatch")
        if str(event.get("operation") or "") != "bounded_crypto_paid_research":
            rejected.append("cost_event_operation_mismatch")
        if int(event.get("structured_json") or 0) != 1:
            rejected.append("cost_event_not_structured_json")
        if str(event.get("status") or "") != outcome:
            rejected.append("cost_event_outcome_mismatch")
        if str(event.get("event_id") or "") != str(lease.get("provider_event_id") or ""):
            rejected.append("cost_event_persisted_identity_mismatch")
        event_at = _strict_timestamp(event.get("created_at"))
        if event_at is None:
            rejected.append("cost_event_timestamp_invalid")
        elif (
            started_at is not None
            and completed_at is not None
            and not (started_at <= event_at <= completed_at)
        ):
            rejected.append("cost_event_outside_lease")
        try:
            event_cost = float(event.get("estimated_cost_usd"))
        except (TypeError, ValueError, OverflowError):
            event_cost = math.nan
        if not math.isfinite(event_cost) or event_cost < 0:
            rejected.append("cost_event_cost_invalid")
        elif not math.isclose(cost_delta, event_cost, rel_tol=0.0, abs_tol=1.0e-9):
            rejected.append("cost_event_delta_mismatch")
        try:
            persisted_cost = float(lease.get("provider_estimated_cost_usd"))
        except (TypeError, ValueError, OverflowError):
            persisted_cost = math.nan
        if not math.isfinite(persisted_cost) or not math.isclose(
            event_cost,
            persisted_cost,
            rel_tol=0.0,
            abs_tol=1.0e-9,
        ):
            rejected.append("cost_event_persisted_cost_mismatch")
    else:
        event_id = ""
        event_cost = math.nan

    autonomous_path = pathlib.Path(
        str(cfg.get("autonomous_ledger_path") or AUTONOMOUS_LEDGER_PATH)
    )
    autonomous_rows: list[dict] = []
    if not autonomous_path.exists():
        rejected.append("autonomous_attempt_ledger_missing")
    else:
        autonomous_conn: sqlite3.Connection | None = None
        try:
            autonomous_conn = sqlite3.connect(
                f"file:{autonomous_path.as_posix()}?mode=ro",
                uri=True,
                timeout=1.0,
            )
            autonomous_conn.row_factory = sqlite3.Row
            autonomous_rows = [
                dict(row)
                for row in autonomous_conn.execute(
                    """
                    select attempt_id,created_at,day_utc,scope_id,source,agent_name,
                           operation,metadata_json
                    from autonomous_paid_attempts
                    order by attempt_id
                    """
                ).fetchall()
            ]
        except (OSError, sqlite3.Error):
            rejected.append("autonomous_attempt_ledger_unreadable")
        finally:
            if autonomous_conn is not None:
                autonomous_conn.close()

    current_autonomous_count = _snapshot_integer(current, "autonomous_attempts_today")
    before_autonomous_digest = str(before.get("autonomous_attempts_digest") or "")
    current_autonomous_digest = str(current.get("autonomous_attempts_digest") or "")
    if len(before_autonomous_digest) != 64 or len(current_autonomous_digest) != 64:
        rejected.append("autonomous_attempt_digest_invalid")
    if current_autonomous_count is None or len(autonomous_rows) != current_autonomous_count:
        rejected.append("autonomous_attempt_ledger_count_mismatch")
    if autonomous_rows and _autonomous_rows_digest(autonomous_rows) != current_autonomous_digest:
        rejected.append("autonomous_attempt_current_digest_mismatch")

    matching_attempts = [
        row
        for row in autonomous_rows
        if str(row.get("scope_id") or "") == lease_id
    ]
    if len(matching_attempts) != 1:
        rejected.append("lease_scoped_autonomous_attempt_not_one")
        autonomous_attempt: dict = {}
    else:
        autonomous_attempt = matching_attempts[0]
        prior_rows = [
            row
            for row in autonomous_rows
            if str(row.get("attempt_id") or "")
            != str(autonomous_attempt.get("attempt_id") or "")
        ]
        if _autonomous_rows_digest(prior_rows) != before_autonomous_digest:
            rejected.append("autonomous_attempt_not_exact_append")
        attempt_at = _strict_timestamp(autonomous_attempt.get("created_at"))
        if attempt_at is None:
            rejected.append("autonomous_attempt_timestamp_invalid")
        elif (
            started_at is not None
            and completed_at is not None
            and not (started_at <= attempt_at <= completed_at)
        ):
            rejected.append("autonomous_attempt_outside_lease")
        if str(autonomous_attempt.get("day_utc") or "") != (
            attempt_at.date().isoformat() if attempt_at is not None else ""
        ):
            rejected.append("autonomous_attempt_day_mismatch")
        if str(autonomous_attempt.get("source") or "") != "paid_research_once":
            rejected.append("autonomous_attempt_source_mismatch")
        if str(autonomous_attempt.get("agent_name") or "") != "global_research_worker":
            rejected.append("autonomous_attempt_agent_mismatch")
        if str(autonomous_attempt.get("operation") or "") != "bounded_crypto_paid_research":
            rejected.append("autonomous_attempt_operation_mismatch")
        try:
            attempt_metadata = json.loads(
                str(autonomous_attempt.get("metadata_json") or "")
            )
        except (TypeError, json.JSONDecodeError):
            attempt_metadata = None
        if not isinstance(attempt_metadata, dict):
            rejected.append("autonomous_attempt_metadata_invalid")
        elif event:
            for key in ("model_name", "model_tier", "provider", "api"):
                if str(attempt_metadata.get(key) or "") != str(event.get(key) or ""):
                    rejected.append(f"autonomous_attempt_metadata_mismatch:{key}")

    paid_predicate = """
        status='model_call' or status like 'model_call:%' or
        status='model_call_reserved' or
        (event_id is not null and status like 'fallback_error:%')
    """
    day_utc = now.date().isoformat()
    rolling_start = (now - dt.timedelta(hours=24)).isoformat()
    for window_name, where_sql, params in (
        ("utc_day", "date(created_at)=?", (day_utc,)),
        ("rolling_24h", "julianday(created_at)>=julianday(?)", (rolling_start,)),
    ):
        usage = conn.execute(
            f"""
            select coalesce(sum(estimated_cost_usd),0),
                   coalesce(sum(case when {paid_predicate} then 1 else 0 end),0)
            from llm_cost_events
            where {where_sql}
            """,
            params,
        ).fetchone()
        try:
            window_cost = float(usage[0] or 0.0)
            window_calls = int(usage[1] or 0)
        except (TypeError, ValueError, OverflowError):
            rejected.append(f"{window_name}_budget_unknown")
            continue
        if not math.isfinite(window_cost) or window_cost < 0 or window_calls < 0:
            rejected.append(f"{window_name}_budget_unknown")
        if window_cost > PAID_RESEARCH_MAX_COST_USD + 1.0e-9:
            rejected.append(f"{window_name}_cost_limit_exceeded")
        if window_calls > PAID_RESEARCH_MAX_CALLS:
            rejected.append(f"{window_name}_call_limit_exceeded")

    if rejected:
        return None, sorted(set(rejected))
    return (
        {
            "lease_id": lease_id,
            "event_id": event_id,
            "cost_event_id": int(event["id"]),
            "event_created_at": str(event["created_at"]),
            "lease_started_at": str(lease["started_at"]),
            "lease_completed_at": str(lease["completed_at"]),
            "status": outcome,
            "result_type": result_type,
            "operation_outcome": operation_outcome,
            "failure_category": failure_category or None,
            "estimated_cost_usd": event_cost,
            "autonomous_attempt_id": str(autonomous_attempt["attempt_id"]),
        },
        [],
    )


def _categorized_zero_delta_paid_gate(
    state: dict,
    watermark: dict,
    before: dict,
    current: dict,
    deltas: dict[str, int],
    cost_delta: float,
    now: dt.datetime,
) -> tuple[dict | None, list[str]]:
    """Validate a known pre-provider paid-research gate with no charged work."""

    rejected: list[str] = []
    if str(state.get("phase") or "") != "research":
        rejected.append("phase_not_research")
    if str(state.get("run_status") or "") != "running":
        rejected.append("campaign_not_running")
    lease = state.get("last_paid_research_lease")
    if not isinstance(lease, dict) or not lease:
        rejected.append("lease_missing")
        lease = {}
    lease_id = str(lease.get("lease_id") or "")
    if not lease_id:
        rejected.append("lease_id_missing")
    if lease_id == str(watermark.get("last_paid_research_lease_id") or ""):
        rejected.append("lease_already_watermarked")
    if str(lease.get("campaign_id") or "") != str(state.get("campaign_id") or ""):
        rejected.append("lease_campaign_mismatch")
    if str(lease.get("config_hash") or "") != str(state.get("config_hash") or ""):
        rejected.append("lease_config_mismatch")

    outcome = str(lease.get("outcome") or "")
    operation_outcome = str(lease.get("operation_outcome") or "")
    allowed_outcomes = {"evidence_denied", "preflight_denied", "budget_denied"}
    if outcome not in allowed_outcomes or operation_outcome != outcome:
        rejected.append("soft_gate_outcome_unrecognized")
    provider_identity_values = [
        lease.get(key)
        for key in ("provider_outcome", "provider_event_id", "provider_estimated_cost_usd")
    ]
    if any(value is not None and value != "" for value in provider_identity_values):
        rejected.append("soft_gate_provider_identity_present")
    failure_category = str(lease.get("failure_category") or "")
    known_categories = {
        "evidence_denied": {"reliable_direct_evidence_unavailable"},
        "preflight_denied": {"model_preflight_blocked", "daily_claim_already_exists"},
        "budget_denied": {"cost_ceiling_or_call_limit"},
    }
    if failure_category not in known_categories.get(operation_outcome, set()):
        rejected.append("soft_gate_failure_category_unrecognized")

    captured_at = _strict_timestamp(watermark.get("captured_at"))
    started_at = _strict_timestamp(lease.get("started_at"))
    completed_at = _strict_timestamp(lease.get("completed_at"))
    if captured_at is None:
        rejected.append("watermark_timestamp_invalid")
    if started_at is None:
        rejected.append("lease_started_at_invalid")
    if completed_at is None:
        rejected.append("lease_completed_at_invalid")
    if captured_at is not None and started_at is not None and started_at < captured_at:
        rejected.append("lease_started_before_watermark")
    if started_at is not None and completed_at is not None and completed_at < started_at:
        rejected.append("lease_completion_precedes_start")
    if completed_at is not None and completed_at > now:
        rejected.append("lease_completion_in_future")

    if any(value != 0 for value in deltas.values()):
        rejected.append("soft_gate_safety_counter_changed")
    if not math.isfinite(cost_delta) or abs(cost_delta) > 1.0e-9:
        rejected.append("soft_gate_cost_changed")
    for key in ("llm_cost_event_max_id", "llm_cost_event_sequence"):
        previous = _snapshot_integer(before, key)
        observed = _snapshot_integer(current, key)
        if previous is None or observed is None or previous != observed:
            rejected.append(f"soft_gate_cost_marker_changed:{key}")
    if str(before.get("autonomous_attempts_digest") or "") != str(
        current.get("autonomous_attempts_digest") or ""
    ):
        rejected.append("soft_gate_autonomous_ledger_changed")

    if rejected:
        return None, sorted(set(rejected))
    return (
        {
            "lease_id": lease_id,
            "result_type": "soft_gate_blocked",
            "status": outcome,
            "operation_outcome": operation_outcome,
            "failure_category": failure_category,
            "lease_started_at": str(lease["started_at"]),
            "lease_completed_at": str(lease["completed_at"]),
        },
        [],
    )


def _intercycle_safety_check(
    conn: sqlite3.Connection,
    state: dict,
    current: dict,
    now: dt.datetime,
    cfg: dict,
) -> dict:
    """Detect forbidden writes since the exact end of the prior cycle."""

    result = {
        "status": "clean",
        "checked_at": _iso(now),
        "hard_halt": False,
        "reasons": [],
        "deltas": {},
        "authorized_paid_research": None,
    }
    watermark = state.get("last_completed_safety_watermark")
    if not watermark:
        if int(state.get("total_cycle_count", 0) or 0) == 0:
            result["status"] = "bootstrap_no_completed_cycle"
            return result
        result.update(
            {
                "status": "hard_halt",
                "hard_halt": True,
                "reasons": ["intercycle_safety_watermark_missing"],
            }
        )
        return result
    if not isinstance(watermark, dict):
        result.update(
            {
                "status": "hard_halt",
                "hard_halt": True,
                "reasons": ["intercycle_safety_watermark_invalid"],
            }
        )
        return result
    result["watermark_cycle_id"] = str(watermark.get("cycle_id") or "")
    result["watermark_captured_at"] = watermark.get("captured_at")
    reasons: list[str] = []
    if int(watermark.get("version", 0) or 0) != SAFETY_WATERMARK_VERSION:
        reasons.append("intercycle_safety_watermark_version_invalid")
    if not result["watermark_cycle_id"]:
        reasons.append("intercycle_safety_watermark_cycle_id_missing")
    if _strict_timestamp(watermark.get("captured_at")) is None:
        reasons.append("intercycle_safety_watermark_timestamp_invalid")
    if str(watermark.get("config_hash") or "") != str(state.get("config_hash") or ""):
        reasons.append("intercycle_safety_watermark_config_mismatch")
    prior_lease_digest = str(watermark.get("last_paid_research_lease_digest") or "")
    current_lease_digest = _json_digest(state.get("last_paid_research_lease"))
    if len(prior_lease_digest) != 64:
        reasons.append("intercycle_paid_research_lease_watermark_invalid")
        lease_changed = False
    else:
        lease_changed = prior_lease_digest != current_lease_digest
    before = watermark.get("safety_snapshot")
    if not isinstance(before, dict):
        reasons.append("intercycle_safety_snapshot_missing")
        before = {}

    deltas: dict[str, int] = {}
    for key in INTERCYCLE_INTEGER_COUNTERS:
        previous = _snapshot_integer(before, key)
        observed = _snapshot_integer(current, key)
        if previous is None:
            reasons.append(f"intercycle_watermark_counter_unknown:{key}")
        if observed is None:
            reasons.append(f"intercycle_current_counter_unknown:{key}")
        if previous is not None and observed is not None:
            deltas[key] = observed - previous
    result["deltas"] = copy.deepcopy(deltas)

    before_cost = _snapshot_cost(before)
    current_cost = _snapshot_cost(current)
    if before_cost is None:
        reasons.append("intercycle_watermark_cost_unknown")
        cost_delta = math.nan
    elif current_cost is None:
        reasons.append("intercycle_current_cost_unknown")
        cost_delta = math.nan
    else:
        cost_delta = current_cost - before_cost
        result["llm_cost_usd_delta"] = round(cost_delta, 10)
        if cost_delta < -1.0e-9:
            reasons.append("intercycle_counter_regression:llm_cost_usd")

    for key in ("invalid_llm_timestamps", "invalid_llm_costs", "invalid_deferred_cost_lines"):
        value = _snapshot_integer(current, key)
        if value is None or value != 0:
            reasons.append(f"intercycle_unknown_cost_state:{key}")
    if current.get("cost_ledger_known") is not True:
        reasons.append("intercycle_cost_ledger_unknown")

    marker_changed = False
    for key in ("llm_cost_event_max_id", "llm_cost_event_sequence"):
        previous = _snapshot_integer(before, key)
        observed = _snapshot_integer(current, key)
        if previous is None or observed is None:
            reasons.append(f"intercycle_cost_event_marker_unknown:{key}")
        elif previous != observed:
            marker_changed = True
    before_autonomous_digest = str(before.get("autonomous_attempts_digest") or "")
    current_autonomous_digest = str(current.get("autonomous_attempts_digest") or "")
    if len(before_autonomous_digest) != 64 or len(current_autonomous_digest) != 64:
        reasons.append("intercycle_autonomous_attempt_digest_unknown")
        autonomous_marker_changed = False
    else:
        autonomous_marker_changed = before_autonomous_digest != current_autonomous_digest
    changed_keys = sorted(key for key, value in deltas.items() if value != 0)
    model_changed = bool(
        marker_changed
        or deltas.get("llm_cost_events", 0) != 0
        or deltas.get("paid_model_attempts", 0) != 0
        or (math.isfinite(cost_delta) and abs(cost_delta) > 1.0e-9)
    )
    non_model_changed = [
        key
        for key in changed_keys
        if key
        not in {
            "llm_cost_events",
            "paid_model_attempts",
            "autonomous_attempts_today",
        }
    ]

    attribution = None
    attribution_rejections: list[str] = []
    if model_changed and not reasons:
        attribution, attribution_rejections = _authorized_paid_research_attribution(
            conn,
            state,
            watermark,
            before,
            current,
            deltas,
            cost_delta,
            now,
            cfg,
        )
    elif lease_changed and not autonomous_marker_changed and not reasons:
        attribution, attribution_rejections = _categorized_zero_delta_paid_gate(
            state,
            watermark,
            before,
            current,
            deltas,
            cost_delta,
            now,
        )
    if attribution is not None and not non_model_changed:
        result_type = str(attribution.get("result_type") or "")
        if result_type == "success":
            result["status"] = "authorized_paid_research"
        elif result_type == "soft_gate_blocked":
            result["status"] = "attributed_paid_research_gate"
        else:
            result["status"] = "attributed_paid_research_failure"
        result["soft_pause"] = result_type != "success"
        result["authorized_paid_research"] = attribution
        return result

    if model_changed:
        reasons.append("intercycle_unattributed_model_activity")
        reasons.extend(
            f"intercycle_paid_research_attribution:{reason}"
            for reason in attribution_rejections
        )
    elif lease_changed:
        reasons.append("intercycle_unattributed_paid_research_lease")
        reasons.extend(
            f"intercycle_paid_research_attribution:{reason}"
            for reason in attribution_rejections
        )
    if autonomous_marker_changed or deltas.get("autonomous_attempts_today", 0) != 0:
        if deltas.get("autonomous_attempts_today", 0) < 0:
            reasons.append("intercycle_counter_regression:autonomous_attempts_today")
        else:
            reasons.append("intercycle_forbidden_activity:autonomous_attempts_today")
    for key in non_model_changed:
        if deltas[key] < 0:
            reasons.append(f"intercycle_counter_regression:{key}")
        else:
            reasons.append(f"intercycle_forbidden_activity:{key}")
    if reasons:
        result["status"] = "hard_halt"
        result["hard_halt"] = True
        result["reasons"] = sorted(set(reasons))
    return result


def _recovery_pause_gate(attribution: dict, now: dt.datetime) -> dict:
    operation_outcome = str(attribution.get("operation_outcome") or "")
    failure_category = str(attribution.get("failure_category") or "")
    if operation_outcome == "evidence_denied":
        revalidation_kind = "research_evidence"
        requires_revalidation = True
    elif operation_outcome == "budget_denied":
        revalidation_kind = "paid_cost_capacity"
        requires_revalidation = True
    elif (
        operation_outcome == "preflight_denied"
        and failure_category == "daily_claim_already_exists"
    ):
        revalidation_kind = "utc_day_rollover"
        requires_revalidation = True
    else:
        # Provider/downstream failures and non-deterministic model preflight
        # failures use the bounded three-probe cooldown.  Radar intentionally
        # has no provider credentials with which to repeat model preflight.
        revalidation_kind = "transient_cooldown"
        requires_revalidation = False
    completed_at = _strict_timestamp(attribution.get("lease_completed_at"))
    return {
        "source": "paid_research_once",
        "lease_id": str(attribution.get("lease_id") or ""),
        "operation_outcome": operation_outcome or None,
        "result_type": str(attribution.get("result_type") or "") or None,
        "failure_category": failure_category or None,
        "latched_at": _iso(now),
        "lease_completed_at": (
            _iso(completed_at) if completed_at is not None else None
        ),
        "blocked_utc_day": (
            completed_at.date().isoformat() if completed_at is not None else None
        ),
        "revalidation_kind": revalidation_kind,
        "requires_revalidation": requires_revalidation,
    }


def _paid_cost_capacity_revalidation(
    conn: sqlite3.Connection,
    now: dt.datetime,
    settings: dict,
) -> dict:
    paid_predicate = """
        status='model_call' or status like 'model_call:%' or
        status='model_call_reserved' or
        (event_id is not null and status like 'fallback_error:%')
    """
    day_utc = now.date().isoformat()
    rolling_start = (now - dt.timedelta(hours=24)).isoformat()
    windows: dict[str, dict] = {}
    healthy = True
    try:
        from cost_router import (
            _locked_budget_limit,
            _locked_call_limit,
            load_llm_config,
        )

        llm_cfg = load_llm_config()
        agent_cfg = (llm_cfg.get("agents") or {}).get("global_research_worker", {})
        global_day_budget = _locked_budget_limit(
            llm_cfg,
            "daily_budget_usd",
            PAID_RESEARCH_MAX_COST_USD,
            PAID_RESEARCH_MAX_COST_USD,
        )
        global_rolling_budget = _locked_budget_limit(
            llm_cfg,
            "rolling_24h_budget_usd",
            global_day_budget,
            PAID_RESEARCH_MAX_COST_USD,
        )
        global_day_calls = _locked_call_limit(
            llm_cfg,
            "daily_call_limit",
            PAID_RESEARCH_MAX_CALLS,
            PAID_RESEARCH_MAX_CALLS,
        )
        global_rolling_calls = _locked_call_limit(
            llm_cfg,
            "rolling_24h_call_limit",
            global_day_calls,
            PAID_RESEARCH_MAX_CALLS,
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
        agent_day_calls = _locked_call_limit(
            agent_cfg,
            "daily_call_limit",
            global_day_calls,
            global_day_calls,
        )
        agent_rolling_calls = _locked_call_limit(
            agent_cfg,
            "rolling_24h_call_limit",
            min(agent_day_calls, global_rolling_calls),
            global_rolling_calls,
        )
        for scope_name, agent_clause, agent_params, limits in (
            (
                "global",
                "",
                (),
                {
                    "utc_day": (global_day_budget, global_day_calls),
                    "rolling_24h": (global_rolling_budget, global_rolling_calls),
                },
            ),
            (
                "paid_research_agent",
                " and agent_name=?",
                ("global_research_worker",),
                {
                    "utc_day": (agent_day_budget, agent_day_calls),
                    "rolling_24h": (agent_rolling_budget, agent_rolling_calls),
                },
            ),
        ):
            for window_name, where_sql, params in (
                ("utc_day", "date(created_at)=?", (day_utc,)),
                (
                    "rolling_24h",
                    "julianday(created_at)>=julianday(?)",
                    (rolling_start,),
                ),
            ):
                row = conn.execute(
                    f"""
                    select coalesce(sum(estimated_cost_usd),0),
                           coalesce(sum(case when {paid_predicate} then 1 else 0 end),0)
                    from llm_cost_events
                    where {where_sql}{agent_clause}
                    """,
                    (*params, *agent_params),
                ).fetchone()
                cost_usd = float(row[0] or 0.0)
                calls = int(row[1] or 0)
                cost_limit, call_limit = limits[window_name]
                window_healthy = bool(
                    math.isfinite(cost_usd)
                    and cost_usd >= 0
                    and cost_limit > 0
                    and cost_usd < cost_limit
                    and call_limit > 0
                    and 0 <= calls < call_limit
                )
                windows[f"{scope_name}_{window_name}"] = {
                    "cost_usd": cost_usd,
                    "calls": calls,
                    "cost_limit_usd": cost_limit,
                    "call_limit": call_limit,
                    "healthy": window_healthy,
                }
                healthy = healthy and window_healthy
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        return {
            "healthy": False,
            "reason": f"cost_capacity_unavailable:{type(exc).__name__}",
            "windows": windows,
        }
    expansion = settings.get("paper_expansion") or {}
    deferred = _deferred_cost_state(
        pathlib.Path(str(expansion.get("deferred_cost_path") or DEFERRED_COST_PATH))
    )
    deferred_clear = bool(deferred.get("known")) and int(
        deferred.get("line_count", -1) or 0
    ) == 0
    autonomous_path = pathlib.Path(
        str(expansion.get("autonomous_ledger_path") or AUTONOMOUS_LEDGER_PATH)
    )
    autonomous_known = True
    autonomous_calls = 0
    if autonomous_path.exists():
        autonomous_conn: sqlite3.Connection | None = None
        try:
            autonomous_conn = sqlite3.connect(
                f"file:{autonomous_path.as_posix()}?mode=ro",
                uri=True,
                timeout=1.0,
            )
            row = autonomous_conn.execute(
                "select count(*) from autonomous_paid_attempts where day_utc=?",
                (day_utc,),
            ).fetchone()
            autonomous_calls = int((row[0] if row else 0) or 0)
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
            autonomous_known = False
        finally:
            if autonomous_conn is not None:
                autonomous_conn.close()
    autonomous_capacity = bool(
        autonomous_known and 0 <= autonomous_calls < PAID_RESEARCH_MAX_CALLS
    )
    healthy = healthy and deferred_clear and autonomous_capacity
    reason = "cost_capacity_available"
    if not deferred_clear:
        reason = "deferred_cost_replay_pending_or_unknown"
    elif not autonomous_capacity:
        reason = "autonomous_paid_capacity_exhausted_or_unknown"
    elif not healthy:
        reason = "cost_capacity_exhausted"
    return {
        "healthy": healthy,
        "reason": reason,
        "limits": {
            "global_cost_usd": PAID_RESEARCH_MAX_COST_USD,
            "global_calls": PAID_RESEARCH_MAX_CALLS,
        },
        "windows": windows,
        "deferred_cost_lines": deferred.get("line_count"),
        "deferred_cost_known": bool(deferred.get("known")),
        "autonomous_attempts_utc_day": autonomous_calls,
        "autonomous_attempts_known": autonomous_known,
    }


def _research_evidence_revalidation(
    conn: sqlite3.Connection,
    settings: dict,
) -> dict:
    try:
        from paid_research_once import _research_context, _research_evidence_contracts

        contracts = _research_evidence_contracts(_research_context(conn, settings))
    except Exception as exc:  # The pause stays latched on any deterministic read failure.
        return {
            "healthy": False,
            "reason": f"research_evidence_unavailable:{type(exc).__name__}",
            "contract_count": 0,
        }
    return {
        "healthy": bool(contracts),
        "reason": (
            "reliable_direct_evidence_available"
            if contracts
            else "reliable_direct_evidence_unavailable"
        ),
        "contract_count": len(contracts),
    }


def _pause_gate_revalidation(
    conn: sqlite3.Connection,
    state: dict,
    settings: dict,
    now: dt.datetime,
) -> dict:
    gate = state.get("recovery_pause_gate")
    if not isinstance(gate, dict) or not gate:
        return {
            "active": False,
            "requires_revalidation": False,
            "healthy": True,
            "checked_at": _iso(now),
            "kind": None,
            "reason": "no_recovery_pause_gate",
        }
    kind = str(gate.get("revalidation_kind") or "")
    required = bool(gate.get("requires_revalidation"))
    if kind == "research_evidence":
        detail = _research_evidence_revalidation(conn, settings)
    elif kind == "paid_cost_capacity":
        detail = _paid_cost_capacity_revalidation(conn, now, settings)
    elif kind == "utc_day_rollover":
        blocked_day = str(gate.get("blocked_utc_day") or "")
        healthy = bool(blocked_day and now.date().isoformat() > blocked_day)
        detail = {
            "healthy": healthy,
            "reason": "utc_day_advanced" if healthy else "daily_claim_window_still_blocked",
            "blocked_utc_day": blocked_day or None,
            "current_utc_day": now.date().isoformat(),
        }
    else:
        detail = {
            "healthy": True,
            "reason": "bounded_transient_cooldown",
        }
    return {
        "active": True,
        "requires_revalidation": required,
        "healthy": bool(detail.get("healthy")) if required else True,
        "checked_at": _iso(now),
        "kind": kind or "transient_cooldown",
        "lease_id": str(gate.get("lease_id") or "") or None,
        "operation_outcome": gate.get("operation_outcome"),
        "failure_category": gate.get("failure_category"),
        "reason": str(detail.get("reason") or "pause_gate_revalidation_failed"),
        "detail": detail,
    }


def _force_radar_plane_off(effective: dict) -> None:
    false_flags = {
        "llm_bridge": ("enabled", "ingest_recommendations"),
        "llm_swarm": ("enabled", "auto_run", "run_in_radar_loop"),
        "dynamic_agents": ("enabled",),
        "research_worker": ("enabled", "run_every_evolution_cycle"),
        "autonomous_builder": ("enabled", "auto_run", "run_in_radar_loop"),
        "codex_repo_agent": (
            "enabled",
            "parallel_sessions_enabled",
            "network_access",
            "chatgpt_account_fallback_enabled",
            "fallback_on_api_quota",
        ),
        "adapter_implementation_owner": ("enabled",),
        "market_activation_owner": ("enabled",),
        "strategy_implementation_owner": ("enabled", "contract_intake_enabled"),
        "codex_worker_pool": ("enabled",),
        "code_evolution": ("enabled", "git_release_enabled", "auto_merge_paper_only"),
        "evolution_worker": ("enabled",),
        "agent_memory": ("enabled",),
        "okx_signal_research": ("enabled",),
        "paper_exploration": ("enabled",),
        "signal_redesign": ("enabled",),
        "hunter": ("enabled",),
        "self_improvement": ("enabled", "process_code_changes_in_radar_loop"),
        "signal_safety": ("enabled",),
        "contextual_failure_filters": ("enabled", "create_policies"),
        "okx_perp_funding_basis_decay_quarantine": ("enabled",),
        "paper_context_drag": ("enabled",),
        "paper_context_priors": ("enabled",),
        "paper_context_loss_quarantine": ("enabled",),
    }
    for section, keys in false_flags.items():
        target = effective.setdefault(section, {})
        for key in keys:
            target[key] = False
    reliability = effective.setdefault("strategy_reliability", {})
    reliability["enabled"] = False
    reliability["bypass_all_overlays"] = True
    learning = effective.setdefault("learning", {})
    learning["task_emission_enabled"] = False
    learning["growth_experiment_emission_enabled"] = False


def _bounded_process_guard() -> dict:
    role = str(os.environ.get("RADAR_PROCESS_ROLE") or "").strip()
    supervisor_count = _environment_count("RADAR_BOUNDED_SUPERVISOR_COUNT")
    child_count = _environment_count("RADAR_BOUNDED_CHILD_COUNT")
    forbidden_worker_count = _environment_count("RADAR_FORBIDDEN_WORKER_COUNT")
    reasons: list[str] = []
    if role != "bounded_paper_radar":
        reasons.append("process_role")
    if supervisor_count != 1:
        reasons.append("supervisor_cardinality")
    if child_count != 1:
        reasons.append("child_cardinality")
    if forbidden_worker_count != 0:
        reasons.append("forbidden_worker_overlap")
    return {
        "authorized": not reasons,
        "role": role or None,
        "supervisor_count": supervisor_count,
        "child_count": child_count,
        "forbidden_worker_count": forbidden_worker_count,
        "reasons": reasons,
    }


def _force_reconciliation_only(effective: dict, *, health_probe: bool = False) -> None:
    """Suppress all new work while retaining the base open-trade pricing pass."""

    scanner = effective.setdefault("scanner", {})
    for key in (
        "scan_universe",
        "review_top",
        "global_review_top",
        "global_market_discovery_review_top",
        "prediction_review_top",
        "frontier_crypto_review_top",
        "max_new_paper_trades",
        "max_new_paper_observations",
    ):
        scanner[key] = 0
    for key in (
        "enable_global_proxy_scan",
        "enable_global_market_discovery_scan",
        "enable_prediction_market_scan",
        "enable_public_market_adapter_scan",
    ):
        scanner[key] = False
    scanner["enable_crypto_venue_health_scan"] = bool(health_probe)
    scanner["enable_frontier_crypto_adapter_scan"] = bool(health_probe)

    effective.setdefault("risk", {})["max_open_paper_trades"] = 0
    admission = effective.setdefault("market_admission", {})
    admission.update(
        {
            "enabled": True,
            "monitor_enabled": False,
            "paper_queue_enabled": True,
            "queue_enabled": True,
            "bridge_enabled": False,
            "diagnostics_enabled": False,
            "actions_enabled": False,
        }
    )
    queue = admission.setdefault("paper_queue", {})
    queue["max_enqueue_per_cycle"] = 0
    queue["max_select_per_cycle"] = 0
    queue["max_terminal_audit_per_cycle"] = 0
    lab = effective.setdefault("strategy_lab", {})
    lab["snapshot_max_inputs_per_loop"] = 200
    lab["snapshot_max_instruments_per_loop"] = 50
    lab["feature_history_max_points"] = 288
    lab.update(
        {
            "enabled": False,
            "promoted_signal_plugins_enabled": False,
            "bootstrap_recovery_canary_enabled": False,
            "snapshot_warmup_enabled": False,
            "runtime_generation_enabled": False,
            "evaluation_enabled": False,
            "lifecycle_mutations_enabled": False,
            "recommendation_emission_enabled": False,
            "promotion_enabled": False,
            "adaptive_relaxation_enabled": False,
            "region_splits_enabled": False,
            "max_candidates_per_loop": 0,
            "max_candidates_per_experiment": 0,
            "runtime_review_reserved_slots": 0,
        }
    )
    lab.setdefault("adaptive_relaxation", {})["enabled"] = False
    for section in (
        "okx_signal_research",
        "paper_exploration",
        "signal_redesign",
    ):
        effective.setdefault(section, {})["enabled"] = False
    if not health_probe:
        effective.setdefault("frontier_crypto_adapter", {})["enabled"] = False
    expansion = effective.setdefault("paper_expansion", {})
    expansion["measurement_probe_enabled"] = False
    expansion["reconciliation_only"] = True


def _latch_runtime_overlap(
    conn: sqlite3.Connection,
    state: dict,
    *,
    reason: str,
    now: dt.datetime,
) -> None:
    state["run_status"] = "hard_halted"
    state["healthy_streak"] = 0
    state["hard_halt_reason"] = reason
    state["stop_reason"] = reason
    state["updated_at"] = _iso(now)
    _persist_state(conn, state)


def _atomic_claim_campaign_cycle(
    conn: sqlite3.Connection,
    *,
    expected_state: dict,
    inflight_cycle: dict,
    now: dt.datetime,
) -> dict:
    """Serialize radar/paid leases and claim exactly one bounded cycle.

    Expensive scan preparation happens outside the write transaction.  The
    final compare-and-claim is deliberately short, but reloads the complete
    persisted state under ``BEGIN IMMEDIATE`` so copied configs or direct
    Python invocations cannot race the PowerShell mutex.
    """

    if conn.in_transaction:
        conn.commit()
    campaign_id = str(expected_state["campaign_id"])
    config_hash = str(expected_state.get("config_hash") or "")
    try:
        conn.execute("begin immediate")
        row = conn.execute(
            "select * from paper_expansion_campaign_state where campaign_id=?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise CampaignError("campaign_state_missing_during_cycle_claim")
        current = _decode_state(row, now)
        existing_cycle = current.get("inflight_cycle")
        paid_lease = current.get("paid_research_inflight")
        reason = None
        if isinstance(existing_cycle, dict) and existing_cycle:
            started = _parse_timestamp(existing_cycle.get("cycle_started_at"), now)
            age_seconds = max(0.0, (now - started).total_seconds())
            timeout_seconds = float(
                (
                    ((existing_cycle.get("campaign_config") or {}).get("health") or {}).get(
                        "runtime_halt_seconds", 720
                    )
                )
                or 720
            )
            reason = (
                "stale_or_timed_out_inflight_cycle"
                if age_seconds >= timeout_seconds
                else "overlapping_bounded_workers"
            )
        elif paid_lease:
            reason = "paid_research_overlap_or_stale_lease"
        elif str(current.get("config_hash") or "") != config_hash:
            reason = "config_hash_changed_during_cycle_claim"
        else:
            comparable_keys = (
                "phase",
                "run_status",
                "phase_cycle_count",
                "total_cycle_count",
                "phase_started_at",
                "updated_at",
            )
            if any(current.get(key) != expected_state.get(key) for key in comparable_keys):
                reason = "campaign_state_changed_during_cycle_claim"
        if reason:
            _latch_runtime_overlap(conn, current, reason=reason, now=now)
            conn.commit()
            raise CampaignError(reason)
        current["inflight_cycle"] = copy.deepcopy(inflight_cycle)
        current["updated_at"] = _iso(now)
        _persist_state(conn, current)
        conn.commit()
        return current
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def apply_campaign_controls(conn: sqlite3.Connection, settings: dict) -> tuple[dict, dict]:
    """Return fail-closed effective settings and a per-cycle campaign token."""

    cfg = _campaign_config(settings)
    if not cfg:
        return copy.deepcopy(settings), {"enabled": False, "status": "campaign_disabled"}
    _assert_fail_closed_settings(settings)
    now = _utc_now()
    campaign_id = str(cfg.get("campaign_id") or "bounded_crypto_paper_v1")
    # Lock the complete normalized runtime profile, not only the phase block.
    # Queue freshness/lease rules, scanner toggles, route gates, and every
    # other effective safety knob must participate in drift detection.
    config_hash = _config_hash(settings)
    state = _load_or_create_state(conn, campaign_id, now, config_hash)
    process_guard = _bounded_process_guard()
    if not process_guard["authorized"] and state["run_status"] != "hard_halted":
        reason = "bounded_process_guard:" + ",".join(process_guard["reasons"])
        state["run_status"] = "hard_halted"
        state["healthy_streak"] = 0
        state["hard_halt_reason"] = reason
        state["stop_reason"] = reason
        state["last_process_guard"] = copy.deepcopy(process_guard)
        state["updated_at"] = _iso(now)
        _persist_state(conn, state)
        conn.commit()
    phase = str(state["phase"])
    phase_cfg = copy.deepcopy((cfg.get("phases") or {})[phase])
    effective = copy.deepcopy(settings)
    effective["mode"] = "paper"
    effective["allow_live_trading"] = False
    effective.setdefault("risk", {})["max_live_notional_usd"] = 0.0
    effective["risk"]["paper_notional_usd"] = 100.0
    effective["risk"]["max_open_paper_trades"] = max(
        0, int(phase_cfg.get("max_open_paper_trades", 0))
    )
    scanner = effective.setdefault("scanner", {})
    scanner["scan_universe"] = max(0, int(phase_cfg.get("scan_universe", 0)))
    scanner["review_top"] = max(0, int(phase_cfg.get("review_top", 0)))
    scanner["max_new_paper_trades"] = max(0, int(phase_cfg.get("max_new_paper_trades", 0)))
    scanner["max_new_paper_observations"] = max(
        0, int(phase_cfg.get("max_new_paper_observations", 0))
    )
    for key in (
        "enable_global_proxy_scan",
        "enable_global_market_discovery_scan",
        "enable_prediction_market_scan",
        "enable_public_market_adapter_scan",
    ):
        scanner[key] = False
    lab = effective.setdefault("strategy_lab", {})
    lab["enabled"] = bool(phase_cfg.get("strategy_lab_enabled", False))
    lab["promoted_signal_plugins_enabled"] = bool(
        phase_cfg.get("promoted_signal_plugins_enabled", False)
    )
    if "strategy_lab_max_candidates_per_loop" in phase_cfg:
        lab["max_candidates_per_loop"] = int(phase_cfg["strategy_lab_max_candidates_per_loop"])
    if "strategy_lab_max_candidates_per_experiment" in phase_cfg:
        lab["max_candidates_per_experiment"] = int(
            phase_cfg["strategy_lab_max_candidates_per_experiment"]
        )
    if "strategy_lab_runtime_review_reserved_slots" in phase_cfg:
        lab["runtime_review_reserved_slots"] = int(
            phase_cfg["strategy_lab_runtime_review_reserved_slots"]
        )
    if "max_active_strategy_roots" in phase_cfg:
        lab["max_active_strategy_roots"] = int(phase_cfg["max_active_strategy_roots"])
    for key, default in (
        ("bootstrap_recovery_canary_enabled", False),
        ("snapshot_warmup_enabled", False),
        ("runtime_generation_enabled", False),
        ("evaluation_enabled", False),
        ("lifecycle_mutations_enabled", False),
        ("recommendation_emission_enabled", False),
        ("promotion_enabled", False),
        ("adaptive_relaxation_enabled", False),
        ("region_splits_enabled", False),
    ):
        lab[key] = bool(phase_cfg.get(key, default))
    lab["experiment_root_allowlist"] = _bounded_strategy_root_allowlist(
        conn,
        phase=phase,
        configured=copy.deepcopy(phase_cfg.get("experiment_root_allowlist", [])),
        max_roots=int(phase_cfg.get("max_active_strategy_roots", 0) or 0),
    )
    promotion_contract = cfg.get("discovery_promotion") or {}
    if phase in {"canary", "research"}:
        lab.update(
            {
                "promote_min_labels": int(promotion_contract.get("min_training_labels", 100)),
                "promote_min_training_labels": int(
                    promotion_contract.get("min_training_labels", 100)
                ),
                "promote_min_holdout_labels": int(
                    promotion_contract.get("min_holdout_labels", 50)
                ),
                "holdout_min_labels": int(promotion_contract.get("min_holdout_labels", 50)),
                "promote_holdout_min_labels": int(
                    promotion_contract.get("min_holdout_labels", 50)
                ),
                "promote_min_active_hours": float(
                    promotion_contract.get("min_elapsed_hours", 168)
                ),
                "promote_min_avg_pnl_bps": float(
                    promotion_contract.get("min_avg_net_pnl_bps", 10.0)
                ),
                "promote_min_win_rate": float(promotion_contract.get("min_win_rate", 0.53)),
                "promote_worst_decile_floor_bps": float(
                    promotion_contract.get("min_worst_decile_bps", -45.0)
                ),
                "promote_min_valid_label_rate": float(
                    promotion_contract.get("min_timely_label_rate", 0.90)
                ),
                "consecutive_passes_to_promote": int(
                    promotion_contract.get("min_consecutive_passes", 2)
                ),
            }
        )
    lab.setdefault("adaptive_relaxation", {})["enabled"] = False
    admission = effective.setdefault("market_admission", {})
    admission.update(
        {
            "enabled": True,
            "monitor_enabled": True,
            "paper_queue_enabled": True,
            "bridge_enabled": False,
            "diagnostics_enabled": False,
            "actions_enabled": False,
        }
    )
    admission.setdefault("paper_queue", {})["max_select_per_cycle"] = max(
        0,
        int(
            phase_cfg.get(
                "paper_queue_max_select_per_cycle",
                min(30, int(phase_cfg.get("review_top", 0) or 0)),
            )
        ),
    )
    _force_radar_plane_off(effective)

    runtime_phase = {
        "burn_in": "burn_in",
        "measurement": "measurement",
        "canary": "strategy_lab_canary",
        "research": "paid_research",
    }[phase]
    expansion_runtime = effective.setdefault("paper_expansion", {})
    expansion_runtime["runtime_phase"] = runtime_phase
    expansion_runtime["measurement_probe_enabled"] = phase in {"measurement", "canary", "research"}
    expansion_runtime["measurement_probe_allocation_multiplier"] = 1.0
    expansion_runtime["max_active_strategy_roots"] = int(
        phase_cfg.get("max_active_strategy_roots", 0) or 0
    )
    expansion_runtime["reconciliation_only"] = False

    baseline = _safety_snapshot(conn, cfg, now)
    intercycle_check = _intercycle_safety_check(conn, state, baseline, now, cfg)
    if bool(intercycle_check.get("hard_halt")) and state["run_status"] != "hard_halted":
        intercycle_reasons = list(intercycle_check.get("reasons") or [])
        reason = intercycle_reasons[0] if intercycle_reasons else "intercycle_safety_violation"
        state["run_status"] = "hard_halted"
        state["healthy_streak"] = 0
        state["hard_halt_reason"] = reason
        state["stop_reason"] = ";".join(intercycle_reasons) or reason
        state["last_intercycle_safety_check"] = copy.deepcopy(intercycle_check)
        state["updated_at"] = _iso(now)
        _persist_state(conn, state)
        conn.commit()
    elif bool(intercycle_check.get("soft_pause")) and state["run_status"] == "running":
        attribution = intercycle_check.get("authorized_paid_research") or {}
        reason = "attributed_paid_research_failure:" + str(
            attribution.get("status") or "fallback_error"
        )
        state["run_status"] = "soft_paused"
        state["healthy_streak"] = 0
        state["stop_reason"] = reason
        state["recovery_pause_gate"] = _recovery_pause_gate(attribution, now)
        state["last_intercycle_safety_check"] = copy.deepcopy(intercycle_check)
        state["updated_at"] = _iso(now)
        _persist_state(conn, state)
        conn.commit()
    ledger_halt_reason = None
    if int(baseline.get("invalid_llm_timestamps", 0) or 0) != 0:
        ledger_halt_reason = "cost_ledger_invalid_timestamps_at_cycle_start"
    elif int(baseline.get("invalid_llm_costs", 0) or 0) != 0:
        ledger_halt_reason = "cost_ledger_invalid_costs_at_cycle_start"
    elif int(baseline.get("invalid_deferred_cost_lines", 0) or 0) != 0:
        ledger_halt_reason = "deferred_cost_ledger_invalid_at_cycle_start"
    elif not bool(baseline.get("cost_ledger_known", False)):
        ledger_halt_reason = "cost_ledger_unknown_at_cycle_start"
    if ledger_halt_reason and state["run_status"] != "hard_halted":
        state["run_status"] = "hard_halted"
        state["healthy_streak"] = 0
        state["hard_halt_reason"] = ledger_halt_reason
        state["stop_reason"] = ledger_halt_reason
        state["updated_at"] = _iso(now)
        _persist_state(conn, state)
        conn.commit()

    pause_gate_revalidation = _pause_gate_revalidation(conn, state, settings, now)

    if state["run_status"] in {"soft_paused", "hard_halted"}:
        _force_reconciliation_only(
            effective,
            health_probe=state["run_status"] == "soft_paused",
        )

    cycle = {
        **copy.deepcopy(state),
        "enabled": True,
        "cycle_id": str(uuid.uuid4()),
        "cycle_started_at": _iso(now),
        "baseline": baseline,
        "intercycle_safety_check": copy.deepcopy(intercycle_check),
        "pause_gate_revalidation": copy.deepcopy(pause_gate_revalidation),
        "campaign_config": copy.deepcopy(cfg),
        "config_hash": config_hash,
        "effective_controls": {
            "scan_universe": scanner["scan_universe"],
            "review_top": scanner["review_top"],
            "max_new_paper_trades": scanner["max_new_paper_trades"],
            "max_new_paper_observations": scanner["max_new_paper_observations"],
            "max_open_paper_trades": effective["risk"]["max_open_paper_trades"],
            "strategy_lab_enabled": lab["enabled"],
            "promoted_signal_plugins_enabled": lab["promoted_signal_plugins_enabled"],
            "research_process_separate": True,
            "runtime_phase": runtime_phase,
            "measurement_probe_enabled": expansion_runtime["measurement_probe_enabled"],
            "max_active_strategy_roots": expansion_runtime["max_active_strategy_roots"],
            "strategy_lab_max_candidates_per_loop": lab.get("max_candidates_per_loop"),
            "strategy_lab_runtime_review_reserved_slots": lab.get(
                "runtime_review_reserved_slots"
            ),
            "strategy_lab_experiment_root_allowlist": copy.deepcopy(
                lab["experiment_root_allowlist"]
            ),
            "strategy_lab_lifecycle_mutations_enabled": lab[
                "lifecycle_mutations_enabled"
            ],
            "strategy_lab_recommendation_emission_enabled": lab[
                "recommendation_emission_enabled"
            ],
            "strategy_lab_promotion_enabled": lab["promotion_enabled"],
            "reconciliation_only": expansion_runtime["reconciliation_only"],
            "bounded_process_guard": copy.deepcopy(process_guard),
            "paper_queue_max_select_per_cycle": admission["paper_queue"][
                "max_select_per_cycle"
            ],
        },
    }
    effective["paper_expansion_runtime"] = {
        "campaign_id": campaign_id,
        "phase": runtime_phase,
        "campaign_phase": phase,
        "run_status": state["run_status"],
        "cycle_id": cycle["cycle_id"],
        "research_process_separate": True,
        "reconciliation_only": expansion_runtime["reconciliation_only"],
        "bounded_process_guard": copy.deepcopy(process_guard),
    }
    inflight_cycle = {
        "enabled": True,
        "campaign_id": campaign_id,
        "phase": phase,
        "run_status": state["run_status"],
        "cycle_id": cycle["cycle_id"],
        "cycle_started_at": cycle["cycle_started_at"],
        "baseline": copy.deepcopy(baseline),
        "intercycle_safety_check": copy.deepcopy(intercycle_check),
        "pause_gate_revalidation": copy.deepcopy(pause_gate_revalidation),
        "campaign_config": copy.deepcopy(cfg),
        "config_hash": config_hash,
        "pid": os.getpid(),
    }
    _atomic_claim_campaign_cycle(
        conn,
        expected_state=state,
        inflight_cycle=inflight_cycle,
        now=now,
    )
    return effective, cycle


def _delta(after: dict, before: dict, key: str) -> float:
    return float(after.get(key, 0) or 0) - float(before.get(key, 0) or 0)


def _merged_metrics(conn: sqlite3.Connection, state: dict, metrics: dict, cfg: dict, now: dt.datetime) -> dict:
    baseline = state.get("baseline") or {}
    current = _safety_snapshot(conn, cfg, now)
    merged = copy.deepcopy(metrics or {})
    if "phase_distinct_exact_attributed_admission_keys_paper_evaluated" not in merged and (
        "new_exact_attributed_admission_keys_paper_evaluated" in merged
    ):
        merged["phase_distinct_exact_attributed_admission_keys_paper_evaluated"] = merged[
            "new_exact_attributed_admission_keys_paper_evaluated"
        ]
    merged.update(
        {
            "cost_ledger_known": bool(baseline.get("cost_ledger_known", False))
            and bool(current.get("cost_ledger_known", False)),
            "invalid_llm_timestamps": int(current.get("invalid_llm_timestamps", -1) or 0),
            "invalid_llm_costs": int(current.get("invalid_llm_costs", -1) or 0),
            "invalid_deferred_cost_lines": int(
                current.get("invalid_deferred_cost_lines", -1) or 0
            ),
            "new_llm_cost_events": int(_delta(current, baseline, "llm_cost_events")),
            "new_llm_cost_usd": round(_delta(current, baseline, "llm_cost_usd"), 10),
            "new_paid_model_attempts": int(_delta(current, baseline, "paid_model_attempts")),
            "new_agent_runs": int(_delta(current, baseline, "agent_runs")),
            "new_strategy_owner_runs": int(_delta(current, baseline, "strategy_owner_runs")),
            "new_codex_claims": int(_delta(current, baseline, "codex_claims")),
            "new_owner_task_claims": int(_delta(current, baseline, "owner_task_claims")),
            "new_llm_recommendations": int(_delta(current, baseline, "llm_recommendations")),
            "new_memory_writes": int(
                sum(
                    _delta(current, baseline, key)
                    for key in (
                        "memory_facts",
                        "temporal_memories",
                        "temporal_memory_links",
                        "memory_retrieval_events",
                        "memory_system_state_updates",
                    )
                )
            ),
            "pending_execution_count": int(current.get("pending_execution", 0) or 0),
            "new_live_orders": int(_delta(current, baseline, "live_orders")),
            "new_nonpaper_fills": int(_delta(current, baseline, "nonpaper_fills")),
            "new_deferred_cost_lines": int(_delta(current, baseline, "deferred_cost_lines")),
            "new_autonomous_attempts": int(
                _delta(current, baseline, "autonomous_attempts_today")
            ),
            "deferred_cost_line_count": int(current.get("deferred_cost_lines", -1) or 0),
            "safety_snapshot_at_completion": copy.deepcopy(current),
            "intercycle_safety_check": copy.deepcopy(
                state.get("intercycle_safety_check") or {}
            ),
            "pause_gate_revalidation": copy.deepcopy(
                state.get("pause_gate_revalidation") or {}
            ),
            "recovery_pause_gate": copy.deepcopy(
                state.get("recovery_pause_gate") or {}
            ),
        }
    )
    return merged


def _required_metric_keys(phase: str) -> tuple[str, ...]:
    operational = (
        "cycle_success",
        "exit_code",
        "runtime_seconds",
        "peak_rss_mb",
        "supervisor_count",
        "child_count",
        "forbidden_worker_count",
        "terminal_opportunity_rate",
        "frontier_observation_count",
        "reachable_venue_count",
        "db_growth_bytes",
        "db_footprint_start_bytes",
        "db_footprint_bytes",
        "db_finalization_accounted",
        "artifact_sizes",
    )
    if phase == "measurement":
        return operational + (
            "phase_distinct_exact_attributed_admission_keys_paper_evaluated",
            "new_direct_closes",
            "new_reliable_direct_closes",
            "new_timely_direct_closes",
            "new_horizon_outcomes",
            "new_timely_horizon_outcomes",
            "phase_due_direct_closes",
            "phase_reliable_direct_closes",
            "phase_timely_direct_closes",
            "phase_due_horizon_outcomes",
            "phase_timely_horizon_outcomes",
            "new_opportunity_lineage_records",
            "new_opportunity_lineage_complete",
            "new_order_lineage_records",
            "new_order_lineage_complete",
            "new_trade_lineage_records",
            "new_trade_lineage_complete",
            "new_synthetic_proxy_primary",
            "lineage_corruption_count",
        )
    if phase == "canary":
        return operational + (
            "active_canary_count",
            "new_canary_reliable_direct_labels",
            "phase_canary_reliable_direct_labels",
        )
    return operational


def _health_reasons(metrics: dict, health_cfg: dict, phase: str) -> tuple[list[str], list[str]]:
    hard: list[str] = []
    soft: list[str] = []
    for key in _required_metric_keys(phase):
        if key not in metrics or metrics.get(key) is None:
            soft.append(f"missing_metric:{key}")
    if not bool(metrics.get("db_finalization_accounted", False)):
        soft.append("database_finalization_unaccounted")
    recovery_gate = metrics.get("recovery_pause_gate") or {}
    gate_revalidation = metrics.get("pause_gate_revalidation") or {}
    if isinstance(recovery_gate, dict) and bool(
        recovery_gate.get("requires_revalidation")
    ):
        gate_kind = str(recovery_gate.get("revalidation_kind") or "unknown")
        if not isinstance(gate_revalidation, dict) or not bool(
            gate_revalidation.get("active")
        ):
            soft.append(f"recovery_gate_revalidation_missing:{gate_kind}")
        elif gate_revalidation.get("healthy") is not True:
            soft.append(f"recovery_gate_unhealthy:{gate_kind}")
    if not bool(metrics.get("cost_ledger_known", False)):
        hard.append("cost_ledger_unknown")
    if bool(metrics.get("timed_out", False)):
        hard.append("cycle_timeout")
    if int(metrics.get("invalid_llm_timestamps", 0) or 0) != 0:
        hard.append("cost_ledger_invalid_timestamps")
    if int(metrics.get("invalid_llm_costs", 0) or 0) != 0:
        hard.append("cost_ledger_invalid_costs")
    if int(metrics.get("invalid_deferred_cost_lines", 0) or 0) != 0:
        hard.append("deferred_cost_ledger_invalid")
    if metrics.get("cycle_success") is False or (
        metrics.get("exit_code") is not None and int(metrics.get("exit_code") or 0) != 0
    ):
        soft.append("cycle_failed")
    if float(metrics.get("runtime_seconds", 0) or 0) > float(health_cfg.get("runtime_halt_seconds", 720)):
        hard.append("runtime_hard_limit")
    elif metrics.get("rolling_runtime_p95_seconds") is not None and float(
        metrics["rolling_runtime_p95_seconds"]
    ) > float(health_cfg.get("runtime_warn_seconds", 480)):
        soft.append("runtime_warning")
    if float(metrics.get("peak_rss_mb", 0) or 0) > float(health_cfg.get("peak_rss_halt_mb", 1024)):
        hard.append("memory_hard_limit")
    elif metrics.get("rolling_memory_p95_mb") is not None and float(
        metrics["rolling_memory_p95_mb"]
    ) > float(health_cfg.get("peak_rss_warn_mb", 750)):
        soft.append("memory_warning")
    if metrics.get("supervisor_count") is not None and int(metrics["supervisor_count"]) != 1:
        hard.append("supervisor_cardinality")
    if metrics.get("child_count") is not None and int(metrics["child_count"]) > 1:
        hard.append("overlapping_child")
    if metrics.get("forbidden_worker_count") is not None and int(
        metrics["forbidden_worker_count"]
    ) != 0:
        hard.append("forbidden_worker_overlap")
    for key in (
        "new_llm_cost_events",
        "new_paid_model_attempts",
        "new_agent_runs",
        "new_strategy_owner_runs",
        "new_llm_recommendations",
        "new_memory_writes",
        "new_live_orders",
        "new_nonpaper_fills",
        "new_deferred_cost_lines",
        "new_autonomous_attempts",
        "new_codex_claims",
        "new_owner_task_claims",
    ):
        value = float(metrics.get(key, 0) or 0)
        if value > 0:
            hard.append(key)
        elif value < 0:
            hard.append(f"counter_regression:{key}")
    if float(metrics.get("new_llm_cost_usd", 0) or 0) > 0:
        hard.append("new_llm_cost_usd")
    if int(metrics.get("pending_execution_count", 0) or 0) > 0:
        soft.append("pending_execution_drift")
    for prefix in ("opportunity", "order", "trade"):
        total_key = f"new_{prefix}_lineage_records"
        complete_key = f"new_{prefix}_lineage_complete"
        if total_key in metrics and complete_key in metrics:
            total = int(metrics.get(total_key, 0) or 0)
            complete = int(metrics.get(complete_key, 0) or 0)
            if total < 0 or complete < 0 or complete > total or complete != total:
                hard.append(f"{prefix}_lineage_corruption")
    if int(metrics.get("new_synthetic_proxy_primary", 0) or 0) > 0:
        hard.append("synthetic_proxy_primary_detected")
    if int(metrics.get("lineage_corruption_count", 0) or 0) > 0:
        hard.append("lineage_corruption")
    if int(metrics.get("active_canary_count", 0) or 0) > 1:
        hard.append("multiple_active_canaries")
    terminal_rate = metrics.get("terminal_opportunity_rate")
    if terminal_rate is not None and float(terminal_rate) < float(
        health_cfg.get("min_terminal_opportunity_rate", 1.0)
    ):
        soft.append("terminal_opportunity_rate")
    observation_count = metrics.get("frontier_observation_count")
    if observation_count is not None and int(observation_count) < int(
        health_cfg.get("min_frontier_observations", 0)
    ):
        soft.append("frontier_observation_count")
    reachable = metrics.get("reachable_venue_count")
    if reachable is not None and int(reachable) < int(health_cfg.get("min_reachable_venues", 0)):
        soft.append("reachable_venue_count")
    if int(metrics.get("rolling_db_growth_bytes_24h", 0) or 0) > int(
        health_cfg.get("max_db_growth_bytes_per_day", 262144000)
    ):
        soft.append("database_growth")
    artifact_sizes = metrics.get("artifact_sizes") or {}
    for name, limit in (health_cfg.get("max_artifact_bytes") or {}).items():
        if int(artifact_sizes.get(name, 0) or 0) > int(limit):
            soft.append(f"artifact_size:{name}")
    return sorted(set(hard)), sorted(set(soft))


def _accumulate(state: dict, metrics: dict) -> None:
    accumulated = state.setdefault("accumulated", {})
    # Gate-critical timing cohorts are authoritative phase snapshots.  Summing
    # only newly persisted rows lets an entirely missing due close/horizon
    # disappear from the denominator, and prevents an earlier horizon from
    # becoming eligible when its trade later obtains a reliable final close.
    phase_snapshot_fields = {
        "direct_closes": "phase_due_direct_closes",
        "reliable_direct_closes": "phase_reliable_direct_closes",
        "timely_direct_closes": "phase_timely_direct_closes",
        "horizon_outcomes": "phase_due_horizon_outcomes",
        "timely_horizon_outcomes": "phase_timely_horizon_outcomes",
        "canary_reliable_direct_labels": "phase_canary_reliable_direct_labels",
    }
    fields = {
        "direct_closes": "new_direct_closes",
        "reliable_direct_closes": "new_reliable_direct_closes",
        "timely_direct_closes": "new_timely_direct_closes",
        "horizon_outcomes": "new_horizon_outcomes",
        "timely_horizon_outcomes": "new_timely_horizon_outcomes",
        "opportunity_lineage_records": "new_opportunity_lineage_records",
        "opportunity_lineage_complete": "new_opportunity_lineage_complete",
        "order_lineage_records": "new_order_lineage_records",
        "order_lineage_complete": "new_order_lineage_complete",
        "trade_lineage_records": "new_trade_lineage_records",
        "trade_lineage_complete": "new_trade_lineage_complete",
        "synthetic_proxy_primary": "new_synthetic_proxy_primary",
        "canary_reliable_direct_labels": "new_canary_reliable_direct_labels",
    }
    for total_key, metric_key in fields.items():
        if phase_snapshot_fields.get(total_key) in metrics:
            continue
        accumulated[total_key] = int(accumulated.get(total_key, 0) or 0) + max(
            0, int(metrics.get(metric_key, 0) or 0)
        )
    for total_key, metric_key in phase_snapshot_fields.items():
        if metric_key in metrics:
            accumulated[total_key] = max(0, int(metrics.get(metric_key, 0) or 0))
    exact_distinct = metrics.get(
        "phase_distinct_exact_attributed_admission_keys_paper_evaluated"
    )
    if exact_distinct is not None:
        accumulated["exact_attributed_admission_keys"] = max(
            int(accumulated.get("exact_attributed_admission_keys", 0) or 0),
            max(0, int(exact_distinct or 0)),
        )


def _append_operational_history(state: dict, metrics: dict, now: dt.datetime) -> None:
    history = state.setdefault("operational_history", {})
    for key in ("runtime_seconds", "peak_rss_mb"):
        samples = list(history.get(key) or [])
        if metrics.get(key) is not None:
            samples.append(float(metrics[key]))
        history[key] = samples[-2000:]
    growth = list(history.get("db_growth") or [])
    if metrics.get("db_growth_bytes") is not None:
        growth.append({"at": _iso(now), "bytes": max(0, int(metrics["db_growth_bytes"]))})
    cutoff = now - dt.timedelta(hours=48)
    history["db_growth"] = [
        row
        for row in growth
        if _parse_timestamp((row or {}).get("at"), cutoff) >= cutoff
    ][-2000:]
    footprint = list(history.get("db_footprint") or [])
    if metrics.get("db_footprint_start_bytes") is not None:
        footprint.append(
            {
                "at": str(metrics.get("db_footprint_start_at") or _iso(now)),
                "bytes": max(0, int(metrics["db_footprint_start_bytes"])),
            }
        )
    if metrics.get("db_footprint_bytes") is not None:
        footprint.append(
            {"at": _iso(now), "bytes": max(0, int(metrics["db_footprint_bytes"]))}
        )
    history["db_footprint"] = [
        row
        for row in footprint
        if _parse_timestamp((row or {}).get("at"), cutoff) >= cutoff
    ][-2000:]


def _p95(values: list[object]) -> float | None:
    numeric = sorted(float(value) for value in values if value is not None)
    if not numeric:
        return None
    index = max(0, ((95 * len(numeric) + 99) // 100) - 1)
    return numeric[index]


def _rolling_db_footprint_growth(state: dict, now: dt.datetime) -> tuple[int, int] | None:
    history = state.get("operational_history") or {}
    samples: list[tuple[dt.datetime, int]] = []
    for row in history.get("db_footprint") or []:
        if not isinstance(row, dict) or row.get("bytes") is None:
            continue
        samples.append(
            (
                _parse_timestamp(row.get("at"), now),
                max(0, int(row.get("bytes", 0) or 0)),
            )
        )
    samples.sort(key=lambda item: item[0])
    samples = [item for item in samples if item[0] <= now]
    if not samples:
        return None
    cutoff = now - dt.timedelta(hours=24)
    before_cutoff = [item for item in samples if item[0] <= cutoff]
    baseline = before_cutoff[-1] if before_cutoff else samples[0]
    window = [item for item in samples if item[0] >= baseline[0]]
    current_bytes = samples[-1][1]
    baseline_bytes = baseline[1]
    net_growth = max(0, current_bytes - baseline_bytes)
    peak_growth = max(0, max(item[1] for item in window) - baseline_bytes)
    return net_growth, peak_growth


def _rolling_db_growth_bytes(state: dict, now: dt.datetime) -> int:
    footprint_growth = _rolling_db_footprint_growth(state, now)
    if footprint_growth is not None:
        return footprint_growth[1]
    cutoff = now - dt.timedelta(hours=24)
    return sum(
        max(0, int((row or {}).get("bytes", 0) or 0))
        for row in ((state.get("operational_history") or {}).get("db_growth") or [])
        if _parse_timestamp((row or {}).get("at"), cutoff) >= cutoff
    )


def _attach_operational_rollups(state: dict, metrics: dict, now: dt.datetime) -> None:
    history = state.get("operational_history") or {}
    memory_values = list(history.get("peak_rss_mb") or [])
    metrics["rolling_runtime_p95_seconds"] = _p95(list(history.get("runtime_seconds") or []))
    metrics["rolling_memory_p95_mb"] = _p95(memory_values)
    metrics["rolling_memory_max_mb"] = max(
        (float(value) for value in memory_values),
        default=None,
    )
    footprint_growth = _rolling_db_footprint_growth(state, now)
    if footprint_growth is None:
        fallback = _rolling_db_growth_bytes(state, now)
        metrics["rolling_db_net_growth_bytes_24h"] = fallback
        metrics["rolling_db_peak_growth_bytes_24h"] = fallback
    else:
        net_growth, peak_growth = footprint_growth
        metrics["rolling_db_net_growth_bytes_24h"] = net_growth
        metrics["rolling_db_peak_growth_bytes_24h"] = peak_growth
    metrics["rolling_db_growth_bytes_24h"] = metrics[
        "rolling_db_peak_growth_bytes_24h"
    ]


def _ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator <= 0 else float(numerator) / float(denominator)


def _phase_healthy_running_seconds(state: dict) -> float:
    try:
        seconds = float(state.get("phase_healthy_running_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return seconds if math.isfinite(seconds) and seconds >= 0.0 else 0.0


def _account_phase_healthy_running_time(
    state: dict,
    now: dt.datetime,
    *,
    cycle_started_running: bool,
    health_status: str,
) -> tuple[float, float]:
    """Advance the persisted phase clock and return raw/credited seconds.

    The scheduled interval is credited only after a normal healthy cycle
    proves that the campaign remained running.  Failed cycles, pause probes,
    resume cycles, and hard-halted periods permanently discard their interval.
    A single later cycle can never recover more than one 15-minute supervisor
    cadence, which prevents host downtime from becoming apparent healthy time.
    """

    checkpoint = _parse_timestamp(state.get("phase_clock_checkpoint_at"), now)
    raw_seconds = max(0.0, (now - checkpoint).total_seconds())
    credited_seconds = (
        min(raw_seconds, MAX_HEALTHY_CYCLE_CREDIT_SECONDS)
        if cycle_started_running and health_status == "healthy"
        else 0.0
    )
    state["phase_healthy_running_seconds"] = (
        _phase_healthy_running_seconds(state) + credited_seconds
    )
    state["phase_clock_checkpoint_at"] = _iso(now)
    return raw_seconds, credited_seconds


def _phase_gate_passed(
    state: dict,
    cfg: dict,
    metrics: dict,
    now: dt.datetime,
) -> tuple[bool, list[str], dict]:
    phase = str(state["phase"])
    phase_cfg = (cfg.get("phases") or {})[phase]
    accumulated = state.get("accumulated") or {}
    wall_elapsed_hours = max(
        0.0,
        (now - _parse_timestamp(state.get("phase_started_at"), now)).total_seconds() / 3600.0,
    )
    healthy_elapsed_hours = _phase_healthy_running_seconds(state) / 3600.0
    elapsed_hours = (
        healthy_elapsed_hours if phase in {"measurement", "canary"} else wall_elapsed_hours
    )
    reasons: list[str] = []
    evidence: dict[str, Any] = {
        "phase": phase,
        "evaluated_at": _iso(now),
        "elapsed_hours": round(elapsed_hours, 6),
        "healthy_elapsed_hours": round(healthy_elapsed_hours, 6),
        "wall_elapsed_hours": round(wall_elapsed_hours, 6),
        "thresholds": copy.deepcopy(phase_cfg),
        "actual": {},
    }
    if phase == "burn_in":
        healthy_cycles = int(state.get("phase_healthy_cycles", 0))
        runtime_p95 = _p95((state.get("operational_history") or {}).get("runtime_seconds") or [])
        memory_values = (state.get("operational_history") or {}).get("peak_rss_mb") or []
        memory_p95 = _p95(memory_values)
        memory_max = max((float(value) for value in memory_values), default=None)
        db_growth_day = _rolling_db_growth_bytes(state, now)
        evidence["actual"] = {
            "elapsed_hours": round(elapsed_hours, 6),
            "healthy_cycles": healthy_cycles,
            "runtime_p95_seconds": runtime_p95,
            "memory_p95_mb": memory_p95,
            "memory_max_mb": memory_max,
            "db_growth_bytes_24h": db_growth_day,
            "new_deferred_cost_lines": int(metrics.get("new_deferred_cost_lines", 0) or 0),
            "supervisor_count": metrics.get("supervisor_count"),
            "child_count": metrics.get("child_count"),
            "forbidden_worker_count": metrics.get("forbidden_worker_count"),
        }
        if healthy_cycles < int(phase_cfg.get("min_healthy_cycles", 90)):
            reasons.append("burn_in_healthy_cycles")
        if elapsed_hours < float(phase_cfg.get("min_elapsed_hours", 24)):
            reasons.append("burn_in_elapsed_hours")
        if runtime_p95 is None or runtime_p95 > float(phase_cfg.get("max_runtime_p95_seconds", 480)):
            reasons.append("burn_in_runtime_p95")
        if memory_p95 is None or memory_p95 > float(phase_cfg.get("max_memory_p95_mb", 750)):
            reasons.append("burn_in_memory_p95")
        if memory_max is None or memory_max > float(phase_cfg.get("max_memory_mb", 1024)):
            reasons.append("burn_in_memory_max")
        if db_growth_day > int(phase_cfg.get("max_db_growth_bytes_per_day", 262144000)):
            reasons.append("burn_in_database_growth_24h")
        if int(metrics.get("supervisor_count", 0) or 0) != 1 or int(
            metrics.get("child_count", 0) or 0
        ) > 1:
            reasons.append("burn_in_process_overlap")
        if int(metrics.get("forbidden_worker_count", 0) or 0) != 0:
            reasons.append("burn_in_forbidden_worker_overlap")
        if int(metrics.get("new_deferred_cost_lines", 0) or 0) != 0:
            reasons.append("burn_in_deferred_ledger_growth")
    elif phase == "measurement":
        closes = int(accumulated.get("direct_closes", 0) or 0)
        reliable_closes = int(accumulated.get("reliable_direct_closes", 0) or 0)
        timely_closes = int(accumulated.get("timely_direct_closes", 0) or 0)
        outcomes = int(accumulated.get("horizon_outcomes", 0) or 0)
        timely_outcomes = int(accumulated.get("timely_horizon_outcomes", 0) or 0)
        exact_keys = int(accumulated.get("exact_attributed_admission_keys", 0) or 0)
        lineage_rates = {
            prefix: _ratio(
                int(accumulated.get(f"{prefix}_lineage_complete", 0) or 0),
                int(accumulated.get(f"{prefix}_lineage_records", 0) or 0),
            )
            for prefix in ("opportunity", "order", "trade")
        }
        synthetic_proxy = int(accumulated.get("synthetic_proxy_primary", 0) or 0)
        evidence["actual"] = {
            "healthy_elapsed_hours": round(healthy_elapsed_hours, 6),
            "wall_elapsed_hours": round(wall_elapsed_hours, 6),
            "exact_attributed_admission_keys": exact_keys,
            "reliable_direct_closes": reliable_closes,
            "due_direct_close_denominator": closes,
            "timely_close_rate": _ratio(timely_closes, closes),
            "due_horizon_denominator": outcomes,
            "timely_horizon_rate": _ratio(timely_outcomes, outcomes),
            "lineage_rates": lineage_rates,
            "synthetic_proxy_primary": synthetic_proxy,
        }
        if elapsed_hours < float(phase_cfg.get("min_elapsed_hours", 168)):
            reasons.append("measurement_elapsed_hours")
        if exact_keys < int(phase_cfg.get("min_exact_attributed_admission_keys", 100)):
            reasons.append("measurement_exact_attributed_admission_keys")
        if reliable_closes < int(phase_cfg.get("min_reliable_direct_closes", 250)):
            reasons.append("measurement_reliable_direct_closes")
        if _ratio(timely_closes, closes) < float(phase_cfg.get("min_timely_close_rate", 0.90)):
            reasons.append("measurement_timely_close_rate")
        if _ratio(timely_outcomes, outcomes) < float(
            phase_cfg.get("min_timely_horizon_rate", 0.90)
        ):
            reasons.append("measurement_timely_horizon_rate")
        required_lineage_rate = float(phase_cfg.get("required_lineage_rate", 1.0))
        for prefix, rate in lineage_rates.items():
            if rate < required_lineage_rate:
                reasons.append(f"measurement_{prefix}_lineage_rate")
        if synthetic_proxy > int(phase_cfg.get("max_synthetic_proxy_primary", 0)):
            reasons.append("measurement_synthetic_proxy_primary")
    elif phase == "canary":
        labels = int(accumulated.get("canary_reliable_direct_labels", 0) or 0)
        active_canaries = int(metrics.get("active_canary_count", 0) or 0)
        evidence["actual"] = {
            "healthy_elapsed_hours": round(healthy_elapsed_hours, 6),
            "wall_elapsed_hours": round(wall_elapsed_hours, 6),
            "reliable_direct_labels": labels,
            "active_canary_count": active_canaries,
        }
        if elapsed_hours < float(phase_cfg.get("min_elapsed_hours", 48)):
            reasons.append("canary_elapsed_hours")
        if labels < int(phase_cfg.get("min_reliable_direct_labels", 30)):
            reasons.append("canary_reliable_direct_labels")
        if active_canaries != int(phase_cfg.get("required_active_canaries", 1)):
            reasons.append("canary_single_active_canary")
    else:
        evidence["actual"] = {"terminal_radar_phase": True}
        reasons.append("research_is_terminal_radar_phase")
    evidence["passed"] = not reasons
    evidence["reasons"] = list(reasons)
    return not reasons, reasons, evidence


def _advance_phase(state: dict, now: dt.datetime) -> None:
    index = PHASES.index(str(state["phase"]))
    if index >= len(PHASES) - 1:
        return
    state["phase"] = PHASES[index + 1]
    state["phase_started_at"] = _iso(now)
    state["phase_healthy_running_seconds"] = 0.0
    state["phase_clock_checkpoint_at"] = _iso(now)
    state["phase_cycle_count"] = 0
    state["phase_healthy_cycles"] = 0
    state["accumulated"] = {
        "direct_closes": 0,
        "reliable_direct_closes": 0,
        "timely_direct_closes": 0,
        "horizon_outcomes": 0,
        "timely_horizon_outcomes": 0,
        "exact_attributed_admission_keys": 0,
        "opportunity_lineage_records": 0,
        "opportunity_lineage_complete": 0,
        "order_lineage_records": 0,
        "order_lineage_complete": 0,
        "trade_lineage_records": 0,
        "trade_lineage_complete": 0,
        "synthetic_proxy_primary": 0,
        "canary_reliable_direct_labels": 0,
    }


def _persist_state(conn: sqlite3.Connection, state: dict) -> None:
    conn.execute(
        """
        update paper_expansion_campaign_state
        set phase=?,run_status=?,healthy_streak=?,phase_cycle_count=?,
            total_cycle_count=?,phase_started_at=?,updated_at=?,state_json=?
        where campaign_id=?
        """,
        (
            state["phase"],
            state["run_status"],
            int(state.get("healthy_streak", 0)),
            int(state.get("phase_cycle_count", 0)),
            int(state.get("total_cycle_count", 0)),
            state["phase_started_at"],
            state["updated_at"],
            json.dumps(state, sort_keys=True, default=str),
            state["campaign_id"],
        ),
    )


def _sqlite_logical_footprint_bytes(conn: sqlite3.Connection) -> int:
    """Return allocated SQLite pages, including uncommitted pages in this transaction."""

    page_count = int(conn.execute("pragma page_count").fetchone()[0] or 0)
    page_size = int(conn.execute("pragma page_size").fetchone()[0] or 0)
    return max(0, page_count) * max(0, page_size)


def _evaluate_campaign_cycle(
    base_current: dict,
    cycle_state: dict,
    metrics: dict,
    cfg: dict,
    now: dt.datetime,
    config_hash: str,
) -> dict:
    """Evaluate one cycle from an immutable base so final DB size can be replayed."""

    current = copy.deepcopy(base_current)
    merged = copy.deepcopy(metrics)
    _append_operational_history(current, merged, now)
    _attach_operational_rollups(current, merged, now)
    health_cfg = cfg.get("health") or {}
    phase_before = str(current["phase"])
    hard_reasons, soft_reasons = _health_reasons(merged, health_cfg, phase_before)
    prior_status = str(current["run_status"])
    resumed = False
    if prior_status == "hard_halted":
        health_status = "hard_halted"
        hard_reasons = sorted(
            set(
                hard_reasons
                + [str(current.get("hard_halt_reason") or "prior_hard_halt")]
            )
        )
    elif hard_reasons:
        health_status = "hard_halted"
        current["run_status"] = "hard_halted"
        current["healthy_streak"] = 0
        current["hard_halt_reason"] = hard_reasons[0]
        current["stop_reason"] = ";".join(hard_reasons)
    elif soft_reasons:
        health_status = "soft_paused"
        current["run_status"] = "soft_paused"
        current["healthy_streak"] = 0
        current["stop_reason"] = ";".join(soft_reasons)
    elif prior_status == "soft_paused":
        current["healthy_streak"] = int(current.get("healthy_streak", 0)) + 1
        resume_after = max(1, int(cfg.get("resume_healthy_cycles", 3)))
        if current["healthy_streak"] >= resume_after:
            current["run_status"] = "running"
            current["healthy_streak"] = 0
            health_status = "healthy_resumed"
            resumed = True
            current["stop_reason"] = None
        else:
            health_status = "healthy_resume_probe"
    else:
        health_status = "healthy"
        current["run_status"] = "running"
        current["healthy_streak"] = 0
        current["stop_reason"] = None

    current["total_cycle_count"] = int(current.get("total_cycle_count", 0)) + 1
    current["phase_cycle_count"] = int(current.get("phase_cycle_count", 0)) + 1
    _accumulate(current, merged)
    healthy_cycle = not hard_reasons and not soft_reasons
    normal_running_healthy_cycle = (
        health_status == "healthy"
        and prior_status == "running"
        and str(cycle_state.get("run_status") or "") == "running"
    )
    if normal_running_healthy_cycle:
        current["phase_healthy_cycles"] = int(current.get("phase_healthy_cycles", 0)) + 1
        current["last_good_phase"] = phase_before
    raw_clock_seconds, credited_clock_seconds = _account_phase_healthy_running_time(
        current,
        now,
        cycle_started_running=(
            prior_status == "running"
            and str(cycle_state.get("run_status") or "") == "running"
        ),
        health_status=health_status,
    )
    merged["phase_clock_interval_seconds"] = round(raw_clock_seconds, 6)
    merged["phase_healthy_seconds_credited"] = round(credited_clock_seconds, 6)
    merged["phase_healthy_running_seconds"] = round(
        _phase_healthy_running_seconds(current), 6
    )
    gate_passed, gate_reasons, gate_evidence = _phase_gate_passed(
        current,
        cfg,
        merged,
        now,
    )
    gate_evidence["health_blocked"] = bool(hard_reasons or soft_reasons)
    gate_evidence["run_status_before"] = prior_status
    current["gate_evidence"] = gate_evidence
    transitioned = False
    if healthy_cycle and current["run_status"] == "running" and not resumed and gate_passed:
        _advance_phase(current, now)
        transitioned = True
        current["gate_evidence"]["transitioned_to"] = current["phase"]
    current["updated_at"] = _iso(now)
    if str(current.get("hard_halt_reason") or "") != "config_hash_changed":
        current["config_hash"] = config_hash
    current["last_health_status"] = health_status
    current["last_reasons"] = hard_reasons + soft_reasons
    pause_gate_revalidation = copy.deepcopy(
        merged.get("pause_gate_revalidation") or {}
    )
    recovery_gate = current.get("recovery_pause_gate")
    if isinstance(recovery_gate, dict) and recovery_gate:
        recovery_gate = copy.deepcopy(recovery_gate)
        recovery_gate["last_revalidation"] = pause_gate_revalidation
        current["recovery_pause_gate"] = recovery_gate
        current["last_pause_gate_revalidation"] = pause_gate_revalidation
        if resumed:
            current["last_resolved_pause_gate"] = {
                **recovery_gate,
                "resolved_at": _iso(now),
            }
            current.pop("recovery_pause_gate", None)
    intercycle_check = copy.deepcopy(cycle_state.get("intercycle_safety_check") or {})
    completion_snapshot = copy.deepcopy(merged.get("safety_snapshot_at_completion") or {})
    last_paid_lease = current.get("last_paid_research_lease")
    last_paid_lease_id = (
        str(last_paid_lease.get("lease_id") or "")
        if isinstance(last_paid_lease, dict)
        else ""
    )
    current["last_intercycle_safety_check"] = intercycle_check
    current["last_completed_safety_watermark"] = {
        "version": SAFETY_WATERMARK_VERSION,
        "cycle_id": str(cycle_state.get("cycle_id") or ""),
        "cycle_phase": phase_before,
        "phase_after": str(current.get("phase") or phase_before),
        "captured_at": _iso(now),
        "config_hash": config_hash,
        "safety_snapshot": completion_snapshot,
        "last_paid_research_lease_id": last_paid_lease_id or None,
        "last_paid_research_lease_digest": _json_digest(last_paid_lease),
        "authorized_paid_research": copy.deepcopy(
            intercycle_check.get("authorized_paid_research")
        ),
    }
    current.pop("inflight_cycle", None)
    return {
        "current": current,
        "metrics": merged,
        "phase_before": phase_before,
        "health_status": health_status,
        "hard_reasons": hard_reasons,
        "soft_reasons": soft_reasons,
        "gate_reasons": gate_reasons,
        "gate_evidence": gate_evidence,
        "transitioned": transitioned,
    }


def _write_campaign_cycle_evaluation(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    campaign_id: str,
    cycle_state: dict,
    evaluated: dict,
    now: dt.datetime,
) -> None:
    current = evaluated["current"]
    merged = evaluated["metrics"]
    reasons = (
        evaluated["hard_reasons"]
        + evaluated["soft_reasons"]
        + evaluated["gate_reasons"]
    )
    _persist_state(conn, current)
    conn.execute(
        """
        insert into paper_expansion_campaign_cycles (
            cycle_id,campaign_id,phase,started_at,completed_at,health_status,
            metrics_json,reasons_json
        ) values (?,?,?,?,?,?,?,?)
        on conflict(cycle_id) do update set
            campaign_id=excluded.campaign_id,
            phase=excluded.phase,
            started_at=excluded.started_at,
            completed_at=excluded.completed_at,
            health_status=excluded.health_status,
            metrics_json=excluded.metrics_json,
            reasons_json=excluded.reasons_json
        """,
        (
            cycle_id,
            campaign_id,
            evaluated["phase_before"],
            str(cycle_state.get("cycle_started_at") or _iso(now)),
            _iso(now),
            evaluated["health_status"],
            json.dumps(merged, sort_keys=True, default=str),
            json.dumps(reasons, sort_keys=True),
        ),
    )


def record_campaign_cycle(conn: sqlite3.Connection, state: dict, metrics: dict) -> dict:
    """Record one cycle idempotently and apply automatic pause/phase gates."""

    if not state.get("enabled", False):
        return {"enabled": False, "status": "campaign_disabled"}
    _require_schema(conn)
    cycle_id = str(state.get("cycle_id") or "")
    if not cycle_id:
        raise CampaignError("campaign_cycle_id_missing")
    now = _utc_now()
    campaign_id = str(state["campaign_id"])
    cfg = state.get("campaign_config") or metrics.get("campaign_config") or {}
    if not cfg:
        raise CampaignError("campaign_config_missing_from_cycle_record")
    config_hash = str(state.get("config_hash") or "")
    if len(config_hash) != 64 or any(
        character not in "0123456789abcdef" for character in config_hash.lower()
    ):
        raise CampaignError("campaign_config_hash_missing_from_cycle_record")
    evaluated: dict | None = None
    try:
        # A radar cycle normally already owns a write transaction containing
        # all of its artifacts.  Direct callers still need serialization with
        # another finalizer before the idempotency check.
        if not conn.in_transaction:
            conn.execute("begin immediate")
        prior_cycle = conn.execute(
            "select metrics_json,reasons_json,health_status,phase from paper_expansion_campaign_cycles where cycle_id=?",
            (cycle_id,),
        ).fetchone()
        if prior_cycle is not None:
            current = _load_or_create_state(conn, campaign_id, now, config_hash)
            if str((current.get("inflight_cycle") or {}).get("cycle_id") or "") == cycle_id:
                current.pop("inflight_cycle", None)
                current["updated_at"] = _iso(now)
                _persist_state(conn, current)
            conn.commit()
            return {
                "enabled": True,
                "status": "already_recorded",
                "cycle_id": cycle_id,
                "phase": prior_cycle["phase"],
                "health_status": prior_cycle["health_status"],
                "metrics": json.loads(prior_cycle["metrics_json"]),
                "reasons": json.loads(prior_cycle["reasons_json"]),
            }

        base_current = _load_or_create_state(conn, campaign_id, now, config_hash)
        active_cycle_id = str(
            (base_current.get("inflight_cycle") or {}).get("cycle_id") or ""
        )
        if active_cycle_id != cycle_id:
            raise CampaignError(
                f"campaign_inflight_cycle_mismatch:{active_cycle_id or 'missing'}"
            )
        merged = _merged_metrics(conn, state, metrics, cfg, now)
        # No gate may pass on the caller's pre-final footprint.  First stage a
        # fail-closed evaluation, then replay from the immutable pre-cycle state
        # with the state/audit-row writes included in the logical page count.
        merged["db_finalization_accounted"] = False
        merged["db_footprint_measurement"] = "sqlite_page_count_x_page_size"
        max_finalization_passes = 4
        for pass_number in range(1, max_finalization_passes + 1):
            evaluated = _evaluate_campaign_cycle(
                base_current,
                state,
                merged,
                cfg,
                now,
                config_hash,
            )
            _write_campaign_cycle_evaluation(
                conn,
                cycle_id=cycle_id,
                campaign_id=campaign_id,
                cycle_state=state,
                evaluated=evaluated,
                now=now,
            )
            observed_footprint = _sqlite_logical_footprint_bytes(conn)
            reported_footprint = merged.get("db_footprint_bytes")
            footprint_is_final = (
                bool(merged.get("db_finalization_accounted"))
                and reported_footprint is not None
                and int(reported_footprint) == observed_footprint
            )
            if footprint_is_final:
                break
            merged["db_finalization_accounted"] = True
            merged["db_footprint_bytes"] = observed_footprint
            if merged.get("db_footprint_start_bytes") is not None:
                merged["db_growth_bytes"] = max(
                    0,
                    observed_footprint
                    - int(merged.get("db_footprint_start_bytes", 0) or 0),
                )
            merged["db_finalization_passes"] = pass_number
        else:
            raise CampaignError("campaign_db_footprint_finalization_unstable")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise

    if evaluated is None:  # Defensive: the bounded loop above always evaluates once.
        raise CampaignError("campaign_cycle_evaluation_missing")
    current = evaluated["current"]
    merged = evaluated["metrics"]
    phase_before = evaluated["phase_before"]
    health_status = evaluated["health_status"]
    hard_reasons = evaluated["hard_reasons"]
    soft_reasons = evaluated["soft_reasons"]
    gate_reasons = evaluated["gate_reasons"]
    gate_evidence = evaluated["gate_evidence"]
    transitioned = evaluated["transitioned"]
    return {
        "enabled": True,
        "status": current["run_status"],
        "cycle_id": cycle_id,
        "health_status": health_status,
        "phase_before": phase_before,
        "phase_after": current["phase"],
        "transitioned": transitioned,
        "research_ready": current["phase"] == "research" and current["run_status"] == "running",
        "hard_reasons": hard_reasons,
        "soft_reasons": soft_reasons,
        "gate_reasons": gate_reasons,
        "gate_evidence": gate_evidence,
        "metrics": merged,
        "state": current,
    }


def record_inflight_failure(
    conn: sqlite3.Connection,
    settings: dict,
    *,
    metrics: dict | None = None,
) -> dict:
    """Finalize the currently persisted cycle after a supervisor-observed failure."""

    cfg = _campaign_config(settings)
    if not cfg:
        return {"enabled": False, "status": "campaign_disabled"}
    campaign_id = str(cfg.get("campaign_id") or "bounded_crypto_paper_v1")
    now = _utc_now()
    current = _load_or_create_state(conn, campaign_id, now, _config_hash(settings))
    token = copy.deepcopy(current.get("inflight_cycle") or {})
    if not token:
        return {"enabled": True, "status": "no_inflight_cycle", "campaign_id": campaign_id}
    failure_metrics = copy.deepcopy(metrics or {})
    failure_metrics.setdefault("cycle_success", False)
    failure_metrics.setdefault("exit_code", 125)
    failure_metrics.setdefault(
        "runtime_seconds",
        max(
            0.0,
            (
                now
                - _parse_timestamp(token.get("cycle_started_at"), now)
            ).total_seconds(),
        ),
    )
    return record_campaign_cycle(conn, token, failure_metrics)


def reset_hard_halt(
    conn: sqlite3.Connection,
    settings: dict,
    *,
    campaign_id: str,
    operator_reason: str,
    clear_stale_runtime_leases: bool = False,
) -> dict:
    """Explicit operator-only reset; hard halts never resume automatically."""

    if not operator_reason.strip():
        raise CampaignError("operator_reason_required")
    cfg = _campaign_config(settings)
    if not cfg or str(cfg.get("campaign_id")) != str(campaign_id):
        raise CampaignError("campaign_id_mismatch")
    now = _utc_now()
    expected_config_hash = _config_hash(settings)
    state = _load_or_create_state(conn, campaign_id, now, expected_config_hash)
    if state["run_status"] != "hard_halted":
        return {"status": "not_hard_halted", "state": state}
    stale_leases: dict[str, dict] = {}
    active_leases: list[str] = []
    inflight = state.get("inflight_cycle")
    if isinstance(inflight, dict) and inflight:
        started = _parse_timestamp(inflight.get("cycle_started_at"), now)
        timeout_seconds = float(
            (((inflight.get("campaign_config") or {}).get("health") or {}).get(
                "runtime_halt_seconds", 720
            ))
            or 720
        )
        if (now - started).total_seconds() < timeout_seconds:
            active_leases.append("inflight_cycle")
        else:
            stale_leases["inflight_cycle"] = copy.deepcopy(inflight)
    paid_lease = state.get("paid_research_inflight")
    if isinstance(paid_lease, dict) and paid_lease:
        expires = _parse_timestamp(paid_lease.get("lease_expires_at"), now)
        if expires >= now:
            active_leases.append("paid_research_inflight")
        else:
            stale_leases["paid_research_inflight"] = copy.deepcopy(paid_lease)
    if active_leases:
        raise CampaignError("active_runtime_lease_blocks_reset:" + ",".join(active_leases))
    if stale_leases and not clear_stale_runtime_leases:
        raise CampaignError(
            "stale_runtime_lease_requires_explicit_clear:" + ",".join(stale_leases)
        )
    if stale_leases:
        archive = list(state.get("cleared_runtime_leases") or [])
        archive.append(
            {
                "cleared_at": _iso(now),
                "operator_reason": operator_reason.strip(),
                "leases": stale_leases,
            }
        )
        state["cleared_runtime_leases"] = archive[-20:]
        for key in stale_leases:
            state.pop(key, None)
    previous_config_hash = state.get("config_hash")
    state["run_status"] = "soft_paused"
    state["healthy_streak"] = 0
    state["hard_halt_reason"] = None
    state["stop_reason"] = f"operator_reset:{operator_reason.strip()}"
    state["operator_reset_reason"] = operator_reason.strip()
    state["config_hash"] = expected_config_hash
    adoption = {
        "adopted_at": _iso(now),
        "previous_config_hash": previous_config_hash,
        "adopted_config_hash": expected_config_hash,
        "operator_reason": operator_reason.strip(),
    }
    state["last_config_hash_adoption"] = adoption
    state["phase_clock_checkpoint_at"] = _iso(now)
    history = list(state.get("config_hash_adoptions") or [])
    history.append(adoption)
    state["config_hash_adoptions"] = history[-20:]
    state["updated_at"] = _iso(now)
    _persist_state(conn, state)
    conn.commit()
    return {"status": "reset_to_soft_pause", "state": state}
