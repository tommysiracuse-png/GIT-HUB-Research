#!/usr/bin/env python3
"""Paper-only order routing helpers.

This module contains candidate-level guards for the paper research path. It does
not talk to brokers, private APIs, or order endpoints; callers pass in candidate
dictionaries and receive annotated copies that can be logged by a paper runner.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

from paper_route_registry import apply_paper_route_registry


FRONTIER_MARKER = "frontier_crypto_venue_map"
FRONTIER_PAPER_ADMISSION_MARKER = "frontier_paper_admission_guard_applies"
FRONTIER_SHADOW_REASON = "frontier_shadow_filtered"
SPOT_BORROW_SHADOW_CODE = "spot_borrow_unconfirmed"
ROUTE_FEASIBILITY_SCORE_SHADOW_REASON = "paper_route_feasibility_score_below_threshold"
DEFAULT_ROUTE_FEASIBILITY_THRESHOLD = 0.65
PROXY_NOT_LIVE_EQUIVALENT = "proxy_not_live_equivalent"
FRONTIER_PAPER_ADMISSION_MIN_QUALITY_SCORE = 80.0
FRONTIER_PAPER_ADMISSION_MIN_NET_BUFFER_BPS = 5.0
FRONTIER_PAPER_ADMISSION_HIGH_SEVERITY_ANOMALIES = frozenset(
    {
        "empty_book",
        "invalid_best_prices",
        "ticker_book_midpoint_mismatch",
        "simulated_slippage_exceeds_edge",
        "stale_book",
        "depth_cliff",
    }
)

_ROUTE_FLAG_KEYS = (
    "frontier_route_feasibility_guard_enabled",
    "paper_frontier_route_feasibility_guard_enabled",
    "paper_route_feasibility_guard_enabled",
)
_ROUTE_EXECUTABLE_STATUSES = {"executable", "available", "configured", "ready", "ok", "paper_executable", "direct"}
_ROUTE_PROXY_STATUSES = {"paper_testable_proxy", "proxy", "paper_proxy", "proxy_only"}
_ROUTE_BLOCKED_STATUSES = {"blocked", "not_executable", "unavailable", "denied", "rejected"}
_ROUTE_RESEARCH_ONLY_STATUSES = {"research_only", "manual_only", "live_only", "requires_live_borrow", "needs_live_borrow"}
_FLAG_KEYS = (
    "frontier_shadow_guard_enabled",
    "frontier_paper_shadow_guard_enabled",
    "paper_frontier_shadow_guard_enabled",
    "paper_frontier_candidate_guard_enabled",
)
_FLAG_SCOPES = ("paper_order_router", "paper", "frontier_crypto_adapter", "frontier")
_FALSE_VALUES = {"0", "false", "f", "no", "n", "off", "disabled"}
_TRUE_VALUES = {"1", "true", "t", "yes", "y", "on", "enabled"}

_ROUTE_FEASIBILITY_SCORE_FIELDS = (
    "route_feasibility_score",
    "paper_route_feasibility_score",
)
_ROUTE_SENSITIVITY_FIELDS = (
    "route_sensitive",
    "is_route_sensitive",
    "paper_route_sensitive",
)
_CONDITIONAL_ROUTE_STATUSES = {"conditional", "paper_conditional"}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _FALSE_VALUES:
        return False
    if text in _TRUE_VALUES:
        return True
    return default


def frontier_paper_guard_enabled(config: Mapping[str, Any] | bool | None = None) -> bool:
    """Return whether the bounded frontier paper guard is enabled.

    The guard defaults on for paper mode. Passing any supported flag as false in
    a top-level config or in a paper/frontier scoped config disables it for
    rollback without changing candidate generation code.
    """
    if isinstance(config, bool):
        return config
    if not isinstance(config, Mapping):
        return True

    for key in _FLAG_KEYS:
        if key in config:
            return _as_bool(config.get(key), True)

    for scope in _FLAG_SCOPES:
        scoped = config.get(scope)
        if not isinstance(scoped, Mapping):
            continue
        for key in _FLAG_KEYS:
            if key in scoped:
                return _as_bool(scoped.get(key), True)
    return True


def frontier_route_feasibility_guard_enabled(config: Mapping[str, Any] | bool | None = None) -> bool:
    """Return whether frontier route-feasibility paper gating is enabled."""
    if isinstance(config, bool):
        return config
    if not isinstance(config, Mapping):
        return True

    for key in _ROUTE_FLAG_KEYS:
        if key in config:
            return _as_bool(config.get(key), True)

    for scope in _FLAG_SCOPES:
        scoped = config.get(scope)
        if not isinstance(scoped, Mapping):
            continue
        for key in _ROUTE_FLAG_KEYS:
            if key in scoped:
                return _as_bool(scoped.get(key), True)
    return True


def _paper_route_feasibility_gate_config(
    config: Mapping[str, Any] | bool | None,
) -> tuple[bool, float]:
    """Return the paper-only score-gate toggle and bounded threshold."""
    if isinstance(config, bool):
        return config, DEFAULT_ROUTE_FEASIBILITY_THRESHOLD
    profile: Mapping[str, Any] = {}
    if isinstance(config, Mapping):
        configured = config.get("paper_route_feasibility_gate")
        if isinstance(configured, Mapping):
            profile = configured
        elif configured is not None:
            profile = {"enabled": configured}
    enabled = _as_bool(profile.get("enabled"), True)
    threshold = _finite_float(profile.get("min_score"))
    if threshold is None:
        threshold = _finite_float(profile.get("threshold"))
    if threshold is None:
        threshold = DEFAULT_ROUTE_FEASIBILITY_THRESHOLD
    return enabled, max(0.0, min(1.0, threshold))


def _candidate_containers(candidate: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    containers: list[Mapping[str, Any]] = [candidate]
    for field in (
        "execution_feasibility",
        "execution_route",
        "route_requirements",
        "paper_route_requirements",
    ):
        value = candidate.get(field)
        if isinstance(value, Mapping):
            containers.append(value)
    return tuple(containers)


def _route_sensitive_marker(candidate: Mapping[str, Any]) -> bool:
    for container in _candidate_containers(candidate):
        for field in _ROUTE_SENSITIVITY_FIELDS:
            if field in container and _as_bool(container.get(field), False):
                return True
        if _coerce_flags(container.get("route_sensitivity_reasons")):
            return True
    return False


def _route_feasibility_score(candidate: Mapping[str, Any]) -> tuple[str | None, float | None]:
    for container in _candidate_containers(candidate):
        for field in _ROUTE_FEASIBILITY_SCORE_FIELDS:
            score = _finite_float(container.get(field))
            if score is not None and 0.0 <= score <= 1.0:
                return field, score
    return None, None


def _route_statuses(candidate: Mapping[str, Any]) -> set[str]:
    statuses: set[str] = set()
    for container in _candidate_containers(candidate):
        for field in ("route_status", "status", "feasibility_status"):
            status = _normalize_route_status(container.get(field))
            if status:
                statuses.add(status)
    return statuses


def _route_sensitivity_scope(candidate: Mapping[str, Any]) -> list[str]:
    """Identify the conditional route prerequisites covered by this policy."""
    descriptor_fields = (
        "direction",
        "trade_type",
        "strategy",
        "strategy_id",
        "strategy_profile",
        "signal_key",
        "market_key",
        "route_type",
        "route_id",
        "market_surface",
        "execution_surface",
    )
    descriptor_parts: list[str] = []
    for container in _candidate_containers(candidate):
        descriptor_parts.extend(str(container.get(field) or "") for field in descriptor_fields)
    descriptor = " ".join(descriptor_parts).lower().replace("-", "_")
    blockers = {item.lower().replace("-", "_") for item in _route_blockers(candidate)}
    reasons: list[str] = []

    short_spot = (
        "short_frontier_spot" in descriptor
        or "long_perp_short_spot" in descriptor
        or "short_spot" in descriptor
        or ("short" in descriptor and "spot" in descriptor)
    )
    if short_spot:
        reasons.append("short_spot")
    if any("borrow" in item for item in blockers) or any(
        _as_bool(container.get(field), False)
        for container in _candidate_containers(candidate)
        for field in ("borrow_required", "requires_spot_borrow", "borrow_dependent")
    ):
        reasons.append("borrow_dependency")
    cross_venue = "cross_venue" in descriptor or "cross venue" in descriptor
    basis = "basis" in descriptor or "multi_leg" in descriptor or "multi leg" in descriptor
    if cross_venue and basis:
        reasons.append("cross_venue_basis")
    prerequisite_tokens = ("api", "margin", "permission", "venue_access", "jurisdiction")
    if any(any(token in item for token in prerequisite_tokens) for item in blockers) or any(
        _as_bool(container.get(field), False)
        for container in _candidate_containers(candidate)
        for field in ("margin_required", "venue_api_required", "venue_permission_required")
    ):
        reasons.append("venue_api_or_margin_prerequisite")
    return list(dict.fromkeys(reasons))


def paper_route_feasibility_gate_review(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any]:
    """Evaluate the minimum route-feasibility score for paper admission.

    The gate is deliberately inactive outside paper mode and only applies to
    candidates explicitly marked route-sensitive after route enrichment.
    """
    enabled, threshold = _paper_route_feasibility_gate_config(config)
    mode = str(config.get("mode", "paper") if isinstance(config, Mapping) else "paper").strip().lower()
    route_sensitive = _route_sensitive_marker(candidate)
    statuses = _route_statuses(candidate)
    explicit_route_statuses = {
        _normalize_route_status(container.get("route_status"))
        for container in _candidate_containers(candidate)
        if container.get("route_status") not in (None, "")
    }
    explicit_route_statuses.discard("")
    conditional = bool(
        (explicit_route_statuses or statuses) & _CONDITIONAL_ROUTE_STATUSES
    )
    if not conditional and not explicit_route_statuses:
        descriptor = " ".join(
            str(candidate.get(field) or "")
            for field in ("signal_key", "market_key", "trade_type", "strategy_profile")
        ).lower()
        conditional = "conditional" in descriptor
    scope_reasons = _route_sensitivity_scope(candidate)
    score_source, score = _route_feasibility_score(candidate)
    applies = bool(enabled and mode == "paper" and route_sensitive and conditional and scope_reasons)
    diagnostics_only = _conditional_short_route_diagnostics_only(candidate)
    eligible = diagnostics_only or not applies or (score is not None and score >= threshold)
    if diagnostics_only:
        action = "diagnostic_only"
        reason = None
    elif not applies:
        action = "not_applicable"
        reason = None
    elif eligible:
        action = "admit"
        reason = None
    else:
        action = "shadow_filter"
        reason = (
            "route_feasibility_score_missing"
            if score is None
            else ROUTE_FEASIBILITY_SCORE_SHADOW_REASON
        )
    return {
        "enabled": enabled,
        "paper_only": True,
        "mode": mode,
        "applies": applies,
        "route_sensitive": route_sensitive,
        "conditional": conditional,
        "scope_reasons": scope_reasons,
        "route_feasibility_score": score,
        "score_source": score_source,
        "threshold": threshold,
        "eligible": eligible,
        "paper_fill_allowed": eligible,
        "action": action,
        "reason": reason,
        "route_diagnostics_only": diagnostics_only,
    }

def _text_contains_frontier_marker(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return FRONTIER_MARKER in value
    if isinstance(value, Mapping):
        return any(_text_contains_frontier_marker(item) for item in value.values())
    if isinstance(value, Iterable):
        return any(_text_contains_frontier_marker(item) for item in value)
    return FRONTIER_MARKER in str(value)


def is_frontier_crypto_candidate(candidate: Mapping[str, Any]) -> bool:
    """Return true for candidates from the frontier crypto venue-map family."""
    marker_fields = (
        "market_surface",
        "market_key",
        "trade_type",
        "signal_family",
        "signal_key",
        "strategy",
        "strategy_id",
        "variant",
        "variant_id",
        "context_key",
    )
    for field in marker_fields:
        if _text_contains_frontier_marker(candidate.get(field)):
            return True
    for field in ("tags", "candidate_tags", "contexts"):
        if _text_contains_frontier_marker(candidate.get(field)):
            return True
    return False


def _finite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _first_float(candidate: Mapping[str, Any], fields: tuple[str, ...]) -> tuple[str | None, float | None]:
    for field in fields:
        numeric = _finite_float(candidate.get(field))
        if numeric is not None:
            return field, numeric
    return None, None


def _coerce_flags(value: Any) -> set[str]:
    if value in (None, ""):
        return set()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return set()
        if text[:1] in "[{":
            try:
                return _coerce_flags(json.loads(text))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        parts = text.replace("|", ",").split(",")
        return {part.strip().strip("'\"[] ") for part in parts if part.strip().strip("'\"[] ")}
    if isinstance(value, Mapping):
        return {str(key) for key, flagged in value.items() if flagged}
    if isinstance(value, Iterable):
        return {str(item) for item in value if item not in (None, "")}
    return {str(value)}


def _route_blockers(candidate: Mapping[str, Any]) -> set[str]:
    blockers: set[str] = set()
    for container in _candidate_containers(candidate):
        for field in (
            "route_blockers",
            "missing_requirements",
            "missing_permissions",
            "missing_prerequisites",
            "eligibility_missing_prerequisites",
        ):
            blockers.update(_coerce_flags(container.get(field)))
    return {item for item in blockers if item}


def _normalize_route_status(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _frontier_admission_route_status(candidate: Mapping[str, Any]) -> tuple[str | None, str | None]:
    for container, prefix in (
        (candidate.get("execution_route"), "execution_route."),
        (candidate.get("execution_feasibility"), "execution_feasibility."),
        (candidate, ""),
    ):
        if not isinstance(container, Mapping):
            continue
        for field in ("route_status", "status", "feasibility_status"):
            if container.get(field) not in (None, ""):
                return _normalize_route_status(container.get(field)), f"{prefix}{field}"
    return None, None


def frontier_paper_admission_reason(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    """Return the scanner-scoped paper-only frontier admission diagnostic."""
    profile = config if isinstance(config, Mapping) else {}
    if (
        not frontier_paper_guard_enabled(config)
        or not is_frontier_crypto_candidate(candidate)
        or not _as_bool(candidate.get(FRONTIER_PAPER_ADMISSION_MARKER), False)
        or str(profile.get("mode", "paper")).strip().lower() != "paper"
        or _as_bool(profile.get("allow_live_trading"), False)
    ):
        return None

    checks: list[dict[str, Any]] = []
    route_status, route_field = _frontier_admission_route_status(candidate)
    if route_status != "standard":
        checks.append({"code": "route_status_not_standard", "field": route_field or "route_status", "value": route_status})

    quality_status = str(candidate.get("quality_status") or "").strip().lower()
    if quality_status != "verified":
        checks.append({"code": "quality_status_not_verified", "field": "quality_status", "value": candidate.get("quality_status")})
    quality_action = str(candidate.get("quality_action") or "").strip().lower().replace("-", "_")
    if quality_action != "normal":
        checks.append({"code": "quality_action_not_normal", "field": "quality_action", "value": candidate.get("quality_action")})
    quality_score = _finite_float(candidate.get("quality_score"))
    if quality_score is None or quality_score < FRONTIER_PAPER_ADMISSION_MIN_QUALITY_SCORE:
        checks.append({"code": "quality_score_below_80", "field": "quality_score", "value": quality_score})

    edge_field, edge_bps = _first_float(candidate, ("edge_bps_estimate", "net_edge_bps_estimate", "edge_bps"))
    if edge_bps is None or edge_bps <= 0.0:
        checks.append({"code": "non_positive_net_edge", "field": edge_field or "edge_bps_estimate", "value": edge_bps})
    gross_field, gross_bps = _first_float(candidate, ("gross_edge_bps_estimate", "gross_edge_bps", "raw_edge_bps"))
    cost_field, cost_bps = _first_float(candidate, ("estimated_round_trip_cost_bps", "round_trip_cost_bps", "total_cost_bps"))
    if gross_bps is None or cost_bps is None or gross_bps < cost_bps + FRONTIER_PAPER_ADMISSION_MIN_NET_BUFFER_BPS:
        checks.append(
            {
                "code": "gross_edge_not_at_least_5bps_above_round_trip_cost",
                "gross_field": gross_field or "gross_edge_bps_estimate",
                "gross_edge_bps": gross_bps,
                "cost_field": cost_field or "estimated_round_trip_cost_bps",
                "round_trip_cost_bps": cost_bps,
            }
        )
    anomaly_flags = _coerce_flags(candidate.get("anomaly_flags"))
    for flag in sorted(FRONTIER_PAPER_ADMISSION_HIGH_SEVERITY_ANOMALIES.intersection(anomaly_flags)):
        checks.append({"code": flag, "field": "anomaly_flags", "value": sorted(anomaly_flags)})
    if not checks:
        return None
    return {
        "reason": FRONTIER_SHADOW_REASON,
        "paper_only": True,
        "paper_fill_allowed": False,
        "guard": "frontier_paper_admission_guard",
        "candidate": _candidate_reference(candidate),
        "cell": _paper_signal_cell(candidate),
        "checks": checks,
    }


def _best_route_alternative(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    for container in (candidate, candidate.get("execution_feasibility"), candidate.get("execution_route")):
        if not isinstance(container, Mapping):
            continue
        for field in ("best_route_alternative", "route_alternative", "paper_route_alternative", "best_alternative"):
            alternative = container.get(field)
            if isinstance(alternative, Mapping):
                return alternative
    return {}


def _merged_paper_allocation_multiplier(candidate: Mapping[str, Any], proposed: float) -> float:
    existing = _finite_float(candidate.get("paper_allocation_multiplier"))
    if existing is not None and existing > 0.0:
        return round(min(existing, proposed), 3)
    return round(proposed, 3)


def _paper_assumption_route_allowed(candidate: Mapping[str, Any]) -> bool:
    verdict = candidate.get("paper_route_eligibility")
    if not isinstance(verdict, Mapping):
        return False
    return bool(
        verdict.get("feasibility_status") == "feasible_with_simulation_assumptions"
        and verdict.get("execution_eligibility") == "eligible"
        and not _as_bool(verdict.get("suppressed"), False)
        and isinstance(verdict.get("simulation_assumptions"), Mapping)
        and verdict.get("simulation_assumptions")
    )


def _conditional_short_route_diagnostics_only(candidate: Mapping[str, Any]) -> bool:
    """Return whether short-route facts must stay paper-only diagnostics."""

    for container in (
        candidate,
        candidate.get("execution_feasibility"),
        candidate.get("execution_route"),
    ):
        if not isinstance(container, Mapping):
            continue
        packet = container.get("conditional_short_route_intelligence")
        if not isinstance(packet, Mapping):
            continue
        if (
            _as_bool(packet.get("paper_only"), False)
            and _as_bool(packet.get("read_only"), False)
            and _as_bool(packet.get("applies"), False)
            and not _as_bool(packet.get("hard_blocking"), False)
        ):
            return True
    return False


def frontier_route_feasibility_record(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize direct-vs-proxy paper route feasibility metadata."""
    blockers = sorted(_route_blockers(candidate))
    direct_status = ""
    for container in (candidate, candidate.get("execution_feasibility"), candidate.get("execution_route")):
        if not isinstance(container, Mapping):
            continue
        for field in ("paper_route_status", "route_status", "status", "execution_status"):
            direct_status = _normalize_route_status(container.get(field))
            if direct_status:
                break
        if direct_status:
            break

    alternative = _best_route_alternative(candidate)
    alternative_status = _normalize_route_status(alternative.get("status") or alternative.get("route_status"))
    alternative_missing = set()
    for field in ("missing_permissions", "missing_requirements", "route_blockers"):
        alternative_missing.update(_coerce_flags(alternative.get(field)))
    alternative_replaces = _coerce_flags(alternative.get("replaces_blockers"))

    proxy_available = alternative_status in _ROUTE_PROXY_STATUSES and not alternative_missing
    if proxy_available:
        execution_semantics = "proxy"
        proxy_execution_semantics = str(
            alternative.get("execution_semantics") or PROXY_NOT_LIVE_EQUIVALENT
        )
        paper_route_status = "paper_testable_proxy"
        paper_fill_allowed = True
        multiplier = (
            _finite_float(alternative.get("paper_allocation_multiplier"))
            or _finite_float(alternative.get("allocation_multiplier"))
            or _finite_float(candidate.get("paper_proxy_allocation_multiplier"))
            or 0.5
        )
    elif _paper_assumption_route_allowed(candidate):
        execution_semantics = "simulation_assumption"
        proxy_execution_semantics = None
        paper_route_status = "feasible_with_simulation_assumptions"
        paper_fill_allowed = True
        multiplier = (
            _finite_float(
                (candidate.get("paper_route_eligibility") or {}).get(
                    "paper_score_multiplier"
                )
            )
            or 0.2
        )
    elif _conditional_short_route_diagnostics_only(candidate):
        execution_semantics = "route_diagnostics_only"
        proxy_execution_semantics = "synthetic_research_paper"
        paper_route_status = "conditional_paper_diagnostics"
        paper_fill_allowed = True
        multiplier = _finite_float(
            (candidate.get("conditional_short_route_diagnostics") or {}).get(
                "paper_rank_multiplier"
            )
        ) or 1.0
    elif direct_status in _ROUTE_RESEARCH_ONLY_STATUSES:
        execution_semantics = "research_only"
        proxy_execution_semantics = None
        paper_route_status = direct_status
        paper_fill_allowed = False
        multiplier = 0.0
    elif direct_status in _ROUTE_BLOCKED_STATUSES or (
        blockers and _is_short_frontier_spot(candidate) and not _is_confirmed_borrow(candidate)
    ):
        execution_semantics = "blocked"
        proxy_execution_semantics = None
        paper_route_status = direct_status or "blocked"
        paper_fill_allowed = False
        multiplier = 0.0
    elif direct_status in _ROUTE_EXECUTABLE_STATUSES or not blockers:
        execution_semantics = "direct"
        proxy_execution_semantics = None
        paper_route_status = direct_status or "executable"
        paper_fill_allowed = True
        multiplier = 1.0
    else:
        execution_semantics = "unknown"
        proxy_execution_semantics = None
        paper_route_status = direct_status or "unknown"
        paper_fill_allowed = True
        multiplier = 1.0

    proxy_route: dict[str, Any] | None = None
    if proxy_available:
        proxy_route = {
            "status": "paper_testable_proxy",
            "route_id": alternative.get("route_id") or alternative.get("route"),
            "venue": alternative.get("venue"),
            "replaces_blockers": sorted(alternative_replaces),
            "missing_permissions": sorted(alternative_missing),
        }

    return {
        "paper_route_status": paper_route_status,
        "execution_semantics": execution_semantics,
        "proxy_execution_semantics": proxy_execution_semantics,
        "proxy_not_live_equivalent": proxy_execution_semantics == PROXY_NOT_LIVE_EQUIVALENT,
        "paper_fill_allowed": paper_fill_allowed,
        "paper_proxy_used": proxy_available,
        "paper_allocation_multiplier": _merged_paper_allocation_multiplier(candidate, multiplier),
        "blocker_count": len(blockers),
        "route_blockers": blockers,
        "missing_requirement_ids": blockers,
        "direct_route_status": direct_status or "unknown",
        "alternative_status": alternative_status or None,
        "proxy_route": proxy_route,
    }


