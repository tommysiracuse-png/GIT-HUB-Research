"""Run at most one explicitly authorized paid crypto-research call per UTC day."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import sqlite3
import uuid
from itertools import product

from autonomous_cost_guard import autonomous_paid_scope
from cost_router import (
    COST_LOG_DEFERRED_PATH,
    complete,
    completion_preflight_status,
    cost_budget_status,
    deferred_cost_reconciliation_status,
)
from settings import SettingsError, config_fingerprint, load_settings
from storage import (
    RUNS_DIR,
    bounded_paper_trade_lineage_valid,
    connect,
    reliable_paper_label_eligibility_for_trade_row,
)
from strategy_lab import (
    RECOVERY_CANARY_STRATEGY_LAB_ID,
    SUPPORTED_LOGIC_TYPES,
    _strategy_lab_lineage_root_map,
    _validate_contract,
    ingest_strategy_lab_recommendation,
)


MAX_NEW_ROOTS_PER_UTC_DAY = 2
MAX_ACTIVE_ROOTS = 6
PAID_RESEARCH_LEASE_SECONDS = 300
RADAR_CADENCE_SECONDS = 900
MAX_HEALTHY_RADAR_CYCLE_AGE_SECONDS = RADAR_CADENCE_SECONDS * 2
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
CUSTOM_CRYPTO_VENUE_REGISTRY = PROJECT_ROOT / "config" / "frontier_crypto_venues.json"
DEFAULT_CRYPTO_VENUE_REGISTRY = PROJECT_ROOT / "config" / "frontier_crypto_venues.example.json"
CORE_DIRECT_CRYPTO_VENUES = frozenset({"OKX", "OKX_SPOT", "OKX_PERP"})
DIRECT_CRYPTO_SURFACES = {"frontier_crypto_venue_map", "perp_funding_basis"}
DIRECT_CRYPTO_DIRECTIONS = {
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
TERMINAL_ROOT_STATUSES = {
    "promoted_to_code",
    "retired_bad_evidence",
    "retired_no_activity",
    "rejected_invalid",
    "quarantined_surface_policy",
}


def _campaign_hash(settings: dict) -> str:
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _claim_paid_research_lease(
    conn: sqlite3.Connection,
    settings: dict,
    campaign_id: str,
) -> tuple[dict, dict]:
    """Atomically exclude radar and duplicate paid work at the campaign DB."""

    now = dt.datetime.now(dt.timezone.utc)
    started_at = now.isoformat()
    config_hash = _campaign_hash(settings)
    try:
        conn.execute("begin immediate")
        row = conn.execute(
            "select phase,run_status,state_json from paper_expansion_campaign_state where campaign_id=?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise SettingsError("campaign state is not initialized")
        try:
            state = json.loads(row["state_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise SettingsError("campaign state JSON is invalid") from exc
        if not isinstance(state, dict):
            raise SettingsError("campaign state JSON must be an object")
        if str(row["phase"]) != "research" or str(row["run_status"]) != "running":
            raise SettingsError("paid research is allowed only in the running research phase")
        if state.get("config_hash") != config_hash:
            raise SettingsError("campaign config hash does not match persisted state")
        if state.get("inflight_cycle"):
            raise SettingsError("paid research must run between bounded radar cycles")
        latest_cycle = conn.execute(
            """
            select completed_at,health_status
            from paper_expansion_campaign_cycles
            where campaign_id=?
            order by completed_at desc
            limit 1
            """,
            (campaign_id,),
        ).fetchone()
        if latest_cycle is None:
            raise SettingsError("paid research requires a recent healthy bounded radar cycle")
        try:
            completed_at = dt.datetime.fromisoformat(
                str(latest_cycle["completed_at"] or "").replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise SettingsError(
                "latest bounded radar cycle has no valid completion timestamp"
            ) from exc
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=dt.timezone.utc)
        completed_at = completed_at.astimezone(dt.timezone.utc)
        cycle_age_seconds = (now - completed_at).total_seconds()
        if str(latest_cycle["health_status"]) not in {"healthy", "healthy_resumed"}:
            raise SettingsError("latest bounded radar cycle is not healthy")
        if not 0.0 <= cycle_age_seconds <= MAX_HEALTHY_RADAR_CYCLE_AGE_SECONDS:
            raise SettingsError("latest healthy bounded radar cycle is stale")
        active_lease = state.get("paid_research_inflight") or {}
        if active_lease:
            try:
                lease_expires_at = dt.datetime.fromisoformat(
                    str(active_lease.get("lease_expires_at") or "").replace("Z", "+00:00")
                )
            except (TypeError, ValueError) as exc:
                raise SettingsError(
                    "existing paid research lease has no valid expiration; operator cleanup is required"
                ) from exc
            if lease_expires_at.tzinfo is None:
                lease_expires_at = lease_expires_at.replace(tzinfo=dt.timezone.utc)
            lease_expires_at = lease_expires_at.astimezone(dt.timezone.utc)
            if now < lease_expires_at:
                raise SettingsError("another paid research operation already owns the campaign lease")
            # An expired lease is not proof that the prior provider attempt was
            # harmless.  It may have stranded a reservation or an unknown
            # charged result.  Preserve it for the audited reset path and
            # latch the campaign instead of silently taking ownership.
            reason = "stale_paid_research_lease_requires_manual_reset"
            state.update(
                {
                    "run_status": "hard_halted",
                    "healthy_streak": 0,
                    "hard_halt_reason": reason,
                    "stop_reason": reason,
                    "stale_paid_research_lease_detected_at": started_at,
                    "updated_at": started_at,
                }
            )
            conn.execute(
                """
                update paper_expansion_campaign_state
                set run_status='hard_halted',healthy_streak=0,updated_at=?,state_json=?
                where campaign_id=?
                """,
                (started_at, json.dumps(state, sort_keys=True), campaign_id),
            )
            conn.commit()
            raise SettingsError(reason)
        lease = {
            "lease_id": str(uuid.uuid4()),
            "campaign_id": campaign_id,
            "started_at": started_at,
            "lease_expires_at": (
                now + dt.timedelta(seconds=PAID_RESEARCH_LEASE_SECONDS)
            ).isoformat(),
            "pid": os.getpid(),
            "config_hash": config_hash,
        }
        state["paid_research_inflight"] = lease
        state["updated_at"] = started_at
        conn.execute(
            """
            update paper_expansion_campaign_state
            set updated_at=?,state_json=?
            where campaign_id=?
            """,
            (started_at, json.dumps(state, sort_keys=True), campaign_id),
        )
        context = _research_context(conn, settings)
        conn.commit()
        return lease, context
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _release_paid_research_lease(
    conn: sqlite3.Connection,
    campaign_id: str,
    lease: dict,
    *,
    outcome: str,
    details: dict | None = None,
) -> None:
    completed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        conn.execute("begin immediate")
        row = conn.execute(
            "select state_json from paper_expansion_campaign_state where campaign_id=?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise SettingsError("campaign state disappeared while releasing paid research lease")
        try:
            state = json.loads(row["state_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise SettingsError("campaign state JSON is invalid while releasing paid research lease") from exc
        if not isinstance(state, dict):
            raise SettingsError("campaign state JSON must be an object")
        active = state.get("paid_research_inflight") or {}
        expected_lease_id = str(lease.get("lease_id") or "")
        if str(active.get("lease_id") or "") != expected_lease_id:
            raise SettingsError("paid research lease ownership changed before release")
        state.pop("paid_research_inflight", None)
        state["last_paid_research_lease"] = {
            **lease,
            "completed_at": completed_at,
            "outcome": str(outcome or "unknown"),
            **dict(details or {}),
        }
        state["updated_at"] = completed_at
        conn.execute(
            """
            update paper_expansion_campaign_state
            set updated_at=?,state_json=?
            where campaign_id=?
            """,
            (completed_at, json.dumps(state, sort_keys=True), campaign_id),
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _require_scoped_override() -> None:
    if str(os.environ.get("RADAR_PROCESS_ROLE") or "").strip().lower() != "research_one_shot":
        raise SettingsError("RADAR_PROCESS_ROLE must be research_one_shot")
    if str(os.environ.get("RADAR_RESEARCH_MODEL_OVERRIDE") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        raise SettingsError("explicit RADAR_RESEARCH_MODEL_OVERRIDE is required")
    if os.environ.get("RADAR_USE_LITELLM") != "1":
        raise SettingsError("RADAR_USE_LITELLM=1 is required only in this one-shot process")


def _require_deferred_cost_reconciliation(settings: dict) -> dict:
    """Block paid work until the append-only deferred ledger matches SQLite."""

    expansion = settings.get("paper_expansion") or {}
    source = pathlib.Path(
        str(expansion.get("deferred_cost_path") or COST_LOG_DEFERRED_PATH)
    ).expanduser().resolve()
    try:
        with connect(initialize=False) as conn:
            report = deferred_cost_reconciliation_status(conn, source)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise SettingsError(
            f"deferred_cost_reconciliation_blocked:unavailable:{type(exc).__name__}"
        ) from exc
    if not isinstance(report, dict):
        raise SettingsError(
            "deferred_cost_reconciliation_blocked:invalid_status_contract"
        )
    if report.get("complete") is not True:
        status = str(report.get("status") or "incomplete").strip() or "incomplete"
        count_values: list[str] = []
        for key in ("pending", "reserved", "invalid", "conflicting"):
            try:
                value = str(int(report.get(key, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                value = "unknown"
            count_values.append(f"{key}={value}")
        counts = ",".join(count_values)
        raise SettingsError(
            f"deferred_cost_reconciliation_blocked:{status}:{counts}"
        )
    return report


def _direct_crypto_venue_allowlist() -> set[str]:
    path = (
        CUSTOM_CRYPTO_VENUE_REGISTRY
        if CUSTOM_CRYPTO_VENUE_REGISTRY.exists()
        else DEFAULT_CRYPTO_VENUE_REGISTRY
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, TypeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"crypto venue registry unavailable or invalid: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("venues"), list):
        raise SettingsError("crypto venue registry must contain a venues list")
    allowed = set(CORE_DIRECT_CRYPTO_VENUES)
    for row in payload["venues"]:
        if not isinstance(row, dict) or row.get("enabled") is False:
            continue
        static_status = str(row.get("static_status") or "").strip().lower()
        parser = str(row.get("parser") or "").strip().lower()
        route_id = str(row.get("route_id") or "").strip().lower()
        if (
            static_status in {"watch_only", "reference", "reference_only"}
            or parser in {"watch_only", "watch_only_rail", "reference_only"}
            or route_id.endswith("_watch_only")
        ):
            continue
        venue = str(row.get("venue") or "").strip().upper()
        if venue:
            allowed.add(venue)
    if not allowed:
        raise SettingsError("direct crypto venue allowlist is empty")
    return allowed


def _research_context(conn: sqlite3.Connection, settings: dict) -> dict:
    rows = conn.execute(
        """
        select id,venue,trade_type,direction,signal_key,pnl_bps,
               candidate_json,review_json,context_json,
               close_measurement_status,admission_key,admission_episode_id
        from paper_trades
        where status='closed' and pnl_bps is not null
        order by closed_at desc,id desc
        limit 5000
        """
    ).fetchall()
    aggregates: dict[tuple[str, str, str, str], dict[str, float | int | str]] = {}
    qualified_venues = _direct_crypto_venue_allowlist()
    for row in rows:
        eligibility = reliable_paper_label_eligibility_for_trade_row(row)
        if not bool(eligibility.get("paper_label_eligible")):
            continue
        if not bounded_paper_trade_lineage_valid(conn, row, settings):
            continue
        if str(eligibility.get("paper_signal_stats_scope") or "").lower() != "direct":
            continue
        if str(eligibility.get("paper_label_route_status") or "").lower() != "standard":
            continue
        signal_key = str(row["signal_key"] or "").strip()
        if not signal_key:
            continue
        candidate = _stored_json_object(row["candidate_json"])
        venue = str(candidate.get("venue") or row["venue"] or "").strip().upper()
        trade_type = str(
            candidate.get("trade_type") or row["trade_type"] or ""
        ).strip()
        direction = str(
            candidate.get("direction") or row["direction"] or ""
        ).strip().lower()
        if (
            venue not in qualified_venues
            or trade_type not in DIRECT_CRYPTO_SURFACES
            or direction not in DIRECT_CRYPTO_DIRECTIONS
        ):
            continue
        pnl = float(row["pnl_bps"])
        aggregate_key = (signal_key, venue, trade_type, direction)
        aggregate = aggregates.setdefault(
            aggregate_key,
            {
                "signal_key": signal_key,
                "venue": venue,
                "trade_type": trade_type,
                "direction": direction,
                "closed_count": 0,
                "wins": 0,
                "total_pnl_bps": 0.0,
            },
        )
        aggregate["closed_count"] = int(aggregate["closed_count"]) + 1
        aggregate["wins"] = int(aggregate["wins"]) + int(pnl > 0)
        aggregate["total_pnl_bps"] = float(aggregate["total_pnl_bps"]) + pnl
    direct_stats: list[dict] = []
    for aggregate in aggregates.values():
        closed_count = int(aggregate["closed_count"])
        total_pnl = float(aggregate["total_pnl_bps"])
        wins = int(aggregate["wins"])
        direct_stats.append(
            {
                "signal_key": aggregate["signal_key"],
                "venue": aggregate["venue"],
                "trade_type": aggregate["trade_type"],
                "direction": aggregate["direction"],
                "closed_count": closed_count,
                "wins": wins,
                "avg_pnl_bps": round(total_pnl / closed_count, 6),
                "win_rate": round(wins / closed_count, 6),
                "evidence_scope": "reliable_timely_direct_standard_route_only",
            }
        )
    direct_stats.sort(
        key=lambda row: (int(row["closed_count"]), abs(float(row["avg_pnl_bps"]))),
        reverse=True,
    )
    evidence_contracts = [
        {
            "venue": str(row["venue"]),
            "trade_type": str(row["trade_type"]),
            "direction": str(row["direction"]),
            "reliable_label_count": int(row["closed_count"]),
        }
        for row in direct_stats
    ]
    return {
        "signal_stats": direct_stats[:60],
        "evidence_contracts": evidence_contracts[:60],
        "constraints": {
            "asset_scope": "crypto_only",
            "mode": "paper_only",
            "max_active_discovery_roots": 6,
            "max_new_discovery_roots_per_utc_day": 2,
            "live_trading": False,
            "output_contract": "structured_strategy_lab_contracts",
            "allowed_direct_crypto_venues": sorted(
                {str(row["venue"]) for row in evidence_contracts}
            ),
            "evidence_scope": "reliable_timely_direct_standard_route_only",
        },
    }


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _research_evidence_contracts(context: object) -> set[tuple[str, str, str]]:
    if not isinstance(context, dict):
        return set()
    rows = context.get("evidence_contracts")
    if not isinstance(rows, list):
        return set()
    contracts: set[tuple[str, str, str]] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        venue = str(raw.get("venue") or "").strip().upper()
        trade_type = str(raw.get("trade_type") or "").strip()
        direction = str(raw.get("direction") or "").strip().lower()
        try:
            reliable_count = int(raw.get("reliable_label_count") or 0)
        except (TypeError, ValueError):
            reliable_count = 0
        if venue and trade_type and direction and reliable_count > 0:
            contracts.add((venue, trade_type, direction))
    return contracts


def _stored_json_object(value: object) -> dict:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _stored_json_list(value: object) -> list:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _persisted_paid_root_is_bounded(row: sqlite3.Row) -> bool:
    if row["parent_strategy_lab_id"] is not None:
        return False
    if str(row["source_agent"] or "") != "paid_research_one_shot":
        return False
    if str(row["experiment_type"] or "") != "market_strategy":
        return False
    logic = _stored_json_object(row["strategy_logic_json"])
    if str(logic.get("type") or "") not in SUPPORTED_LOGIC_TYPES:
        return False
    trade_types = set(_string_values(logic.get("trade_types")))
    asset_classes = {value.lower() for value in _string_values(logic.get("asset_classes"))}
    directions = {value.lower() for value in _string_values(logic.get("directions"))}
    venues = {value.upper() for value in _string_values(logic.get("venues"))}
    if not trade_types or not trade_types.issubset(DIRECT_CRYPTO_SURFACES):
        return False
    if not asset_classes or not asset_classes.issubset({"crypto", "crypto_spot"}):
        return False
    if not directions or not directions.issubset(DIRECT_CRYPTO_DIRECTIONS):
        return False
    if not venues or not venues.issubset(_direct_crypto_venue_allowlist()):
        return False
    if bool(logic.get("allow_any_surface")):
        return False
    if str(row["source_surface"] or "") not in DIRECT_CRYPTO_SURFACES:
        return False
    targets = set(_string_values(_stored_json_list(row["permitted_target_surfaces_json"])))
    if not targets or not targets.issubset(DIRECT_CRYPTO_SURFACES):
        return False
    requirements = _stored_json_object(row["data_requirements_json"])
    promotion_rules = _stored_json_object(row["promotion_rules_json"])
    return not promotion_rules and requirements.get("paper_only") is True and str(
        requirements.get("route_status") or ""
    ).strip().lower() == "standard"


def _active_strategy_root_ids(conn: sqlite3.Connection) -> set[str]:
    roots = _strategy_lab_lineage_root_map(conn)
    rows = conn.execute(
        """
        select strategy_lab_id,parent_strategy_lab_id,status,source_agent,
               experiment_type,strategy_logic_json,data_requirements_json,
               promotion_rules_json,source_surface,permitted_target_surfaces_json
        from strategy_lab_experiments
        """
    ).fetchall()
    bounded_paid_roots = {
        str(row["strategy_lab_id"])
        for row in rows
        if _persisted_paid_root_is_bounded(row)
    }
    return {
        roots.get(str(row["strategy_lab_id"]), str(row["strategy_lab_id"]))
        for row in rows
        if str(row["status"] or "") not in TERMINAL_ROOT_STATUSES
        and (
            roots.get(str(row["strategy_lab_id"]), str(row["strategy_lab_id"]))
            == RECOVERY_CANARY_STRATEGY_LAB_ID
            or roots.get(str(row["strategy_lab_id"]), str(row["strategy_lab_id"]))
            in bounded_paid_roots
        )
    }


def _research_phase_settings(settings: dict) -> dict:
    effective = copy.deepcopy(settings)
    phase = ((effective.get("paper_expansion") or {}).get("phases") or {}).get(
        "research"
    ) or {}
    lab = effective.setdefault("strategy_lab", {})
    lab.update(
        {
            "enabled": bool(phase.get("strategy_lab_enabled", False)),
            "lifecycle_mutations_enabled": bool(
                phase.get("lifecycle_mutations_enabled", False)
            ),
            "recommendation_emission_enabled": False,
            "promotion_enabled": False,
            "adaptive_relaxation_enabled": False,
            "region_splits_enabled": False,
            "max_active_strategy_roots": min(
                MAX_ACTIVE_ROOTS,
                max(0, int(phase.get("max_active_strategy_roots", 0) or 0)),
            ),
        }
    )
    return effective


def _parse_contract_output(text: str) -> tuple[list[dict], list[dict]]:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return [], [{"reason": "output_not_json_object"}]
    if not isinstance(payload, dict):
        return [], [{"reason": "output_not_json_object"}]
    raw_contracts = payload.get("strategy_contracts")
    if not isinstance(raw_contracts, list):
        return [], [{"reason": "strategy_contracts_list_required"}]
    contracts: list[dict] = []
    rejected: list[dict] = []
    for index, value in enumerate(raw_contracts):
        if index >= MAX_NEW_ROOTS_PER_UTC_DAY:
            rejected.append({"index": index, "reason": "daily_new_root_output_cap"})
        elif not isinstance(value, dict):
            rejected.append({"index": index, "reason": "contract_must_be_object"})
        else:
            contracts.append(dict(value))
    return contracts, rejected


def _validate_paid_contract(
    raw: dict,
    *,
    evidence_contracts: set[tuple[str, str, str]] | None = None,
) -> tuple[dict | None, str | None]:
    payload = {
        "title": str(raw.get("hypothesis") or raw.get("strategy_lab_id") or "").strip(),
        "rationale": str(raw.get("hypothesis") or "").strip(),
        "agent_name": "paid_research_one_shot",
        "strategy_lab_experiment": raw,
    }
    contract, error = _validate_contract(payload)
    if contract is None:
        return None, error or "invalid_contract"
    if contract.get("status") != "proposed":
        return None, f"contract_status:{contract.get('status')}"
    if contract.get("experiment_type") != "market_strategy":
        return None, "market_strategy_contract_required"
    if contract.get("parent_strategy_lab_id"):
        return None, "new_root_must_not_have_parent"
    logic = contract.get("strategy_logic") or {}
    if str(logic.get("type") or "") not in SUPPORTED_LOGIC_TYPES:
        return None, "unsupported_strategy_logic"
    trade_types = set(_string_values(logic.get("trade_types")))
    if not trade_types or not trade_types.issubset(DIRECT_CRYPTO_SURFACES):
        return None, "direct_crypto_trade_type_required"
    asset_classes = {value.lower() for value in _string_values(logic.get("asset_classes"))}
    if not asset_classes or not asset_classes.issubset({"crypto", "crypto_spot"}):
        return None, "crypto_asset_class_required"
    directions = {value.lower() for value in _string_values(logic.get("directions"))}
    if not directions or not directions.issubset(DIRECT_CRYPTO_DIRECTIONS):
        return None, "direct_crypto_direction_required"
    venues = {value.upper() for value in _string_values(logic.get("venues"))}
    if not venues:
        return None, "explicit_crypto_venue_required"
    if not venues.issubset(_direct_crypto_venue_allowlist()):
        return None, "unsupported_direct_crypto_venue"
    if evidence_contracts is not None:
        requested_contracts = {
            (venue, trade_type, direction)
            for venue, trade_type, direction in product(
                venues, trade_types, directions
            )
        }
        if not requested_contracts or not requested_contracts.issubset(
            evidence_contracts
        ):
            return None, "contract_not_supported_by_research_evidence"
    if bool(logic.get("allow_any_surface")):
        return None, "unbounded_surface_forbidden"
    source_surface = str(contract.get("source_surface") or "").strip()
    target_surfaces = set(_string_values(contract.get("permitted_target_surface")))
    if source_surface not in DIRECT_CRYPTO_SURFACES:
        return None, "direct_crypto_source_surface_required"
    if not target_surfaces or not target_surfaces.issubset(DIRECT_CRYPTO_SURFACES):
        return None, "direct_crypto_target_surface_required"
    data_requirements = contract.get("data_requirements") or {}
    if data_requirements.get("paper_only") is not True:
        return None, "paper_only_contract_required"
    if str(data_requirements.get("route_status") or "").strip().lower() != "standard":
        return None, "standard_direct_route_required"
    contract["parent_strategy_lab_id"] = None
    contract["promotion_rules"] = {}
    return contract, None


def _ingest_contracts(
    conn: sqlite3.Connection,
    settings: dict,
    raw_contracts: list[dict],
    *,
    day_utc: str,
    research_context: dict | None = None,
) -> dict:
    active_before = _active_strategy_root_ids(conn)
    already_created_today = int(
        conn.execute(
            """
            select count(*) from strategy_lab_experiments
            where parent_strategy_lab_id is null
              and source_agent='paid_research_one_shot'
              and date(created_at)=?
            """,
            (day_utc,),
        ).fetchone()[0]
    )
    daily_slots = max(0, MAX_NEW_ROOTS_PER_UTC_DAY - already_created_today)
    active_slots = max(0, MAX_ACTIVE_ROOTS - len(active_before))
    slots = min(daily_slots, active_slots)
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen_ids: set[str] = set()
    research_settings = _research_phase_settings(settings)
    if research_context is None:
        research_context = _research_context(conn, settings)
    evidence_contracts = _research_evidence_contracts(research_context)
    for index, raw in enumerate(raw_contracts):
        contract, error = _validate_paid_contract(
            raw,
            evidence_contracts=evidence_contracts,
        )
        strategy_lab_id = str((contract or raw).get("strategy_lab_id") or "").strip()
        if error:
            rejected.append({"index": index, "strategy_lab_id": strategy_lab_id or None, "reason": error})
            continue
        if not strategy_lab_id or strategy_lab_id in seen_ids:
            rejected.append({"index": index, "strategy_lab_id": strategy_lab_id or None, "reason": "duplicate_or_missing_strategy_lab_id"})
            continue
        seen_ids.add(strategy_lab_id)
        if conn.execute(
            "select 1 from strategy_lab_experiments where strategy_lab_id=?",
            (strategy_lab_id,),
        ).fetchone() is not None:
            rejected.append({"index": index, "strategy_lab_id": strategy_lab_id, "reason": "existing_root_not_mutated"})
            continue
        if slots <= 0:
            reason = "daily_new_root_cap" if daily_slots <= 0 else "active_root_cap"
            rejected.append({"index": index, "strategy_lab_id": strategy_lab_id, "reason": reason})
            continue
        rec = {
            "source_agent": "paid_research_one_shot",
            "payload": {
                "title": contract["hypothesis"],
                "rationale": contract["hypothesis"],
                "agent_name": "paid_research_one_shot",
                "strategy_lab_experiment": contract,
            },
        }
        actions = ingest_strategy_lab_recommendation(conn, rec, research_settings)
        action = actions[0] if actions else {}
        if not bool(action.get("created")) or action.get("action_status") != "created":
            rejected.append(
                {
                    "index": index,
                    "strategy_lab_id": strategy_lab_id,
                    "reason": str(action.get("reason") or action.get("action_status") or "ingestion_rejected"),
                }
            )
            continue
        slots -= 1
        daily_slots -= 1
        active_slots -= 1
        accepted.append(
            {
                "strategy_lab_id": strategy_lab_id,
                "status": action.get("status"),
                "logic_type": (contract.get("strategy_logic") or {}).get("type"),
                "trade_types": _string_values((contract.get("strategy_logic") or {}).get("trade_types")),
                "source_surface": contract.get("source_surface"),
                "permitted_target_surface": contract.get("permitted_target_surface"),
            }
        )
    active_after = _active_strategy_root_ids(conn)
    if len(active_after) > MAX_ACTIVE_ROOTS:
        raise SettingsError("active strategy root cap was violated")
    return {
        "accepted": accepted,
        "rejected": rejected,
        "new_root_count": len(accepted),
        "already_created_today": already_created_today,
        "active_root_count_before": len(active_before),
        "active_root_count_after": len(active_after),
        "max_new_roots_per_utc_day": MAX_NEW_ROOTS_PER_UTC_DAY,
        "max_active_roots": MAX_ACTIVE_ROOTS,
    }


def run_once(config_path: str | pathlib.Path) -> dict:
    _require_scoped_override()
    path = pathlib.Path(config_path).expanduser().resolve()
    settings = load_settings(path, require_explicit=True)
    expansion = settings.get("paper_expansion") or {}
    campaign_id = str(expansion.get("campaign_id") or "")
    _require_deferred_cost_reconciliation(settings)
    now = dt.datetime.now(dt.timezone.utc)
    day = now.date().isoformat()
    output_dir = RUNS_DIR / "paid_research"
    output_dir.mkdir(parents=True, exist_ok=True)
    claim_path = output_dir / f"{campaign_id}.{day}.claim.json"
    output_path = output_dir / f"{campaign_id}.{day}.result.json"
    if output_path.exists() or claim_path.exists():
        return {
            "status": "already_claimed_today",
            "campaign_id": campaign_id,
            "day_utc": day,
            "result_path": str(output_path) if output_path.exists() else None,
        }
    lease: dict | None = None
    lease_outcome = "unknown_failure"
    lease_details = {
        "operation_outcome": "unknown",
        "failure_category": "unclassified",
        "provider_outcome": None,
        "provider_event_id": None,
        "provider_estimated_cost_usd": None,
    }
    downstream_stage = "not_started"
    try:
        with connect(initialize=False) as conn:
            lease, context = _claim_paid_research_lease(conn, settings, campaign_id)

        if not _research_evidence_contracts(context):
            lease_outcome = "evidence_denied"
            lease_details.update(
                {
                    "operation_outcome": "evidence_denied",
                    "failure_category": "reliable_direct_evidence_unavailable",
                }
            )
            raise SettingsError(
                "paid research requires existing reliable direct crypto evidence"
            )

        prompt = json.dumps(
            {
                "task": (
                    "Derive one high-quality, testable crypto-only strategy hypothesis from the supplied "
                    "bounded campaign evidence. Do not browse or rely on outside evidence. Return one JSON "
                    "object with the exact key strategy_contracts containing a list of at most two complete "
                    "Strategy Lab contracts. Every contract must be a parentless market_strategy with a "
                    "supported structured strategy_logic, an allowlisted direct crypto venue, an explicit "
                    "direct crypto trade_type/source_surface/permitted_target_surface limited to "
                    "perp_funding_basis or frontier_crypto_venue_map, data_requirements.paper_only=true and "
                    "data_requirements.route_status=standard, plus features, invalidation, paper test, "
                    "measurement horizons, and risks. Do not propose live trading, credentials, code changes, "
                    "synthetic/proxy primary evidence, prediction markets, or non-crypto markets."
                ),
                "evidence": context,
            },
            sort_keys=True,
        )
        system_prompt = (
            "You are the isolated paid-research plane for a crypto-only paper trading campaign. "
            "Use only the supplied reliable direct campaign evidence, produce research for deterministic "
            "review, and never browse, call tools, or perform an external write."
        )
        with autonomous_paid_scope(
            settings,
            source="paid_research_once",
            scope_id=str(lease["lease_id"]),
        ):
            preflight = completion_preflight_status(
                "global_research_worker",
                prompt,
                system=system_prompt,
                max_output_tokens_override=4000,
            )
            if not preflight.get("ok", False):
                lease_outcome = "preflight_denied"
                lease_details.update(
                    {
                        "operation_outcome": "preflight_denied",
                        "failure_category": "model_preflight_blocked",
                    }
                )
                raise SettingsError(
                    str(preflight.get("status") or "paid model preflight blocked")
                )
            budget = cost_budget_status(
                agent_name="global_research_worker",
                replay_deferred=False,
            )
            if not budget.get("allowed", False):
                budget_reason = str(budget.get("reason") or budget.get("status") or "")
                unknown_budget = any(
                    token in budget_reason.lower()
                    for token in ("unknown", "invalid", "unavailable")
                )
                lease_outcome = "budget_denied"
                lease_details.update(
                    {
                        "operation_outcome": "budget_denied",
                        "failure_category": (
                            "cost_ledger_unknown"
                            if unknown_budget
                            else "cost_ceiling_or_call_limit"
                        ),
                    }
                )
                raise SettingsError(str(budget.get("reason") or budget.get("status")))
            claim = {
                "status": "claimed",
                "claimed_at": now.isoformat(),
                "campaign_id": campaign_id,
                "day_utc": day,
                "config_path": str(path),
                "config_sha256": config_fingerprint(path),
                "pid": os.getpid(),
                "lease_id": lease["lease_id"],
            }
            try:
                with claim_path.open("x", encoding="utf-8") as handle:
                    json.dump(claim, handle, indent=2, sort_keys=True)
            except FileExistsError:
                lease_outcome = "preflight_denied"
                lease_details.update(
                    {
                        "operation_outcome": "preflight_denied",
                        "failure_category": "daily_claim_already_exists",
                    }
                )
                return {
                    "status": "already_claimed_today",
                    "campaign_id": campaign_id,
                    "day_utc": day,
                }

            result = complete(
                "global_research_worker",
                prompt,
                system=system_prompt,
                operation="bounded_crypto_paid_research",
                structured_json=True,
                max_output_tokens_override=4000,
                timeout_seconds_override=180,
            )
        # Persist immutable provider-attempt identity before parsing, database
        # ingestion, or report I/O.  A downstream failure must never erase or
        # relabel a known charged attempt.
        provider_outcome = str(result.status or "unknown")
        lease_outcome = provider_outcome
        lease_details.update(
            {
                "provider_outcome": provider_outcome,
                "provider_event_id": result.event_id,
                "provider_estimated_cost_usd": float(result.estimated_cost_usd),
                "operation_outcome": "downstream_failure",
                "failure_category": (
                    "provider_failure"
                    if provider_outcome.startswith("fallback_error:")
                    else "downstream_not_completed"
                ),
            }
        )
        downstream_stage = "parse_and_ingest"
        parsed_contracts: list[dict] = []
        output_rejections: list[dict] = []
        ingestion = {
            "accepted": [],
            "rejected": [],
            "new_root_count": 0,
            "max_new_roots_per_utc_day": MAX_NEW_ROOTS_PER_UTC_DAY,
            "max_active_roots": MAX_ACTIVE_ROOTS,
        }
        if str(result.status).startswith("model_call:"):
            parsed_contracts, output_rejections = _parse_contract_output(result.text)
            with connect(initialize=False) as conn:
                ingestion = _ingest_contracts(
                    conn,
                    settings,
                    parsed_contracts,
                    day_utc=day,
                    research_context=context,
                )
        else:
            output_rejections.append({"reason": "paid_model_call_not_completed"})
        ingestion["rejected"] = [
            *output_rejections,
            *list(ingestion.get("rejected") or []),
        ]
        report = {
            "status": result.status,
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "campaign_id": campaign_id,
            "day_utc": day,
            "model_name": result.model_name,
            "model_tier": result.model_tier,
            "event_id": result.event_id,
            "estimated_cost_usd": result.estimated_cost_usd,
            "raw_research": result.text,
            "structured_contracts": parsed_contracts,
            "ingestion": ingestion,
            "ingestion_scope": "paper_strategy_lab_contracts_only",
            "recommendation_emitted": False,
            "code_change_emitted": False,
            "external_tools_used": False,
            "crypto_only": True,
            "paper_only": True,
        }
        downstream_stage = "report_write"
        output_path.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if provider_outcome.startswith("model_call:"):
            lease_details.update(
                {
                    "operation_outcome": "completed",
                    "failure_category": None,
                }
            )
        elif not provider_outcome.startswith("fallback_error:"):
            lease_details["failure_category"] = "provider_completion_not_confirmed"
        return {**report, "result_path": str(output_path)}
    except Exception:
        if (
            lease is not None
            and lease_details.get("provider_outcome") is not None
            and lease_details.get("operation_outcome") != "completed"
        ):
            lease_details.update(
                {
                    "operation_outcome": "downstream_failure",
                    "failure_category": f"downstream_{downstream_stage}_failed",
                }
            )
        raise
    finally:
        if lease is not None:
            with connect(initialize=False) as conn:
                _release_paid_research_lease(
                    conn,
                    campaign_id,
                    lease,
                    outcome=lease_outcome,
                    details=lease_details,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        report = run_once(args.config)
    except (FileNotFoundError, OSError, SettingsError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
