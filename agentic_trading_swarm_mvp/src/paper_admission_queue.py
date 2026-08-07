"""Bounded, paper-only admission queue for fresh crypto candidates.

The queue deliberately stops at candidate selection. Radar remains responsible
for review and paper execution; exact admission/episode identifiers let the
reconciler recover the resulting opportunity, order, trade, and label later.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from collections import Counter, defaultdict, deque
from typing import Any

from due_outcome_collector import outcome_measurement_capability
from market_admission import (
    admission_evidence_fingerprint,
    admission_identity_audit_for,
    admission_key_for,
    admission_lineage_for,
    admission_surface_for,
    admission_terminal_class_for,
    paper_admission_queue_enabled,
)
from storage import (
    bounded_paper_artifact_identity_valid,
    bounded_paper_trade_lineage_valid,
    reliable_paper_label_eligibility_for_trade_row,
    signal_key,
    utc_now,
)
from paired_direct_contract import is_paired_direction, validate_paired_direct_entry


PRIMARY_ROUTES = {"standard", "feasible"}
SYNTHETIC_ROUTES = {
    "paper_proxy",
    "paper_testable_proxy",
    "paper_testable_research",
    "synthetic",
    "synthetic_research_only",
    "synthetic_shadow_only",
}
ACTIVE_STATUSES = {
    "queued_review",
    "approved_waiting_capacity",
    "paper_open",
    "waiting_outcome",
    "retry_wait",
}
ARTIFACT_INFLIGHT_STATUSES = {"paper_open", "waiting_outcome"}
SELECTABLE_STATUSES = {"queued_review", "approved_waiting_capacity", "retry_wait"}
TERMINAL_STATUSES = {
    "completed_valid",
    "terminal_reject",
    "terminal_reference",
    "synthetic_shadow_only",
}
CRYPTO_TRADE_TYPES = {
    "perp_funding_basis",
    "frontier_crypto_venue_map",
    "crypto_spot",
    "spot",
    "perpetual",
    "perp",
}
CRYPTO_MARKET_TYPES = {"crypto", "spot", "perp", "perpetual", "swap", "futures"}
SUPPORTED_HISTORICAL_GUARDS = {
    "paper_strategy_family_quarantine",
    "paper_lineage_source_health",
    "strategy_reliability",
}
HARD_BLOCK_TOKENS = {
    "borrow",
    "capability",
    "cost",
    "depth",
    "liquidity",
    "permission",
    "region",
    "route",
    "slippage",
    "spread",
    "stale",
    "unavailable",
}
ENTRY_EVENT_FIELDS = (
    "book_timestamp",
    "exchange_timestamp",
    "source_timestamp",
    "ticker_timestamp",
    "source_observed_at",
    "book_observed_at",
    "observed_at",
    "seen_at",
)


def paper_admission_queue_config(settings: dict) -> dict:
    admission_cfg = settings.get("market_admission") or {}
    nested = admission_cfg.get("paper_queue") or {}
    top_level = settings.get("paper_admission_queue") or {}
    configured = {**top_level, **nested}

    def bounded_int(name: str, default: int, hard_max: int) -> int:
        return min(hard_max, max(0, int(configured.get(name, default))))

    return {
        "enabled": paper_admission_queue_enabled(settings),
        "max_active": bounded_int("max_active", 200, 200),
        "max_enqueue_per_cycle": bounded_int("max_enqueue_per_cycle", 30, 30),
        "max_select_per_cycle": bounded_int("max_select_per_cycle", 30, 30),
        "max_terminal_audit_per_cycle": bounded_int(
            "max_terminal_audit_per_cycle", 30, 30
        ),
        "selection_lease_seconds": max(30, int(configured.get("selection_lease_seconds", 900))),
        "retry_backoff_seconds": max(30, int(configured.get("retry_backoff_seconds", 300))),
        "max_freshness_age_seconds": max(
            1.0, float(configured.get("max_freshness_age_seconds", 90.0))
        ),
        "poor_cohort_min_labels": max(1, int(configured.get("poor_cohort_min_labels", 20))),
        "poor_cohort_max_avg_pnl_bps": float(
            configured.get("poor_cohort_max_avg_pnl_bps", -8.0)
        ),
        "poor_cohort_max_win_rate": float(configured.get("poor_cohort_max_win_rate", 0.43)),
        "claimant": str(configured.get("claimant") or "radar_loop"),
    }


def _route_status(candidate: dict) -> str:
    for container in (
        candidate.get("direct_execution_route"),
        candidate.get("direct_execution_feasibility"),
        candidate,
        candidate.get("execution_route"),
        candidate.get("execution_feasibility"),
    ):
        if not isinstance(container, dict):
            continue
        for key in ("route_status", "status", "paper_route_status"):
            value = str(container.get(key) or "").strip().lower()
            if value:
                return value
    return "unknown"


def _lineage_root(candidate: dict) -> str:
    return str(
        candidate.get("strategy_lineage_root_id")
        or candidate.get("strategy_lab_lineage_root_id")
        or candidate.get("signal_lineage_root_id")
        or admission_lineage_for(candidate)
    ).strip()


def _canonical_queue_candidate(
    candidate: dict,
    *,
    admission_key: str,
    episode_id: str,
    queue_id: str,
    lane: str,
    evidence_observed_at: str | None = None,
) -> dict:
    output = dict(candidate)
    paper_admission = dict(output.get("paper_admission") or {})
    paper_admission.update(
        {
            "admission_key": admission_key,
            "episode_id": episode_id,
            **admission_identity_audit_for(output),
        }
    )
    paper_admission.setdefault("strategy_lineage", admission_lineage_for(output))
    if evidence_observed_at:
        paper_admission["evidence_observed_at"] = str(evidence_observed_at)
    output.update(
        {
            "admission_key": admission_key,
            "episode_id": episode_id,
            "admission_episode_id": episode_id,
            "admission_identity_version": admission_identity_audit_for(output)[
                "identity_version"
            ],
            "paper_admission": paper_admission,
            "_paper_admission_queue_id": queue_id,
            "_paper_admission_lane": lane,
        }
    )
    if evidence_observed_at:
        output["_paper_admission_evidence_observed_at"] = str(
            evidence_observed_at
        )
    return output


def _score(candidate: dict) -> float:
    try:
        return float(candidate.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _admission_identity_validation(candidate: dict) -> dict[str, Any]:
    computed = admission_key_for(candidate)
    metadata = candidate.get("paper_admission")
    metadata = metadata if isinstance(metadata, dict) else {}
    supplied = {
        name: str(value).strip()
        for name, value in (
            ("admission_key", candidate.get("admission_key")),
            ("paper_admission_key", candidate.get("paper_admission_key")),
            ("paper_admission.admission_key", metadata.get("admission_key")),
        )
        if str(value or "").strip()
    }
    mismatched = {
        name: value for name, value in supplied.items() if value != computed
    }
    return {
        "valid": not mismatched,
        "computed_admission_key": computed,
        "supplied_admission_keys": supplied,
        "mismatched_admission_keys": mismatched,
    }


def _is_crypto(candidate: dict) -> bool:
    asset_class = str(candidate.get("asset_class") or "").strip().lower()
    market_type = str(candidate.get("market_type") or "").strip().lower()
    trade_type = str(candidate.get("trade_type") or "").strip().lower()
    venue = str(candidate.get("venue") or "").strip().upper()
    return bool(
        "crypto" in asset_class
        or market_type in CRYPTO_MARKET_TYPES
        or trade_type in CRYPTO_TRADE_TYPES
        or venue in {"OKX", "OKX_SPOT", "BYBIT", "BYBIT_SPOT"}
    )


def _freshness(candidate: dict, maximum_age_seconds: float) -> tuple[bool, float | None, str]:
    freshness_state = str(candidate.get("freshness_state") or "").strip().lower()
    if freshness_state in {"stale", "expired", "unknown"}:
        return False, None, f"freshness_state_{freshness_state}"
    age: float | None = None
    for key, multiplier in (
        ("freshness_age_seconds", 1.0),
        ("signal_age_seconds", 1.0),
        ("stale_minutes", 60.0),
    ):
        if candidate.get(key) is None:
            continue
        try:
            age = float(candidate[key]) * multiplier
        except (TypeError, ValueError):
            return False, None, f"invalid_{key}"
        break
    if age is None and freshness_state != "fresh":
        return False, None, "freshness_evidence_missing"
    if age is not None and (age < 0 or age > maximum_age_seconds):
        return False, age, "freshness_age_exceeded"
    return True, age, "fresh"


def _quality_status(candidate: dict) -> str:
    return str(candidate.get("quality_status") or candidate.get("proxy_quality_status") or "").strip().lower()


def _explicit_blockers(candidate: dict) -> list[str]:
    blockers: list[str] = []
    reject_reason = str(candidate.get("candidate_reject_reason") or "").strip()
    if reject_reason:
        blockers.append(reject_reason)
    for key in ("hard_blocks", "route_blockers", "capability_blockers"):
        raw = candidate.get(key)
        if isinstance(raw, (list, tuple, set)):
            blockers.extend(str(item).strip() for item in raw if str(item).strip())
    quarantine = candidate.get("paper_context_loss_quarantine")
    if isinstance(quarantine, dict) and not quarantine.get("paper_fill_allowed", True):
        blockers.append(str(quarantine.get("reason") or "paper_context_loss_quarantine"))
    return blockers


def _measurement_probe_guard(candidate: dict, blockers: list[str]) -> str | None:
    """Return one explicit, supported historical guard and nothing broader.

    An under-sampled cohort is not itself a reason to bypass review.  A probe is
    tagged only when the candidate carries exactly one named historical guard;
    quality, route, cost, freshness, and capability blockers are never eligible.
    """

    discovered: set[str] = set()
    explicit = str(candidate.get("_paper_measurement_probe_guard") or "").strip().lower()
    if explicit:
        discovered.add(explicit)
    detail = candidate.get("candidate_reject_detail")
    if isinstance(detail, dict):
        guard = str(detail.get("guard") or detail.get("guard_name") or "").strip().lower()
        if guard:
            discovered.add(guard)
    reliability = candidate.get("strategy_reliability")
    if isinstance(reliability, dict):
        action = str(reliability.get("action") or "").lower()
        if any(token in action for token in ("block", "disable", "quarantine", "suppress")):
            discovered.add("strategy_reliability")
    for field, guard in (
        ("paper_strategy_family_quarantine", "paper_strategy_family_quarantine"),
        ("paper_strategy_quarantine", "paper_strategy_family_quarantine"),
        ("paper_lineage_source_health", "paper_lineage_source_health"),
    ):
        value = candidate.get(field)
        if isinstance(value, dict) and (
            value.get("active")
            or value.get("blocked")
            or str(value.get("action") or "").lower() in {"block", "quarantine", "suppress"}
        ):
            discovered.add(guard)
    for blocker in blockers:
        normalized = blocker.strip().lower()
        matches = {guard for guard in SUPPORTED_HISTORICAL_GUARDS if guard in normalized}
        if "lineage_source" in normalized:
            matches.add("paper_lineage_source_health")
        if "strategy_family" in normalized or "strategy_quarantine" in normalized:
            matches.add("paper_strategy_family_quarantine")
        if any(token in normalized for token in HARD_BLOCK_TOKENS):
            return None
        # A measurement probe may bypass exactly one named historical guard.
        # The presence of that guard must never make an unrelated safety,
        # route, quality, cost, or unknown blocker disappear.
        if not matches:
            return None
        discovered.update(matches)
    if len(discovered) != 1 or not discovered.issubset(SUPPORTED_HISTORICAL_GUARDS):
        return None
    return next(iter(discovered))


def _evidence_lane(candidate: dict) -> bool:
    venue = str(candidate.get("venue") or "").strip().upper()
    direction = str(candidate.get("direction") or "").strip().lower()
    if venue == "OKX" and direction == "short_perp_long_spot":
        return True
    return (
        venue == "BYBIT_SPOT"
        and direction == "long_frontier_spot"
        and str(candidate.get("data_status") or "").strip().lower() == "reachable"
    )


def classify_paper_admission_candidate(candidate: dict, settings: dict) -> dict[str, Any]:
    cfg = paper_admission_queue_config(settings)
    identity = _admission_identity_validation(candidate)
    if not identity["valid"]:
        return {
            "eligible": False,
            "queue_status": None,
            "reason": "admission_identity_mismatch",
            **identity,
        }
    route_status = _route_status(candidate)
    direction = str(candidate.get("direction") or "").strip().lower()
    historical_cohort = candidate.get("bounded_historical_cohort")
    historical_cohort = (
        dict(historical_cohort) if isinstance(historical_cohort, dict) else None
    )
    if historical_cohort is not None and historical_cohort.get("status") != "ready":
        return {
            "eligible": False,
            "queue_status": "terminal_reject",
            "reason": "bounded_historical_cohort_evidence_unavailable",
            "route_status": route_status,
            "lane": "evidence" if _evidence_lane(candidate) else "discovery",
            "bounded_historical_cohort": historical_cohort,
        }
    if historical_cohort is not None and bool(
        historical_cohort.get("known_losing_cohort", False)
    ):
        return {
            "eligible": False,
            "queue_status": "terminal_reject",
            "reason": "known_losing_cohort",
            "route_status": route_status,
            "lane": "evidence" if _evidence_lane(candidate) else "discovery",
            "bounded_historical_cohort": historical_cohort,
        }
    quality_status = _quality_status(candidate)
    data_status = str(candidate.get("data_status") or "reachable").strip().lower()
    if admission_terminal_class_for(candidate) == "terminal_reference":
        return {
            "eligible": False,
            "queue_status": "terminal_reference",
            "reason": "terminal_reference_inventory",
            "route_status": route_status,
            "lane": "discovery",
        }
    synthetic = bool(
        route_status in SYNTHETIC_ROUTES
        or candidate.get("paper_observation_only")
        or candidate.get("synthetic_research_only")
        or candidate.get("shadow_only")
    )
    if synthetic:
        return {
            "eligible": False,
            "queue_status": "synthetic_shadow_only",
            "reason": f"synthetic_route_{route_status}",
            "route_status": route_status,
        }
    if not _is_crypto(candidate):
        return {"eligible": False, "queue_status": None, "reason": "non_crypto", "route_status": route_status}
    if not direction or direction == "watch_only":
        return {"eligible": False, "queue_status": None, "reason": "direction_missing", "route_status": route_status}
    fail_closed_recovery = bool(
        (settings.get("operations") or {}).get("fail_closed_recovery_profile", False)
    )
    measurement_capability: dict[str, Any] | None = None
    if fail_closed_recovery:
        if is_paired_direction(direction):
            paired_entry = validate_paired_direct_entry(candidate, settings=settings)
            if not paired_entry.get("valid"):
                return {
                    "eligible": False,
                    "queue_status": "synthetic_shadow_only",
                    "reason": "paired_direct_contract_invalid_or_incomplete",
                    "route_status": route_status,
                    "paired_direct_entry_validation": paired_entry,
                }
            paired_contract = paired_entry.get("contract") or {}
            components = paired_contract.get("entry_components") or {}
            component_capabilities = {
                name: outcome_measurement_capability(
                    component.get("venue"),
                    component.get("inst_id"),
                    component.get("market_surface"),
                    candidate.get("trade_type"),
                )
                for name, component in components.items()
                if name in {"perp", "spot"} and isinstance(component, dict)
            }
            measurement_capability = {
                "capable": bool(
                    set(component_capabilities) == {"perp", "spot"}
                    and all(
                        item.get("capable")
                        for item in component_capabilities.values()
                    )
                ),
                "paired_outcome_complete": True,
                "contract_version": "paired_direct_v1",
                "components": component_capabilities,
                "requires_realized_funding_coverage": True,
            }
        else:
            measurement_capability = outcome_measurement_capability(
                candidate.get("venue"),
                candidate.get("inst_id") or candidate.get("instrument_id"),
                candidate.get("market_surface"),
                candidate.get("trade_type"),
                symbol=candidate.get("symbol"),
            )
        if not measurement_capability.get("capable"):
            return {
                "eligible": False,
                "queue_status": "synthetic_shadow_only",
                "reason": "outcome_measurement_capability_unavailable",
                "route_status": route_status,
                "outcome_measurement_capability": measurement_capability,
            }
    if data_status != "reachable":
        return {
            "eligible": False,
            "queue_status": None,
            "reason": f"data_{data_status}",
            "route_status": route_status,
        }
    fresh, freshness_age, freshness_reason = _freshness(candidate, cfg["max_freshness_age_seconds"])
    if not fresh:
        return {
            "eligible": False,
            "queue_status": None,
            "reason": freshness_reason,
            "freshness_age_seconds": freshness_age,
            "route_status": route_status,
        }
    if quality_status not in {"verified", "normal"}:
        return {
            "eligible": False,
            "queue_status": None,
            "reason": f"quality_{quality_status or 'missing'}",
            "route_status": route_status,
        }
    if route_status not in PRIMARY_ROUTES:
        return {
            "eligible": False,
            "queue_status": None,
            "reason": f"route_{route_status}",
            "route_status": route_status,
        }
    try:
        last = float(candidate.get("last") or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    if last <= 0:
        return {"eligible": False, "queue_status": None, "reason": "price_missing", "route_status": route_status}
    blockers = _explicit_blockers(candidate)
    historical_probe_guard = _measurement_probe_guard(candidate, blockers)
    if blockers and historical_probe_guard is None:
        return {
            "eligible": False,
            "queue_status": None,
            "reason": "explicit_quality_route_cost_or_capability_block",
            "blockers": blockers,
            "route_status": route_status,
        }
    return {
        "eligible": True,
        "queue_status": "queued_review",
        "reason": "strict_primary_candidate",
        "route_status": route_status,
        "freshness_age_seconds": freshness_age,
        "quality_status": quality_status,
        "lane": "evidence" if _evidence_lane(candidate) else "discovery",
        "candidate_blockers": blockers,
        **(
            {"outcome_measurement_capability": measurement_capability}
            if measurement_capability is not None
            else {}
        ),
        "historical_probe_guard": historical_probe_guard,
    }


def _cohort_stats(
    conn: sqlite3.Connection,
    candidates: list[dict],
    settings: dict,
) -> dict[str, dict[str, Any]]:
    keys = sorted({signal_key(candidate) for candidate in candidates if candidate.get("direction")})
    output = {
        key: {"reliable_labels": 0, "avg_pnl_bps": None, "win_rate": None}
        for key in keys
    }
    if not keys:
        return output
    rows: list[sqlite3.Row] = []
    for start in range(0, len(keys), 400):
        chunk = keys[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            conn.execute(
                f"""
                select p.id,p.signal_key,p.status,p.pnl_bps,p.candidate_json,p.review_json,
                       p.context_json,p.close_measurement_status,
                       p.admission_key,p.admission_episode_id,
                       sum(case when o.measurement_status='valid' then 1 else 0 end) as valid_outcomes
                from paper_trades p
                left join paper_trade_outcomes o on o.trade_id=p.id
                where p.signal_key in ({placeholders}) and p.status='closed'
                  and (p.admission_key is null or o.admission_key = p.admission_key)
                  and (p.admission_episode_id is null or o.admission_episode_id = p.admission_episode_id)
                group by p.id
                """,
                chunk,
            ).fetchall()
        )
    pnl_by_key: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if int(row["valid_outcomes"] or 0) <= 0:
            continue
        if not reliable_paper_label_eligibility_for_trade_row(row)["paper_label_eligible"]:
            continue
        if not bounded_paper_trade_lineage_valid(conn, row, settings):
            continue
        try:
            pnl = float(row["pnl_bps"])
        except (TypeError, ValueError):
            continue
        pnl_by_key[str(row["signal_key"])].append(pnl)
    for key, pnls in pnl_by_key.items():
        output[key] = {
            "reliable_labels": len(pnls),
            "avg_pnl_bps": sum(pnls) / len(pnls) if pnls else None,
            "win_rate": sum(value > 0 for value in pnls) / len(pnls) if pnls else None,
        }
    return output


def _cohort_eligibility(
    candidate: dict,
    classification: dict[str, Any],
    cfg: dict,
    cohort_stats: dict[str, dict[str, Any]],
    *,
    lane: str | None = None,
    active_status: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Recompute cohort gates from current reliable labels, never a queue snapshot."""

    result = dict(classification)
    effective_lane = str(lane or result.get("lane") or "discovery")
    effective_status = str(active_status or result.get("queue_status") or "")
    stats = cohort_stats.get(signal_key(candidate), {})
    reliable_labels = int(stats.get("reliable_labels") or 0)
    avg_pnl = stats.get("avg_pnl_bps")
    win_rate = stats.get("win_rate")
    historical = candidate.get("bounded_historical_cohort")
    historical = historical if isinstance(historical, dict) else None
    historical_status = str((historical or {}).get("status") or "legacy_unavailable")
    historical_reliable_labels = (
        int((historical or {}).get("reliable_labels") or 0)
        if historical is not None
        else reliable_labels
    )
    active = effective_status in ACTIVE_STATUSES
    poor_cohort = bool(
        active
        and effective_lane == "discovery"
        and reliable_labels >= cfg["poor_cohort_min_labels"]
        and (
            (avg_pnl is not None and float(avg_pnl) <= cfg["poor_cohort_max_avg_pnl_bps"])
            or (win_rate is not None and float(win_rate) <= cfg["poor_cohort_max_win_rate"])
        )
    )
    result.update(
        {
            "lane": effective_lane,
            "reliable_labels": reliable_labels,
            "avg_pnl_bps": avg_pnl,
            "win_rate": win_rate,
            "historical_cohort_status": historical_status,
            "historical_reliable_labels": historical_reliable_labels,
            "poor_discovery_cohort": poor_cohort,
            "measurement_probe_allowed": bool(
                active
                and bool(result.get("eligible"))
                and effective_lane == "discovery"
                and historical_status in {"ready", "legacy_unavailable"}
                and historical_reliable_labels < cfg["poor_cohort_min_labels"]
                and result.get("historical_probe_guard") in SUPPORTED_HISTORICAL_GUARDS
            ),
        }
    )
    return result, poor_cohort