def _apply_route_feasibility_metadata(candidate: Mapping[str, Any]) -> dict[str, Any]:
    annotated = dict(candidate)
    record = frontier_route_feasibility_record(annotated)
    annotated["frontier_route_feasibility"] = record
    annotated["paper_route_status"] = record["paper_route_status"]
    annotated["paper_route_type"] = record["execution_semantics"]
    annotated["paper_execution_semantics"] = (
        record.get("proxy_execution_semantics") or record["execution_semantics"]
    )
    annotated["proxy_not_live_equivalent"] = bool(record.get("proxy_not_live_equivalent"))
    annotated["paper_proxy_not_live_equivalent"] = bool(record.get("proxy_not_live_equivalent"))
    annotated["paper_fill_allowed_by_route"] = record["paper_fill_allowed"]
    annotated["paper_proxy_used"] = record["paper_proxy_used"]
    annotated["paper_allocation_multiplier"] = record["paper_allocation_multiplier"]
    if record.get("proxy_route"):
        annotated["paper_proxy_route"] = record["proxy_route"]
    cell = _paper_signal_cell(annotated)
    if cell:
        annotated["paper_signal_cell"] = cell
        annotated["paper_signal_cell_key"] = cell.get("cell_key")
    lineage_context = _paper_lineage_context(annotated)
    if lineage_context:
        annotated["paper_lineage_context"] = lineage_context
        annotated["paper_lineage_context_key"] = lineage_context.get("context_key")
        annotated["paper_lineage_inherited_boost_allowed"] = lineage_context.get(
            "inherited_score_boost_allowed"
        )
    try:
        from strategy_reliability import apply_paper_route_lineage_confirmation
    except Exception:
        apply_paper_route_lineage_confirmation = None
    if apply_paper_route_lineage_confirmation is not None:
        # The helper tags every candidate and turns an unconfirmed translated
        # route into a paper observation with a score haircut.  It does not
        # turn route-local evidence gaps into an entry block.
        apply_paper_route_lineage_confirmation(annotated)
    execution_quality = _paper_frontier_execution_quality_gate(annotated)
    if execution_quality:
        annotated["paper_frontier_execution_quality"] = execution_quality
        annotated["paper_frontier_execution_quality_key"] = execution_quality.get("guard")
        annotated["paper_execution_quality_favorable"] = execution_quality.get("favorable_context")
        annotated["paper_execution_quality_score_multiplier"] = execution_quality.get(
            "paper_score_multiplier"
        )
    return annotated


