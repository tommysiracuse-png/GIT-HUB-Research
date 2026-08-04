"""Market-context cost floor for paper-only proxy and frontier candidates.

The policy is deliberately independent from execution adapters.  It turns
public market context into an auditable hurdle and never places an order.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


DEFAULT_PAPER_CONTEXT_COST_POLICY = {
    "enabled": True,
    "min_net_edge_buffer_bps": 2.0,
    "safety_multiplier": 1.25,
    "base_cost_bps": 1.0,
    "spread_weight": 1.0,
    "default_half_spread_bps": 2.0,
    "default_slippage_bps": 3.0,
    "surface_costs": {
        "proxy": {"slippage_bps_per_side": 1.5, "fee_bps_per_side": 0.5},
        "frontier": {"slippage_bps_per_side": 3.0, "fee_bps_per_side": 1.0},
        "carry": {"slippage_bps_per_side": 2.0, "fee_bps_per_side": 1.0},
    },
    "default_latency_decay_bps": 0.0,
    "latency_decay_bps_per_window": 1.0,
    "max_latency_decay_bps": 12.0,
    "default_carry_bps_horizon": 0.0,
    "missing_carry_bps_horizon": 2.0,
    "carry_horizon_hours": 8.0,
    "default_volatility_tail_buffer_bps": 1.0,
    "frontier_tail_buffer_bps": 2.0,
    "gap_risk_weight": 0.10,
    "max_gap_risk_buffer_bps": 10.0,
    "funding_instability_weight": 0.50,
    "max_funding_instability_buffer_bps": 8.0,
    "max_liquidity_penalty_bps": 6.0,
    "volatility_weight": 0.08,
    "max_volatility_penalty_bps": 12.0,
    "freshness_penalty_bps_per_window": 2.0,
    "max_freshness_penalty_bps": 12.0,
    "frontier_freshness_window_seconds": 30.0,
    "proxy_freshness_window_seconds": 900.0,
    "carry_freshness_window_seconds": 30.0,
    "frontier_max_signal_age_seconds": 90.0,
    "proxy_max_signal_age_seconds": 900.0,
    "carry_max_signal_age_seconds": 30.0,
    "extra_leg_cost_bps": 4.0,
    "missing_spread_penalty_bps": 4.0,
    "missing_liquidity_penalty_bps": 3.0,
    "missing_freshness_penalty_bps": 2.0,
    "conditional_route_penalty_bps": 4.0,
    "paper_proxy_route_penalty_bps": 6.0,
    "unknown_route_penalty_bps": 8.0,
    "proxy_short_min_liquidity_score": 0.65,
    "proxy_short_max_freshness_age_seconds": 900.0,
    "frontier_long_min_liquidity_score": 0.35,
    "frontier_long_max_freshness_age_seconds": 90.0,
    "minimum_score_multiplier": 0.5,
}

_PAPER_CONTEXT_FAMILIES = {
    "global_proxy_momentum": "proxy",
    "global_market_discovery_proxy": "proxy",
    "global_proxy_shock_reversal": "proxy",
    "frontier_crypto_venue_map": "frontier",
    "perp_funding_basis": "carry",
}
_BLOCKED_ROUTE_STATUSES = {
    "blocked",
    "blocked_for_paper_route",
    "route_unknown",
    "unknown",
    "unavailable",
    "watch_only",
}
_CONDITIONAL_ROUTE_STATUSES = {"conditional", "paper_testable_proxy"}
_PAPER_PROXY_ROUTE_STATUSES = {"paper_proxy", "proxy", "synthetic", "simulated"}


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


def _surface_cost_policy(policy: Mapping[str, Any], family_kind: str | None) -> dict[str, float]:
    """Return conservative per-side defaults for the candidate surface."""
    defaults = DEFAULT_PAPER_CONTEXT_COST_POLICY["surface_costs"].get(family_kind or "", {})
    configured_surfaces = policy.get("surface_costs")
    configured = (
        configured_surfaces.get(family_kind or "", {})
        if isinstance(configured_surfaces, Mapping)
        else {}
    )
    configured_slippage = _finite_float(configured.get("slippage_bps_per_side"))
    configured_fee = _finite_float(configured.get("fee_bps_per_side"))
    default_slippage = _finite_float(defaults.get("slippage_bps_per_side"))
    default_fee = _finite_float(defaults.get("fee_bps_per_side"))
    return {
        "slippage_bps_per_side": max(
            0.0,
            configured_slippage
            if configured_slippage is not None
            else default_slippage
            if default_slippage is not None
            else float(policy["default_slippage_bps"]) / 2.0,
        ),
        "fee_bps_per_side": max(
            0.0,
            configured_fee
            if configured_fee is not None
            else default_fee
            if default_fee is not None
            else float(policy["base_cost_bps"]) / 2.0,
        ),
    }


def _freshness_age_seconds(candidate: Mapping[str, Any]) -> tuple[str | None, float | None]:
    field, age = _first_number(
        candidate,
        "freshness_age_seconds",
        "provider_age_seconds",
        "quote_age_seconds",
        "data_age_seconds",
        "signal_age_seconds",
    )
    if age is not None:
        return field, max(0.0, age)
    stale_minutes = _finite_float(candidate.get("stale_minutes"))
    if stale_minutes is not None:
        return "stale_minutes", max(0.0, stale_minutes * 60.0)
    return None, None


def _funding_drag_bps(candidate: Mapping[str, Any], policy: Mapping[str, Any]) -> float:
    """Return conservative funding/borrow drag over the configured horizon."""
    explicit_field, explicit = _first_number(
        candidate,
        "carry_bps_horizon",
        "expected_carry_cost_bps",
        "funding_drag_bps_horizon",
        "borrow_cost_bps_horizon",
    )
    if explicit_field is not None and explicit is not None:
        return max(0.0, explicit)

    funding = _finite_float(candidate.get("funding_bps"))
    if funding is None:
        return float(policy["missing_carry_bps_horizon"])
    direction = str(candidate.get("direction") or "").lower()
    pays_funding = ("long_perp" in direction and funding > 0.0) or (
        "short_perp" in direction and funding < 0.0
    )
    if not pays_funding:
        return 0.0
    interval_hours = max(0.001, _finite_float(candidate.get("funding_interval_hours")) or 8.0)
    horizon_hours = max(0.0, float(policy["carry_horizon_hours"]))
    return abs(funding) * horizon_hours / interval_hours


def _funding_instability_bps(candidate: Mapping[str, Any]) -> float:
    explicit = _finite_float(candidate.get("funding_instability_bps"))
    if explicit is not None:
        return max(0.0, explicit)
    low = _finite_float(candidate.get("funding_history_min_bps"))
    high = _finite_float(candidate.get("funding_history_max_bps"))
    if low is not None and high is not None:
        return abs(high - low)
    slope = _finite_float(candidate.get("funding_history_slope_bps"))
    return abs(slope) if slope is not None else 0.0


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
    direction = str(candidate.get("direction") or "").lower()
    if "perp" in direction and "spot" in direction:
        return 2
    return 1


def _route_context(candidate: Mapping[str, Any]) -> tuple[str, str | None, float | None]:
    containers: list[Any] = [
        candidate,
        candidate.get("frontier_route_feasibility"),
        candidate.get("execution_feasibility"),
        candidate.get("execution_route"),
        candidate.get("route_intelligence"),
        candidate.get("paper_route_eligibility"),
    ]
    for parent in tuple(containers):
        if isinstance(parent, Mapping):
            containers.append(parent.get("paper_route_eligibility"))
    route_status = "unknown"
    cost_field: str | None = None
    route_cost: float | None = None
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        if route_status == "unknown":
            for field in ("paper_route_status", "route_status", "status", "execution_status"):
                value = str(container.get(field) or "").strip().lower().replace("-", "_").replace(" ", "_")
                if value:
                    route_status = value
                    break
        if route_cost is None:
            for field in (
                "route_cost_bps_paper",
                "paper_route_cost_bps",
                "estimated_route_cost_bps",
                "total_route_cost_bps",
                "assumed_route_cost_bps",
                "route_friction_bps",
            ):
                value = _finite_float(container.get(field))
                if value is not None:
                    cost_field = field
                    route_cost = max(0.0, value)
                    break
    return route_status, cost_field, route_cost


def _quality_floor(
    candidate: Mapping[str, Any],
    family_kind: str,
    policy: Mapping[str, Any],
    liquidity: float | None,
    freshness_age: float | None,
) -> dict[str, Any]:
    direction = str(candidate.get("direction") or "").strip().lower()
    if family_kind == "proxy" and direction == "short_proxy":
        floor_name = "proxy_short"
    elif family_kind == "frontier" and direction.startswith("long_frontier"):
        floor_name = "frontier_long"
    else:
        return {"applies": False, "passed": True, "reasons": []}

    min_liquidity = float(policy[f"{floor_name}_min_liquidity_score"])
    max_freshness = float(policy[f"{floor_name}_max_freshness_age_seconds"])
    reasons: list[str] = []
    if liquidity is None:
        reasons.append("missing_minimum_liquidity_evidence")
    elif liquidity < min_liquidity:
        reasons.append("liquidity_below_promotion_floor")
    if freshness_age is None:
        reasons.append("missing_minimum_freshness_evidence")
    elif freshness_age > max_freshness:
        reasons.append("freshness_above_promotion_ceiling")
    return {
        "applies": True,
        "name": floor_name,
        "passed": not reasons,
        "min_liquidity_score": min_liquidity,
        "max_freshness_age_seconds": max_freshness,
        "reasons": reasons,
    }


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
    family_kind = _PAPER_CONTEXT_FAMILIES.get(family or "")
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
        "predicted_edge_bps",
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

    half_spread_field, explicit_half_spread = _first_number(
        candidate,
        "round_trip_spread_bps",
        "half_spread_bps",
    )
    if explicit_half_spread is not None:
        spread_component = max(0.0, explicit_half_spread)
    elif spread is not None:
        # Crossing into and out of a position consumes two half-spreads.
        spread_component = max(0.0, spread) * float(policy["spread_weight"])
    else:
        spread_component = float(policy["default_half_spread_bps"]) * 2.0

    surface_costs = _surface_cost_policy(policy, family_kind)
    slippage_field, explicit_slippage = _first_number(
        candidate,
        "round_trip_slippage_bps",
        "slippage_bps",
        "estimated_slippage_bps",
        "expected_slippage_bps",
    )
    entry_slippage_field, entry_slippage = _first_number(
        candidate,
        "entry_slippage_bps_estimate",
        "entry_slippage_bps",
    )
    exit_slippage_field, exit_slippage = _first_number(
        candidate,
        "exit_slippage_bps_estimate",
        "exit_slippage_bps",
    )
    liquidity_penalty = (
        max(0.0, min(1.0, 1.0 - liquidity)) * float(policy["max_liquidity_penalty_bps"])
        if liquidity is not None
        else float(policy["missing_liquidity_penalty_bps"])
    )
    leg_count = _leg_count(candidate)
    fee_field, explicit_round_trip_fee = _first_number(
        candidate,
        "round_trip_fee_bps",
        "estimated_round_trip_fee_bps",
        "total_fee_bps",
    )
    fee_per_side_field, explicit_fee_per_side = _first_number(
        candidate,
        "estimated_fee_bps_per_side",
        "fee_bps_per_side",
        "taker_fee_bps",
    )
    entry_fee_field, entry_fee = _first_number(candidate, "entry_fee_bps_estimate", "entry_fee_bps")
    exit_fee_field, exit_fee = _first_number(candidate, "exit_fee_bps_estimate", "exit_fee_bps")
    if explicit_round_trip_fee is not None:
        fee_component = max(0.0, explicit_round_trip_fee)
    elif entry_fee is not None or exit_fee is not None:
        fee_component = max(0.0, entry_fee or 0.0) + max(0.0, exit_fee or 0.0)
    elif explicit_fee_per_side is not None:
        fee_component = max(0.0, explicit_fee_per_side) * 2.0 * leg_count
    elif explicit_half_spread is not None and explicit_slippage is not None:
        # Legacy callers that provide both execution components have already
        # specified their complete cost fixture; retain that contract.
        fee_component = 0.0
    else:
        fee_component = surface_costs["fee_bps_per_side"] * 2.0 * leg_count
    route_status, route_cost_field, route_cost = _route_context(candidate)
    if route_status in _CONDITIONAL_ROUTE_STATUSES:
        route_status_penalty = float(policy["conditional_route_penalty_bps"])
    elif route_status in _PAPER_PROXY_ROUTE_STATUSES:
        route_status_penalty = float(policy["paper_proxy_route_penalty_bps"])
    elif route_status in _BLOCKED_ROUTE_STATUSES:
        route_status_penalty = float(policy["unknown_route_penalty_bps"])
    else:
        route_status_penalty = 0.0
    if explicit_slippage is not None:
        slippage_component = max(0.0, explicit_slippage)
    elif entry_slippage is not None or exit_slippage is not None:
        slippage_component = (
            max(0.0, entry_slippage if entry_slippage is not None else surface_costs["slippage_bps_per_side"])
            + max(0.0, exit_slippage if exit_slippage is not None else surface_costs["slippage_bps_per_side"])
        )
    else:
        slippage_component = surface_costs["slippage_bps_per_side"] * 2.0 * leg_count
    slippage_component += liquidity_penalty
    complexity_component = max(0, leg_count - 1) * float(policy["extra_leg_cost_bps"])
    slippage_component += complexity_component
    modeled_slippage_floor = max(
        0.0,
        (modeled_cost or 0.0) - spread_component - fee_component,
    )
    slippage_component = max(slippage_component, modeled_slippage_floor)
    route_cost_increment = max(
        0.0,
        (route_cost or 0.0) - spread_component - slippage_component - fee_component,
    )
    route_component = route_cost_increment + route_status_penalty
    slippage_component += route_component

    freshness_key = {
        "proxy": "proxy_freshness_window_seconds",
        "carry": "carry_freshness_window_seconds",
    }.get(family_kind, "frontier_freshness_window_seconds")
    freshness_window = max(1.0, float(policy[freshness_key]))
    latency_field, explicit_latency = _first_number(
        candidate,
        "latency_decay_bps",
        "expected_latency_decay_bps",
    )
    if explicit_latency is not None:
        latency_decay_component = max(0.0, explicit_latency)
    else:
        latency_decay_component = float(policy["default_latency_decay_bps"])
        if freshness_age is None:
            latency_decay_component += float(policy["missing_freshness_penalty_bps"])
        else:
            latency_decay_component += min(
                float(policy["max_latency_decay_bps"]),
                (freshness_age / freshness_window) * float(policy["latency_decay_bps_per_window"]),
            )

    carry_component = (
        _funding_drag_bps(candidate, policy)
        if family_kind == "carry"
        else max(
            0.0,
            _finite_float(candidate.get("carry_bps_horizon"))
            or float(policy["default_carry_bps_horizon"]),
        )
    )
    tail_field, explicit_tail = _first_number(
        candidate,
        "volatility_tail_buffer_bps",
        "tail_risk_buffer_bps",
    )
    volatility_tail_component = (
        max(0.0, explicit_tail)
        if explicit_tail is not None
        else max(
            float(policy["default_volatility_tail_buffer_bps"]),
            min(
                float(policy["max_volatility_penalty_bps"]),
                max(0.0, volatility or 0.0) * float(policy["volatility_weight"]),
            ),
        )
    )
    gap_field, gap_risk = _first_number(
        candidate,
        "recent_gap_bps",
        "max_recent_gap_bps",
        "gap_risk_bps",
    )
    gap_buffer = min(
        float(policy["max_gap_risk_buffer_bps"]),
        abs(gap_risk or 0.0) * float(policy["gap_risk_weight"]),
    )
    funding_instability = _funding_instability_bps(candidate) if family_kind == "carry" else 0.0
    funding_instability_buffer = min(
        float(policy["max_funding_instability_buffer_bps"]),
        funding_instability * float(policy["funding_instability_weight"]),
    )
    frontier_buffer = float(policy["frontier_tail_buffer_bps"]) if family_kind == "frontier" else 0.0
    volatility_tail_component += gap_buffer + funding_instability_buffer + frontier_buffer

    safety_multiplier = max(1.0, float(policy["safety_multiplier"]))
    pre_safety_cost = (
        spread_component
        + slippage_component
        + fee_component
        + latency_decay_component
        + carry_component
        + volatility_tail_component
    )
    volatility_tail_component += pre_safety_cost * (safety_multiplier - 1.0)
    effective_cost = (
        spread_component
        + slippage_component
        + fee_component
        + latency_decay_component
        + carry_component
        + volatility_tail_component
    )
    min_net_edge_buffer = max(0.0, float(policy["min_net_edge_buffer_bps"]))
    required_edge = effective_cost + min_net_edge_buffer
    gate_margin = None if gross_edge is None else gross_edge - required_edge
    max_age_key = {
        "proxy": "proxy_max_signal_age_seconds",
        "carry": "carry_max_signal_age_seconds",
    }.get(family_kind, "frontier_max_signal_age_seconds")
    max_signal_age = max(0.0, float(policy[max_age_key]))
    age_passed = freshness_age is not None and freshness_age < max_signal_age
    quality_floor = _quality_floor(candidate, family_kind or "", policy, liquidity, freshness_age)
    route_blocked = route_status in _BLOCKED_ROUTE_STATUSES
    eligible = bool(
        enabled is False
        or (
            gross_edge is not None
            and gross_edge > required_edge
            and age_passed
        )
    )

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
    elif freshness_age >= max_signal_age:
        reasons.append("signal_age_limit_exceeded")
    elif freshness_age > freshness_window:
        reasons.append("stale_market_context")
    reasons.extend(quality_floor["reasons"])
    if route_blocked:
        reasons.append("route_status_not_paper_promotable")

    if gross_edge is None:
        veto_reason = "missing_predicted_edge"
    elif freshness_age is None:
        veto_reason = "missing_signal_age"
    elif not age_passed:
        veto_reason = "signal_too_old"
    elif gross_edge <= required_edge:
        veto_reason = "effective_cost_exceeds_edge"
    else:
        veto_reason = None

    if not enabled:
        gating_reason = "paper_context_cost_gate_disabled"
    elif veto_reason is not None:
        gating_reason = veto_reason
    elif route_blocked:
        gating_reason = "route_status_not_paper_promotable"
    else:
        gating_reason = "gross_edge_clears_modeled_cost_and_buffer"

    net_edge = None if gross_edge is None else gross_edge - effective_cost
    freshness_minutes = None if freshness_age is None else freshness_age / 60.0

    if not enabled:
        score_multiplier = 1.0
    elif not eligible:
        score_multiplier = float(policy["minimum_score_multiplier"])
    else:
        edge_ratio = max(0.0, gate_margin or 0.0) / max(required_edge, 0.001)
        edge_multiplier = min(1.0, 0.75 + 0.25 * edge_ratio)
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
        "family_kind": family_kind,
        "eligible": eligible,
        "paper_eligible": eligible,
        "predicted_edge_bps": round(gross_edge, 3) if gross_edge is not None else None,
        "effective_cost_bps": round(effective_cost, 3),
        "min_net_edge_buffer_bps": round(min_net_edge_buffer, 3),
        "gate_margin_bps": round(gate_margin, 3) if gate_margin is not None else None,
        "signal_age_seconds": round(freshness_age, 3) if freshness_age is not None else None,
        "max_signal_age_seconds": round(max_signal_age, 3),
        "carry_bps_horizon": round(carry_component, 3),
        "spread_proxy_bps": round(spread, 3) if spread is not None else None,
        "veto_reason": veto_reason,
        "gross_edge_field": gross_field,
        "gross_edge_bps": round(gross_edge, 3) if gross_edge is not None else None,
        "modeled_cost_bps": round(effective_cost, 3),
        "net_edge_bps": round(net_edge, 3) if net_edge is not None else None,
        "freshness_minutes": round(freshness_minutes, 3) if freshness_minutes is not None else None,
        "gating_reason": gating_reason,
        "context_cost_floor_bps": round(effective_cost, 3),
        "safety_multiplier": round(safety_multiplier, 4),
        "required_gross_edge_bps": round(required_edge, 3),
        "score_multiplier": round(score_multiplier, 4),
        "reasons": reasons,
        "quality_floor": quality_floor,
        "inputs": {
            "spread_field": spread_field,
            "spread_bps": spread,
            "liquidity_field": liquidity_field,
            "liquidity_score": liquidity,
            "freshness_field": freshness_field,
            "freshness_age_seconds": freshness_age,
            "freshness_window_seconds": freshness_window,
            "max_signal_age_seconds": max_signal_age,
            "volatility_field": volatility_field,
            "recent_volatility_bps": volatility,
            "modeled_cost_field": cost_field,
            "modeled_round_trip_cost_bps": modeled_cost,
            "route_status": route_status,
            "route_cost_field": route_cost_field,
            "route_cost_bps": route_cost,
            "leg_count": leg_count,
            "half_spread_field": half_spread_field,
            "slippage_field": slippage_field,
            "entry_slippage_field": entry_slippage_field,
            "exit_slippage_field": exit_slippage_field,
            "fee_field": fee_field,
            "fee_per_side_field": fee_per_side_field,
            "entry_fee_field": entry_fee_field,
            "exit_fee_field": exit_fee_field,
            "surface_cost_defaults": surface_costs,
            "latency_decay_field": latency_field,
            "volatility_tail_buffer_field": tail_field,
            "gap_risk_field": gap_field,
            "recent_gap_risk_bps": gap_risk,
            "funding_instability_bps": funding_instability,
        },
        "components_bps": {
            "half_spread_bps": round(spread_component, 3),
            "round_trip_spread_bps": round(spread_component, 3),
            "slippage_bps": round(slippage_component, 3),
            "fees_bps": round(fee_component, 3),
            "latency_decay_bps": round(latency_decay_component, 3),
            "carry_bps_horizon": round(carry_component, 3),
            "volatility_tail_buffer_bps": round(volatility_tail_component, 3),
            "execution": round(spread_component + slippage_component + fee_component, 3),
            "liquidity": round(liquidity_penalty, 3),
            "freshness": round(latency_decay_component, 3),
            "volatility": round(volatility_tail_component, 3),
            "complexity": round(complexity_component, 3),
            "route": round(route_component, 3),
        },
    }


def realized_paper_cost_audit(
    candidate: Mapping[str, Any],
    observed_pnl_bps: Any,
    *,
    charged_cost_bps: Any = 0.0,
    settings: Mapping[str, Any] | None = None,
    already_backfilled: bool = False,
) -> dict[str, Any]:
    """Backfill modeled paper friction omitted from a realized PnL label.

    ``charged_cost_bps`` is the fee/slippage already represented in the label.
    Only the positive difference to the entry-time context floor is deducted,
    which avoids double charging fills while preserving route/freshness costs.
    """
    observed = _finite_float(observed_pnl_bps)
    charged = max(0.0, _finite_float(charged_cost_bps) or 0.0)
    stored_gate = candidate.get("paper_context_cost_gate")
    has_stored_gate = isinstance(stored_gate, Mapping)
    gate = dict(stored_gate) if has_stored_gate else paper_context_cost_gate(candidate, settings)
    applicable = bool(gate.get("applicable")) and observed is not None
    legacy_entry_cost = None
    if not has_stored_gate:
        _, legacy_entry_cost = _first_number(
            candidate,
            "estimated_round_trip_cost_bps",
            "round_trip_cost_bps",
            "total_cost_bps",
        )
    modeled_floor = max(
        0.0,
        legacy_entry_cost
        if legacy_entry_cost is not None
        else (_finite_float(gate.get("context_cost_floor_bps")) or 0.0),
    )
    backfill = 0.0 if already_backfilled or not applicable else max(0.0, modeled_floor - charged)
    adjusted = None if observed is None else observed - backfill
    return {
        "paper_only": True,
        "applicable": applicable,
        "cost_basis": "after_modeled_context_cost",
        "observed_pnl_bps": round(observed, 3) if observed is not None else None,
        "charged_cost_bps": round(charged, 3),
        "modeled_context_cost_bps": round(modeled_floor, 3),
        "modeled_cost_source": (
            "stored_paper_context_cost_gate"
            if has_stored_gate
            else "legacy_entry_round_trip_cost"
            if legacy_entry_cost is not None
            else "current_policy_fallback"
        ),
        "realized_cost_backfill_bps": round(backfill, 3),
        "adjusted_pnl_bps": round(adjusted, 3) if adjusted is not None else None,
        "backfill_applied": backfill > 0.0,
        "already_backfilled": bool(already_backfilled),
        "route_status": (gate.get("inputs") or {}).get("route_status"),
        "quality_floor": gate.get("quality_floor") or {},
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
    annotated["paper_effective_cost_log"] = {
        field: gate.get(field)
        for field in (
            "predicted_edge_bps",
            "effective_cost_bps",
            "gate_margin_bps",
            "signal_age_seconds",
            "max_signal_age_seconds",
            "carry_bps_horizon",
            "spread_proxy_bps",
            "veto_reason",
            "paper_eligible",
        )
    }
    annotated["paper_eligible"] = bool(gate["eligible"])
    annotated["effective_cost_bps"] = gate["effective_cost_bps"]
    annotated["gate_margin_bps"] = gate["gate_margin_bps"]
    annotated["veto_reason"] = gate["veto_reason"]
    annotated["gross_edge_bps"] = gate["gross_edge_bps"]
    annotated["modeled_cost_bps"] = gate["modeled_cost_bps"]
    annotated["net_edge_bps"] = gate["net_edge_bps"]
    annotated["freshness_minutes"] = gate["freshness_minutes"]
    annotated["gating_reason"] = gate["gating_reason"]
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


def paper_context_cost_report(
    candidates: Iterable[Mapping[str, Any]],
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Build a compact cross-surface audit for runtime reports and agents."""
    rows: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    for candidate in candidates:
        stored = candidate.get("paper_context_cost_gate")
        gate = dict(stored) if isinstance(stored, Mapping) else paper_context_cost_gate(candidate)
        if not gate.get("applicable"):
            continue
        reason = str(gate.get("gating_reason") or "unknown")
        family_kind = str(gate.get("family_kind") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        family_counts[family_kind] = family_counts.get(family_kind, 0) + 1
        rows.append(
            {
                "venue": candidate.get("venue"),
                "inst_id": candidate.get("inst_id"),
                "trade_type": candidate.get("trade_type"),
                "direction": candidate.get("direction"),
                "family_kind": family_kind,
                "score": candidate.get("score"),
                "paper_eligible": bool(gate.get("eligible")),
                "gross_edge_bps": gate.get("gross_edge_bps"),
                "modeled_cost_bps": gate.get("modeled_cost_bps"),
                "net_edge_bps": gate.get("net_edge_bps"),
                "freshness_minutes": gate.get("freshness_minutes"),
                "gating_reason": reason,
            }
        )
    rows.sort(
        key=lambda row: (
            bool(row["paper_eligible"]),
            row["net_edge_bps"] if row["net_edge_bps"] is not None else float("-inf"),
        )
    )
    gated_count = sum(not row["paper_eligible"] for row in rows)
    return {
        "paper_only": True,
        "candidate_count": len(rows),
        "gated_candidate_count": gated_count,
        "eligible_candidate_count": len(rows) - gated_count,
        "by_family_kind": family_counts,
        "by_gating_reason": reason_counts,
        "candidates": rows[: max(0, int(limit))],
    }


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
        "veto_reason": gate.get("veto_reason"),
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