def _fresh_active_invalidation_reason(
    candidate: dict,
    classification: dict[str, Any],
    cfg: dict,
) -> str | None:
    """Return a fresh, safety-relevant reason that invalidates a queued snapshot."""

    if classification.get("eligible") or classification.get("queue_status") in TERMINAL_STATUSES:
        return None
    reason = str(classification.get("reason") or "")
    # Stale/missing-age reports are not authoritative enough to retire a good
    # snapshot. Every other fresh non-eligible result is safety-relevant,
    # including price=0 and a direction that has reverted to watch-only.
    if (
        reason == "admission_identity_mismatch"
        or reason.startswith("freshness_")
        or reason.startswith("invalid_freshness_")
        or reason.startswith("invalid_signal_age_")
        or reason.startswith("invalid_stale_")
    ):
        return None
    fresh, _age, _freshness_reason = _freshness(
        candidate, cfg["max_freshness_age_seconds"]
    )
    return reason if fresh else None


def _ensure_minimal_admission_state(
    conn: sqlite3.Connection,
    candidate: dict,
    admission_key: str,
    evidence_fingerprint: str,
    evidence_observed_at: str,
    now: str,
    queue_status: str,
) -> None:
    venue = str(candidate.get("venue") or "unknown").strip().upper()
    inst_id = str(candidate.get("inst_id") or candidate.get("instrument_id") or "unknown").strip()
    surface = admission_surface_for(candidate)
    lineage = admission_lineage_for(candidate)
    episode_id = str(candidate.get("episode_id") or "").strip() or None
    if queue_status == "terminal_reference":
        current_stage = "discovered"
        health_status = "terminal_reference"
        blocker_code = "terminal_reference_inventory"
        terminal_class = "terminal_reference"
    elif queue_status == "synthetic_shadow_only":
        current_stage = "strategy_candidate"
        health_status = "synthetic_shadow_only"
        blocker_code = "synthetic_shadow_only"
        terminal_class = "synthetic_shadow_only"
    else:
        current_stage = "route_feasible"
        health_status = "healthy"
        blocker_code = None
        terminal_class = None
    conn.execute(
        """
        insert into market_admission_states(
            admission_key,venue,inst_id,data_source,market_surface,strategy_lineage,
            current_stage,highest_stage,health_status,blocker_code,session_status,
            attempts,eligible_scans,stalled_eligible_scans,consecutive_failures,
            first_seen_at,last_seen_at,last_advanced_at,last_observation_at,
            last_evidence_fingerprint,fresh_evidence_scans,stage_entered_at,
            current_episode_id,last_review_opportunity_id,last_paper_trade_id,
            terminal_class,details_json
        ) values(?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,?,?,?,?,?,1,?,?,?,?,?,?)
        on conflict(admission_key) do nothing
        """,
        (
            admission_key,
            venue,
            inst_id,
            str(candidate.get("price_source") or candidate.get("source_url") or "paper_queue"),
            surface,
            lineage,
            current_stage,
            current_stage,
            health_status,
            blocker_code,
            str(candidate.get("session_status") or "unknown"),
            now,
            now,
            now,
            evidence_observed_at,
            evidence_fingerprint,
            now,
            episode_id,
            None,
            None,
            terminal_class,
            json.dumps(
                {
                    "source": "paper_admission_queue_bootstrap",
                    "queue_status": queue_status,
                    **admission_identity_audit_for(candidate),
                },
                sort_keys=True,
            ),
        ),
    )
    if queue_status == "terminal_reference":
        conn.execute(
            """
            update market_admission_states
            set health_status='terminal_reference',
                blocker_code='terminal_reference_inventory',
                terminal_class='terminal_reference',last_seen_at=?,
                last_observation_at=?,last_evidence_fingerprint=?,
                current_episode_id=coalesce(?,current_episode_id)
            where admission_key=?
            """,
            (now, evidence_observed_at, evidence_fingerprint, episode_id, admission_key),
        )