def _apply_paper_route_eligibility_metadata(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Defensively evaluate explicit venue capabilities before a paper fill.

    Normal scanner candidates already carry this verdict from route enrichment.
    This fallback closes direct execution/test paths without loading account
    settings or making any broker call.
    """

    annotated = dict(candidate)
    upstream_verdict = annotated.get("paper_route_eligibility")
    if _conditional_short_route_diagnostics_only(annotated):
        # Resolver enrichment has already preserved the direct-route verdict
        # under a read-only diagnostic while keeping the paper candidate
        # eligible for ranking. Do not re-run the legacy hard route gate.
        return annotated
    if (
        annotated.get("paper_proxy_activated") is True
        and annotated.get("paper_proxy_not_live_equivalent") is True
        and annotated.get("paper_execution_semantics") == PROXY_NOT_LIVE_EQUIVALENT
        and (annotated.get("paper_proxy_route") or {}).get("route_id") == "okx_derivatives_paper"
        and isinstance(upstream_verdict, Mapping)
        and not upstream_verdict.get("suppressed", False)
        and upstream_verdict.get("route_decision") == "executable_proxy"
    ):
        return annotated
    if isinstance(upstream_verdict, Mapping) and upstream_verdict.get("suppressed"):
        annotated.setdefault(
            "paper_feasibility_status",
            upstream_verdict.get("feasibility_status", "infeasible_for_paper"),
        )
        annotated.setdefault("execution_eligibility", "blocked")
        annotated.setdefault("paper_route_score_multiplier", 0.0)
        annotated.setdefault("route_intelligence_status", upstream_verdict.get("route_status"))
        annotated.setdefault("candidate_status", upstream_verdict.get("candidate_status"))
        annotated.setdefault("required_capabilities", upstream_verdict.get("required_capabilities", []))
        annotated.setdefault("blocking_reason", upstream_verdict.get("blocking_reason"))
        annotated.setdefault("paper_route_notes", upstream_verdict.get("paper_route_notes", []))
        annotated.setdefault("rank_contribution_cap", upstream_verdict.get("rank_contribution_cap"))
        annotated.setdefault("rank_contribution", upstream_verdict.get("rank_contribution"))
        return annotated
    try:
        from route_resolver import evaluate_route_intelligence
    except ImportError:  # pragma: no cover - package import fallback
        try:
            from src.route_resolver import evaluate_route_intelligence
        except ImportError:
            return annotated

    verdict = evaluate_route_intelligence(annotated)
    if not (
        isinstance(verdict, Mapping)
        and verdict.get("applies")
        and (
            verdict.get("spot_short_required")
            or verdict.get("hedged_structure_required")
            or verdict.get("venue_capability_metadata_present")
        )
    ):
        return annotated
    annotated["paper_route_eligibility"] = dict(verdict)
    annotated["paper_feasibility_status"] = verdict.get("feasibility_status")
    annotated["execution_eligibility"] = verdict.get("execution_eligibility")
    annotated["paper_route_score_multiplier"] = verdict.get("paper_score_multiplier", 1.0)
    annotated["route_intelligence_status"] = verdict.get("route_status")
    annotated["candidate_status"] = verdict.get("candidate_status")
    annotated["required_capabilities"] = verdict.get("required_capabilities", [])
    annotated["route_capability_checks"] = verdict.get("capability_checks", [])
    annotated["blocking_reason"] = verdict.get("blocking_reason")
    annotated["paper_route_notes"] = verdict.get("paper_route_notes", [])
    annotated["rank_contribution_cap"] = verdict.get("rank_contribution_cap")
    annotated["rank_contribution"] = verdict.get("rank_contribution")
    if verdict.get("suppressed"):
        annotated["paper_entry_blocked"] = True
        annotated["promotion_eligible"] = False
        annotated["paper_route_allocation_multiplier"] = 0.0
    elif verdict.get("assumption_penalty_applied"):
        multiplier = float(verdict.get("paper_score_multiplier") or 0.0)
        already_penalized = bool(annotated.get("paper_route_assumption_penalty_applied"))
        annotated.setdefault("pre_route_eligibility_score", annotated.get("score"))
        if not already_penalized and _finite_float(annotated.get("score")) is not None:
            annotated["score"] = round(float(annotated["score"]) * multiplier, 6)
        annotated["paper_route_allocation_multiplier"] = multiplier
        annotated["paper_allocation_multiplier"] = _merged_paper_allocation_multiplier(
            annotated, multiplier
        )
        annotated["paper_route_assumption_penalty_applied"] = True
    return annotated


def _is_confirmed_borrow(candidate: Mapping[str, Any]) -> bool:
    for field in ("borrow_confirmed", "spot_borrow_confirmed"):
        if _as_bool(candidate.get(field), False):
            return True
    for field in ("borrow_status", "borrow_availability", "spot_borrow_status"):
        if str(candidate.get(field) or "").strip().lower() in {"confirmed", "configured", "available"}:
            return True
    route = candidate.get("execution_route")
    if isinstance(route, Mapping) and str(route.get("borrow_status") or "").strip().lower() in {"confirmed", "configured", "available"}:
        return True
    for container in (candidate.get("execution_feasibility"), candidate.get("execution_route")):
        if not isinstance(container, Mapping):
            continue
        alternative = container.get("best_route_alternative") or {}
        if not isinstance(alternative, Mapping):
            continue
        if (
            alternative.get("status") == "paper_testable_proxy"
            and "spot_borrow" in _coerce_flags(alternative.get("replaces_blockers") or alternative.get("route_blockers"))
            and not _coerce_flags(alternative.get("missing_permissions"))
        ):
            return True
    return False


def _is_short_frontier_spot(candidate: Mapping[str, Any]) -> bool:
    haystack = " ".join(
        str(candidate.get(field) or "")
        for field in (
            "direction",
            "signal_key",
            "trade_type",
            "strategy",
            "strategy_id",
            "variant",
            "context_key",
        )
    ).lower()
    return "short_frontier_spot" in haystack or (
        "frontier_crypto_venue_map" in haystack and "short" in haystack and "spot" in haystack
    )


def _candidate_reference(candidate: Mapping[str, Any]) -> dict[str, Any]:
    reference: dict[str, Any] = {}
    for field in (
        "inst_id",
        "instrument_id",
        "symbol",
        "venue",
        "signal_key",
        "trade_type",
        "market_surface",
        "market_key",
    ):
        if candidate.get(field) not in (None, ""):
            reference[field] = candidate.get(field)
    cell = _paper_signal_cell(candidate)
    if cell:
        reference["paper_signal_cell_key"] = cell.get("cell_key")
        reference["paper_signal_cell"] = cell
    lineage_context = _paper_lineage_context(candidate)
    if lineage_context:
        reference["paper_lineage_context_key"] = lineage_context.get("context_key")
        reference["paper_lineage_context"] = lineage_context
    return reference


def _paper_family_quarantine_reason(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    try:
        from strategy_reliability import paper_family_quarantine_record as _paper_family_quarantine_record
    except Exception:
        return None

    try:
        return _paper_family_quarantine_record(candidate, config=config)
    except Exception:
        return None


def _paper_lineage_source_health_reason(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    try:
        from strategy_reliability import paper_lineage_source_health_record
    except Exception:
        return None

    try:
        record = paper_lineage_source_health_record(candidate, config=config)
    except Exception:
        return None
    if isinstance(record, Mapping) and not _as_bool(record.get("paper_fill_allowed"), True):
        return dict(record)
    return None


def _paper_context_loss_quarantine_reason(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    if isinstance(config, Mapping):
        for container in (
            config,
            config.get("paper"),
            config.get("paper_policy"),
            config.get("strategy_reliability"),
        ):
            if isinstance(container, Mapping) and str(
                container.get("mode")
                or container.get("runtime_mode")
                or container.get("execution_mode")
                or ""
            ).strip().lower() in {"live", "production", "prod", "real", "broker"}:
                return None
    existing = candidate.get("paper_context_loss_quarantine")
    if isinstance(existing, Mapping) and not _as_bool(existing.get("paper_fill_allowed"), True):
        return dict(existing)
    try:
        from strategy_reliability import paper_context_loss_quarantine_record
    except Exception:
        return None
    try:
        record = paper_context_loss_quarantine_record(candidate, config=config)
    except Exception:
        return None
    if isinstance(record, Mapping) and not _as_bool(record.get("paper_fill_allowed"), True):
        return dict(record)
    return None


def _paper_portability_quarantine_reason(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    try:
        from strategy_reliability import paper_portability_quarantine_record
    except Exception:
        return None

    try:
        record = paper_portability_quarantine_record(candidate, config=config)
    except Exception:
        return None
    if isinstance(record, Mapping) and not _as_bool(record.get("eligible"), False):
        return dict(record)
    return None


def _paper_signal_cell(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        from strategy_reliability import paper_signal_cell as _paper_signal_cell_record
    except Exception:
        return None

    try:
        record = _paper_signal_cell_record(candidate)
    except Exception:
        return None
    if isinstance(record, Mapping):
        return dict(record)
    return None


def _paper_lineage_context(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        from strategy_reliability import paper_lineage_context as _paper_lineage_context_record
    except Exception:
        return None

    try:
        record = _paper_lineage_context_record(dict(candidate))
    except Exception:
        return None
    if isinstance(record, Mapping):
        return dict(record)
    return None


def _paper_context_promotion_guard(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    try:
        from strategy_reliability import paper_context_promotion_guard_record as _context_guard_record
    except Exception:
        return None

    try:
        record = _context_guard_record(dict(candidate), config=config)
    except Exception:
        return None
    return dict(record) if isinstance(record, Mapping) else None


def _paper_frontier_execution_quality_gate(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    try:
        from strategy_reliability import paper_frontier_execution_quality_gate_record as _quality_gate_record
    except Exception:
        return None

    try:
        record = _quality_gate_record(dict(candidate), config=config)
    except Exception:
        return None
    return dict(record) if isinstance(record, Mapping) else None


def _paper_okx_basis_carry_gate(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    try:
        from strategy_reliability import okx_basis_paper_carry_gate_record as _carry_gate_record
    except Exception:
        return None

    try:
        record = _carry_gate_record(dict(candidate), config=config)
    except Exception:
        return None
    return dict(record) if isinstance(record, Mapping) else None


def _paper_yahoo_proxy_cross_surface_alignment_guard(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    """Load the paper-only alignment guard without coupling router imports."""
    try:
        from frontier_data_quality import paper_only_yahoo_proxy_cross_surface_alignment_guard
    except ImportError:  # pragma: no cover - package import fallback
        try:
            from src.frontier_data_quality import paper_only_yahoo_proxy_cross_surface_alignment_guard
        except ImportError:
            return None

    profile = config if isinstance(config, Mapping) else {}
    review = paper_only_yahoo_proxy_cross_surface_alignment_guard(dict(candidate), profile)
    return dict(review) if isinstance(review, Mapping) else None


def frontier_shadow_filter_reason(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    """Return a structured paper-only shadow-filter reason, or ``None``.

    A paper-only family quarantine can suppress known-decayed lineages before
    frontier-specific checks run. The bounded frontier guard remains narrow:
    verified positive-net candidates with no slippage or quality blocker remain
    eligible for paper fills.
    """
    # Recompute this policy at the routing boundary.  Cached reviews created
    # before the quarantine may still say that local alignment was eligible.
    alignment_guard = _paper_yahoo_proxy_cross_surface_alignment_guard(candidate, config)
    if (
        isinstance(alignment_guard, Mapping)
        and alignment_guard.get("quarantine_status") == "shadow_quarantined"
    ):
        # This lineage status is intentionally observational. Keep a
        # priceable synthetic-paper route available while withholding only
        # promotion; weak paper results are diagnostic evidence, not a reason
        # to suppress exploration.
        return None
    if (
        isinstance(alignment_guard, Mapping)
        and _as_bool(alignment_guard.get("applies"), False)
        and not _as_bool(alignment_guard.get("eligible"), False)
    ):
        return {
            "reason": "paper_yahoo_proxy_cross_surface_quarantined",
            "paper_only": True,
            "paper_fill_allowed": False,
            "guard": "yahoo_proxy_cross_surface_alignment_guard",
            "candidate": _candidate_reference(candidate),
            "checks": [
                {
                    "code": alignment_guard.get("reason") or "yahoo_proxy_cross_surface_quarantined",
                    "field": "yahoo_proxy_cross_surface_alignment_guard",
                }
            ],
            "alignment_guard": dict(alignment_guard),
        }

    lineage_source_health = _paper_lineage_source_health_reason(candidate, config=config)
    if lineage_source_health is not None:
        return {
            **lineage_source_health,
            "reason": lineage_source_health.get("reason")
            or "paper_lineage_source_negative_edge_quarantine",
            "paper_only": True,
            "paper_fill_allowed": False,
            "guard": "paper_lineage_source_health",
            "candidate": _candidate_reference(candidate),
            "checks": [
                {
                    "code": lineage_source_health.get("reason"),
                    "field": "lineage_source_health",
                }
            ],
        }

    context_loss_quarantine = _paper_context_loss_quarantine_reason(candidate, config=config)
    if context_loss_quarantine is not None:
        return {
            **context_loss_quarantine,
            "reason": context_loss_quarantine.get("reason") or "paper_context_loss_quarantine",
            "paper_only": True,
            "paper_fill_allowed": False,
            "guard": "paper_context_loss_quarantine",
            "candidate": _candidate_reference(candidate),
            "checks": [
                {
                    "code": context_loss_quarantine.get("reason") or "paper_context_loss_quarantine",
                    "field": "paper_context_loss_quarantine",
                }
            ],
        }

    route_eligibility = candidate.get("paper_route_eligibility") or {}
    if isinstance(route_eligibility, Mapping) and _as_bool(
        route_eligibility.get("suppressed"), False
    ):
        checks: list[dict[str, Any]] = []
        if (
            _is_short_frontier_spot(candidate)
            and not _is_confirmed_borrow(candidate)
            and (
                "spot_borrow_missing" in route_eligibility.get("blocker_reasons", [])
                or "spot_borrow" in _route_blockers(candidate)
            )
        ):
            checks.append(
                {
                    "code": SPOT_BORROW_SHADOW_CODE,
                    "field": "paper_route_eligibility",
                    "note": "short spot frontier paper fill requires confirmed borrow or equivalent hedge route",
                }
            )
        return {
            "reason": "paper_route_eligibility_blocked",
            "paper_only": True,
            "paper_fill_allowed": False,
            "guard": "paper_route_eligibility_gate",
            "candidate": _candidate_reference(candidate),
            "missing_prerequisites": route_eligibility.get("missing_prerequisites") or [],
            "blocker_reasons": route_eligibility.get("blocker_reasons") or [],
            "checks": checks,
            "route_eligibility": dict(route_eligibility),
        }

    family_reason = _paper_family_quarantine_reason(candidate, config=config)
    if family_reason is not None:
        normalized = dict(family_reason) if isinstance(family_reason, Mapping) else {"detail": family_reason}
        normalized.setdefault("reason", "paper_strategy_family_quarantine")
        normalized.setdefault("paper_only", True)
        normalized.setdefault("paper_fill_allowed", False)
        normalized.setdefault("guard", "paper_strategy_family_quarantine")
        normalized.setdefault("candidate", _candidate_reference(candidate))
        normalized.setdefault("cell", _paper_signal_cell(candidate))
        return normalized

    carry_gate = _paper_okx_basis_carry_gate(candidate, config=config)
    if carry_gate is not None and not _as_bool(carry_gate.get("eligible"), True):
        return {
            "reason": "paper_okx_basis_carry_gate",
            "paper_only": True,
            "paper_fill_allowed": False,
            "guard": "paper_okx_basis_carry_gate",
            "candidate": _candidate_reference(candidate),
            "cell": _paper_signal_cell(candidate),
            "checks": carry_gate.get("checks") or [],
            "failed_checks": carry_gate.get("failed_checks") or [],
            "conviction_cap": carry_gate.get("conviction_cap") or "hold",
            "basis_carry_gate": carry_gate,
        }
    if not frontier_paper_guard_enabled(config) or not is_frontier_crypto_candidate(candidate):
        return None

    admission_reason = frontier_paper_admission_reason(candidate, config)
    if admission_reason is not None:
        return admission_reason

    execution_quality = _paper_frontier_execution_quality_gate(candidate, config=config)
    if execution_quality is not None and not _as_bool(execution_quality.get("eligible"), True):
        return {
            "reason": "paper_frontier_execution_quality_gate",
            "paper_only": True,
            "paper_fill_allowed": False,
            "guard": "paper_frontier_execution_quality_gate",
            "candidate": _candidate_reference(candidate),
            "cell": _paper_signal_cell(candidate),
            "checks": execution_quality.get("checks") or [],
            "failed_checks": execution_quality.get("failed_checks") or [],
            "execution_quality": execution_quality,
        }
    checks: list[dict[str, Any]] = []
    edge_field, edge_bps = _first_float(candidate, ("edge_bps_estimate", "net_edge_bps_estimate", "edge_bps"))
    if edge_bps is not None and edge_bps <= 0.0:
        checks.append({"code": "non_positive_net_edge", "field": edge_field, "value": edge_bps})

    gross_field, gross_bps = _first_float(candidate, ("gross_edge_bps_estimate", "gross_edge_bps", "raw_edge_bps"))
    cost_field, cost_bps = _first_float(
        candidate,
        ("estimated_round_trip_cost_bps", "round_trip_cost_bps", "total_cost_bps"),
    )
    if gross_bps is not None and cost_bps is not None and gross_bps <= cost_bps:
        checks.append(
            {
                "code": "gross_edge_not_above_round_trip_cost",
                "gross_field": gross_field,
                "gross_edge_bps": gross_bps,
                "cost_field": cost_field,
                "round_trip_cost_bps": cost_bps,
            }
        )

    anomaly_flags = _coerce_flags(candidate.get("anomaly_flags"))
    if "simulated_slippage_exceeds_edge" in anomaly_flags:
        checks.append(
            {
                "code": "simulated_slippage_exceeds_edge",
                "field": "anomaly_flags",
                "value": sorted(anomaly_flags),
            }
        )

    quality_action = str(candidate.get("quality_action") or "").strip().lower().replace("-", "_")
    if quality_action == "shadow_only":
        checks.append({"code": "quality_action_shadow_only", "field": "quality_action", "value": candidate.get("quality_action")})

    route_blockers = _route_blockers(candidate)
    if (
        "spot_borrow" in route_blockers
        and _is_short_frontier_spot(candidate)
        and not _is_confirmed_borrow(candidate)
        and not _paper_assumption_route_allowed(candidate)
        and not _conditional_short_route_diagnostics_only(candidate)
    ):
        checks.append(
            {
                "code": SPOT_BORROW_SHADOW_CODE,
                "field": "route_blockers",
                "value": sorted(route_blockers),
                "note": "short spot frontier paper fill requires confirmed borrow or equivalent hedge route",
            }
        )
    if (
        frontier_route_feasibility_guard_enabled(config)
        and _is_short_frontier_spot(candidate)
        and not _conditional_short_route_diagnostics_only(candidate)
    ):
        route_record = frontier_route_feasibility_record(candidate)
        if not route_record.get("paper_fill_allowed", True):
            checks.append(
                {
                    "code": "route_not_paper_testable",
                    "field": "execution_feasibility",
                    "value": route_record.get("paper_route_status"),
                    "route_feasibility": {
                        "paper_route_status": route_record.get("paper_route_status"),
                        "execution_semantics": route_record.get("execution_semantics"),
                        "route_blockers": route_record.get("route_blockers"),
                        "alternative_status": route_record.get("alternative_status"),
                    },
                }
            )


    if not checks:
        return None

    return {
        "reason": FRONTIER_SHADOW_REASON,
        "paper_only": True,
        "paper_fill_allowed": False,
        "guard": "frontier_cost_or_quality_shadow_guard",
        "candidate": _candidate_reference(candidate),
        "cell": _paper_signal_cell(candidate),
        "checks": checks,
    }


def should_shadow_filter_frontier_candidate(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> bool:
    """Return true when a frontier candidate should not become paper_filled."""
    return frontier_shadow_filter_reason(candidate, config) is not None


def _annotate_shadow_filtered_candidate(
    guarded: dict[str, Any],
    reason: Mapping[str, Any],
    detail_field: str,
) -> dict[str, Any]:
    guarded["shadow_filtered"] = True
    guarded["paper_fill_allowed"] = False
    guarded["paper_action"] = "shadow_filtered"
    guarded["paper_status"] = "shadow_filtered"
    guarded["paper_fill_status"] = "shadow_filtered"
    guarded["paper_order_status"] = "shadow_filtered"
    guarded["router_action"] = "shadow_filtered"
    guarded["candidate_reject_reason"] = guarded.get("candidate_reject_reason") or reason.get("reason") or FRONTIER_SHADOW_REASON
    guarded["candidate_reject_detail"] = dict(reason)
    guarded[detail_field] = dict(reason)
    if reason.get("guard") in {
        "yahoo_proxy_cross_surface_alignment_guard",
        "paper_lineage_source_health",
        "paper_context_loss_quarantine",
        "paper_cross_family_portability_quarantine",
    }:
        guarded["paper_entry_blocked"] = True
        guarded["promotion_eligible"] = False
        guarded["paper_allocation_multiplier"] = 0.0
        guarded["emit_recommendation"] = False
        guarded["emit_route"] = False

    for boolean_field in ("paper_filled", "filled"):
        if guarded.get(boolean_field) is True:
            guarded[boolean_field] = False
    for status_field in ("status", "order_status", "fill_status"):
        if str(guarded.get(status_field) or "").lower() == "paper_filled":
            guarded[status_field] = "shadow_filtered"
    return guarded


def apply_frontier_paper_admission_guard(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any]:
    """Apply only the scanner-scoped paper-only frontier admission guard."""
    guarded = dict(candidate)
    reason = frontier_paper_admission_reason(guarded, config)
    if reason is None:
        return guarded
    return _annotate_shadow_filtered_candidate(
        guarded,
        reason,
        "frontier_paper_admission_guard",
    )


def _retain_research_only_route_candidate(
    guarded: dict[str, Any],
    reason: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Keep an explicitly research-only route visible without implying execution.

    Exploration mode converts this candidate to an isolated synthetic paper
    route later.  Outside exploration mode it remains non-fillable, but route
    limitations are diagnostics instead of a shadow-filtered strategy result.
    """
    route_record = frontier_route_feasibility_record(guarded)
    direct_status = _normalize_route_status(route_record.get("direct_route_status"))
    if direct_status not in _ROUTE_RESEARCH_ONLY_STATUSES:
        return None

    unmet_gates: list[str] = []
    for check in reason.get("checks") or []:
        if isinstance(check, Mapping) and check.get("code"):
            unmet_gates.append(str(check["code"]))
    for field in ("missing_prerequisites", "blocker_reasons"):
        unmet_gates.extend(str(item) for item in (reason.get(field) or []) if item)
    if not unmet_gates:
        unmet_gates.append("direct_route_research_only")

    guarded["paper_route_gate_diagnostic"] = {
        "paper_only": True,
        "action": "retain_for_synthetic_research",
        "direct_route_status": direct_status,
        "unmet_gates": list(dict.fromkeys(unmet_gates)),
        "route_feasibility": route_record,
        "would_block": dict(reason),
    }
    guarded["shadow_filtered"] = False
    guarded["paper_fill_allowed"] = False
    guarded["paper_action"] = "route_diagnostic"
    guarded["router_action"] = "route_diagnostic"
    guarded["candidate_status"] = "research_only_route_diagnostic"
    guarded["promotion_eligible"] = False
    guarded["_hunter_bucket"] = "diagnose"
    guarded.pop("candidate_reject_reason", None)
    guarded.pop("candidate_reject_detail", None)
    return guarded


def apply_frontier_paper_guard(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any]:
    """Return a copy of ``candidate`` annotated as shadow-filtered when needed."""
    route_guard_enabled = frontier_route_feasibility_guard_enabled(config)
    guarded = apply_paper_route_registry(candidate, config if isinstance(config, Mapping) else None)
    registry_gate = guarded.get("paper_route_registry") or {}
    existing_route_reason = frontier_shadow_filter_reason(guarded, config)
    score_gate = paper_route_feasibility_gate_review(guarded, config)
    guarded["paper_route_feasibility_gate"] = score_gate
    if score_gate["applies"] and not score_gate["eligible"]:
        guarded["paper_entry_blocked"] = True
        guarded["promotion_eligible"] = False
        guarded["paper_allocation_multiplier"] = 0.0
        reason = {
            "reason": score_gate["reason"],
            "paper_only": True,
            "paper_fill_allowed": False,
            "guard": "paper_route_feasibility_score_gate",
            "candidate": _candidate_reference(guarded),
            "route_feasibility": score_gate,
        }
        guarded = _annotate_shadow_filtered_candidate(
            guarded,
            reason,
            "paper_route_feasibility_guard",
        )
        if (
            isinstance(existing_route_reason, Mapping)
            and existing_route_reason.get("guard") == "paper_route_eligibility_gate"
        ):
            guarded["frontier_paper_guard"] = dict(existing_route_reason)
        return guarded
    if (
        isinstance(existing_route_reason, Mapping)
        and existing_route_reason.get("guard") == "paper_route_eligibility_gate"
    ):
        return _annotate_shadow_filtered_candidate(
            guarded,
            existing_route_reason,
            "frontier_paper_guard",
        )
    alignment_guard = _paper_yahoo_proxy_cross_surface_alignment_guard(guarded, config)
    if isinstance(alignment_guard, Mapping) and alignment_guard.get("applies"):
        guarded["yahoo_proxy_cross_surface_alignment_guard"] = dict(alignment_guard)
        guarded["local_cross_surface_confirmation"] = alignment_guard.get(
            "local_direction_confirmed"
        )
        if alignment_guard.get("quarantine_status") == "shadow_quarantined":
            guarded["candidate_status"] = "shadow_quarantined"
            guarded["paper_quarantine_status"] = "shadow_quarantined"
            guarded["paper_shadow_diagnostics"] = dict(alignment_guard.get("lineage_quarantine") or {})
            guarded["paper_execution_mode"] = "synthetic_paper"
            guarded["promotion_eligible"] = False
        elif not alignment_guard.get("eligible"):
            guarded["paper_entry_blocked"] = True
            guarded["promotion_eligible"] = False
            guarded["paper_allocation_multiplier"] = 0.0
            guarded["emit_recommendation"] = False
            guarded["emit_route"] = False
    guarded = _apply_paper_route_eligibility_metadata(guarded) if route_guard_enabled else guarded
    guarded = _apply_route_feasibility_metadata(guarded) if (
        is_frontier_crypto_candidate(guarded) and route_guard_enabled
    ) else guarded
    if isinstance(alignment_guard, Mapping) and alignment_guard.get("quarantine_status") == "shadow_quarantined":
        guarded["candidate_status"] = "shadow_quarantined"
        guarded["paper_quarantine_status"] = "shadow_quarantined"
        guarded["paper_shadow_diagnostics"] = dict(alignment_guard.get("lineage_quarantine") or {})
        guarded["paper_execution_mode"] = "synthetic_paper"
        guarded["promotion_eligible"] = False
        guarded["paper_fill_allowed"] = bool(
            guarded.get("paper_fill_allowed_by_route", guarded.get("paper_fill_allowed", True))
        )
    reason = frontier_shadow_filter_reason(guarded, config)
    if reason is not None:
        retained = _retain_research_only_route_candidate(guarded, reason)
        if retained is not None:
            return retained
        return _annotate_shadow_filtered_candidate(guarded, reason, "frontier_paper_guard")
    if registry_gate.get("action") == "suppress":
        reason = {
            "reason": "unsupported_paper_route",
            "paper_only": True,
            "paper_fill_allowed": False,
            "guard": "paper_route_registry_gate",
            "candidate": _candidate_reference(guarded),
            "route_registry": dict(registry_gate),
        }
        return _annotate_shadow_filtered_candidate(
            guarded,
            reason,
            "paper_route_registry_guard",
        )
    return guarded


def filter_frontier_paper_candidates(
    candidates: Iterable[Mapping[str, Any]],
    config: Mapping[str, Any] | bool | None = None,
) -> list[dict[str, Any]]:
    """Apply the paper-only frontier guard to a sequence of candidates."""
    return [apply_frontier_paper_guard(candidate, config) for candidate in candidates]


guard_frontier_paper_candidate = apply_frontier_paper_guard
frontier_candidate_shadow_filter_reason = frontier_shadow_filter_reason
