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


FRONTIER_MARKER = "frontier_crypto_venue_map"
FRONTIER_SHADOW_REASON = "frontier_shadow_filtered"
SPOT_BORROW_SHADOW_CODE = "spot_borrow_unconfirmed"

_FLAG_KEYS = (
    "frontier_shadow_guard_enabled",
    "frontier_paper_shadow_guard_enabled",
    "paper_frontier_shadow_guard_enabled",
    "paper_frontier_candidate_guard_enabled",
)
_FLAG_SCOPES = ("paper_order_router", "paper", "frontier_crypto_adapter", "frontier")
_FALSE_VALUES = {"0", "false", "f", "no", "n", "off", "disabled"}
_TRUE_VALUES = {"1", "true", "t", "yes", "y", "on", "enabled"}


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
    for field in ("route_blockers", "missing_requirements", "missing_permissions"):
        blockers.update(_coerce_flags(candidate.get(field)))
    route = candidate.get("execution_route")
    if isinstance(route, Mapping):
        for field in ("route_blockers", "missing_requirements", "missing_permissions"):
            blockers.update(_coerce_flags(route.get(field)))
    feasibility = candidate.get("execution_feasibility")
    if isinstance(feasibility, Mapping):
        for field in ("route_blockers", "missing_requirements", "missing_permissions"):
            blockers.update(_coerce_flags(feasibility.get(field)))
    return {item for item in blockers if item}


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
            and "spot_borrow" in _coerce_flags(alternative.get("replaces_blockers"))
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
    for field in ("inst_id", "instrument_id", "symbol", "venue", "signal_key", "trade_type", "market_surface"):
        if candidate.get(field) not in (None, ""):
            reference[field] = candidate.get(field)
    return reference


def frontier_shadow_filter_reason(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    """Return a structured paper-only shadow-filter reason, or ``None``.

    The guard is intentionally narrow: only frontier crypto venue-map candidates
    are considered, and verified positive-net candidates with no slippage/quality
    blocker remain eligible for paper fills.
    """
    if not frontier_paper_guard_enabled(config) or not is_frontier_crypto_candidate(candidate):
        return None

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
    if "spot_borrow" in route_blockers and _is_short_frontier_spot(candidate) and not _is_confirmed_borrow(candidate):
        checks.append(
            {
                "code": SPOT_BORROW_SHADOW_CODE,
                "field": "route_blockers",
                "value": sorted(route_blockers),
                "note": "short spot frontier paper fill requires confirmed borrow or equivalent hedge route",
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
        "checks": checks,
    }


def should_shadow_filter_frontier_candidate(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> bool:
    """Return true when a frontier candidate should not become paper_filled."""
    return frontier_shadow_filter_reason(candidate, config) is not None


def apply_frontier_paper_guard(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any]:
    """Return a copy of ``candidate`` annotated as shadow-filtered when needed."""
    guarded = dict(candidate)
    reason = frontier_shadow_filter_reason(guarded, config)
    if reason is None:
        return guarded

    guarded["shadow_filtered"] = True
    guarded["paper_fill_allowed"] = False
    guarded["paper_action"] = "shadow_filtered"
    guarded["paper_status"] = "shadow_filtered"
    guarded["paper_fill_status"] = "shadow_filtered"
    guarded["paper_order_status"] = "shadow_filtered"
    guarded["router_action"] = "shadow_filtered"
    guarded["candidate_reject_reason"] = guarded.get("candidate_reject_reason") or FRONTIER_SHADOW_REASON
    guarded["candidate_reject_detail"] = reason
    guarded["frontier_paper_guard"] = reason

    for boolean_field in ("paper_filled", "filled"):
        if guarded.get(boolean_field) is True:
            guarded[boolean_field] = False
    for status_field in ("status", "order_status", "fill_status"):
        if str(guarded.get(status_field) or "").lower() == "paper_filled":
            guarded[status_field] = "shadow_filtered"

    return guarded


def filter_frontier_paper_candidates(
    candidates: Iterable[Mapping[str, Any]],
    config: Mapping[str, Any] | bool | None = None,
) -> list[dict[str, Any]]:
    """Apply the paper-only frontier guard to a sequence of candidates."""
    return [apply_frontier_paper_guard(candidate, config) for candidate in candidates]


guard_frontier_paper_candidate = apply_frontier_paper_guard
frontier_candidate_shadow_filter_reason = frontier_shadow_filter_reason