def _record_transition(
    conn: sqlite3.Connection,
    row: dict,
    from_status: str | None,
    to_status: str,
    kind: str,
    reason: str | None,
    now: str,
) -> None:
    conn.execute(
        """
        insert into market_admission_transitions(
            admission_key,episode_id,occurred_at,from_stage,to_stage,
            transition_kind,reason_code,evidence_fingerprint,details_json
        ) values(?,?,?,?,?,?,?,?,?)
        """,
        (
            row["admission_key"],
            row.get("episode_id"),
            now,
            f"queue:{from_status}" if from_status else None,
            f"queue:{to_status}",
            kind,
            reason,
            row.get("evidence_fingerprint"),
            json.dumps({"queue_id": row.get("queue_id"), "lane": row.get("lane")}, sort_keys=True),
        ),
    )


def _venue_balanced_enqueue_entries(entries: list[dict]) -> list[dict]:
    by_venue: dict[str, deque[dict]] = defaultdict(deque)
    for entry in sorted(
        entries,
        key=lambda value: (-_score(value["candidate"]), int(value["position"])),
    ):
        venue = str(entry["candidate"].get("venue") or "unknown").upper()
        by_venue[venue].append(entry)
    output: list[dict] = []
    venue_order = deque(sorted(by_venue))
    while venue_order:
        venue = venue_order.popleft()
        bucket = by_venue[venue]
        output.append(bucket.popleft())
        if bucket:
            venue_order.append(venue)
    return output


