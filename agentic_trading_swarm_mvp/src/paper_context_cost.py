"""Market-context cost floor for paper-only proxy and frontier candidates.

The policy is deliberately independent from execution adapters.  It turns
public market context into an auditable hurdle and never places an order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


DEFAULT_PAPER_CONTEXT_COST_POLICY = {
    "enabled": True,
    "safety_multiplier": 1.25,
    "base_cost_bps": 1.0,
    "spread_weight": 1.0,
    "max_liquidity_penalty_bps": 6.0,
    "volatility_weight": 0.08,
    "max_volatility_penalty_bps": 12.0,
    "freshness_penalty_bps_per_window": 2.0,
    "max_freshness_penalty_bps": 12.0,
    "frontier_freshness_window_seconds": 30.0,
    "proxy_freshness_window_seconds": 900.0,
    "extra_leg_cost_bps": 4.0,
    "missing_spread_penalty_bps": 4.0,
    "missing_liquidity_penalty_bps": 3.0,
    "missing_freshness_penalty_bps": 2.0,
    "minimum_score_multiplier": 0.5,
}

_PAPER_CONTEXT_FAMILIES = {"global_proxy_momentum", "frontier_crypto_venue_map"}


def _finite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_number(candidate: Mapping[str, Any], *fields: str) -> tuple[str | None, float | None]:
    for field in fields:
        value = _finite_float(candidate.get(field))
        if value is not None:
            return field, value
    return None, None


def paper_context_family(candidate: Mapping[str, Any]) -> str | None:
    """Return the supported paper family without broadening into live routes."""
    for field in ("trade_type", "market_surface", "signal_family", "strategy", "signal_key"):
        text = str(candidate.get(field) or "").lower()
        for family in _PAPER_CONTEXT_FAMILIES:
            if family in text:
                return family
    return None


def _policy(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_PAPER_CONTEXT_COST_POLICY)
    configured: Mapping[str, Any] | None = None
    if isinstance(settings, Mapping):
        risk = settings.get("risk")
        if isinstance(risk, Mapping) and isinstance(risk.get("paper_context_cost_floor"), Mapping):
            merged.update(risk["paper_context_cost_floor"])
        configured = settings.get("paper_context_cost_floor")
    if isinstance(configured, Mapping):
        merged.update(configured)
    return merged


def _freshness_age_seconds(candidate: Mapping[str, Any]) -> tuple[str | None, float | None]:
    field, age = _first_number(
        candidate,
        "freshness_age_seconds",
        "provider_age_seconds",
        "quote_age_seconds",
        "data_age_seconds",
    )
    if age is not None:
        return field, max(0.0, age)
    stale_minutes = _finite_float(candidate.get("stale_minutes"))
    if stale_minutes is not None:
        return "stale_minutes", max(0.0, stale_minutes * 60.0)
    return None, None


def _leg_count(candidate: Mapping[str, Any]) -> int:
    _, explicit = _first_number(candidate, "execution_leg_count", "leg_count", "estimated_leg_count")
    if explicit is not None:
        return max(1, int(math.ceil(explicit)))
    legs = candidate.get("legs")
    if isinstance(legs, (list, tuple)) and legs:
        return len(legs)
    feasibility = candidate.get("execution_feasibility")
    if isinstance(feasibility, Mapping):
        legs = feasibility.get("legs")
        if isinstance(legs, (list, tuple)) and legs:
            return len(legs)
    return 1


def paper_context_cost_gate(
    candidate: Mapping[str, Any],
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute the paper-only gross-edge hurdle and score multiplier.

    Known round-trip execution cost is preserved as the base.  Spread is used
    as the fallback execution cost, while freshness, thinness, volatility and
    additional legs are conservative premiums on top.
    """
    family = paper_context_family(candidate)
    policy = _policy(settings)
    paper_mode = str((settings or {}).get("mode", "paper")).lower() == "paper"
    enabled = bool(policy.get("enabled", True)) and paper_mode
    if family is None:
        return {
            "paper_only": True,
            "applicable": False,
            "enabled": enabled,
            "eligible": True,
            "score_multiplier": 1.0,
            "reasons": [],
        }

    gross_field, gross_edge = _first_number(
        candidate,
        "gross_edge_bps_estimate",
        "expected_gross_edge_bps",
        "gross_edge_bps",
        "raw_edge_bps",
    )
    if gross_edge is None:
        gross_field, gross_edge = _first_number(
            candidate,
            "expected_edge_bps",
            "edge_bps_estimate",
            "net_edge_bps_estimate",
            "edge_bps",
        )

    spread_field, spread = _first_number(
        candidate,
        "spread_bps",
        "effective_spread_bps",
        "quoted_spread_bps",
    )
    liquidity_field, liquidity = _first_number(
        candidate,
        "liquidity_score",
        "depth_liquidity_score",
        "liquidity_proxy",
    )
    depth_liquidity = _finite_float(candidate.get("depth_liquidity_score"))
    if depth_liquidity is not None and (liquidity is None or depth_liquidity < liquidity):
        liquidity_field, liquidity = "depth_liquidity_score", depth_liquidity
    volatility_field, volatility = _first_number(
        candidate,
        "recent_volatility_bps",
        "realized_volatility_bps",
        "volatility_bps",
    )
    freshness_field, freshness_age = _freshness_age_seconds(candidate)
    cost_field, modeled_cost = _first_number(
        candidate,
        "estimated_round_trip_cost_bps",
        "round_trip_cost_bps",
        "total_cost_bps",
    )

    spread_component = (
        max(0.0, spread) * float(policy["spread_weight"])
        if spread is not None
        else float(policy["missing_spread_penalty_bps"])
    )
    execution_component = max(
        float(policy["base_cost_bps"]) + spread_component,
        max(0.0, modeled_cost or 0.0),
    )
    liquidity_component = (
        max(0.0, min(1.0, 1.0 - liquidity)) * float(policy["max_liquidity_penalty_bps"])
        if liquidity is not None
        else float(policy["missing_liquidity_penalty_bps"])
    )
    freshness_window = float(
        policy["proxy_freshness_window_seconds"]
        if family == "global_proxy_momentum"
        else policy["frontier_freshness_window_seconds"]
    )
    if freshness_age is None:
        freshness_component = float(policy["missing_freshness_penalty_bps"])
    else:
        overdue_windows = max(0.0, (freshness_age / max(freshness_window, 1.0)) - 1.0)
        freshness_component = min(
            float(policy["max_freshness_penalty_bps"]),
            overdue_windows * float(policy["freshness_penalty_bps_per_window"]),
        )
    volatility_component = min(
        float(policy["max_volatility_penalty_bps"]),
        max(0.0, volatility or 0.0) * float(policy["volatility_weight"]),
    )
    leg_count = _leg_count(candidate)
    complexity_component = max(0, leg_count - 1) * float(policy["extra_leg_cost_bps"])
    floor = (
        execution_component
        + liquidity_component
        + freshness_component
        + volatility_component
        + complexity_component
    )
    safety_multiplier = max(1.0, float(policy["safety_multiplier"]))
    required_edge = floor * safety_multiplier
    eligible = bool(enabled is False or (gross_edge is not None and gross_edge > required_edge))

    reasons: list[str] = []
    if gross_edge is None:
        reasons.append("missing_expected_gross_edge")
    elif gross_edge <= required_edge:
        reasons.append("gross_edge_does_not_clear_context_cost_floor")
    if spread is None:
        reasons.append("missing_spread_proxy")
    if liquidity is None:
        reasons.append("missing_liquidity_proxy")
    elif liquidity < 0.35:
        reasons.append("thin_liquidity_proxy")
    if freshness_age is None:
        reasons.append("missing_freshness_age")
    elif freshness_age > freshness_window:
        reasons.append("stale_market_context")

    if not enabled:
        score_multiplier = 1.0
    elif not eligible:
        score_multiplier = float(policy["minimum_score_multiplier"])
    else:
        edge_ratio = gross_edge / max(required_edge, 0.001)
        edge_multiplier = min(1.0, 0.75 + 0.25 * max(0.0, edge_ratio - 1.0))
        liquidity_multiplier = (
            1.0
            if liquidity is None
            else 0.75 + 0.25 * max(0.0, min(1.0, liquidity))
        )
        freshness_multiplier = 1.0
        if freshness_age is not None and freshness_age > freshness_window:
            freshness_multiplier = max(0.6, 1.0 - 0.08 * ((freshness_age / freshness_window) - 1.0))
        score_multiplier = max(
            float(policy["minimum_score_multiplier"]),
            min(edge_multiplier, liquidity_multiplier, freshness_multiplier),
        )

    return {
        "paper_only": True,
        "applicable": True,
        "enabled": enabled,
        "family": family,
        "eligible": eligible,
        "gross_edge_field": gross_field,
        "gross_edge_bps": round(gross_edge, 3) if gross_edge is not None else None,
        "context_cost_floor_bps": round(floor, 3),
        "safety_multiplier": round(safety_multiplier, 4),
        "required_gross_edge_bps": round(required_edge, 3),
        "score_multiplier": round(score_multiplier, 4),
        "reasons": reasons,
        "inputs": {
            "spread_field": spread_field,
            "spread_bps": spread,
            "liquidity_field": liquidity_field,
            "liquidity_score": liquidity,
            "freshness_field": freshness_field,
            "freshness_age_seconds": freshness_age,
            "freshness_window_seconds": freshness_window,
            "volatility_field": volatility_field,
            "recent_volatility_bps": volatility,
            "modeled_cost_field": cost_field,
            "modeled_round_trip_cost_bps": modeled_cost,
            "leg_count": leg_count,
        },
        "components_bps": {
            "execution": round(execution_component, 3),
            "liquidity": round(liquidity_component, 3),
            "freshness": round(freshness_component, 3),
            "volatility": round(volatility_component, 3),
            "complexity": round(complexity_component, 3),
        },
    }