def _ordered_enqueue_entries(
    candidates: list[dict],
    settings: dict,
    *,
    active_limit: int | None = None,
) -> list[dict]:
    cfg = paper_admission_queue_config(settings)
    entries = [
        {
            "position": position,
            "candidate": dict(raw),
            "classification": classify_paper_admission_candidate(dict(raw), settings),
        }
        for position, raw in enumerate(candidates)
    ]
    evidence = [
        entry
        for entry in entries
        if entry["classification"].get("eligible")
        and entry["classification"].get("lane") == "evidence"
    ]
    discovery = [
        entry
        for entry in entries
        if entry["classification"].get("eligible")
        and entry["classification"].get("lane") == "discovery"
    ]
    terminal = [
        entry
        for entry in entries
        if entry["classification"].get("queue_status") in TERMINAL_STATUSES
    ]
    classified_ids = {id(entry) for entry in (*evidence, *discovery, *terminal)}
    ineligible = [entry for entry in entries if id(entry) not in classified_ids]
    evidence.sort(
        key=lambda value: (-_score(value["candidate"]), int(value["position"]))
    )
    discovery = _venue_balanced_enqueue_entries(discovery)
    active_limit = (
        int(cfg["max_enqueue_per_cycle"])
        if active_limit is None
        else max(0, min(int(cfg["max_enqueue_per_cycle"]), int(active_limit)))
    )
    evidence_target = (active_limit + 1) // 2
    discovery_target = active_limit // 2
    terminal.sort(key=lambda value: int(value["position"]))
    ineligible.sort(key=lambda value: int(value["position"]))
    return [
        *evidence[:evidence_target],
        *discovery[:discovery_target],
        *evidence[evidence_target:],
        *discovery[discovery_target:],
        *terminal,
        *ineligible,
    ]


def enqueue_paper_admission_candidates(
    conn: sqlite3.Connection,
    settings: dict,
    candidates: list[dict],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    cfg = paper_admission_queue_config(settings)
    if not cfg["enabled"]:
        return {"enabled": False, "considered": len(candidates), "enqueued": 0, "by_result": {}}
    now = now or utc_now()
    cohort_stats = _cohort_stats(conn, candidates, settings)
    results: Counter[str] = Counter()
    created_rows: list[dict] = []
    active_created = 0
    terminal_created = 0
    active_count = int(
        conn.execute(
            "select count(*) from paper_admission_queue where status in (%s)"
            % ",".join("?" for _ in ACTIVE_STATUSES),
            sorted(ACTIVE_STATUSES),
        ).fetchone()[0]
    )
    available_active_slots = min(
        cfg["max_enqueue_per_cycle"], max(0, cfg["max_active"] - active_count)
    )
    for entry in _ordered_enqueue_entries(
        candidates,
        settings,
        active_limit=available_active_slots,
    ):
        candidate = entry["candidate"]
        classification = dict(entry["classification"])
        queue_status = classification.get("queue_status")
        # A foreign identity alias must never be allowed to mutate even the
        # candidate's correctly computed active row.
        if classification.get("reason") == "admission_identity_mismatch":
            results[str(classification.get("reason") or "ineligible")] += 1
            continue
        admission_key = str(admission_key_for(candidate))
        fingerprint = admission_evidence_fingerprint(candidate)
        evidence_observed_at = _candidate_evidence_time(candidate, now).isoformat()
        invalidation_reason = _fresh_active_invalidation_reason(
            candidate, classification, cfg
        )
        exact_existing = conn.execute(
            "select * from paper_admission_queue where admission_key=? and evidence_fingerprint=? limit 1",
            (admission_key, fingerprint),
        ).fetchone()
        active_existing = (
            exact_existing
            if exact_existing is not None and str(exact_existing["status"]) in ACTIVE_STATUSES
            else conn.execute(
                "select * from paper_admission_queue where admission_key=? and status in (%s) limit 1"
                % ",".join("?" for _ in ACTIVE_STATUSES),
                (admission_key, *sorted(ACTIVE_STATUSES)),
            ).fetchone()
        )
        if active_existing is None and invalidation_reason == "direction_missing":
            directionless_matches = conn.execute(
                """
                select * from paper_admission_queue
                where venue=? and inst_id=? and market_surface=? and lineage_root=?
                  and status in ('queued_review','approved_waiting_capacity','retry_wait')
                """,
                (
                    str(candidate.get("venue") or "unknown").upper(),
                    str(candidate.get("inst_id") or candidate.get("instrument_id") or "unknown"),
                    admission_surface_for(candidate),
                    _lineage_root(candidate),
                ),
            ).fetchall()
            if directionless_matches:
                for raw in directionless_matches:
                    eligibility = json.loads(raw["eligibility_json"] or "{}")
                    eligibility.update(classification)
                    eligibility.update(
                        {
                            "measurement_probe_allowed": False,
                            "latest_invalidating_evidence_fingerprint": fingerprint,
                        }
                    )
                    row = {**dict(raw), "evidence_fingerprint": fingerprint}
                    conn.execute(
                        """
                        update paper_admission_queue
                        set evidence_fingerprint=?,evidence_observed_at=?,eligibility_json=?,
                            route_status=?,updated_at=?,dedupe_count=dedupe_count+1
                        where queue_id=?
                        """,
                        (
                            fingerprint,
                            evidence_observed_at,
                            json.dumps(eligibility, sort_keys=True, default=str),
                            str(classification.get("route_status") or "unknown"),
                            now,
                            raw["queue_id"],
                        ),
                    )
                    _transition_queue_status(
                        conn,
                        row,
                        "terminal_reject",
                        now=now,
                        reason="fresh_invalidating_evidence:direction_missing",
                        decision="terminal_reject",
                    )
                results["fresh_invalidating_evidence_terminalized"] += len(
                    directionless_matches
                )
                active_count = max(0, active_count - len(directionless_matches))
                continue
        if active_existing is not None:
            is_exact_refresh = bool(
                exact_existing is not None
                and str(exact_existing["queue_id"]) == str(active_existing["queue_id"])
            )
            active_evidence_observed_at = (
                str(active_existing["evidence_observed_at"] or evidence_observed_at)
                if is_exact_refresh
                else evidence_observed_at
            )
            existing_eligibility = json.loads(active_existing["eligibility_json"] or "{}")
            existing_eligibility.update(classification)
            existing_eligibility, poor_cohort = _cohort_eligibility(
                candidate,
                existing_eligibility,
                cfg,
                cohort_stats,
                lane=str(active_existing["lane"]),
                active_status=str(active_existing["status"]),
            )
            canonical_candidate = _canonical_queue_candidate(
                candidate,
                admission_key=str(active_existing["admission_key"]),
                episode_id=str(active_existing["episode_id"]),
                queue_id=str(active_existing["queue_id"]),
                lane=str(active_existing["lane"]),
                evidence_observed_at=active_evidence_observed_at,
            )
            target_status: str | None = None
            terminal_reason: str | None = None
            result_name: str | None = None
            active_status = str(active_existing["status"])
            if (
                active_status in ARTIFACT_INFLIGHT_STATUSES
                and queue_status in TERMINAL_STATUSES
            ):
                # Once an exact paper artifact exists, later inventory,
                # reference, or synthetic classifications cannot erase its
                # reconciliation path.  The trade/outcome is authoritative.
                conn.execute(
                    """
                    update paper_admission_queue
                    set updated_at=?,dedupe_count=dedupe_count+1
                    where queue_id=?
                    """,
                    (now, active_existing["queue_id"]),
                )
                results["artifact_inflight_terminal_refresh_preserved"] += 1
                continue
            if queue_status in TERMINAL_STATUSES:
                target_status = str(queue_status)
                terminal_reason = str(classification.get("reason") or queue_status)
                result_name = str(queue_status)
            elif (
                str(active_existing["status"]) in SELECTABLE_STATUSES
                and invalidation_reason is not None
            ):
                target_status = "terminal_reject"
                terminal_reason = f"fresh_invalidating_evidence:{invalidation_reason}"
                result_name = "fresh_invalidating_evidence_terminalized"
            elif (
                str(active_existing["status"]) in SELECTABLE_STATUSES and poor_cohort
            ):
                target_status = "terminal_reject"
                terminal_reason = "poor_discovery_cohort"
                result_name = "poor_discovery_cohort_terminalized"

            if target_status is not None:
                row = {
                    **dict(active_existing),
                    "evidence_fingerprint": fingerprint,
                }
                if target_status == "terminal_reference":
                    _ensure_minimal_admission_state(
                        conn,
                        canonical_candidate,
                        str(active_existing["admission_key"]),
                        fingerprint,
                        active_evidence_observed_at,
                        now,
                        "terminal_reference",
                    )
                conn.execute(
                    """
                    update paper_admission_queue
                    set evidence_fingerprint=?,evidence_observed_at=?,candidate_json=?,
                        eligibility_json=?,route_status=?,priority=max(priority,?),
                        updated_at=?,dedupe_count=dedupe_count+1
                    where queue_id=?
                    """,
                    (
                        fingerprint,
                        active_evidence_observed_at,
                        json.dumps(canonical_candidate, sort_keys=True, default=str),
                        json.dumps(existing_eligibility, sort_keys=True, default=str),
                        str(classification.get("route_status") or "unknown"),
                        _score(candidate),
                        now,
                        active_existing["queue_id"],
                    ),
                )
                _transition_queue_status(
                    conn,
                    row,
                    target_status,
                    now=now,
                    reason=str(terminal_reason),
                    decision=target_status,
                )
                results[str(result_name)] += 1
                active_count = max(0, active_count - 1)
                continue
            if not classification.get("eligible"):
                results[str(classification.get("reason") or "ineligible")] += 1
                continue
            conn.execute(
                """
                update paper_admission_queue
                set evidence_fingerprint=?,evidence_observed_at=?,candidate_json=?,
                    eligibility_json=?,route_status=?,priority=max(priority,?),
                    updated_at=?,dedupe_count=dedupe_count+1
                where queue_id=?
                """,
                (
                    fingerprint,
                    active_evidence_observed_at,
                    json.dumps(canonical_candidate, sort_keys=True, default=str),
                    json.dumps(existing_eligibility, sort_keys=True, default=str),
                    str(classification.get("route_status") or "unknown"),
                    _score(candidate),
                    now,
                    active_existing["queue_id"],
                ),
            )
            results["deduplicated" if is_exact_refresh else "active_refreshed"] += 1
            continue
        if exact_existing is not None:
            conn.execute(
                "update paper_admission_queue set dedupe_count=dedupe_count+1,updated_at=? where queue_id=?",
                (now, exact_existing["queue_id"]),
            )
            results["deduplicated"] += 1
            continue
        if not classification.get("eligible") and queue_status not in TERMINAL_STATUSES:
            results[str(classification.get("reason") or "ineligible")] += 1
            continue
        lane = str(classification.get("lane") or "discovery")
        classification, poor_cohort = _cohort_eligibility(
            candidate,
            classification,
            cfg,
            cohort_stats,
            lane=lane,
        )
        if poor_cohort:
            results["poor_discovery_cohort"] += 1
            continue
        total_created = active_created + terminal_created
        if total_created >= cfg["max_enqueue_per_cycle"]:
            results["cycle_enqueue_cap"] += 1
            continue
        if queue_status in TERMINAL_STATUSES and terminal_created >= cfg["max_terminal_audit_per_cycle"]:
            results["terminal_audit_cap"] += 1
            continue
        if queue_status in ACTIVE_STATUSES and active_count >= cfg["max_active"]:
            results["active_queue_cap"] += 1
            continue
        queue_id = f"paper-admission-{uuid.uuid4().hex}"
        episode_id = f"episode-{uuid.uuid4().hex}"
        candidate = _canonical_queue_candidate(
            candidate,
            admission_key=admission_key,
            episode_id=episode_id,
            queue_id=queue_id,
            lane=lane,
            evidence_observed_at=evidence_observed_at,
        )
        _ensure_minimal_admission_state(
            conn,
            candidate,
            admission_key,
            fingerprint,
            evidence_observed_at,
            now,
            str(queue_status),
        )
        row = {
            "queue_id": queue_id,
            "admission_key": admission_key,
            "episode_id": episode_id,
            "evidence_fingerprint": fingerprint,
            "lane": lane,
        }
        conn.execute(
            """
            insert into paper_admission_queue(
                queue_id,admission_key,episode_id,evidence_fingerprint,evidence_observed_at,
                lane,status,priority,
                venue,inst_id,market_surface,lineage_root,direction,route_status,
                candidate_json,eligibility_json,enqueued_at,updated_at
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                queue_id,
                admission_key,
                episode_id,
                fingerprint,
                evidence_observed_at,
                lane,
                queue_status,
                _score(candidate),
                str(candidate.get("venue") or "unknown").upper(),
                str(candidate.get("inst_id") or candidate.get("instrument_id") or "unknown"),
                admission_surface_for(candidate),
                _lineage_root(candidate),
                str(candidate.get("direction") or "unknown"),
                str(classification.get("route_status") or "unknown"),
                json.dumps(candidate, sort_keys=True, default=str),
                json.dumps(classification, sort_keys=True, default=str),
                now,
                now,
            ),
        )
        _record_transition(conn, row, None, str(queue_status), "queue_enqueued", classification.get("reason"), now)
        created_rows.append(row)
        results[str(queue_status)] += 1
        if queue_status in ACTIVE_STATUSES:
            active_count += 1
            active_created += 1
        elif queue_status in TERMINAL_STATUSES:
            terminal_created += 1
    conn.commit()
    return {
        "enabled": True,
        "considered": len(candidates),
        "enqueued": len(created_rows),
        "active_enqueued": active_created,
        "terminal_audit_enqueued": terminal_created,
        "by_result": dict(results),
        "queue_ids": [row["queue_id"] for row in created_rows],
        "summary": paper_admission_queue_summary(conn, settings, now=now),
    }


def _parse_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _iso_after(now: str, seconds: int) -> str:
    parsed = _parse_time(now) or dt.datetime.now(dt.timezone.utc)
    return (parsed + dt.timedelta(seconds=max(0, seconds))).isoformat()


def _candidate_evidence_time(candidate: dict, fallback: str) -> dt.datetime:
    for key in ENTRY_EVENT_FIELDS:
        parsed = _parse_time(candidate.get(key))
        if parsed is not None:
            return parsed
    observed = _parse_time(fallback) or dt.datetime.now(dt.timezone.utc)
    try:
        recorded_age = max(0.0, float(candidate.get("freshness_age_seconds") or 0.0))
    except (TypeError, ValueError):
        recorded_age = 0.0
    return observed - dt.timedelta(seconds=recorded_age)


def _selection_evidence_time(row: sqlite3.Row, candidate: dict) -> dt.datetime | None:
    stored = _parse_time(row["evidence_observed_at"])
    if stored is not None:
        return stored
    return _candidate_evidence_time(candidate, str(row["enqueued_at"] or ""))


def _selection_freshness_reason(
    row: sqlite3.Row,
    candidate: dict,
    *,
    now: str,
    maximum_age_seconds: float,
) -> str:
    current = _parse_time(now) or dt.datetime.now(dt.timezone.utc)
    observed = _selection_evidence_time(row, candidate)
    if observed is None:
        return "evidence_time_missing"
    source_present = False
    source_observed: dt.datetime | None = None
    for field in ENTRY_EVENT_FIELDS:
        raw = candidate.get(field)
        if raw in (None, ""):
            continue
        source_present = True
        source_observed = _parse_time(raw)
        if source_observed is not None:
            break
    if source_present and source_observed is None:
        return "source_evidence_time_invalid"
    if source_observed is not None and source_observed != observed:
        return "source_evidence_time_mismatch"
    metadata = candidate.get("paper_admission")
    metadata = metadata if isinstance(metadata, dict) else {}
    marker = candidate.get("_paper_admission_evidence_observed_at") or metadata.get(
        "evidence_observed_at"
    )
    if marker not in (None, "") and _parse_time(marker) != observed:
        return "canonical_evidence_time_mismatch"
    age_seconds = (current - observed).total_seconds()
    if age_seconds < 0.0:
        return "evidence_time_in_future"
    if age_seconds > maximum_age_seconds:
        return "evidence_age_exceeded"
    return "fresh"


def _fresh_at_selection(
    row: sqlite3.Row,
    candidate: dict,
    *,
    now: str,
    maximum_age_seconds: float,
) -> bool:
    return (
        _selection_freshness_reason(
            row,
            candidate,
            now=now,
            maximum_age_seconds=maximum_age_seconds,
        )
        == "fresh"
    )


def release_expired_paper_admission_leases(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    commit: bool = True,
) -> int:
    now = now or utc_now()
    rows = conn.execute(
        """
        select * from paper_admission_queue
        where status in ('queued_review','approved_waiting_capacity','retry_wait')
          and lease_expires_at is not null
          and julianday(lease_expires_at) <= julianday(?)
        """,
        (now,),
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        conn.execute(
            """
            update paper_admission_queue
            set claimed_by=null,claim_token=null,lease_expires_at=null,
                updated_at=?,last_reason='selection_lease_expired'
            where queue_id=? and status in ('queued_review','approved_waiting_capacity','retry_wait')
            """,
            (now, row["queue_id"]),
        )
        _record_transition(
            conn,
            row,
            str(row["status"]),
            str(row["status"]),
            "queue_lease_released",
            "lease_expired",
            now,
        )
    if commit:
        conn.commit()
    return len(rows)


def _balanced_by_venue(rows: list[sqlite3.Row], limit: int) -> list[sqlite3.Row]:
    by_venue: dict[str, deque[sqlite3.Row]] = defaultdict(deque)
    for row in rows:
        by_venue[str(row["venue"])].append(row)
    selected: list[sqlite3.Row] = []
    venue_order = deque(sorted(by_venue))
    while venue_order and len(selected) < limit:
        venue = venue_order.popleft()
        bucket = by_venue[venue]
        selected.append(bucket.popleft())
        if bucket:
            venue_order.append(venue)
    return selected


def _fair_lane_selection(rows: list[sqlite3.Row], limit: int, *, venue_balanced: bool) -> list[sqlite3.Row]:
    if limit <= 0:
        return []
    ordered = sorted(
        rows,
        key=lambda row: (
            -float(row["priority"] or 0.0),
            str(row["next_eligible_at"] or row["enqueued_at"]),
            str(row["enqueued_at"]),
            str(row["queue_id"]),
        ),
    )
    if venue_balanced:
        return _balanced_by_venue(ordered, limit)
    selected: list[sqlite3.Row] = []
    seen_lineages: set[str] = set()
    for row in ordered:
        lineage = str(row["lineage_root"])
        if lineage in seen_lineages:
            continue
        selected.append(row)
        seen_lineages.add(lineage)
        if len(selected) >= limit:
            return selected
    selected_ids = {str(row["queue_id"]) for row in selected}
    selected.extend(row for row in ordered if str(row["queue_id"]) not in selected_ids)
    return selected[:limit]


def _apply_paper_fill_capacity(
    rows: list[sqlite3.Row],
    paper_fill_slots_by_lane: dict[str, int] | None,
) -> list[sqlite3.Row]:
    """Cap only post-review fill claims; preserve the rest of review capacity."""

    if paper_fill_slots_by_lane is None:
        return rows
    allowed_approved_ids: set[str] = set()
    for lane in ("evidence", "discovery"):
        capacity = max(0, int(paper_fill_slots_by_lane.get(lane, 0)))
        approved = [
            row
            for row in rows
            if str(row["lane"]) == lane
            and str(row["status"]) == "approved_waiting_capacity"
        ]
        chosen = _fair_lane_selection(
            approved,
            capacity,
            venue_balanced=lane == "discovery",
        )
        allowed_approved_ids.update(str(row["queue_id"]) for row in chosen)
    return [
        row
        for row in rows
        if str(row["status"]) != "approved_waiting_capacity"
        or str(row["queue_id"]) in allowed_approved_ids
    ]


def select_paper_admission_candidates(
    conn: sqlite3.Connection,
    settings: dict,
    *,
    now: str | None = None,
    claimant: str | None = None,
    limit: int | None = None,
    paper_fill_slots_by_lane: dict[str, int] | None = None,
    required_lineage_root: str | None = None,
) -> list[dict]:
    cfg = paper_admission_queue_config(settings)
    selection_limit = cfg["max_select_per_cycle"]
    if limit is not None:
        selection_limit = min(selection_limit, max(0, int(limit)))
    if not cfg["enabled"] or selection_limit <= 0:
        return []
    now = now or utc_now()
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("begin immediate")
    try:
        release_expired_paper_admission_leases(conn, now=now, commit=False)
        lineage_clause = ""
        query_params: list[Any] = [now]
        if required_lineage_root is not None:
            required_lineage_root = str(required_lineage_root).strip()
            if not required_lineage_root:
                raise ValueError("required_lineage_root must not be empty")
            lineage_clause = " and lineage_root=?"
            query_params.append(required_lineage_root)
        rows = conn.execute(
            f"""
            select * from paper_admission_queue
            where status in ('queued_review','approved_waiting_capacity','retry_wait')
              and claim_token is null and lease_expires_at is null
              and (next_eligible_at is null or julianday(next_eligible_at) <= julianday(?))
              {lineage_clause}
            """,
            query_params,
        ).fetchall()
        fresh_rows: list[sqlite3.Row] = []
        for raw in rows:
            snapshot = json.loads(raw["candidate_json"] or "{}")
            freshness_reason = _selection_freshness_reason(
                raw,
                snapshot,
                now=now,
                maximum_age_seconds=cfg["max_freshness_age_seconds"],
            )
            if freshness_reason == "fresh":
                fresh_rows.append(raw)
                continue
            if (
                str(raw["status"]) == "approved_waiting_capacity"
                and freshness_reason == "evidence_age_exceeded"
            ):
                # Capacity deferral spans the 15-minute campaign cadence while
                # quote freshness is intentionally capped at 90 seconds. Keep
                # the same admission episode dormant until enqueue supplies a
                # genuinely fresh evidence snapshot.
                continue
            _transition_queue_status(
                conn,
                dict(raw),
                "terminal_reject",
                now=now,
                reason="stale_before_review_claim",
                decision="terminal_reject",
            )
        rows = fresh_rows
        snapshots = {
            str(raw["queue_id"]): json.loads(raw["candidate_json"] or "{}")
            for raw in rows
        }
        current_cohort_stats = _cohort_stats(conn, list(snapshots.values()), settings)
        current_eligibility: dict[str, dict[str, Any]] = {}
        cohort_eligible_rows: list[sqlite3.Row] = []
        for raw in rows:
            queue_id = str(raw["queue_id"])
            eligibility = json.loads(raw["eligibility_json"] or "{}")
            eligibility, poor_cohort = _cohort_eligibility(
                snapshots[queue_id],
                eligibility,
                cfg,
                current_cohort_stats,
                lane=str(raw["lane"]),
                active_status=str(raw["status"]),
            )
            conn.execute(
                "update paper_admission_queue set eligibility_json=? where queue_id=?",
                (json.dumps(eligibility, sort_keys=True, default=str), queue_id),
            )
            if poor_cohort and str(raw["lane"]) == "discovery":
                _transition_queue_status(
                    conn,
                    dict(raw),
                    "terminal_reject",
                    now=now,
                    reason="poor_discovery_cohort_before_claim",
                    decision="terminal_reject",
                )
                continue
            current_eligibility[queue_id] = eligibility
            cohort_eligible_rows.append(raw)
        rows = _apply_paper_fill_capacity(
            cohort_eligible_rows, paper_fill_slots_by_lane
        )
        evidence = [row for row in rows if str(row["lane"]) == "evidence"]
        discovery = [row for row in rows if str(row["lane"]) == "discovery"]
        evidence_target = (selection_limit + 1) // 2
        discovery_target = selection_limit // 2
        selected = _fair_lane_selection(evidence, evidence_target, venue_balanced=False)
        selected += _fair_lane_selection(discovery, discovery_target, venue_balanced=True)
        selected_ids = {str(row["queue_id"]) for row in selected}
        if len(selected) < selection_limit:
            remaining = [row for row in rows if str(row["queue_id"]) not in selected_ids]
            selected += _fair_lane_selection(
                remaining, selection_limit - len(selected), venue_balanced=True
            )
        claim_owner = claimant or cfg["claimant"]
        lease_expires_at = _iso_after(now, cfg["selection_lease_seconds"])
        output: list[dict] = []
        for raw in selected[:selection_limit]:
            row = dict(raw)
            token = uuid.uuid4().hex
            updated = conn.execute(
                """
                update paper_admission_queue
                set selected_at=?,claimed_by=?,claim_token=?,
                    consumed_claim_token=null,claim_consumed_at=null,
                    lease_expires_at=?,selection_count=selection_count+1,
                    attempt_count=attempt_count+1,updated_at=?
                where queue_id=? and status in ('queued_review','approved_waiting_capacity','retry_wait')
                  and claim_token is null and lease_expires_at is null
                """,
                (now, claim_owner, token, lease_expires_at, now, row["queue_id"]),
            ).rowcount
            if not updated:
                continue
            candidate = _canonical_queue_candidate(
                json.loads(row["candidate_json"] or "{}"),
                admission_key=str(row["admission_key"]),
                episode_id=str(row["episode_id"]),
                queue_id=str(row["queue_id"]),
                lane=str(row["lane"]),
                evidence_observed_at=str(row["evidence_observed_at"]),
            )
            eligibility = current_eligibility[str(row["queue_id"])]
            paper_admission = dict(candidate.get("paper_admission") or {})
            paper_admission.update(
                {
                    "queue_id": row["queue_id"],
                    "claim_token": token,
                }
            )
            candidate.update(
                {
                    "paper_admission": paper_admission,
                    "paper_admission_claim_token": token,
                    "_paper_admission_claim_token": token,
                    "_paper_admission_reliable_labels": int(eligibility.get("reliable_labels") or 0),
                    "_paper_admission_avg_pnl_bps": eligibility.get("avg_pnl_bps"),
                    "_paper_admission_win_rate": eligibility.get("win_rate"),
                    "_paper_measurement_probe_allowed": bool(
                        eligibility.get("measurement_probe_allowed")
                    ),
                    "_paper_measurement_probe_guard": eligibility.get(
                        "historical_probe_guard"
                    ),
                }
            )
            conn.execute(
                "update paper_admission_queue set candidate_json=? where queue_id=?",
                (json.dumps(candidate, sort_keys=True, default=str), row["queue_id"]),
            )
            row.update({"claim_token": token, "lane": row["lane"]})
            _record_transition(
                conn,
                row,
                str(raw["status"]),
                str(raw["status"]),
                "queue_selected",
                "fair_50_50_selection",
                now,
            )
            output.append(candidate)
        if started_transaction:
            conn.commit()
        return output
    except Exception:
        if started_transaction:
            conn.rollback()
        raise


def _review_historical_guard(review_json: str | None) -> str | None:
    try:
        review = json.loads(review_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    blocks = [str(item) for item in review.get("hard_blocks") or []]
    return _measurement_probe_guard({}, blocks) if blocks else None


def _is_reference_artifact(candidate: dict, decision: str, order_status: str) -> bool:
    if admission_terminal_class_for(candidate) == "terminal_reference":
        return True
    if order_status in {"paper_reference_labeled", "paper_reference_rejected", "reference_only"}:
        return True
    if decision in {"terminal_reference", "reference_only", "paper_reference_labeled"}:
        return True
    if any(
        candidate.get(key)
        for key in (
            "paper_nav_reference",
            "paper_auction_reference",
            "paper_only_reference",
            "reference_only",
        )
    ):
        return True
    return "reference" in str(candidate.get("execution_semantics") or "").lower()


def _transition_queue_status(
    conn: sqlite3.Connection,
    row: dict,
    new_status: str,
    *,
    now: str,
    reason: str,
    decision: str | None = None,
    next_eligible_at: str | None = None,
    opportunity_id: int | None = None,
    execution_order_id: int | None = None,
    paper_trade_id: int | None = None,
) -> bool:
    old_status = str(row["status"])
    artifact_changed = any(
        new_value is not None and int(new_value) != int(row.get(field) or 0)
        for new_value, field in (
            (opportunity_id, "opportunity_id"),
            (execution_order_id, "execution_order_id"),
            (paper_trade_id, "paper_trade_id"),
        )
    )
    effective_next_eligible_at = (
        next_eligible_at
        if old_status != new_status or artifact_changed
        else row.get("next_eligible_at")
    )
    conn.execute(
        """
        update paper_admission_queue
        set status=?,updated_at=?,next_eligible_at=?,claimed_by=null,claim_token=null,
            lease_expires_at=null,last_decision=coalesce(?,last_decision),last_reason=?,
            opportunity_id=coalesce(?,opportunity_id),
            execution_order_id=coalesce(?,execution_order_id),
            paper_trade_id=coalesce(?,paper_trade_id),
            completed_at=case when ? in ('completed_valid','terminal_reject','terminal_reference','synthetic_shadow_only')
                              then ? else completed_at end
        where queue_id=?
        """,
        (
            new_status,
            now,
            effective_next_eligible_at,
            decision,
            reason,
            opportunity_id,
            execution_order_id,
            paper_trade_id,
            new_status,
            now,
            row["queue_id"],
        ),
    )
    if old_status != new_status:
        _record_transition(conn, row, old_status, new_status, "queue_reconciled", reason, now)
        return True
    return False


def reconcile_paper_admission_queue(
    conn: sqlite3.Connection,
    settings: dict,
    *,
    now: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    cfg = paper_admission_queue_config(settings)
    if not cfg["enabled"]:
        return {"enabled": False, "examined": 0, "transitions": 0}
    now = now or utc_now()
    leases_released = release_expired_paper_admission_leases(conn, now=now)
    rows = conn.execute(
        "select * from paper_admission_queue where status in (%s) order by updated_at asc limit ?"
        % ",".join("?" for _ in ACTIVE_STATUSES),
        (*sorted(ACTIVE_STATUSES), max(1, int(limit))),
    ).fetchall()
    transitions = 0
    decisions: Counter[str] = Counter()
    for raw in rows:
        row = dict(raw)
        binding_issue: str | None = None
        if row.get("opportunity_id") is not None:
            opportunity = conn.execute(
                """
                select * from opportunities
                where id=? and admission_key=? and admission_episode_id=? limit 1
                """,
                (row["opportunity_id"], row["admission_key"], row["episode_id"]),
            ).fetchone()
            if opportunity is None:
                binding_issue = "opportunity_binding_corrupt"
            elif int(
                conn.execute(
                    "select count(*) from opportunities where admission_key=? "
                    "and admission_episode_id=?",
                    (row["admission_key"], row["episode_id"]),
                ).fetchone()[0]
            ) > 1:
                binding_issue = "opportunity_lineage_ambiguous"
        else:
            opportunities = conn.execute(
                """
                select * from opportunities where admission_key=?
                  and admission_episode_id=? order by id desc limit 2
                """,
                (row["admission_key"], row["episode_id"]),
            ).fetchall()
            if len(opportunities) > 1:
                opportunity = None
                binding_issue = "opportunity_lineage_ambiguous"
            else:
                opportunity = opportunities[0] if opportunities else None

        if row.get("execution_order_id") is not None:
            order = conn.execute(
                """
                select * from execution_orders
                where id=? and admission_key=? and admission_episode_id=?
                  and status='paper_filled'
                limit 1
                """,
                (
                    row["execution_order_id"],
                    row["admission_key"],
                    row["episode_id"],
                ),
            ).fetchone()
            if order is None:
                binding_issue = binding_issue or "execution_order_binding_corrupt"
        else:
            filled_orders = conn.execute(
                """
                select * from execution_orders
                where admission_key=? and admission_episode_id=?
                  and status='paper_filled'
                order by id desc limit 2
                """,
                (row["admission_key"], row["episode_id"]),
            ).fetchall()
            if len(filled_orders) > 1:
                order = None
                binding_issue = binding_issue or "execution_order_lineage_ambiguous"
            elif filled_orders:
                order = filled_orders[0]
            else:
                # Multiple failed attempts are expected; only a successful
                # paper fill becomes the queue's durable execution binding.
                order = conn.execute(
                    """
                    select * from execution_orders
                    where admission_key=? and admission_episode_id=?
                    order by id desc limit 1
                    """,
                    (row["admission_key"], row["episode_id"]),
                ).fetchone()

        if opportunity is not None and not bounded_paper_artifact_identity_valid(
            row, opportunity
        ):
            binding_issue = binding_issue or "opportunity_identity_corrupt"
        if order is not None and not bounded_paper_artifact_identity_valid(row, order):
            binding_issue = binding_issue or "execution_order_identity_corrupt"

        effective_order_id = int(order["id"]) if order is not None else None
        if row.get("paper_trade_id") is not None:
            trade = conn.execute(
                """
                select * from paper_trades
                where id=? and admission_key=? and admission_episode_id=?
                  and execution_order_id=?
                limit 1
                """,
                (
                    row["paper_trade_id"],
                    row["admission_key"],
                    row["episode_id"],
                    effective_order_id,
                ),
            ).fetchone()
            if trade is None:
                binding_issue = binding_issue or "paper_trade_binding_corrupt"
        else:
            trade_rows = (
                conn.execute(
                    """
                    select * from paper_trades
                    where admission_key=? and admission_episode_id=?
                      and execution_order_id=?
                    order by id desc limit 2
                    """,
                    (row["admission_key"], row["episode_id"], effective_order_id),
                ).fetchall()
                if effective_order_id is not None
                else []
            )
            if len(trade_rows) > 1:
                trade = None
                binding_issue = binding_issue or "paper_trade_lineage_ambiguous"
            else:
                trade = trade_rows[0] if trade_rows else None

        if trade is not None and not bounded_paper_artifact_identity_valid(row, trade):
            binding_issue = binding_issue or "paper_trade_identity_corrupt"

        if binding_issue is not None:
            conn.execute(
                """
                update paper_admission_queue
                set updated_at=?,last_reason=? where queue_id=?
                """,
                (now, binding_issue, row["queue_id"]),
            )
            decisions[binding_issue] += 1
            continue
        opportunity_id = int(opportunity["id"]) if opportunity else None
        order_id = int(order["id"]) if order else None
        trade_id = int(trade["id"]) if trade else None
        if trade is not None:
            if str(trade["status"] or "").lower() == "closed":
                valid_outcomes = int(
                    conn.execute(
                        """
                        select count(*) from paper_trade_outcomes
                        where trade_id=? and measurement_status='valid'
                          and (? is null or admission_key=?)
                          and (? is null or admission_episode_id=?)
                        """,
                        (
                            trade_id,
                            trade["admission_key"],
                            trade["admission_key"],
                            trade["admission_episode_id"],
                            trade["admission_episode_id"],
                        ),
                    ).fetchone()[0]
                )
                reliable = reliable_paper_label_eligibility_for_trade_row(trade)["paper_label_eligible"]
                new_status = "completed_valid" if valid_outcomes > 0 and reliable else "waiting_outcome"
                reason = "reliable_valid_outcome" if new_status == "completed_valid" else "closed_waiting_reliable_outcome"
            else:
                new_status = "paper_open"
                reason = "paper_trade_open"
            transitions += int(
                _transition_queue_status(
                    conn,
                    row,
                    new_status,
                    now=now,
                    reason=reason,
                    opportunity_id=opportunity_id,
                    execution_order_id=(
                        order_id
                        if order is not None
                        and str(order["status"] or "").strip().lower()
                        == "paper_filled"
                        else None
                    ),
                    paper_trade_id=trade_id,
                )
            )
            decisions[reason] += 1
            continue
        order_status = str(order["status"] or "").strip().lower() if order else ""
        decision = str(opportunity["decision"] or "").strip().lower() if opportunity else ""
        candidate = json.loads(row["candidate_json"] or "{}")
        if _is_reference_artifact(candidate, decision, order_status):
            new_status, reason = "terminal_reference", decision or order_status or "reference_only"
        elif order_status == "deferred_capacity" or decision in {
            "deferred_capacity",
            "deferred_observation_capacity",
        }:
            new_status, reason = "approved_waiting_capacity", decision or order_status
        elif order_status in {"shadow_only", "shadow_filtered"} or decision in {
            "shadow_only",
            "shadow_filtered",
            "shadow_observed",
        }:
            new_status, reason = "synthetic_shadow_only", decision or order_status
        elif order_status in {"execution_error", "transient_error", "retry_wait", "failed_transient"} or decision in {
            "execution_error",
            "transient_execution_error",
            "retry_wait",
        }:
            new_status, reason = "retry_wait", decision or order_status
        elif decision in {"approve_paper_trade", "approve_conditional_paper_trade"}:
            new_status, reason = "approved_waiting_capacity", "approved_waiting_execution_artifact"
        elif decision.startswith("reject") or decision in {
            "conditional_review",
            "execution_abandoned",
            "reject",
        }:
            new_status, reason = "terminal_reject", decision
        else:
            if opportunity_id or order_id:
                filled_order_id = (
                    order_id if order_status == "paper_filled" else None
                )
                conn.execute(
                    """
                    update paper_admission_queue
                    set opportunity_id=coalesce(?,opportunity_id),
                        execution_order_id=coalesce(?,execution_order_id),updated_at=?
                    where queue_id=?
                    """,
                    (opportunity_id, filled_order_id, now, row["queue_id"]),
                )
            continue
        transitions += int(
            _transition_queue_status(
                conn,
                row,
                new_status,
                now=now,
                reason=reason,
                decision=decision or order_status,
                next_eligible_at=(
                    _iso_after(now, cfg["retry_backoff_seconds"])
                    if new_status in {"approved_waiting_capacity", "retry_wait"}
                    else None
                ),
                opportunity_id=opportunity_id,
                execution_order_id=(
                    order_id if order_status == "paper_filled" else None
                ),
            )
        )
        decisions[reason] += 1
    conn.commit()
    return {
        "enabled": True,
        "examined": len(rows),
        "transitions": transitions,
        "leases_released": leases_released,
        "by_decision": dict(decisions),
        "summary": paper_admission_queue_summary(conn, settings, now=now),
    }


def paper_admission_queue_summary(
    conn: sqlite3.Connection,
    settings: dict | None = None,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    rows = conn.execute(
        "select status,lane,count(*) as row_count,sum(selection_count) as selections,sum(dedupe_count) as dedupes "
        "from paper_admission_queue group by status,lane"
    ).fetchall()
    by_status: Counter[str] = Counter()
    by_lane: Counter[str] = Counter()
    selections = 0
    dedupes = 0
    for row in rows:
        count = int(row["row_count"] or 0)
        by_status[str(row["status"])] += count
        by_lane[str(row["lane"])] += count
        selections += int(row["selections"] or 0)
        dedupes += int(row["dedupes"] or 0)
    active_depth = sum(by_status.get(status, 0) for status in ACTIVE_STATUSES)
    oldest = conn.execute(
        "select min(enqueued_at) from paper_admission_queue where status in (%s)"
        % ",".join("?" for _ in ACTIVE_STATUSES),
        sorted(ACTIVE_STATUSES),
    ).fetchone()[0]
    oldest_time = _parse_time(oldest)
    current_time = _parse_time(now) or dt.datetime.now(dt.timezone.utc)
    oldest_age = max(0.0, (current_time - oldest_time).total_seconds()) if oldest_time else None
    linked = conn.execute(
        """
        select sum(opportunity_id is not null),sum(execution_order_id is not null),sum(paper_trade_id is not null)
        from paper_admission_queue
        """
    ).fetchone()
    cfg = paper_admission_queue_config(settings or {"market_admission": {"enabled": True}})
    return {
        "enabled": cfg["enabled"] if settings is not None else True,
        "active_depth": active_depth,
        "capacity": cfg["max_active"],
        "by_status": dict(by_status),
        "by_lane": dict(by_lane),
        "oldest_active_age_seconds": round(oldest_age, 3) if oldest_age is not None else None,
        "selection_count": selections,
        "dedupe_count": dedupes,
        "opportunity_linked": int(linked[0] or 0),
        "execution_order_linked": int(linked[1] or 0),
        "paper_trade_linked": int(linked[2] or 0),
        "queued_review": int(by_status.get("queued_review", 0)),
        "approved_waiting_capacity": int(by_status.get("approved_waiting_capacity", 0)),
        "paper_open": int(by_status.get("paper_open", 0)),
        "waiting_outcome": int(by_status.get("waiting_outcome", 0)),
        "retry_wait": int(by_status.get("retry_wait", 0)),
        "completed_valid": int(by_status.get("completed_valid", 0)),
        "terminal_reject": int(by_status.get("terminal_reject", 0)),
        "terminal_reference": int(by_status.get("terminal_reference", 0)),
        "synthetic_shadow_only": int(by_status.get("synthetic_shadow_only", 0)),
    }