def annotate_paper_context_cost(
    candidate: Mapping[str, Any],
    settings: Mapping[str, Any] | None = None,
    *,
    adjust_score: bool = True,
) -> dict[str, Any]:
    """Return an audited candidate copy, optionally down-ranking its score."""
    annotated = dict(candidate)
    gate = paper_context_cost_gate(annotated, settings)
    if not gate.get("applicable"):
        return annotated
    annotated["paper_context_cost_gate"] = gate
    annotated["context_cost_floor_bps"] = gate["context_cost_floor_bps"]
    annotated["required_gross_edge_bps"] = gate["required_gross_edge_bps"]
    annotated["paper_context_score_multiplier"] = gate["score_multiplier"]
    if adjust_score and annotated.get("score") is not None:
        raw_score = float(annotated["score"])
        annotated["score_before_context_cost"] = round(raw_score, 3)
        annotated["score"] = round(raw_score * float(gate["score_multiplier"]), 3)
    notes = list(annotated.get("risk_notes") or [])
    marker = "paper-only gross edge must clear the market-context cost floor"
    if marker not in notes:
        notes.append(marker)
    annotated["risk_notes"] = notes
    return annotated


def enforce_paper_context_cost_gate(
    candidate: Mapping[str, Any],
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed at the paper-fill boundary when the context hurdle fails."""
    annotated = annotate_paper_context_cost(candidate, settings, adjust_score=False)
    gate = annotated.get("paper_context_cost_gate") or {}
    if not gate.get("applicable") or not gate.get("enabled") or gate.get("eligible"):
        return annotated
    detail = {
        "reason": "paper_context_cost_floor_not_cleared",
        "paper_only": True,
        "paper_fill_allowed": False,
        "guard": "paper_context_cost_floor",
        "context_cost_gate": gate,
    }
    annotated.update(
        {
            "shadow_filtered": True,
            "paper_fill_allowed": False,
            "paper_action": "shadow_filtered",
            "paper_status": "shadow_filtered",
            "paper_fill_status": "shadow_filtered",
            "paper_order_status": "shadow_filtered",
            "router_action": "shadow_filtered",
            "candidate_reject_reason": "paper_context_cost_floor_not_cleared",
            "candidate_reject_detail": detail,
            "paper_context_cost_rejection": detail,
        }
    )
    for boolean_field in ("paper_filled", "filled"):
        if annotated.get(boolean_field) is True:
            annotated[boolean_field] = False
    return annotated
