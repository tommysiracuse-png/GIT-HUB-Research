"""Paper-only route requirement intelligence.

This module is intentionally read-only: it creates route requirement rows and
report text from already-observed paper opportunities.  It does not request or
store credentials, call private APIs, or change any execution path.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

try:  # Support both direct ``src`` imports and package-style imports.
    from paper_route_registry import assess_paper_route_registry
except ModuleNotFoundError:  # pragma: no cover - package import fallback
    from .paper_route_registry import assess_paper_route_registry


UNKNOWN = "unknown"

PAPER_ONLY_CONSTRAINTS = (
    "read_only_output_only",
    "no_credentials",
    "no_private_api_calls",
    "no_live_trading",
    "no_order_execution_changes",
    "conditional_short_route_facts_are_non_blocking",
)

# These labels deliberately describe information that must be collected or
# reviewed for a short-frontier-spot route.  They are not route requirements
# and must never be interpreted as an execution or paper-entry veto.
SHORT_FRONTIER_SPOT_ROUTE_DIAGNOSTIC_DIMENSIONS = (
    "borrow_permissions",
    "fees",
    "margin_constraints",
    "api_reliability",
    "spread_liquidity",
    "carry",
)

ROUTE_REQUIREMENT_FIELDS = (
    "venue",
    "inst_id",
    "direction",
    "route_status",
    "route_blockers",
    "required_account_type",
    "required_permissions",
    "shortability_status",
    "borrow_required",
    "borrow_asset",
    "borrow_fee_bps_estimate_or_unknown",
    "borrow_availability_status",
    "margin_spot_constraints",
    "fee_tier",
    "fee_tier_status",
    "maker_fee_bps_or_unknown",
    "taker_fee_bps_or_unknown",
    "fee_stack_bps_estimate_or_unknown",
    "margin_required",
    "margin_mode",
    "venue_api_requirement",
    "api_permission_status",
    "api_route_status",
    "endpoint_constraints",
    "order_type_support",
    "minimum_liquidity_usd_or_unknown",
    "jurisdiction_requirement",
    "fee_bps_per_side_or_unknown",
    "slippage_bps_per_side_or_unknown",
    "paper_route_only",
    "paper_feasibility",
    "paper_proxy_route",
    "feasibility_state",
    "route_friction_bps",
    "venue_supports_margin_or_equivalent",
    "shortable_inventory_declared",
    "borrow_cost_model_present",
    "fees_modeled",
    "order_api_surface_mapped",
    "paper_recommendation_action",
    "paper_recommendation_reason",
    "paper_proxy_not_live_equivalent",
    "route_type",
    "route_feasible_paper",
    "route_cost_bps_paper",
    "route_cost_reason_codes",
    "api_surface_required",
    "requires_spot_borrow",
    "requires_margin_permission",
    "route_requirement_checklist",
    "route_requirement_checklist_complete",
    "conditional_short_route_diagnostics",
    "broker_permission_status",
    "api_path_readiness",
    "stale_data_status",
    "stale_data_flags",
    "route_requirement_gaps",
    "route_requirement_gap_reason_codes",
    "paper_sizing_guidance",
    "guard_value_measurement",
    "frontier_short_spot_route_intelligence",
    "frontier_short_spot_route_requirements_report",
    "route_economics_telemetry",
    "route_validation_status",
    "route_feasibility_reason",
    "route_validation_notes",
    "freshness_latency_status",
    "freshness_latency_notes",
    "route_requirement_summary",
    "route_friction_summary",
)

# These are the route facts that an opportunity report must carry forward to a
# machine-actionable paper recommendation.  They are intentionally about
# route metadata, rather than trade quality: an unknown fact remains visible
# as ``unknown`` and does not remove the opportunity from paper observation.
ROUTE_REQUIREMENT_CHECKLIST_FIELDS = (
    "broker_permissions",
    "shortability",
    "borrow_availability",
    "fees",
    "margin",
    "api_coverage",
    "order_type_support",
)

_PRIORITY_SPOT_BORROW_INST_IDS = (
    "GATE:ARC_USDT",
    "GATE:DEXE_USDT",
    "COINBASE:XRP-USDT",
)

CONDITIONAL_SHORT_DECAY_FLIP_GUARD = {
    "cooldown_cycles": 12,
    "drawdown_guard_bps": 25.0,
    "min_confirm_count_after_promotion": 20,
    "negative_flip_bps_threshold": 0.0,
    "score_clamp": "set_to_non_admissible",
}


def build_conditional_short_route_intelligence(
    opportunity: dict[str, Any],
    *,
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach the read-only venue facts needed to review a conditional short.

    The packet deliberately describes the direct route as it is currently
    modeled.  It neither probes a venue nor changes candidate admissibility;
    unknown and unsupported facts remain visible for paper ranking and route
    cost review.
    """

    source = dict(opportunity or {})
    resolved_route = dict(route or {})
    route_status = str(
        resolved_route.get("route_status")
        or source.get("route_status")
        or UNKNOWN
    ).strip().lower()
    direction = str(
        resolved_route.get("direction")
        or source.get("direction")
        or UNKNOWN
    ).strip().lower()
    def _items(*values: Any) -> list[str]:
        output: list[str] = []
        for value in values:
            items = (value,) if isinstance(value, str) else (value or [])
            for item in items:
                text = str(item or "").strip()
                if text and text not in output:
                    output.append(text)
        return output

    requirements = _items(
        resolved_route.get("required_permissions"),
        source.get("paper_route_required_permissions"),
        source.get("required_permissions"),
    )
    missing_requirements = _items(
        resolved_route.get("missing_permissions"),
        source.get("missing_requirements"),
        source.get("route_blockers"),
    )
    applies = _conditional_short_route_applies(
        direction,
        route_status,
        source,
        requirements=requirements,
        missing_requirements=missing_requirements,
    )
    borrow_required = bool(
        resolved_route.get("borrow_required")
        or source.get("borrow_required")
        or "spot_borrow" in requirements
        or "spot_borrow" in missing_requirements
    )
    borrow_raw = _first_known(
        source,
        "borrow_availability_status",
        "borrow_available",
        "borrowable",
        "borrow_supported",
    )
    venue_capabilities = source.get("venue_capabilities")
    if borrow_raw == UNKNOWN and isinstance(venue_capabilities, dict):
        borrow_raw = _first_known(
            venue_capabilities,
            "supports_borrow_check",
            "borrow_supported",
            "borrow_inventory_supported",
        )
    if borrow_raw == UNKNOWN:
        borrow_raw = resolved_route.get("borrow_status", UNKNOWN)
    if str(borrow_raw).strip().lower() in {"required_unconfirmed", "not_checked"}:
        borrow_raw = "unconfirmed"
    # A direct route that did not itself require borrow is not evidence that a
    # frontier spot short has no borrow requirement.  Preserve that gap as an
    # unconfirmed paper diagnostic instead of misreporting it as not required.
    if borrow_required and str(borrow_raw).strip().lower() in {
        "not_required",
        "not_applicable",
    }:
        borrow_raw = UNKNOWN
    borrow_availability = _route_fact_status(
        borrow_raw,
        required=borrow_required,
        unresolved=borrow_required,
    )

    margin_mode = _first_known(source, "margin_mode", "margin_account_mode", "leverage_mode")
    account_modes = _items(source.get("paper_route_required_account_modes"))
    margin_required = bool(
        resolved_route.get("margin_required")
        or source.get("margin_required")
        or borrow_required
        or any("margin" in mode.lower() for mode in account_modes)
    )
    if margin_mode == UNKNOWN and margin_required:
        margin_supported = (
            _first_known(
                venue_capabilities,
                "supports_margin_spot",
                "margin_supported",
                "supports_margin",
            )
            if isinstance(venue_capabilities, dict)
            else UNKNOWN
        )
        margin_mode = "unsupported" if margin_supported is False else "required_unconfirmed"

    maker_fee = _first_known(source, "maker_fee_bps", "estimated_maker_fee_bps")
    taker_fee = _first_known(source, "taker_fee_bps", "estimated_taker_fee_bps")
    if isinstance(venue_capabilities, dict):
        if maker_fee == UNKNOWN:
            maker_fee = _first_known(
                venue_capabilities,
                "estimated_maker_fee_bps",
                "maker_fee_bps",
            )
        if taker_fee == UNKNOWN:
            taker_fee = _first_known(
                venue_capabilities,
                "estimated_taker_fee_bps",
                "taker_fee_bps",
            )
    route_costs = source.get("paper_route_estimated_cost_bps")
    fee_class = _first_known(source, "fee_class", "fee_model", "fee_model_status")
    if fee_class == UNKNOWN:
        if maker_fee != UNKNOWN or taker_fee != UNKNOWN:
            fee_class = "maker_taker_estimate"
        elif isinstance(route_costs, dict) and route_costs.get("estimated_total") is not None:
            fee_class = "maintained_paper_route_estimate"

    api_permission_status = _first_known(
        source,
        "api_permission_status",
        "api_access_status",
        "venue_api_status",
        "endpoint_status",
    )
    if api_permission_status == UNKNOWN:
        api_permission_status = str(resolved_route.get("api_access_status") or UNKNOWN)

    return {
        "paper_only": True,
        "read_only": True,
        "applies": applies,
        "venue": str(resolved_route.get("venue") or source.get("venue") or UNKNOWN),
        "direction": direction,
        "route_status": route_status,
        "shorting_requirements": requirements,
        "missing_shorting_requirements": missing_requirements,
        "borrow_required": borrow_required,
        "borrow_availability": borrow_availability,
        "margin_required": margin_required,
        "margin_mode": margin_mode,
        "fee_class": fee_class,
        "maker_fee_bps": maker_fee,
        "taker_fee_bps": taker_fee,
        "api_permission_status": api_permission_status,
        "source": "maintained_paper_route_metadata",
        "venue_capability_profile": (
            str(venue_capabilities.get("capability_profile") or UNKNOWN)
            if isinstance(venue_capabilities, dict)
            else UNKNOWN
        ),
        "ranking_action": "down_rank_only" if applies else "no_rank_adjustment",
        "hard_blocking": False,
    }


def extract_route_requirements(
    opportunity: dict[str, Any],
    *,
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract direct-route requirements without probing a broker or API.

    This is the compact candidate-facing form of route intelligence.  It
    makes the five facts needed before a direct-route recommendation explicit:
    broker permissions, borrow, fees, margin, and API constraints.  A missing
    or unknown fact marks the *direct route* as ``gated``.  That is an
    informational paper-mode state: it never suppresses the priceable
    candidate, changes the selected paper route, or authorizes execution.
    """

    source = dict(opportunity or {})
    resolved_route = dict(route or {})
    for key in (
        "required_permissions",
        "missing_permissions",
        "requirements",
        "borrow_required",
        "borrow_status",
        "margin_required",
        "api_access_status",
        "fee_model_status",
        "route_status",
    ):
        if source.get(key) in (None, "", [], {}):
            source[key] = resolved_route.get(key)

    requirements = [
        dict(item)
        for item in (resolved_route.get("requirements") or source.get("requirements") or [])
        if isinstance(item, dict)
    ]
    missing_permissions = _items_from_values(
        resolved_route.get("missing_permissions"),
        source.get("missing_permissions"),
        source.get("route_blockers"),
    )
    required_permissions = _items_from_values(
        resolved_route.get("required_permissions"),
        source.get("required_permissions"),
    )
    conditional = build_conditional_short_route_intelligence(source, route=resolved_route)

    def requirement_status(tokens: tuple[str, ...], *, required: bool) -> str:
        statuses = []
        for item in requirements:
            identifier = " ".join(
                str(item.get(key) or "")
                for key in ("requirement_id", "capability_key", "category")
            ).lower()
            if any(token in identifier for token in tokens):
                statuses.append(str(item.get("status") or UNKNOWN).lower())
        if any(any(token in value.lower() for token in tokens) for value in missing_permissions):
            statuses.append("missing")
        if not statuses:
            return UNKNOWN if required else "not_applicable"
        if any(status in {"missing", "unavailable", "unsupported", "blocked"} for status in statuses):
            return "missing"
        if any(status in {UNKNOWN, "unconfirmed", "not_checked"} for status in statuses):
            return UNKNOWN
        return "confirmed"

    broker_status = requirement_status(
        ("account", "permission", "crypto", "equity", "prediction", "specialist"),
        required=bool(required_permissions or requirements),
    )
    if any(
        token in permission.lower()
        for permission in missing_permissions
        for token in ("broker", "permission", "account", "jurisdiction")
    ):
        broker_status = "missing"
    borrow_required = bool(conditional["borrow_required"])
    borrow_status = str(conditional["borrow_availability"] or UNKNOWN).lower()
    if borrow_required and borrow_status in {"available", "confirmed"}:
        borrow_status = "confirmed"
    elif borrow_required and borrow_status in {"unavailable", "unsupported", "missing"}:
        borrow_status = "missing"
    elif borrow_required:
        borrow_status = UNKNOWN
    else:
        borrow_status = "not_applicable"

    fee_class = str(conditional["fee_class"] or UNKNOWN).lower()
    fee_model_status = str(source.get("fee_model_status") or UNKNOWN).lower()
    if fee_class == UNKNOWN and fee_model_status not in {UNKNOWN, "not_checked"}:
        fee_class = fee_model_status
    fee_status = (
        "confirmed"
        if fee_class not in {UNKNOWN, "not_checked", "unconfirmed"}
        else UNKNOWN
    )
    margin_required = bool(conditional["margin_required"])
    margin_mode = str(conditional["margin_mode"] or UNKNOWN).lower()
    if not margin_required:
        margin_status = "not_applicable"
    elif margin_mode in {"isolated", "cross", "portfolio", "available", "confirmed"}:
        margin_status = "confirmed"
    elif margin_mode in {"unsupported", "unavailable", "missing"}:
        margin_status = "missing"
    else:
        margin_status = UNKNOWN
    api_status = str(conditional["api_permission_status"] or UNKNOWN).lower()
    if api_status in {"available", "ready", "mapped", "confirmed"}:
        api_requirement_status = "confirmed"
    elif api_status in {"unavailable", "unsupported", "missing", "blocked"}:
        api_requirement_status = "missing"
    else:
        # Public-data access is useful for paper observation but deliberately
        # does not confirm private/order API entitlement.
        api_requirement_status = UNKNOWN

    extracted = {
        "broker_permissions": {
            "status": broker_status,
            "required_permissions": required_permissions,
            "missing_permissions": missing_permissions,
        },
        "borrow_availability": {
            "required": borrow_required,
            "status": borrow_status,
            "observed": conditional["borrow_availability"],
        },
        "fee_class": {"status": fee_status, "value": fee_class},
        "margin_status": {
            "required": margin_required,
            "status": margin_status,
            "mode": conditional["margin_mode"],
        },
        "api_constraints": {
            "status": api_requirement_status,
            "observed": conditional["api_permission_status"],
        },
    }
    unresolved = [
        name
        for name, detail in extracted.items()
        if detail["status"] in {UNKNOWN, "missing"}
    ]
    recommendation_status = "gated" if unresolved else "actionable"
    return {
        "extractor_version": "route_requirements_v1",
        "paper_only": True,
        "read_only": True,
        "requirements": extracted,
        "unresolved_requirements": unresolved,
        "route_recommendation_status": recommendation_status,
        "route_actionability": recommendation_status,
        "direct_route_actionable": not unresolved,
        "paper_candidate_emission": "retained_for_paper_exploration",
        "hard_blocking": False,
        "entry_blocked": False,
    }


def _items_from_values(*values: Any) -> list[str]:
    """Return unique, non-empty string values without interpreting them."""

    output: list[str] = []
    for value in values:
        for item in (value,) if isinstance(value, str) else (value or []):
            text = str(item or "").strip()
            if text and text not in output:
                output.append(text)
    return output


def build_route_requirements_matrix(
    opportunities: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a paper-only route requirements matrix.

    The input is expected to be previously collected paper/research
    opportunities.  The output is a list of dictionaries containing only
    requirement metadata and explicit unknown placeholders.
    """

    rows = []
    for opportunity in opportunities:
        normalized = _route_requirement_opportunity(opportunity)
        row = _build_route_requirement_row(normalized)
        annotated = _annotate_route_feasibility_fields(normalized, row)
        diagnostics = build_conditional_short_route_diagnostics(normalized, row=annotated)
        friction = build_route_friction_summary(
            normalized,
            row=annotated,
            diagnostics=diagnostics,
        )
        annotated.update(
            {
                "borrow_availability_status": diagnostics["borrow_availability"],
                "maker_fee_bps_or_unknown": diagnostics["maker_taker_fee_stack_bps"]["maker_bps"],
                "taker_fee_bps_or_unknown": diagnostics["maker_taker_fee_stack_bps"]["taker_bps"],
                "fee_stack_bps_estimate_or_unknown": diagnostics["maker_taker_fee_stack_bps"],
                "margin_mode": diagnostics["margin_mode"],
                "api_route_status": diagnostics["api_route_status"],
                "minimum_liquidity_usd_or_unknown": diagnostics["minimum_liquidity_usd"],
                "conditional_short_route_diagnostics": diagnostics,
                "route_friction_summary": friction,
            }
        )
        panel = _route_requirements_panel(normalized, annotated)
        annotated.update(panel)
        frontier_intelligence = build_frontier_short_spot_route_intelligence(
            normalized,
            annotation=panel,
            diagnostics=diagnostics,
        )
        frontier_requirements_report = _frontier_short_spot_route_requirements_report(
            normalized,
            {},
            frontier_intelligence,
        )
        annotated.update(
            {
                "frontier_short_spot_route_intelligence": frontier_intelligence,
                "frontier_short_spot_route_requirements_report": frontier_requirements_report,
                "route_economics_telemetry": frontier_intelligence["route_economics_telemetry"],
                "route_validation_status": frontier_intelligence["route_validation_status"],
                "route_feasibility_reason": frontier_intelligence["route_feasibility_reason"],
                "route_validation_notes": frontier_intelligence["route_validation_notes"],
                "freshness_latency_status": frontier_intelligence["freshness_latency_status"],
                "freshness_latency_notes": frontier_intelligence["freshness_latency_notes"],
            }
        )
        annotated["route_requirement_summary"] = build_candidate_route_requirement_summary(
            normalized,
            row=annotated,
        )
        rows.append(annotated)
    return sorted(rows, key=_route_priority_key)


def build_route_requirements_annotation(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Return a read-only route-requirements tag for one paper candidate.

    This is the candidate-facing form of the report panel.  It deliberately
    describes evidence and counterfactual measurements only: callers may use
    it for paper sizing review or guard-value analysis, but it does not alter
    routing, eligibility, allocation, or execution semantics.
    """

    normalized = _route_requirement_opportunity(opportunity)
    row = _build_route_requirement_row(normalized)
    annotated = _annotate_route_feasibility_fields(normalized, row)
    diagnostics = build_conditional_short_route_diagnostics(normalized, row=annotated)
    friction = build_route_friction_summary(
        normalized,
        row=annotated,
        diagnostics=diagnostics,
    )
    annotated.update(
        {
            "borrow_availability_status": diagnostics["borrow_availability"],
            "maker_fee_bps_or_unknown": diagnostics["maker_taker_fee_stack_bps"]["maker_bps"],
            "taker_fee_bps_or_unknown": diagnostics["maker_taker_fee_stack_bps"]["taker_bps"],
            "fee_stack_bps_estimate_or_unknown": diagnostics["maker_taker_fee_stack_bps"],
            "margin_mode": diagnostics["margin_mode"],
            "api_route_status": diagnostics["api_route_status"],
            "minimum_liquidity_usd_or_unknown": diagnostics["minimum_liquidity_usd"],
            "conditional_short_route_diagnostics": diagnostics,
            "route_friction_summary": friction,
        }
    )
    panel = _route_requirements_panel(normalized, annotated)
    frontier_intelligence = build_frontier_short_spot_route_intelligence(
        normalized,
        annotation=panel,
        diagnostics=diagnostics,
    )
    frontier_requirements_report = _frontier_short_spot_route_requirements_report(
        normalized,
        {},
        frontier_intelligence,
    )
    return {
        **panel,
        "route_friction_summary": friction,
        "frontier_short_spot_route_intelligence": frontier_intelligence,
        "frontier_short_spot_route_requirements_report": frontier_requirements_report,
        "route_validation_status": frontier_intelligence["route_validation_status"],
        "route_feasibility_reason": frontier_intelligence["route_feasibility_reason"],
        "route_validation_notes": frontier_intelligence["route_validation_notes"],
        "freshness_latency_status": frontier_intelligence["freshness_latency_status"],
        "freshness_latency_notes": frontier_intelligence["freshness_latency_notes"],
        "route_requirement_summary": build_candidate_route_requirement_summary(
            normalized,
            row={
                **annotated,
                **panel,
                "frontier_short_spot_route_intelligence": frontier_intelligence,
                "route_validation_status": frontier_intelligence["route_validation_status"],
                "route_feasibility_reason": frontier_intelligence["route_feasibility_reason"],
                "route_validation_notes": frontier_intelligence["route_validation_notes"],
                "freshness_latency_status": frontier_intelligence["freshness_latency_status"],
                "freshness_latency_notes": frontier_intelligence["freshness_latency_notes"],
            },
        ),
    }


def build_conditional_short_route_diagnostics(
    opportunity: dict[str, Any],
    *,
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe conditional-short execution requirements without probing a route.

    This is intentionally a ranking diagnostic, not an eligibility decision.
    Unknown borrow, fee, margin, API, or liquidity facts reduce only the
    paper-review rank of a conditional short; they never set a paper-entry
    blocker or alter a broker/account capability.
    """

    source = dict(row or {})
    source.update({key: value for key, value in opportunity.items() if value not in (None, "")})
    route_packet = source.get("conditional_short_route_intelligence")
    if isinstance(route_packet, dict):
        # Prefer a specific venue packet over the resolver's generic
        # ``required_unconfirmed`` shorthand.  This only supplies ranking
        # inputs; it cannot turn a route fact into an entry block.
        for target, packet_key in (
            ("borrow_availability_status", "borrow_availability"),
            ("margin_mode", "margin_mode"),
            ("api_permission_status", "api_permission_status"),
            ("api_route_status", "api_permission_status"),
            ("maker_fee_bps", "maker_fee_bps"),
            ("taker_fee_bps", "taker_fee_bps"),
        ):
            current = source.get(target)
            if current in (None, "", UNKNOWN, "required_unconfirmed", "not_checked"):
                source[target] = route_packet.get(packet_key, current)
    blockers = _route_blockers(source)
    direction = str(source.get("direction") or UNKNOWN).lower()
    route_status = str(source.get("route_status") or _paper_route_status(source, blockers=blockers)).lower()
    applies = _conditional_short_route_applies(
        direction,
        route_status,
        source,
        requirements=source.get("required_permissions"),
        missing_requirements=blockers,
    )
    borrow_required = _requires_spot_borrow(source, blockers=blockers)

    borrow_availability = _route_fact_status(
        _first_known(
            source,
            "borrow_availability_status",
            "borrow_status",
            "borrow_available",
            "borrowable",
            "borrow_supported",
        ),
        required=borrow_required,
        unresolved=borrow_required or "spot_borrow" in blockers,
    )
    borrow_fee = _first_known(
        source,
        "borrow_fee_bps_estimate_or_unknown",
        "borrow_fee_bps_estimate",
        "borrow_fee_bps",
        "borrow_cost_bps",
    )
    maker_fee = _first_known(source, "maker_fee_bps_or_unknown", "maker_fee_bps", "estimated_maker_fee_bps")
    taker_fee = _first_known(
        source,
        "taker_fee_bps_or_unknown",
        "taker_fee_bps",
        "fee_bps_per_side_or_unknown",
        "fee_bps_per_side",
    )
    margin_mode = _first_known(source, "margin_mode", "margin_account_mode", "leverage_mode")
    if margin_mode == UNKNOWN and _requires_margin_permission(source, blockers=blockers, borrow_required=borrow_required) is True:
        margin_mode = "unconfirmed"
    api_route_status = _route_fact_status(
        _first_known(source, "api_route_status", "api_access_status", "venue_api_status", "endpoint_status"),
        required=True,
        unresolved=applies,
    )
    minimum_liquidity = _first_known(
        source,
        "minimum_liquidity_usd_or_unknown",
        "minimum_liquidity_usd",
        "min_liquidity_usd",
        "liquidity_floor_usd",
        "required_liquidity_usd",
    )
    taker_number = _float_or_none(taker_fee)
    fee_stack: dict[str, Any] = {"maker_bps": maker_fee, "taker_bps": taker_fee}
    if taker_number is not None:
        fee_stack["estimated_round_trip_taker_bps"] = round(taker_number * 2.0, 4)
    else:
        fee_stack["estimated_round_trip_taker_bps"] = UNKNOWN

    risk_reasons: list[str] = []
    risk_points = 0
    if applies:
        for value, unknown_reason, unavailable_reason, points in (
            (borrow_availability, "borrow_availability_unconfirmed", "borrow_unavailable", 25),
            (borrow_fee, "borrow_fee_unestimated", "borrow_fee_unestimated", 15),
            (maker_fee, "maker_fee_unestimated", "maker_fee_unestimated", 10),
            (taker_fee, "taker_fee_unestimated", "taker_fee_unestimated", 15),
            (margin_mode, "margin_mode_unconfirmed", "margin_mode_unconfirmed", 15),
            (api_route_status, "api_route_status_unconfirmed", "api_route_unavailable", 20),
            (minimum_liquidity, "minimum_liquidity_unspecified", "minimum_liquidity_unspecified", 10),
        ):
            normalized = str(value or UNKNOWN).strip().lower()
            if normalized in {UNKNOWN, "unconfirmed", "not_checked", "not_applicable"}:
                risk_points += points
                risk_reasons.append(unknown_reason)
            elif normalized in {"missing", "unavailable", "unsupported", "blocked"}:
                risk_points += points * 2
                risk_reasons.append(unavailable_reason)
    risk_score = min(100, risk_points)
    rank_multiplier = round(max(0.35, 1.0 - risk_score / 200.0), 4) if applies else 1.0
    return {
        "paper_only": True,
        "read_only": True,
        "applies": applies,
        "borrow_availability": borrow_availability,
        "estimated_borrow_fee_bps": borrow_fee,
        "maker_taker_fee_stack_bps": fee_stack,
        "margin_mode": margin_mode,
        "api_route_status": api_route_status,
        "minimum_liquidity_usd": minimum_liquidity,
        "execution_risk_score": risk_score,
        "execution_risk_reasons": list(dict.fromkeys(risk_reasons)),
        "paper_rank_multiplier": rank_multiplier,
        "ranking_action": "down_rank_only" if applies and risk_score else "no_rank_adjustment",
        "hard_blocking": False,
    }


def build_route_friction_summary(
    opportunity: dict[str, Any],
    *,
    row: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project route friction into a non-blocking paper ranking/sizing packet.

    The packet is deliberately built only from observed candidate data and the
    maintained paper-route registry annotation.  It is not a route probe and
    does not decide whether a priceable candidate is emitted, routed, or
    entered.  Unknown borrow, fee, API, freshness, and liquidity facts remain
    explicit so weak paper outcomes can be attributed to route friction rather
    than being mistaken for alpha failure.
    """

    source = dict(row or {})
    source.update(
        {key: value for key, value in dict(opportunity or {}).items() if value not in (None, "")}
    )
    route_diagnostics = dict(
        diagnostics or build_conditional_short_route_diagnostics(source, row=row)
    )
    applies = bool(route_diagnostics.get("applies"))
    # The resolver can add a generic route requirement (for example an API
    # capability) while the maintained paper registry carries the more
    # specific short/borrow permissions.  Preserve both instead of allowing
    # one source to mask the other in the attribution report.
    required_permissions: list[str] = []
    for permissions in (
        source.get("required_permissions"),
        source.get("paper_route_required_permissions"),
        source.get("registry_required_permissions"),
    ):
        for permission in _string_list(permissions):
            if permission not in required_permissions:
                required_permissions.append(permission)
    route_blockers = _route_blockers(source)
    borrow_required = bool(
        source.get("borrow_required")
        or source.get("requires_spot_borrow")
        or "spot_borrow" in required_permissions
        or "spot_borrow" in route_blockers
    )
    borrow_range = _indicative_borrow_fee_range(source, required=borrow_required)
    stale_illiquid = _stale_illiquid_route_diagnostics(source)
    base_score = _float_or_none(route_diagnostics.get("execution_risk_score")) or 0.0
    friction_reasons = list(route_diagnostics.get("execution_risk_reasons") or [])
    if applies and stale_illiquid["stale"]:
        base_score += 10.0
        friction_reasons.append("stale_market_data")
    if applies and stale_illiquid["illiquid"]:
        base_score += 10.0
        friction_reasons.append("illiquid_market_diagnostic")
    friction_score = round(min(100.0, max(0.0, base_score)), 4)
    diagnostic_multiplier = _float_or_none(route_diagnostics.get("paper_rank_multiplier"))
    friction_multiplier = max(0.35, 1.0 - friction_score / 200.0) if applies else 1.0
    paper_multiplier = min(
        diagnostic_multiplier if diagnostic_multiplier is not None else 1.0,
        friction_multiplier,
    )
    route_status = str(
        source.get("route_status")
        or source.get("paper_route_registry_status")
        or UNKNOWN
    ).strip().lower()
    registry = source.get("paper_route_registry")
    registry = registry if isinstance(registry, dict) else {}
    registry_status = str(
        registry.get("support_status")
        or source.get("paper_route_registry_status")
        or UNKNOWN
    ).strip().lower()
    api_status = str(
        source.get("api_route_status")
        or route_diagnostics.get("api_route_status")
        or source.get("api_permission_status")
        or source.get("api_access_status")
        or UNKNOWN
    )
    fee_model = _first_known(
        source,
        "fee_model",
        "fee_class",
        "fee_tier",
        "fee_tier_name",
        "fee_model_status",
    )
    maker_fee = _first_known(source, "maker_fee_bps_or_unknown", "maker_fee_bps", "estimated_maker_fee_bps")
    taker_fee = _first_known(source, "taker_fee_bps_or_unknown", "taker_fee_bps", "estimated_taker_fee_bps")
    margin_type = _first_known(source, "margin_mode", "margin_account_mode", "leverage_mode")
    if margin_type == UNKNOWN:
        account_modes = _string_list(
            source.get("paper_route_required_account_modes")
            or source.get("required_account_modes")
        )
        margin_type = account_modes if account_modes else UNKNOWN

    return {
        "summary_version": "paper_route_friction_v1",
        "paper_only": True,
        "read_only": True,
        "applies": applies,
        "use": "paper_ranking_and_sizing_only",
        "required_broker_permissions": required_permissions or [UNKNOWN],
        "borrow_availability": route_diagnostics.get("borrow_availability", UNKNOWN),
        "indicative_borrow_fee_bps_range": borrow_range,
        "margin_type": margin_type,
        "api_coverage": {
            "status": api_status,
            "required_surface": source.get("api_surface_required", UNKNOWN),
            "probe_performed": False,
        },
        "venue_route_status": {
            "resolved_status": route_status,
            "registry_status": registry_status,
        },
        "fee_model": {
            "model": fee_model,
            "maker_fee_bps": maker_fee,
            "taker_fee_bps": taker_fee,
            "route_cost_bps_paper": source.get("route_cost_bps_paper", UNKNOWN),
        },
        "stale_illiquid_diagnostics": stale_illiquid,
        "friction_score": friction_score,
        "friction_reasons": list(dict.fromkeys(friction_reasons)),
        "paper_rank_multiplier": round(paper_multiplier, 4),
        "paper_allocation_multiplier": round(paper_multiplier, 4),
        "ranking_action": "down_rank_and_size_only" if applies and friction_score else "no_rank_adjustment",
        "hard_blocking": False,
        "entry_blocked": False,
        "routing_decision_changed": False,
        "paper_candidate_emission": "retained_for_paper_exploration",
    }


def _indicative_borrow_fee_range(source: dict[str, Any], *, required: bool) -> dict[str, Any]:
    """Normalize available borrow-cost evidence without inventing a venue quote."""

    raw_range = source.get("indicative_borrow_fee_bps_range") or source.get("borrow_fee_bps_range")
    lower = upper = None
    if isinstance(raw_range, dict):
        lower = _float_or_none(raw_range.get("lower_bps", raw_range.get("min_bps")))
        upper = _float_or_none(raw_range.get("upper_bps", raw_range.get("max_bps")))
    elif isinstance(raw_range, (list, tuple)) and len(raw_range) >= 2:
        lower, upper = _float_or_none(raw_range[0]), _float_or_none(raw_range[1])
    estimate = _float_or_none(
        _first_known(
            source,
            "borrow_fee_bps_estimate_or_unknown",
            "borrow_fee_bps_estimate",
            "borrow_fee_bps",
            "borrow_cost_bps",
        )
    )
    route_costs = source.get("paper_route_estimated_cost_bps")
    if estimate is None and isinstance(route_costs, dict):
        estimate = _float_or_none(route_costs.get("borrow"))
    if lower is None and upper is None and estimate is not None:
        lower = upper = estimate
    if lower is not None and upper is None:
        upper = lower
    if upper is not None and lower is None:
        lower = upper
    if lower is not None and upper is not None:
        lower, upper = min(lower, upper), max(lower, upper)
        status = "indicative"
    elif not required:
        status = "not_applicable"
    else:
        status = UNKNOWN
    return {
        "status": status,
        "lower_bps": round(lower, 4) if lower is not None else "not_applicable" if not required else UNKNOWN,
        "upper_bps": round(upper, 4) if upper is not None else "not_applicable" if not required else UNKNOWN,
        "source": "maintained_paper_route_metadata_or_candidate_diagnostic",
    }


def _stale_illiquid_route_diagnostics(source: dict[str, Any]) -> dict[str, Any]:
    """Expose observed stale/illiquid signals without turning them into a gate."""

    stale = _stale_data_diagnostics(source)
    flags = list(stale["flags"])
    liquidity_status = str(
        _first_known(source, "liquidity_status", "market_liquidity_status", "depth_status")
    ).strip().lower()
    illiquid = liquidity_status in {"illiquid", "thin", "unavailable", "stale"}
    if illiquid:
        flags.append(f"liquidity_status:{liquidity_status}")
    liquidity_score = _float_or_none(_first_known(source, "liquidity_score", "market_liquidity_score"))
    if liquidity_score is not None and liquidity_score <= 0.2:
        illiquid = True
        flags.append("liquidity_score_at_or_below_0.2")
    observed_depth = _float_or_none(
        _first_known(source, "available_depth_usd", "depth_usd", "liquidity_usd")
    )
    minimum_depth = _float_or_none(
        _first_known(source, "minimum_liquidity_usd", "min_liquidity_usd", "liquidity_floor_usd")
    )
    if observed_depth is not None and minimum_depth is not None and observed_depth < minimum_depth:
        illiquid = True
        flags.append("observed_depth_below_indicative_minimum")
    if stale["status"] == "stale" and illiquid:
        status = "stale_and_illiquid"
    elif stale["status"] == "stale":
        status = "stale"
    elif illiquid:
        status = "illiquid"
    elif flags:
        status = "observed"
    else:
        status = UNKNOWN
    return {
        "status": status,
        "stale": stale["status"] == "stale",
        "illiquid": illiquid,
        "flags": list(dict.fromkeys(flags)),
        "liquidity_score": liquidity_score if liquidity_score is not None else UNKNOWN,
        "observed_depth_usd": observed_depth if observed_depth is not None else UNKNOWN,
        "non_blocking": True,
    }


def build_paper_route_requirement_report(
    opportunity: dict[str, Any],
    *,
    route: dict[str, Any] | None = None,
    annotation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the refreshed, non-blocking route report for a paper candidate.

    Conditional shorts and short proxy routes need the same route facts even
    when the maintained proxy route is labelled ``standard``.  The report is
    intentionally the only place that converts those facts into paper ranking
    and sizing guidance: it cannot alter eligibility, route status, or live
    execution capability.
    """

    source = dict(opportunity or {})
    resolved_route = dict(route or {})
    if resolved_route:
        source.setdefault("route_status", resolved_route.get("route_status"))
        source.setdefault("route_blockers", resolved_route.get("route_blockers"))
        source.setdefault("required_permissions", resolved_route.get("required_permissions"))

    direction = str(
        resolved_route.get("direction") or source.get("direction") or ""
    ).strip().lower()
    descriptors = " ".join(
        str(source.get(key) or "")
        for key in (
            "trade_type",
            "route_type",
            "market_key",
            "signal_key",
            "strategy",
            "strategy_profile",
        )
    ).lower().replace("-", "_")
    short_proxy = direction == "short_proxy" or (
        "short" in direction and "proxy" in descriptors
    )

    # Reuse the established risk accounting.  A standard paper proxy still
    # needs a conditional diagnostic view, but only in this read-only report;
    # do not relabel the candidate's actual route status.
    diagnostics_source = dict(source)
    if short_proxy and str(diagnostics_source.get("route_status") or "").lower() == "standard":
        diagnostics_source["route_status"] = "conditional"
    diagnostics = build_conditional_short_route_diagnostics(diagnostics_source)
    applies = bool(diagnostics.get("applies") or short_proxy)
    friction = build_route_friction_summary(
        diagnostics_source,
        row=annotation,
        diagnostics=diagnostics,
    )
    panel = dict(annotation or build_route_requirements_annotation(source))
    frontier_short_spot_route_intelligence = build_frontier_short_spot_route_intelligence(
        source,
        route=resolved_route,
        annotation=panel,
        diagnostics=diagnostics,
    )
    route_economics_telemetry = dict(
        frontier_short_spot_route_intelligence.get("route_economics_telemetry") or {}
    )
    rank_multiplier = float(friction.get("paper_rank_multiplier") or 1.0) if applies else 1.0
    rank_multiplier = round(max(0.35, min(1.0, rank_multiplier)), 4)
    sizing_hook = route_economics_telemetry.get("sizing_hook")
    telemetry_allocation = _float_or_none(
        sizing_hook.get("recommended_paper_allocation_multiplier")
        if isinstance(sizing_hook, dict)
        else None
    )
    allocation_multiplier = min(
        rank_multiplier,
        max(0.35, min(1.0, telemetry_allocation)),
    ) if applies and telemetry_allocation is not None else rank_multiplier
    allocation_multiplier = round(allocation_multiplier, 4)
    sizing_guidance = dict(panel.get("paper_sizing_guidance") or {})
    gaps = list(panel.get("route_requirement_gaps") or [])
    sizing_guidance.update(
        {
            "paper_only": True,
            "non_blocking": True,
            "route_requirement_report_multiplier": rank_multiplier,
            "route_economics_telemetry_multiplier": telemetry_allocation if telemetry_allocation is not None else 1.0,
            "recommended_paper_allocation_multiplier": allocation_multiplier,
            "action": (
                "route_aware_paper_sizing" if applies else sizing_guidance.get("action", "standard_paper_sizing_review")
            ),
            "routing_decision_changed": False,
        }
    )
    frontier_short_spot_route_requirements_report = _frontier_short_spot_route_requirements_report(
        source,
        resolved_route,
        frontier_short_spot_route_intelligence,
    )
    route_requirement_summary = panel.get("route_requirement_summary")
    if not isinstance(route_requirement_summary, dict):
        route_requirement_summary = build_candidate_route_requirement_summary(
            source,
            row={
                **panel,
                "frontier_short_spot_route_intelligence": frontier_short_spot_route_intelligence,
            },
        )
    return {
        "report_version": "paper_route_requirements_v1",
        "paper_only": True,
        "read_only": True,
        "applies": applies,
        "route_status_observed": str(source.get("route_status") or UNKNOWN).lower(),
        "candidate_kind": "short_proxy" if short_proxy else "conditional_short" if applies else "other",
        "route_requirements": panel,
        "diagnostics": diagnostics,
        "route_friction_summary": friction,
        "route_requirement_gaps": gaps,
        "paper_rank_multiplier": rank_multiplier,
        "paper_allocation_multiplier": allocation_multiplier,
        "paper_sizing_guidance": sizing_guidance,
        "frontier_short_spot_route_intelligence": frontier_short_spot_route_intelligence,
        "frontier_short_spot_route_requirements_report": frontier_short_spot_route_requirements_report,
        "route_economics_telemetry": route_economics_telemetry,
        "route_feasibility_reason": frontier_short_spot_route_intelligence["route_feasibility_reason"],
        "route_requirement_summary": route_requirement_summary,
        "hard_blocking": False,
        "entry_blocked": False,
        "routing_decision_changed": False,
    }


def build_frontier_short_spot_route_intelligence(
    opportunity: dict[str, Any],
    *,
    route: dict[str, Any] | None = None,
    annotation: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project read-only route facts for a frontier spot-short candidate.

    This pass only reads the candidate, maintained route registry data, and
    public-observation metadata already attached by the scanner.  It makes
    incomplete route facts explicit as ``needs route validation`` while
    retaining the candidate for paper exploration.
    """

    source = dict(opportunity or {})
    resolved_route = dict(route or {})
    for key in (
        "route_status",
        "route_blockers",
        "required_permissions",
        "borrow_status",
        "margin_required",
        "api_access_status",
    ):
        if source.get(key) in (None, "", [], {}):
            source[key] = resolved_route.get(key)
    direction = str(
        resolved_route.get("direction") or source.get("direction") or UNKNOWN
    ).strip().lower()
    applies = direction == "short_frontier_spot"
    panel = dict(annotation or build_route_requirements_annotation(source))
    route_diagnostics = dict(diagnostics or build_conditional_short_route_diagnostics(source))
    blockers = _route_blockers(source)
    broker_permissions = _route_required_permissions(source, blockers)
    if broker_permissions == UNKNOWN:
        broker_permissions = resolved_route.get("required_permissions", UNKNOWN)
    broker_permission_status = panel.get("broker_permission_status", UNKNOWN)
    if broker_permission_status == UNKNOWN and broker_permissions not in (UNKNOWN, None, "", [], {}):
        broker_permission_status = "needs_route_validation"

    fee_estimates = {
        "maker_fee_bps": route_diagnostics.get("maker_taker_fee_stack_bps", {}).get("maker_bps", UNKNOWN),
        "taker_fee_bps": route_diagnostics.get("maker_taker_fee_stack_bps", {}).get("taker_bps", UNKNOWN),
        "estimated_round_trip_taker_bps": route_diagnostics.get("maker_taker_fee_stack_bps", {}).get(
            "estimated_round_trip_taker_bps", UNKNOWN
        ),
        "borrow_fee_bps": route_diagnostics.get("estimated_borrow_fee_bps", UNKNOWN),
    }
    route_metadata = {
        "broker_permissions": broker_permissions,
        "broker_permission_status": broker_permission_status,
        "borrow_availability": route_diagnostics.get("borrow_availability", UNKNOWN),
        "fee_estimates": fee_estimates,
        "margin_required": bool(
            resolved_route.get("margin_required")
            or source.get("margin_required")
            or direction == "short_frontier_spot"
        ),
        "margin_mode": route_diagnostics.get("margin_mode", UNKNOWN),
        "api_route_status": route_diagnostics.get("api_route_status", UNKNOWN),
    }
    missing_route_metadata = []
    if applies:
        for field, value in route_metadata.items():
            if field == "fee_estimates":
                values = value.values()
                incomplete = all(_route_metadata_unconfirmed(item) for item in values)
            else:
                incomplete = _route_metadata_unconfirmed(value)
            if incomplete:
                missing_route_metadata.append(field)

    freshness_latency = _freshness_latency_notes(source)
    route_economics_telemetry = build_frontier_short_spot_route_telemetry(
        source,
        route=resolved_route,
        diagnostics=route_diagnostics,
        freshness_latency=freshness_latency,
    )
    validation_status = (
        "needs route validation"
        if applies and missing_route_metadata
        else "route metadata observed"
        if applies
        else "not_applicable"
    )
    route_feasibility_reason = str(
        source.get("route_feasibility_reason")
        or resolved_route.get("route_feasibility_reason")
        or ("not_applicable" if not applies else UNKNOWN)
    )
    validation_notes = []
    if applies and missing_route_metadata:
        validation_notes.append(
            "Missing route metadata is a read-only paper diagnostic; paper testing remains eligible."
        )
    if applies and freshness_latency["status"] != "observed":
        validation_notes.append(
            "Freshness/latency evidence is incomplete and should be reviewed during route validation."
        )
    return {
        "paper_only": True,
        "read_only": True,
        "applies": applies,
        **route_metadata,
        "freshness_latency_status": freshness_latency["status"],
        "freshness_latency_notes": freshness_latency["notes"],
        "route_economics_telemetry": route_economics_telemetry,
        "missing_route_metadata": missing_route_metadata,
        "route_validation_status": validation_status,
        "route_feasibility_reason": route_feasibility_reason,
        "route_validation_notes": validation_notes,
        "hard_blocking": False,
        "entry_blocked": False,
        "routing_decision_changed": False,
    }


def build_frontier_short_spot_route_telemetry(
    opportunity: dict[str, Any],
    *,
    route: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    freshness_latency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect pre-ranking economics evidence for a frontier spot short.

    This is deliberately a projection of scanner and maintained-route facts;
    it does not probe an account, venue, or order endpoint.  The score hook is
    an ordering signal only.  It never changes candidate emission, paper-route
    selection, or paper-entry eligibility.
    """

    source = dict(opportunity or {})
    resolved_route = dict(route or {})
    direction = str(
        resolved_route.get("direction") or source.get("direction") or UNKNOWN
    ).strip().lower()
    applies = direction == "short_frontier_spot"
    route_diagnostics = dict(diagnostics or build_conditional_short_route_diagnostics(source))
    freshness = dict(freshness_latency or _freshness_latency_notes(source))
    quote_age_seconds = _float_or_none(
        _first_known(source, "freshness_age_seconds", "data_age_seconds")
    )
    outcome_diagnostic = source.get("short_frontier_spot_route_outcome_diagnostic")
    if not isinstance(outcome_diagnostic, dict):
        outcome_diagnostic = source.get("route_outcome_diagnostic")
    outcome_diagnostic = dict(outcome_diagnostic or {})
    outcome_ranking = outcome_diagnostic.get("ranking_input")
    outcome_ranking = dict(outcome_ranking) if isinstance(outcome_ranking, dict) else {}

    def number(*keys: str) -> float | str:
        value = _numeric_field(source, *keys)
        return value if value is not None else UNKNOWN

    spread_bps = number("spread_bps", "effective_spread_bps", "quoted_spread_bps")
    if spread_bps == UNKNOWN:
        bid = _float_or_none(_first_known(source, "best_bid", "bid", "bid_price"))
        ask = _float_or_none(_first_known(source, "best_ask", "ask", "ask_price"))
        if bid is not None and ask is not None and bid > 0.0 and ask > 0.0:
            spread_bps = round((ask - bid) / ((ask + bid) / 2.0) * 10_000.0, 4)
    slippage_bps = number(
        "slippage_bps_per_side_or_unknown",
        "slippage_bps_per_side",
        "estimated_slippage_bps",
        "entry_slippage_bps_estimate",
        "projected_slippage_bps",
    )
    depth_usd = number(
        "available_depth_usd",
        "book_depth_usd",
        "top_of_book_depth_usd",
        "depth_usd",
        "liquidity_usd",
    )
    minimum_depth_usd = number(
        "minimum_liquidity_usd",
        "min_liquidity_usd",
        "min_depth_usd",
        "liquidity_floor_usd",
    )
    depth_value = _float_or_none(depth_usd)
    minimum_depth_value = _float_or_none(minimum_depth_usd)
    if depth_value is None:
        depth_status = UNKNOWN
        depth_ratio = UNKNOWN
        depth_penalty = 0.0
    elif depth_value <= 0.0:
        depth_status = "unavailable"
        depth_ratio = 0.0
        depth_penalty = 25.0
    elif minimum_depth_value is not None and minimum_depth_value > 0.0:
        depth_ratio = round(depth_value / minimum_depth_value, 6)
        if depth_value < minimum_depth_value:
            depth_status = "thin_relative_to_declared_minimum"
            depth_penalty = min(25.0, (1.0 - max(0.0, depth_ratio)) * 25.0)
        else:
            depth_status = "observed"
            depth_penalty = 0.0
    else:
        depth_status = "observed"
        depth_ratio = UNKNOWN
        depth_penalty = 0.0
    shortability_raw = _first_known(
        source,
        "shortability_status",
        "shortable_status",
        "is_shortable",
        "instrument_shortable",
        "instrument_margin_shortable",
        "supports_spot_short",
    )
    if shortability_raw == UNKNOWN and isinstance(source.get("venue_capabilities"), dict):
        shortability_raw = _first_known(
            source["venue_capabilities"],
            "supports_spot_short",
            "spot_short_support",
            "shortability",
        )
    shortability_status = _route_fact_status(
        shortability_raw,
        required=applies,
        unresolved=applies,
    )
    maker_fee_bps = route_diagnostics.get("maker_taker_fee_stack_bps", {}).get("maker_bps", UNKNOWN)
    taker_fee_bps = route_diagnostics.get("maker_taker_fee_stack_bps", {}).get("taker_bps", UNKNOWN)
    borrow_fee_bps = route_diagnostics.get("estimated_borrow_fee_bps", UNKNOWN)
    borrow_availability = route_diagnostics.get("borrow_availability", UNKNOWN)
    margin_mode = route_diagnostics.get("margin_mode", UNKNOWN)
    shortability_api_status = route_diagnostics.get("api_route_status", UNKNOWN)

    cost_parts = [
        _float_or_none(taker_fee_bps),
        _float_or_none(slippage_bps),
    ]
    spread_value = _float_or_none(spread_bps)
    if spread_value is not None:
        cost_parts.append(spread_value / 2.0)
    known_one_way_cost_bps = (
        round(sum(value for value in cost_parts if value is not None), 4)
        if any(value is not None for value in cost_parts)
        else UNKNOWN
    )
    observed = {
        "borrow_availability": borrow_availability,
        "estimated_borrow_fee_bps": borrow_fee_bps,
        "maker_fee_bps": maker_fee_bps,
        "taker_fee_bps": taker_fee_bps,
        "margin_mode": margin_mode,
        "shortability_status": shortability_status,
        "shortability_api_status": shortability_api_status,
        "quote_freshness_status": freshness.get("status", UNKNOWN),
        "spread_bps": spread_bps,
        "depth_usd": depth_usd,
        "slippage_bps_per_side": slippage_bps,
    }
    missing = [key for key, value in observed.items() if _route_metadata_unconfirmed(value)]
    # A bounded, diagnostic ordering signal.  This is intentionally separate
    # from the candidate score so route economics cannot become an entry gate.
    cost_value = _float_or_none(known_one_way_cost_bps)
    ranking_score = (
        100.0
        - min(70.0, max(0.0, cost_value or 0.0))
        - min(45.0, len(missing) * 5.0)
        - depth_penalty
    )
    outcome_rank_score = _float_or_none(outcome_ranking.get("outcome_rank_score"))
    # Outcome evidence is deliberately a bounded secondary ranking input.  It
    # cannot erase a priceable candidate or override the current route facts.
    if applies and outcome_rank_score is not None:
        ranking_score = ranking_score * 0.75 + max(0.0, min(100.0, outcome_rank_score)) * 0.25
    return {
        "telemetry_version": "frontier_short_spot_route_economics_v1",
        "paper_only": True,
        "read_only": True,
        "prepared_before_ranking": True,
        "applies": applies,
        "venue": str(source.get("venue") or resolved_route.get("venue") or UNKNOWN),
        "inst_id": str(source.get("inst_id") or resolved_route.get("inst_id") or UNKNOWN),
        "borrow": {
            "availability": borrow_availability,
            "estimated_fee_bps": borrow_fee_bps,
        },
        "fees": {"maker_bps": maker_fee_bps, "taker_bps": taker_fee_bps},
        "margin": {
            "required": bool(resolved_route.get("margin_required") or source.get("margin_required") or applies),
            "mode": margin_mode,
            "eligibility": _route_fact_status(
                margin_mode,
                required=bool(resolved_route.get("margin_required") or source.get("margin_required") or applies),
                unresolved=applies,
            ),
        },
        "shortability_status": shortability_status,
        "shortability_api_status": shortability_api_status,
        # Keep canonical values alongside the grouped packet so report,
        # ranking, and size consumers do not have to infer a field path.
        "borrow_availability": borrow_availability,
        "maker_fee_bps": maker_fee_bps,
        "taker_fee_bps": taker_fee_bps,
        "margin_eligibility": _route_fact_status(
            margin_mode,
            required=bool(resolved_route.get("margin_required") or source.get("margin_required") or applies),
            unresolved=applies,
        ),
        "api_permission_status": shortability_api_status,
        "quote_freshness_status": freshness.get("status", UNKNOWN),
        "spread_bps": spread_bps,
        "depth_usd": depth_usd,
        "slippage_estimate_bps": slippage_bps,
        "api_permission": {
            "status": shortability_api_status,
            "private_api_probe_performed": False,
        },
        "quote_freshness": {
            "status": freshness.get("status", UNKNOWN),
            "notes": list(freshness.get("notes") or []),
            "age_seconds": quote_age_seconds if quote_age_seconds is not None else UNKNOWN,
        },
        "market_impact_proxies": {
            "spread_bps": spread_bps,
            "depth_usd": depth_usd,
            "minimum_depth_usd": minimum_depth_usd,
            "depth_ratio_to_declared_minimum": depth_ratio,
            "depth_status": depth_status,
            "slippage_bps_per_side": slippage_bps,
            "known_one_way_cost_bps": known_one_way_cost_bps,
        },
        "paper_outcome_diagnostic": outcome_diagnostic,
        "missing_telemetry": missing,
        "ranking_hook": {
            "mode": "paper_ordering_only",
            "score_adjustment": 0.0,
            "route_economics_rank_score": round(max(0.0, ranking_score), 4),
            "outcome_rank_score": outcome_rank_score if outcome_rank_score is not None else UNKNOWN,
            "outcome_ranking_applied": bool(applies and outcome_rank_score is not None),
            "ranking_action": (
                "down_rank_only"
                if applies
                and (missing or cost_value is not None or (outcome_rank_score is not None and outcome_rank_score < 50.0))
                else "no_rank_adjustment"
            ),
        },
        "sizing_hook": {
            "mode": "paper_notional_scaling_only",
            "recommended_paper_allocation_multiplier": round(
                max(0.35, min(1.0, ranking_score / 100.0)), 4
            ) if applies else 1.0,
            "depth_penalty_bps_equivalent": round(depth_penalty, 4),
            "routing_decision_changed": False,
        },
        "hard_blocking": False,
        "entry_blocked": False,
        "routing_decision_changed": False,
    }


def build_short_frontier_spot_route_outcome_diagnostics(
    observations: Iterable[dict[str, Any]] | dict[str, Any],
) -> dict[str, Any]:
    """Join observed short-frontier paper outcomes to route-review metadata.

    Signal statistics are evidence about a *route slice*, not proof that a
    candidate should be suppressed.  This helper intentionally creates only
    read-only diagnostic and paper-ordering inputs for the route hunter and
    build planner.  It does not mutate candidates, probe venues, access
    accounts, or return an entry-blocking decision.
    """

    rows = _route_outcome_observation_rows(observations)
    diagnostics: list[dict[str, Any]] = []
    for observation in rows:
        signal_key = str(observation.get("signal_key") or "")
        direction = str(observation.get("direction") or "").strip().lower()
        trade_type = str(observation.get("trade_type") or "").strip().lower()
        if signal_key:
            signal_parts = [part.strip() for part in signal_key.split("|")]
            direction = direction or _signal_part(signal_parts, "short_frontier_spot")
            trade_type = trade_type or _signal_part(signal_parts, "frontier_crypto_venue_map")
        if direction != "short_frontier_spot" or trade_type != "frontier_crypto_venue_map":
            continue

        venue = str(observation.get("venue") or "").strip().upper()
        if not venue:
            venue = _short_frontier_venue_from_signal(signal_key)
        closed_count = _nonnegative_int(observation.get("closed_count"))
        avg_pnl_bps = _float_or_none(observation.get("avg_pnl_bps"))
        win_rate = _float_or_none(observation.get("win_rate"))
        weak_outcome = bool(
            (avg_pnl_bps is not None and avg_pnl_bps < 0.0)
            or (closed_count > 0 and win_rate is not None and win_rate < 0.5)
        )
        # Preserve unknown metrics as a neutral, visible route-review input.
        outcome_rank_score = 50.0
        if avg_pnl_bps is not None:
            outcome_rank_score += max(-25.0, min(25.0, avg_pnl_bps / 2.0))
        if win_rate is not None:
            outcome_rank_score += max(-15.0, min(15.0, (win_rate - 0.5) * 30.0))
        outcome_rank_score = round(max(0.0, min(100.0, outcome_rank_score)), 4)
        diagnostic_dimensions = {
            "borrow_permissions": {
                "review_fields": ["required_permissions", "borrow_availability", "borrow_fee_bps"],
                "purpose": "separate borrow or permission friction from observed outcome",
            },
            "fees": {
                "review_fields": ["maker_fee_bps", "taker_fee_bps", "fee_tier"],
                "purpose": "attribute fee-stack uncertainty without excluding paper observations",
            },
            "margin_constraints": {
                "review_fields": ["margin_required", "margin_mode", "shortability_status"],
                "purpose": "record margin-route constraints as metadata",
            },
            "api_reliability": {
                "review_fields": ["api_route_status", "freshness_latency_status", "endpoint_constraints"],
                "purpose": "compare public-data reliability with route assumptions",
            },
            "spread_liquidity": {
                "review_fields": ["spread_bps", "slippage_bps_per_side", "minimum_liquidity_usd"],
                "purpose": "measure market-impact proxies for paper ordering",
            },
            "carry": {
                "review_fields": ["borrow_fee_bps", "carry_bps_horizon", "funding_drag_bps_horizon"],
                "purpose": "attribute carry or borrow drag separately from raw paper PnL",
            },
        }
        diagnostics.append(
            {
                "paper_only": True,
                "read_only": True,
                "venue": venue or UNKNOWN,
                "signal_key": signal_key or UNKNOWN,
                "trade_type": trade_type,
                "direction": direction,
                "observed_outcome": {
                    "closed_count": closed_count,
                    "avg_pnl_bps": round(avg_pnl_bps, 4) if avg_pnl_bps is not None else UNKNOWN,
                    "win_rate": round(win_rate, 4) if win_rate is not None else UNKNOWN,
                    "score_adjustment": observation.get("score_adjustment", 0.0),
                },
                "outcome_status": "weak_paper_outcome" if weak_outcome else "paper_outcome_observed",
                "route_diagnostic_dimensions": diagnostic_dimensions,
                "ranking_input": {
                    "mode": "paper_ordering_only",
                    "outcome_rank_score": outcome_rank_score,
                    "ranking_action": "diagnose_and_down_rank_only" if weak_outcome else "diagnose_only",
                    "score_adjustment": 0.0,
                },
                "paper_candidate_emission": "retained_for_paper_exploration",
                "hard_blocking": False,
                "entry_blocked": False,
                "routing_decision_changed": False,
            }
        )
    diagnostics.sort(
        key=lambda item: (
            item["outcome_status"] != "weak_paper_outcome",
            item["ranking_input"]["outcome_rank_score"],
            item["venue"],
        )
    )
    return {
        "diagnostic_version": "short_frontier_spot_route_outcomes_v1",
        "paper_only": True,
        "read_only": True,
        "route_count": len(diagnostics),
        "diagnostic_dimensions": list(SHORT_FRONTIER_SPOT_ROUTE_DIAGNOSTIC_DIMENSIONS),
        "routes": diagnostics,
        "hard_blocking": False,
        "entry_blocked": False,
        "paper_policy": "outcomes_are_route_diagnostics_and_ranking_inputs_not_exclusion",
    }


def _route_outcome_observation_rows(
    observations: Iterable[dict[str, Any]] | dict[str, Any],
) -> list[dict[str, Any]]:
    """Normalize list and venue-keyed outcome payloads without I/O."""

    if isinstance(observations, dict):
        if any(key in observations for key in ("signal_key", "direction", "trade_type")):
            return [dict(observations)]
        return [
            {**dict(value), "venue": value.get("venue") or venue}
            for venue, value in observations.items()
            if isinstance(value, dict)
        ]
    return [dict(value) for value in observations if isinstance(value, dict)]


def _signal_part(parts: list[str], expected: str) -> str:
    return expected if any(part.lower() == expected for part in parts) else ""


def _short_frontier_venue_from_signal(signal_key: str) -> str:
    parts = [part.strip() for part in str(signal_key or "").split("|")]
    for index, part in enumerate(parts):
        if part.lower() == "frontier_crypto_venue_map" and index:
            return parts[index - 1].upper()
    return parts[0].upper() if parts else UNKNOWN


def _nonnegative_int(value: Any) -> int:
    number = _float_or_none(value)
    return max(0, int(number)) if number is not None else 0


def _frontier_short_spot_route_requirements_report(
    source: dict[str, Any],
    route: dict[str, Any],
    intelligence: dict[str, Any],
) -> dict[str, Any]:
    """Return the stable pre-ranking route snapshot for one frontier short.

    The snapshot deliberately reflects only metadata already on the paper
    candidate or its resolved paper route.  In particular, ``api_connectivity``
    is a reported API-path state, not an API probe, and every unknown remains a
    non-blocking diagnostic.
    """

    registry = source.get("paper_route_registry")
    registry = registry if isinstance(registry, dict) else {}
    registry_status = str(
        registry.get("support_status")
        or source.get("paper_route_registry_status")
        or UNKNOWN
    ).strip().lower()
    if registry_status == UNKNOWN:
        # Batch reports can be built directly from scanner output before the
        # resolver has attached its registry annotation.  Consult the same
        # read-only paper registry here so venue status remains visible in
        # that pre-enrichment reporting path as well.
        registry_status = str(
            assess_paper_route_registry(source).get("support_status") or UNKNOWN
        ).strip().lower()
    resolved_status = str(
        route.get("route_status") or source.get("route_status") or UNKNOWN
    ).strip().lower()
    venue_status = registry_status if registry_status != UNKNOWN else resolved_status
    api_status = intelligence.get("api_route_status", UNKNOWN)
    margin_mode = intelligence.get("margin_mode", UNKNOWN)
    freshness_status = intelligence.get("freshness_latency_status", UNKNOWN)
    fee_estimates = dict(intelligence.get("fee_estimates") or {})
    return {
        "report_version": "frontier_short_spot_route_requirements_v1",
        "paper_only": True,
        "read_only": True,
        "prepared_before_ranking_and_sizing": True,
        "applies": bool(intelligence.get("applies")),
        "candidate": {
            "venue": str(source.get("venue") or route.get("venue") or UNKNOWN),
            "inst_id": str(source.get("inst_id") or route.get("inst_id") or UNKNOWN),
            "direction": str(source.get("direction") or route.get("direction") or UNKNOWN),
        },
        "per_venue_status": {
            "status": venue_status,
            "registry_status": registry_status,
            "resolved_route_status": resolved_status,
            "capability_profile": str(
                (source.get("venue_capabilities") or {}).get("capability_profile")
                if isinstance(source.get("venue_capabilities"), dict)
                else UNKNOWN
            ),
        },
        "borrow_availability": intelligence.get("borrow_availability", UNKNOWN),
        "fee_tiers": fee_estimates,
        "margin_eligibility": {
            "required": bool(intelligence.get("margin_required")),
            "mode": margin_mode,
        },
        "api_connectivity": {
            "status": api_status,
            "path_readiness": (
                "unconfirmed"
                if str(api_status).lower() in {UNKNOWN, "public_data_only", "not_checked", "unconfirmed"}
                else "unavailable"
                if str(api_status).lower() in {"unavailable", "unsupported", "missing", "blocked"}
                else "observed"
            ),
            "probe_performed": False,
        },
        "route_freshness": {
            "status": freshness_status,
            "notes": list(intelligence.get("freshness_latency_notes") or []),
        },
        "route_economics_telemetry": dict(intelligence.get("route_economics_telemetry") or {}),
        "route_feasibility_reason": intelligence.get("route_feasibility_reason", UNKNOWN),
        "paper_active_scoring_eligible": bool(
            intelligence.get("paper_active_scoring_eligible", source.get("paper_active_scoring_eligible", True))
        ),
        "paper_route_feasibility_shadow_label": bool(
            intelligence.get(
                "paper_route_feasibility_shadow_label",
                source.get("paper_route_feasibility_shadow_label", False),
            )
        ),
        "hard_blocking": False,
        "entry_blocked": False,
        "routing_decision_changed": False,
    }


def _conditional_short_route_applies(
    direction: str,
    route_status: str,
    source: dict[str, Any],
    *,
    requirements: Any,
    missing_requirements: Any,
) -> bool:
    """Identify short routes that need paper-only route context.

    A maintained venue can label a direct route ``standard`` even when the
    strategy itself is a conditional spot short.  Direction and declared
    short-route requirements therefore take precedence over that status.
    """

    if "short" not in str(direction or "").lower():
        return False
    conditional_statuses = {
        "conditional",
        "route_unknown",
        "unsupported_or_unknown",
        "paper_testable_via_proxy",
        "blocked_until_requirements_confirmed",
    }
    if str(route_status or "").lower() in conditional_statuses:
        return True
    direction_token = str(direction or "").lower()
    if direction_token in {"short_frontier_spot", "long_perp_short_spot"}:
        return True
    descriptors = " ".join(
        str(source.get(key) or "")
        for key in ("market_key", "signal_key", "strategy_profile", "route_type")
    ).lower()
    if "conditional" in descriptors:
        return True
    def _contains_spot_borrow(value: Any) -> bool:
        values = (value,) if isinstance(value, str) else (value or [])
        return any("spot_borrow" in str(item).lower() for item in values)

    return _contains_spot_borrow(requirements) or _contains_spot_borrow(missing_requirements)


def _route_fact_status(value: Any, *, required: bool, unresolved: bool) -> str:
    """Normalize a route fact while retaining unknowns as report diagnostics."""

    if value == UNKNOWN or value in (None, ""):
        return "unconfirmed" if unresolved else "not_applicable" if not required else UNKNOWN
    boolean = _bool_flag(value)
    if boolean is True:
        return "available"
    if boolean is False:
        return "unavailable"
    return str(value)


def build_candidate_route_requirement_summary(
    opportunity: dict[str, Any],
    *,
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the six route facts every paper candidate report must display.

    The summary is a read-only projection of observed and maintained route
    metadata.  In particular, an unknown entitlement, borrow fact, fee, or
    freshness observation stays visible for paper ranking and guard-value
    measurement; it cannot suppress the candidate or alter routing.
    """

    source = dict(opportunity or {})
    facts = dict(row or {})
    diagnostics = facts.get("conditional_short_route_diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    frontier = facts.get("frontier_short_spot_route_intelligence")
    frontier = frontier if isinstance(frontier, dict) else {}
    fee_stack = facts.get("fee_stack_bps_estimate_or_unknown")
    fee_stack = fee_stack if isinstance(fee_stack, dict) else {}
    freshness_notes = facts.get("freshness_latency_notes")
    if not isinstance(freshness_notes, list):
        freshness_notes = _freshness_latency_notes(source)["notes"]

    maker_fee = facts.get("maker_fee_bps_or_unknown", diagnostics.get("maker_taker_fee_stack_bps", {}).get("maker_bps", UNKNOWN))
    taker_fee = facts.get("taker_fee_bps_or_unknown", diagnostics.get("maker_taker_fee_stack_bps", {}).get("taker_bps", UNKNOWN))
    round_trip_taker_fee = fee_stack.get(
        "estimated_round_trip_taker_bps",
        diagnostics.get("maker_taker_fee_stack_bps", {}).get("estimated_round_trip_taker_bps", UNKNOWN),
    )
    api_status = facts.get("api_route_status", diagnostics.get("api_route_status", _first_known(source, "api_access_status", "venue_api_status")))
    borrow_status = facts.get(
        "borrow_availability_status",
        diagnostics.get("borrow_availability", _first_known(source, "borrow_availability_status", "borrow_available", "borrowable")),
    )
    margin_mode = facts.get("margin_mode", diagnostics.get("margin_mode", _first_known(source, "margin_mode", "margin_account_mode")))
    freshness_status = facts.get("freshness_latency_status", frontier.get("freshness_latency_status", _freshness_latency_notes(source)["status"]))

    required_permissions = facts.get(
        "required_permissions", _first_known(source, "required_permissions")
    )
    route_blockers = facts.get("route_blockers", _route_blockers(source))
    broker_status = facts.get("broker_permission_status", UNKNOWN)
    if broker_status == UNKNOWN:
        extraction = source.get("route_requirement_extraction")
        extraction = extraction if isinstance(extraction, dict) else {}
        broker_requirement = (extraction.get("requirements") or {}).get(
            "broker_permissions"
        )
        if isinstance(broker_requirement, dict):
            broker_status = broker_requirement.get("status", UNKNOWN)
    if broker_status == UNKNOWN and route_blockers:
        broker_status = "needs_confirmation"

    def requires_spot_borrow(value: Any) -> bool:
        values = (value,) if isinstance(value, str) else (value or [])
        return any("spot_borrow" in str(item).lower() for item in values)

    explicit_borrow_required = _bool_flag(facts.get("borrow_required"))
    borrow_required = (
        explicit_borrow_required is True
        or _bool_flag(facts.get("requires_spot_borrow")) is True
        or requires_spot_borrow(required_permissions)
        or requires_spot_borrow(route_blockers)
    )
    borrow_asset = facts.get("borrow_asset", UNKNOWN)
    if borrow_required and borrow_asset in (None, "", "not_applicable", UNKNOWN):
        borrow_asset = _borrow_asset(str(facts.get("inst_id") or source.get("inst_id") or ""))
    short_required = "short" in str(facts.get("direction", source.get("direction", ""))).lower() or borrow_required
    shortability = facts.get("shortability_status")
    if shortability in (None, ""):
        shortability = _shortability_status(source, short_required=short_required)
    order_type_support = facts.get("order_type_support")
    if not isinstance(order_type_support, dict):
        order_type_support = _order_type_support(source)
    margin_spot_constraints = facts.get("margin_spot_constraints")
    if not isinstance(margin_spot_constraints, dict):
        margin_spot_constraints = {
            "margin_required": facts.get("margin_required", _first_known(source, "margin_required")),
            "margin_mode": margin_mode,
            "spot_leg_required": "spot" in str(facts.get("direction", source.get("direction", ""))).lower(),
        }
    fee_tier = facts.get(
        "fee_tier",
        _first_known(source, "fee_tier", "fee_tier_name", "maker_taker_fee_tier", "fee_class", "fee_model"),
    )
    api_permission_status = facts.get("api_permission_status", api_status)
    route_friction = facts.get("route_friction_summary")
    if not isinstance(route_friction, dict):
        route_friction = build_route_friction_summary(source, row=facts, diagnostics=diagnostics)

    return {
        "summary_version": "paper_route_requirement_summary_v1",
        "paper_only": True,
        "read_only": True,
        "non_blocking": True,
        "candidate_remains_priceable": True,
        "use": "paper_ranking_and_guard_value_measurement_only",
        "routing_decision_changed": False,
        "candidate": {
            "venue": facts.get("venue", _venue(source)),
            "inst_id": facts.get("inst_id", source.get("inst_id") or UNKNOWN),
            "direction": facts.get("direction", source.get("direction") or UNKNOWN),
        },
        "broker_venue_eligibility": {
            "route_status": facts.get("route_status", source.get("route_status") or UNKNOWN),
            "broker_permission_status": broker_status,
            "required_permissions": required_permissions,
            "route_blockers": route_blockers,
        },
        "short_borrow_availability": {
            "short_required": borrow_required,
            "shortability_status": shortability,
            "borrow_required": borrow_required,
            "borrow_asset": borrow_asset,
            "availability_status": borrow_status,
            "borrow_fee_bps_estimate": facts.get("borrow_fee_bps_estimate_or_unknown", UNKNOWN),
        },
        "margin_mode": {
            "required": facts.get("margin_required", _first_known(source, "margin_required")),
            "mode": margin_mode,
            "spot_constraints": margin_spot_constraints,
        },
        "fee_estimate": {
            "fee_tier": fee_tier,
            "maker_bps": maker_fee,
            "taker_bps": taker_fee,
            "estimated_round_trip_taker_bps": round_trip_taker_fee,
            "route_cost_bps_paper": facts.get("route_cost_bps_paper", UNKNOWN),
        },
        "api_entitlement": {
            "venue_api_requirement": facts.get("venue_api_requirement", UNKNOWN),
            "entitlement_status": api_permission_status,
            "path_readiness": facts.get("api_path_readiness", UNKNOWN),
            "endpoint_constraints": facts.get("endpoint_constraints", UNKNOWN),
        },
        "order_type_support": order_type_support,
        "freshness": {
            "status": freshness_status,
            "state": _first_known(source, "freshness_state", "data_freshness_state"),
            "age_seconds": _first_known(source, "freshness_age_seconds", "data_age_seconds"),
            "notes": list(freshness_notes),
            "stale_flags": list(facts.get("stale_data_flags") or []),
        },
        "route_friction_summary": route_friction,
    }


def build_route_requirements_report(
    opportunities: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Return a JSON-serializable paper-only route requirements report."""

    opportunities = list(opportunities)
    routes = build_route_requirements_matrix(opportunities)
    return {
        "paper_only": True,
        "read_only": True,
        "report_scope": "pre_promotion_route_requirements",
        "route_requirement_checklist_fields": list(ROUTE_REQUIREMENT_CHECKLIST_FIELDS),
        "paper_recommendation_output_policy": {
            "required_checklist_fields": list(ROUTE_REQUIREMENT_CHECKLIST_FIELDS),
            "rule": (
                "A paper recommendation must carry every route-requirement "
                "checklist field; unknown values remain read-only diagnostics "
                "and do not suppress the underlying paper opportunity."
            ),
            "missing_checklist_action": "needs_route_validation",
        },
        "promotion_review": {
            "required_before_route_promotion": True,
            "mode": "report_only",
            "rule": (
                "Review permission, borrow, fee, margin, and endpoint constraints "
                "before promoting a route; this report does not change promotion or execution state."
            ),
        },
        "safety_constraints": list(PAPER_ONLY_CONSTRAINTS),
        "fields": list(ROUTE_REQUIREMENT_FIELDS),
        "routes": routes,
        # A stable, concise view for report consumers.  It repeats no routing
        # decision and is deliberately suitable only for paper ranking and
        # counterfactual guard-value measurement.
        "candidate_route_requirement_summaries": [
            dict(row["route_requirement_summary"])
            for row in routes
            if isinstance(row.get("route_requirement_summary"), dict)
        ],
        "route_friction_summary": summarize_route_friction(routes),
        "playbook_summary": build_route_playbook_summary(opportunities),
        "paper_feasibility_summary": build_route_feasibility_summary(opportunities),
    }


def summarize_route_friction(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate read-only route-friction evidence for paper report consumers."""

    friction_rows = [
        dict(row.get("route_friction_summary") or {})
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("route_friction_summary"), dict)
    ]
    applicable = [row for row in friction_rows if row.get("applies")]
    reason_counts: dict[str, int] = {}
    stale_count = illiquid_count = 0
    scores: list[float] = []
    for friction in applicable:
        score = _float_or_none(friction.get("friction_score"))
        if score is not None:
            scores.append(score)
        for reason in friction.get("friction_reasons") or []:
            reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + 1
        market_data = friction.get("stale_illiquid_diagnostics") or {}
        stale_count += int(bool(market_data.get("stale")))
        illiquid_count += int(bool(market_data.get("illiquid")))
    return {
        "summary_version": "paper_route_friction_rollup_v1",
        "paper_only": True,
        "read_only": True,
        "use": "paper_ranking_and_sizing_only",
        "candidate_count": len(friction_rows),
        "applicable_candidate_count": len(applicable),
        "average_friction_score": round(sum(scores) / len(scores), 4) if scores else UNKNOWN,
        "stale_diagnostic_count": stale_count,
        "illiquid_diagnostic_count": illiquid_count,
        "friction_reason_counts": dict(sorted(reason_counts.items())),
        "hard_blocking": False,
        "entry_blocked": False,
        "routing_decision_changed": False,
        "paper_candidate_emission": "retained_for_paper_exploration",
    }


def build_conditional_paper_quality_gate(opportunities: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize paper-only opportunity gates caused by route or data quality.

    This is diagnostic output only. It does not alter route capabilities, place
    orders, or enable live execution.
    """

    gated: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    decay_flip_guard_count = 0

    for opportunity in opportunities:
        blockers = _route_blockers(opportunity)
        proxy_route = _paper_proxy_route(opportunity, blockers=blockers)
        paper_feasibility = _paper_feasibility(
            opportunity,
            blockers=blockers,
            paper_proxy_route=proxy_route,
        )
        route_feasible_paper = _route_feasible_paper(
            opportunity,
            blockers=blockers,
            paper_proxy_route=proxy_route,
        )
        route_friction_bps, _ = _paper_route_cost_bps(
            opportunity,
            blockers=blockers,
            paper_proxy_route=proxy_route,
        )
        feasibility_fields = _annotate_route_feasibility_fields(
            opportunity,
            {},
            blockers=blockers,
            paper_proxy_route=proxy_route,
            paper_feasibility=paper_feasibility,
            route_feasible_paper=route_feasible_paper,
            route_cost_bps_paper=route_friction_bps,
        )
        feasibility_state = str(feasibility_fields.get("feasibility_state") or UNKNOWN)
        requirement_checklist = _paper_route_requirement_checklist(
            opportunity,
            blockers=blockers,
            borrow_required=feasibility_state == "requires_borrow",
            requires_margin_permission=True if feasibility_state == "requires_margin" else UNKNOWN,
            feasibility_state=feasibility_state,
        )
        route_confidence = _paper_route_confidence(
            requirement_checklist,
            route_required=_requires_frontier_spot_short_route(opportunity),
            feasibility_state=feasibility_state,
        )
        edge_bps_estimate = _first_known(
            opportunity,
            "edge_bps_estimate",
            "net_edge_after_borrow_cost_bps",
            "depth_adjusted_edge_bps",
        )
        edge_bps_number = _float_or_none(edge_bps_estimate)
        reasons = list(route_confidence["reason_codes"])
        reasons = list(_conditional_gate_reasons(opportunity))
        if feasibility_state == "unsupported":
            reasons.append("unsupported_route")
        elif feasibility_state == "requires_borrow":
            reasons.append("spot_borrow_unconfirmed")
            if edge_bps_number is None or route_friction_bps >= edge_bps_number:
                reasons.append("borrow_route_friction_exceeds_edge")
        elif feasibility_state == "requires_margin":
            reasons.append("margin_permission_unconfirmed")
            if edge_bps_number is None or route_friction_bps >= edge_bps_number:
                reasons.append("margin_route_friction_exceeds_edge")
        reasons = list(dict.fromkeys(reasons))
        if not reasons:
            continue
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        gated.append(
            {
                "market_key": _first_known(
                    opportunity,
                    "market_key",
                ),
                "inst_id": str(opportunity.get("inst_id") or opportunity.get("instrument") or UNKNOWN),
                "venue": _venue(opportunity),
                "direction": str(opportunity.get("direction") or UNKNOWN),
                "route_status": _paper_route_status(opportunity),
                "route_blockers": _route_blockers(opportunity),
                "feasibility_state": feasibility_state,
                "route_friction_bps": feasibility_fields.get("route_friction_bps", UNKNOWN),
                "paper_confidence_action": route_confidence["action"],
                "route_requirement_checks": requirement_checklist,
                "route_requirement_gaps": route_confidence["gap_fields"],
                "quality_action": str(opportunity.get("quality_action") or UNKNOWN),
                "edge_bps_estimate": edge_bps_estimate,
                "paper_policy_action": _conditional_paper_policy_action(opportunity),
                "paper_policy_guard": _conditional_decay_flip_guard(opportunity),
                "reasons": reasons,
                "paper_only": True,
            }
        )
    gated.sort(key=lambda item: (-len(item["reasons"]), str(item["inst_id"])))
    return {
        "paper_only": True,
        "policy_mode": "paper_policy",
        "gate_count": len(gated),
        "reason_counts": dict(sorted(reason_counts.items())),
        "paper_policy": {
            "conditional_short_decay_flip_guard": {
                **CONDITIONAL_SHORT_DECAY_FLIP_GUARD,
                "policy_scope": "diagnostic_read_only_output_only",
                "score_clamp_effective_state": "score_clamped_non_admissible",
                "exploit_more_recovery_rule": "positive_expectancy_must_be_reestablished_after_cooldown",
            },
        },
        "top_examples": gated[:10],
        "hard_limits": [
            "Diagnostic gate only.",
            (
                "Paper policy guidance is advisory output only and does not place "
                "orders, write broker state, or enable live execution."
            ),
            "No credentials, broker writes, account changes, or live orders.",
        ],
    }


def build_build_governor_fields(opportunities: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return read-only Build Governor fields for paper-route diagnostics.

    The output is informational only and is safe for paper/reporting use.
    """

    opportunities = list(opportunities)
    return {
        "paper_only": True,
        "opportunity_count": len(opportunities),
        "route_count": len(build_route_requirements_matrix(opportunities)),
        "quality_gate": build_conditional_paper_quality_gate(opportunities),
        "paper_feasibility_summary": build_route_feasibility_summary(opportunities),
        "report_summary": build_route_playbook_summary(opportunities),
    }


def route_requirements_json(opportunities: Iterable[dict[str, Any]]) -> str:
    """Return an optional JSON sidecar payload without writing files."""

    return json.dumps(
        build_route_requirements_report(opportunities),
        indent=2,
        sort_keys=True,
    )


def render_route_requirements_markdown(
    opportunities: Iterable[dict[str, Any]],
) -> str:
    """Render a paper-only markdown matrix without writing files."""

    report = build_route_requirements_report(opportunities)
    fields = report["fields"]
    lines = [
        "# Route Intelligence Requirements Matrix",
        "",
        (
            "Paper-only read-only output. No credentials, no private API calls, "
            "no live trading, and no order execution changes."
        ),
        "",
        "| " + " | ".join(fields) + " |",
        "| " + " | ".join("---" for _ in fields) + " |",
    ]
    for row in report["routes"]:
        lines.append("| " + " | ".join(_markdown_value(row[field]) for field in fields) + " |")
    playbooks = report.get("playbook_summary", {})
    groups = playbooks.get("top_blocker_groups", [])
    lines.extend(["", "## Route Blocker Playbooks", ""])
    if not groups:
        lines.append("No route blockers found.")
    for group in groups[:10]:
        lines.append(
            f"- `{group.get('blocker')}` count=`{group.get('count')}` "
            f"family=`{(group.get('playbook') or {}).get('route_family')}`"
        )
    return "\n".join(lines) + "\n"


def build_route_playbook_summary(opportunities: Iterable[dict[str, Any]]) -> dict[str, Any]:
    max_affected = 10
    groups: dict[str, dict[str, Any]] = {}
    for opportunity in opportunities:
        inst_id = str(opportunity.get("inst_id") or UNKNOWN)
        for blocker in _route_blockers(opportunity):
            group = groups.setdefault(
                blocker,
                {
                    "blocker": blocker,
                    "count": 0,
                    "affected_instruments_top_10": [],
                    "playbook": _route_blocker_playbook(blocker),
                },
            )
            group["count"] += 1
            if len(group["affected_instruments_top_10"]) < max_affected and inst_id not in group["affected_instruments_top_10"]:
                group["affected_instruments_top_10"].append(inst_id)
    ordered = sorted(groups.values(), key=lambda row: (-int(row["count"]), str(row["blocker"])))
    return {
        "paper_only": True,
        "max_affected_instruments_per_group": max_affected,
        "top_blocker_groups": ordered,
        "hard_limits": [
            "Route playbooks are read-only planning notes.",
            "Credential collection, account changes, broker writes, and live orders remain unavailable in paper mode.",
        ],
    }


def build_route_feasibility_summary(opportunities: Iterable[dict[str, Any]]) -> dict[str, Any]:
    opportunities = list(opportunities)
    counts: dict[str, int] = {}
    feasibility_states: dict[str, int] = {}
    proxy_routes: dict[str, int] = {}
    route_types: dict[str, int] = {}
    confidence_actions: dict[str, int] = {}
    route_requirement_gap_counts: dict[str, int] = {}
    recommendation_actions: dict[str, int] = {}
    route_costs: list[float] = []
    for opportunity in opportunities:
        blockers = _route_blockers(opportunity)
        proxy_route = _paper_proxy_route(opportunity, blockers=blockers)
        feasibility = _paper_feasibility(
            opportunity,
            blockers=blockers,
            paper_proxy_route=proxy_route,
        )
        route_type = _route_type(opportunity, blockers=blockers)
        route_feasible_paper = _route_feasible_paper(
            opportunity,
            blockers=blockers,
            paper_proxy_route=proxy_route,
        )
        route_cost_bps_paper, _ = _paper_route_cost_bps(
            opportunity,
            blockers=blockers,
            paper_proxy_route=proxy_route,
        )
        feasibility_fields = _annotate_route_feasibility_fields(
            opportunity,
            {},
            blockers=blockers,
            paper_proxy_route=proxy_route,
            paper_feasibility=feasibility,
            route_feasible_paper=route_feasible_paper,
            route_cost_bps_paper=route_cost_bps_paper,
        )
        feasibility_state = str(feasibility_fields.get("feasibility_state") or UNKNOWN)
        counts[feasibility] = counts.get(feasibility, 0) + 1
        feasibility_states[feasibility_state] = feasibility_states.get(feasibility_state, 0) + 1
        route_types[route_type] = route_types.get(route_type, 0) + 1
        requirement_checklist = _paper_route_requirement_checklist(
            opportunity,
            blockers=blockers,
            borrow_required=feasibility_state == "requires_borrow",
            requires_margin_permission=True if feasibility_state == "requires_margin" else UNKNOWN,
            feasibility_state=feasibility_state,
        )
        route_confidence = _paper_route_confidence(
            requirement_checklist,
            route_required=_requires_frontier_spot_short_route(opportunity),
            feasibility_state=feasibility_state,
        )
        confidence_actions[route_confidence["action"]] = confidence_actions.get(route_confidence["action"], 0) + 1
        recommendation_action = str(feasibility_fields.get("paper_recommendation_action") or UNKNOWN)
        recommendation_actions[recommendation_action] = recommendation_actions.get(recommendation_action, 0) + 1
        if proxy_route != "not_applicable":
            for gap_field in route_confidence["gap_fields"]:
                route_requirement_gap_counts[gap_field] = route_requirement_gap_counts.get(gap_field, 0) + 1
            proxy_routes[proxy_route] = proxy_routes.get(proxy_route, 0) + 1
        if isinstance(route_feasible_paper, bool) and route_feasible_paper and isinstance(route_cost_bps_paper, float):
            route_costs.append(route_cost_bps_paper)
    estimated_cost_summary = {
        "count": len(route_costs),
        "average": round(sum(route_costs) / len(route_costs), 4) if route_costs else 0.0,
        "max": round(max(route_costs), 4) if route_costs else 0.0,
    }
    return {
        "paper_only": True,
        "counts_by_feasibility": dict(sorted(counts.items())),
        "counts_by_feasibility_state": dict(sorted(feasibility_states.items())),
        "counts_by_route_type": dict(sorted(route_types.items())),
        "counts_by_paper_confidence_action": dict(sorted(confidence_actions.items())),
        "route_requirement_gap_counts": dict(sorted(route_requirement_gap_counts.items())),
        "counts_by_paper_recommendation_action": dict(sorted(recommendation_actions.items())),
        "proxy_routes": dict(sorted(proxy_routes.items())),
        "estimated_route_cost_bps": estimated_cost_summary,
    }


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _numeric_field(opportunity: dict[str, Any], *keys: str) -> float | None:
    return _float_or_none(_first_known(opportunity, *keys))


_ROUTE_CHECKLIST_FIELDS = (
    "venue_supports_margin_or_equivalent",
    "shortable_inventory_declared",
    "borrow_cost_model_present",
    "fees_modeled",
    "order_api_surface_mapped",
)

_ROUTE_REQUIREMENT_REASON_CODES = {
    "venue_supports_margin_or_equivalent": "margin_support_unconfirmed",
    "shortable_inventory_declared": "short_inventory_unconfirmed",
    "borrow_cost_model_present": "borrow_cost_model_unconfirmed",
    "fees_modeled": "fees_unconfirmed",
    "order_api_surface_mapped": "order_api_surface_unconfirmed",
}


def _requirement_check_status(*, required: bool, satisfied: bool | None) -> str:
    if not required:
        return "not_applicable"
    if satisfied is True:
        return "satisfied"
    if satisfied is False:
        return "missing"
    return UNKNOWN


def _paper_route_requirement_checklist(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
    borrow_required: bool = False,
    requires_margin_permission: bool | str = UNKNOWN,
    feasibility_state: str = UNKNOWN,
) -> dict[str, str]:
    blockers = blockers if blockers is not None else _route_blockers(opportunity)
    route_requires_frontier_short = _requires_frontier_spot_short_route(opportunity)
    capability_confirmed = _frontier_spot_short_capability_confirmed(opportunity) is True
    explicit_inventory = _bool_flag(
        _first_known(
            opportunity,
            "shortable_inventory_declared",
            "short_inventory_confirmed",
            "inventory_locate_available",
        )
    )
    explicit_borrow_cost_model = _bool_flag(
        _first_known(
            opportunity,
            "borrow_cost_model_present",
            "borrow_fee_modeled",
        )
    )
    explicit_fees_modeled = _bool_flag(
        _first_known(
            opportunity,
            "fees_modeled",
            "entry_exit_fees_modeled",
        )
    )
    explicit_api_mapped = _bool_flag(
        _first_known(
            opportunity,
            "order_api_surface_mapped",
            "api_surface_mapped",
        )
    )
    borrow_cost_bps = _numeric_field(
        opportunity,
        "borrow_fee_bps_estimate_or_unknown",
        "borrow_cost_bps",
        "borrow_fee_bps_estimate",
    )
    fee_bps = _numeric_field(
        opportunity,
        "fee_bps_per_side_or_unknown",
        "fee_bps_per_side",
        "taker_fee_bps",
    )
    api_surface = _api_surface_required(
        opportunity,
        blockers=blockers,
        borrow_required=borrow_required,
        requires_margin_permission=requires_margin_permission,
    )
    venue_margin_required = route_requires_frontier_short or requires_margin_permission is True or borrow_required
    venue_margin_satisfied = True if capability_confirmed else False if feasibility_state == "unsupported" else None
    shortable_inventory_required = route_requires_frontier_short or borrow_required
    if explicit_inventory is not None:
        shortable_inventory_satisfied = explicit_inventory
    elif capability_confirmed:
        shortable_inventory_satisfied = True
    elif feasibility_state == "unsupported" and shortable_inventory_required:
        shortable_inventory_satisfied = False
    else:
        shortable_inventory_satisfied = None
    borrow_cost_model_required = borrow_required
    if explicit_borrow_cost_model is not None:
        borrow_cost_model_satisfied = explicit_borrow_cost_model
    elif borrow_cost_bps is not None:
        borrow_cost_model_satisfied = True
    elif feasibility_state == "unsupported" and borrow_cost_model_required:
        borrow_cost_model_satisfied = False
    else:
        borrow_cost_model_satisfied = None
    fees_modeled_required = route_requires_frontier_short
    if explicit_fees_modeled is not None:
        fees_modeled_satisfied = explicit_fees_modeled
    elif fee_bps is not None:
        fees_modeled_satisfied = True
    elif feasibility_state == "unsupported" and fees_modeled_required:
        fees_modeled_satisfied = False
    else:
        fees_modeled_satisfied = None
    order_api_surface_required = route_requires_frontier_short
    if explicit_api_mapped is not None:
        order_api_surface_satisfied = explicit_api_mapped
    elif capability_confirmed or (api_surface and str(api_surface).lower() != UNKNOWN):
        order_api_surface_satisfied = True
    elif feasibility_state == "unsupported" and order_api_surface_required:
        order_api_surface_satisfied = False
    else:
        order_api_surface_satisfied = None
    return {
        "venue_supports_margin_or_equivalent": _requirement_check_status(required=venue_margin_required, satisfied=venue_margin_satisfied),
        "shortable_inventory_declared": _requirement_check_status(required=shortable_inventory_required, satisfied=shortable_inventory_satisfied),
        "borrow_cost_model_present": _requirement_check_status(required=borrow_cost_model_required, satisfied=borrow_cost_model_satisfied),
        "fees_modeled": _requirement_check_status(required=fees_modeled_required, satisfied=fees_modeled_satisfied),
        "order_api_surface_mapped": _requirement_check_status(required=order_api_surface_required, satisfied=order_api_surface_satisfied),
    }


def _route_requirement_gap_fields(requirement_checklist: dict[str, str]) -> tuple[list[str], list[str]]:
    missing_fields: list[str] = []
    unknown_fields: list[str] = []
    for field in _ROUTE_CHECKLIST_FIELDS:
        status = str(requirement_checklist.get(field) or UNKNOWN).lower()
        if status == "missing":
            missing_fields.append(field)
        elif status == UNKNOWN:
            unknown_fields.append(field)
    return missing_fields, unknown_fields


def _route_requirement_checklist(
    opportunity: dict[str, Any],
    *,
    blockers: list[str],
    borrow_required: bool,
    requires_margin_permission: bool | str,
    operational_checklist: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Project route facts into a stable, read-only recommendation checklist.

    Every category is emitted even when its value is unknown or not applicable.
    This makes the difference between absent metadata and unresolved metadata
    explicit, without probing accounts, brokers, or private endpoints.
    """

    broker_permissions_confirmed = _bool_flag(
        _first_known(
            opportunity,
            "broker_permissions_confirmed",
            "venue_permissions_confirmed",
            "account_permissions_confirmed",
        )
    )
    borrow_available = _bool_flag(
        _first_known(
            opportunity,
            "borrow_available",
            "borrowable",
            "borrow_supported",
            "spot_borrow_supported",
        )
    )
    margin_available = _bool_flag(
        _first_known(
            opportunity,
            "margin_available",
            "margin_supported",
            "spot_margin_supported",
        )
    )
    api_coverage_mapped = _bool_flag(
        _first_known(
            opportunity,
            "api_coverage_mapped",
            "order_api_surface_mapped",
            "api_surface_mapped",
        )
    )
    fees_modeled = operational_checklist.get("fees_modeled", UNKNOWN)
    short_required = "short" in str(opportunity.get("direction") or "").lower() or borrow_required
    shortability = _shortability_status(opportunity, short_required=short_required)
    order_type_support = _order_type_support(opportunity)

    shortability_satisfied = (
        True if shortability in {"available", "confirmed", "supported"}
        else False if shortability in {"unavailable", "unsupported", "missing", "blocked"}
        else None
    )
    order_type_satisfied = (
        True if order_type_support["status"] == "supported"
        else False if order_type_support["status"] == "unsupported"
        else None
    )

    def entry(*, required: bool, status: str, value: Any) -> dict[str, Any]:
        return {
            "required_for_direct_route": required,
            "status": status,
            "value": value if value not in (None, "", [], {}, ()) else UNKNOWN,
            "read_only": True,
        }

    return {
        "broker_permissions": entry(
            required=True,
            status=_requirement_check_status(required=True, satisfied=broker_permissions_confirmed),
            value=_route_required_permissions(opportunity, blockers),
        ),
        "shortability": entry(
            required=short_required,
            status=_requirement_check_status(
                required=short_required,
                satisfied=shortability_satisfied,
            ),
            value=shortability,
        ),
        "borrow_availability": entry(
            required=borrow_required,
            status=_requirement_check_status(required=borrow_required, satisfied=borrow_available),
            value=_first_known(opportunity, "borrow_asset", "borrowable", "borrow_available", "borrow_supported"),
        ),
        "fees": entry(
            required=True,
            status=str(fees_modeled or UNKNOWN),
            value={
                "fee_bps_per_side": _first_known(opportunity, "fee_bps_per_side_or_unknown", "fee_bps_per_side"),
                "slippage_bps_per_side": _first_known(opportunity, "slippage_bps_per_side_or_unknown", "slippage_bps_per_side"),
            },
        ),
        "margin": entry(
            required=requires_margin_permission is True,
            status=_requirement_check_status(
                required=requires_margin_permission is True,
                satisfied=margin_available,
            ),
            value=_first_known(opportunity, "margin_required", "margin_available", "margin_supported"),
        ),
        "api_coverage": entry(
            required=True,
            status=_requirement_check_status(required=True, satisfied=api_coverage_mapped),
            value=_first_known(
                opportunity,
                "api_surface_required",
                "endpoint_constraints",
                "api_access_status",
            ),
        ),
        "order_type_support": entry(
            required=bool(order_type_support["required_order_types"] != [UNKNOWN]),
            status=_requirement_check_status(
                required=bool(order_type_support["required_order_types"] != [UNKNOWN]),
                satisfied=order_type_satisfied,
            ),
            value=order_type_support,
        ),
    }


def _route_requirement_checklist_complete(checklist: Any) -> bool:
    """Return whether all required checklist categories are structurally present."""

    if not isinstance(checklist, dict):
        return False
    for field in ROUTE_REQUIREMENT_CHECKLIST_FIELDS:
        item = checklist.get(field)
        if not isinstance(item, dict) or item.get("status") in (None, "") or "value" not in item:
            return False
    return True


def _route_requirements_panel(
    opportunity: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any]:
    """Project a matrix row into a non-blocking candidate/report panel."""

    checklist = row.get("route_requirement_checklist")
    checklist = checklist if isinstance(checklist, dict) else {}
    category_statuses = {
        category: str((checklist.get(category) or {}).get("status") or UNKNOWN)
        for category in ROUTE_REQUIREMENT_CHECKLIST_FIELDS
    }
    gaps = [
        category
        for category, status in category_statuses.items()
        if status.lower() in {UNKNOWN, "missing", "unconfirmed", "unavailable", "unsupported"}
    ]
    stale_data = _stale_data_diagnostics(opportunity)
    if stale_data["status"] == "stale":
        gaps.append("stale_data")
    gaps = list(dict.fromkeys(gaps))
    reason_codes = [f"{category}_gap" for category in gaps]
    api_status = str(row.get("api_route_status") or UNKNOWN).strip().lower()
    if api_status in {"available", "ready", "mapped", "confirmed"}:
        api_path_readiness = "ready"
    elif api_status in {"unknown", "unconfirmed", "not_checked", "public_data_only"}:
        api_path_readiness = "unconfirmed"
    elif api_status in {"missing", "unavailable", "unsupported", "blocked"}:
        api_path_readiness = "unavailable"
    else:
        api_path_readiness = api_status or UNKNOWN

    # These fields are intentionally recommendations for measurement consumers,
    # not instructions to the router.  A gap can inform a paper-size experiment
    # or a counterfactual guard-value calculation without excluding a candidate.
    sizing_guidance = {
        "paper_only": True,
        "non_blocking": True,
        "action": (
            "retain_candidate_for_route_aware_paper_sizing_review"
            if gaps
            else "standard_paper_sizing_review"
        ),
        "route_requirement_gap_count": len(gaps),
        "routing_decision_changed": False,
    }
    guard_value_measurement = {
        "paper_only": True,
        "non_blocking": True,
        "measure": "route_requirement_gap_counterfactual",
        "enabled": bool(gaps),
        "gap_categories": list(gaps),
        "routing_decision_changed": False,
    }
    return {
        "broker_permission_status": category_statuses["broker_permissions"],
        "api_path_readiness": api_path_readiness,
        "stale_data_status": stale_data["status"],
        "stale_data_flags": stale_data["flags"],
        "route_requirement_gaps": gaps,
        "route_requirement_gap_reason_codes": reason_codes,
        "paper_sizing_guidance": sizing_guidance,
        "guard_value_measurement": guard_value_measurement,
    }


def _stale_data_diagnostics(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Expose stale-data evidence without imposing a route or entry gate."""

    flags: list[str] = []
    freshness_state = str(
        _first_known(opportunity, "freshness_state", "data_freshness_state")
    ).strip().lower()
    data_status = str(_first_known(opportunity, "data_status", "source_status")).strip().lower()
    stale_minutes = _float_or_none(_first_known(opportunity, "stale_minutes"))
    freshness_age_seconds = _float_or_none(
        _first_known(opportunity, "freshness_age_seconds", "data_age_seconds")
    )
    stale_after_seconds = _float_or_none(
        _first_known(opportunity, "stale_after_seconds", "max_freshness_age_seconds")
    )

    if freshness_state in {"stale", "dangerously_stale", "expired"}:
        flags.append(f"freshness_state:{freshness_state}")
    if data_status in {"stale", "expired"}:
        flags.append(f"data_status:{data_status}")
    if stale_minutes is not None and stale_minutes > 90.0:
        flags.append("stale_minutes_over_90")
    if (
        freshness_age_seconds is not None
        and stale_after_seconds is not None
        and freshness_age_seconds > stale_after_seconds
    ):
        flags.append("freshness_age_exceeds_declared_limit")

    if flags:
        status = "stale"
    elif any(
        value not in (UNKNOWN, "")
        for value in (freshness_state, data_status)
    ) or stale_minutes is not None or freshness_age_seconds is not None:
        status = "fresh"
    else:
        status = UNKNOWN
    return {"status": status, "flags": flags}


def _route_metadata_unconfirmed(value: Any) -> bool:
    """Return whether a route fact still needs read-only validation."""

    if value in (None, "", [], {}, ()):
        return True
    return str(value).strip().lower() in {
        UNKNOWN,
        "needs_route_validation",
        "unconfirmed",
        "required_unconfirmed",
        "not_checked",
        "not_applicable",
        "missing",
        "unavailable",
        "unsupported",
        "blocked",
        "public_data_only",
    }


def _freshness_latency_notes(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Summarize existing quote freshness and latency without setting a gate."""

    notes: list[str] = []
    freshness_state = _first_known(opportunity, "freshness_state", "data_freshness_state")
    freshness_age = _float_or_none(
        _first_known(opportunity, "freshness_age_seconds", "data_age_seconds")
    )
    quote_age = _float_or_none(_first_known(opportunity, "quote_age_ms"))
    latency = _float_or_none(_first_known(opportunity, "latency_ms", "fetch_latency_ms"))
    depth_latency = _float_or_none(_first_known(opportunity, "depth_latency_ms"))
    stale = _stale_data_diagnostics(opportunity)

    if freshness_state != UNKNOWN:
        notes.append(f"freshness_state:{freshness_state}")
    if freshness_age is not None:
        notes.append(f"freshness_age_seconds:{round(freshness_age, 3)}")
    if quote_age is not None:
        notes.append(f"quote_age_ms:{round(quote_age, 3)}")
    if latency is not None:
        notes.append(f"latency_ms:{round(latency, 3)}")
    if depth_latency is not None:
        notes.append(f"depth_latency_ms:{round(depth_latency, 3)}")
    notes.extend(f"freshness_flag:{flag}" for flag in stale["flags"])

    if stale["status"] == "stale":
        status = "stale"
    elif notes:
        status = "observed"
    else:
        status = UNKNOWN
        notes.append("freshness_latency_metadata_unavailable")
    return {"status": status, "notes": list(dict.fromkeys(notes))}


def _paper_route_confidence(
    requirement_checklist: dict[str, str],
    *,
    route_required: bool,
    feasibility_state: str,
) -> dict[str, Any]:
    missing_fields, unknown_fields = _route_requirement_gap_fields(requirement_checklist)
    gap_fields = [*missing_fields, *unknown_fields]
    reason_codes = [
        _ROUTE_REQUIREMENT_REASON_CODES.get(field, f"{field}_unconfirmed")
        for field in gap_fields
    ]
    normalized_feasibility_state = str(feasibility_state or UNKNOWN).lower()
    if route_required and normalized_feasibility_state == UNKNOWN:
        reason_codes.append("route_feasibility_unconfirmed")
    if not route_required:
        action = "not_applicable"
    elif normalized_feasibility_state == "unsupported":
        action = "reject"
    elif normalized_feasibility_state in {"requires_borrow", "requires_margin"}:
        action = "paper_conditional"
    elif gap_fields or normalized_feasibility_state == UNKNOWN:
        action = "paper_conditional"
    else:
        action = "executable_paper"
    return {
        "action": action,
        "gap_fields": gap_fields,
        "missing_fields": missing_fields,
        "unknown_fields": unknown_fields,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "checklist_complete": not gap_fields,
        "route_required": route_required,
        "feasibility_state": feasibility_state,
        "paper_only": True,
    }


def _annotate_route_feasibility_fields(
    opportunity: dict[str, Any],
    row: dict[str, Any],
    *,
    blockers: list[str] | None = None,
    paper_proxy_route: str | None = None,
    paper_feasibility: str | None = None,
    route_feasible_paper: bool | str | None = None,
    route_cost_bps_paper: float | None = None,
) -> dict[str, Any]:
    blockers = blockers if blockers is not None else _route_blockers(opportunity)
    paper_proxy_route = paper_proxy_route or _paper_proxy_route(opportunity, blockers=blockers)
    paper_feasibility = paper_feasibility or _paper_feasibility(
        opportunity,
        blockers=blockers,
        paper_proxy_route=paper_proxy_route,
    )
    if route_feasible_paper is None:
        route_feasible_paper = _route_feasible_paper(
            opportunity,
            blockers=blockers,
            paper_proxy_route=paper_proxy_route,
        )
    if route_cost_bps_paper is None:
        route_cost_bps_paper, _ = _paper_route_cost_bps(
            opportunity,
            blockers=blockers,
            paper_proxy_route=paper_proxy_route,
        )
    borrow_required = _requires_spot_borrow(opportunity, blockers=blockers)
    requires_margin_permission = _requires_margin_permission(
        opportunity,
        blockers=blockers,
        borrow_required=borrow_required,
    )
    row["feasibility_state"] = _paper_route_feasibility_state(
        opportunity,
        blockers=blockers,
        paper_proxy_route=paper_proxy_route,
        paper_feasibility=paper_feasibility,
        route_feasible_paper=route_feasible_paper,
        borrow_required=borrow_required,
        requires_margin_permission=requires_margin_permission,
    )
    friction_bps = _float_or_none(route_cost_bps_paper)
    row["route_friction_bps"] = round(friction_bps, 4) if friction_bps is not None else UNKNOWN
    checklist = _paper_route_requirement_checklist(
        opportunity,
        blockers=blockers,
        borrow_required=borrow_required,
        requires_margin_permission=requires_margin_permission,
        feasibility_state=str(row["feasibility_state"] or UNKNOWN),
    )
    row.update(checklist)
    route_requirement_checklist = _route_requirement_checklist(
        opportunity,
        blockers=blockers,
        borrow_required=borrow_required,
        requires_margin_permission=requires_margin_permission,
        operational_checklist=checklist,
    )
    row["route_requirement_checklist"] = route_requirement_checklist
    row["route_requirement_checklist_complete"] = _route_requirement_checklist_complete(
        route_requirement_checklist
    )
    row["paper_recommendation_action"] = _paper_recommendation_action(
        opportunity,
        feasibility_state=str(row["feasibility_state"] or UNKNOWN),
        checklist=checklist,
    )
    row["paper_recommendation_reason"] = _paper_recommendation_reason(
        opportunity,
        feasibility_state=str(row["feasibility_state"] or UNKNOWN),
        checklist=checklist,
    )
    return row


def _paper_recommendation_action(
    opportunity: dict[str, Any],
    *,
    feasibility_state: str,
    checklist: dict[str, Any],
) -> str:
    if not _requires_frontier_spot_short_route(opportunity):
        return "allow_paper_evaluation"
    if feasibility_state == "supported":
        return "allow_paper_evaluation"
    if feasibility_state == "unsupported":
        return "suppress_from_paper_recommendations"
    if any(str(checklist.get(field) or UNKNOWN) == "missing" for field in _ROUTE_CHECKLIST_FIELDS):
        return "suppress_from_paper_recommendations"
    return "downgrade_confidence_and_label_unverified_route"


def _paper_recommendation_reason(
    opportunity: dict[str, Any],
    *,
    feasibility_state: str,
    checklist: dict[str, Any],
) -> str:
    if not _requires_frontier_spot_short_route(opportunity):
        return "not_applicable"
    if feasibility_state == "supported":
        return "explicit_route_supported"
    if feasibility_state == "unsupported":
        return "route_explicitly_unsupported"
    for field in _ROUTE_CHECKLIST_FIELDS:
        if str(checklist.get(field) or UNKNOWN) == "missing":
            return f"{field}_missing"
    for field in _ROUTE_CHECKLIST_FIELDS:
        if str(checklist.get(field) or UNKNOWN) == UNKNOWN:
            return f"{field}_unverified"
    return "route_unverified"


def _paper_route_feasibility_state(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
    paper_proxy_route: str | None = None,
    paper_feasibility: str | None = None,
    route_feasible_paper: bool | str | None = None,
    borrow_required: bool = False,
    requires_margin_permission: bool | str = UNKNOWN,
) -> str:
    paper_feasibility = str(
        paper_feasibility
        or _paper_feasibility(
            opportunity,
            blockers=blockers,
            paper_proxy_route=paper_proxy_route,
        )
        or UNKNOWN
    )
    if route_feasible_paper is None:
        route_feasible_paper = _route_feasible_paper(
            opportunity,
            blockers=blockers,
            paper_proxy_route=paper_proxy_route,
        )
    if paper_feasibility == "blocked" or route_feasible_paper is False:
        return "unsupported"
    if _frontier_spot_short_capability_confirmed(opportunity) is True:
        return "supported"
    if borrow_required:
        return "requires_borrow"
    if requires_margin_permission is True:
        return "requires_margin"
    if paper_feasibility in {"direct_feasible", "proxy_only"}:
        return "supported"
    return UNKNOWN


def _route_type(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
) -> str:
    explicit = str(
        _first_known(
            opportunity,
            "route_type",
            "route_profile",
            "execution_route",
        )
        or ""
    ).strip()
    if explicit and explicit.lower() != UNKNOWN:
        return explicit
    blockers = blockers if blockers is not None else _route_blockers(opportunity)
    combined = " ".join(
        str(
            _first_known(
                opportunity,
                "market_key",
                "strategy_profile",
                "profile",
                "direction",
            )
            or ""
        ).lower()
        for _ in (0,)
    )
    if (
        "long_perp_short_spot" in combined
        or "conditional_spot_short" in combined
        or ("perp" in combined and "spot" in combined and "short" in combined)
    ):
        return "long_perp_short_spot_conditional"
    if any(tag in combined for tag in ("funding", "basis", "carry")):
        return "perp_carry"
    if blockers:
        return "conditional_manual"
    return "direct_market_access"


def _requires_spot_borrow(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
) -> bool:
    blockers = blockers if blockers is not None else _route_blockers(opportunity)
    explicit = _bool_flag(
        _first_known(
            opportunity,
            "requires_spot_borrow",
            "borrow_required",
            "spot_borrow_required",
        )
    )
    if explicit is not None:
        return explicit
    return "spot_borrow" in blockers


def _requires_margin_permission(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
    borrow_required: bool = False,
) -> bool | str:
    explicit = _bool_flag(
        _first_known(
            opportunity,
            "requires_margin_permission",
            "margin_required",
        )
    )
    if explicit is not None:
        return explicit
    blockers = blockers if blockers is not None else _route_blockers(opportunity)
    if borrow_required or "spot_borrow" in blockers or "equity_short" in blockers:
        return True
    return UNKNOWN


def _api_surface_required(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
    borrow_required: bool = False,
    requires_margin_permission: bool | str = UNKNOWN,
) -> str:
    explicit = str(
        _first_known(
            opportunity,
            "api_surface_required",
            "venue_api_requirement",
        )
        or ""
    ).strip()
    if explicit and explicit.lower() != UNKNOWN:
        return explicit
    blockers = blockers if blockers is not None else _route_blockers(opportunity)
    if borrow_required or "spot_borrow" in blockers:
        return "public_plus_margin_and_borrow"
    if requires_margin_permission is True:
        return "public_plus_margin"
    if blockers or "conditional" in _route_type(opportunity, blockers=blockers).lower():
        return "public_plus_conditional_planning"
    return "public_market_data_only"


def _route_feasible_paper(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
    paper_proxy_route: str | None = None,
) -> bool | str:
    feasibility = _paper_feasibility(
        opportunity,
        blockers=blockers,
        paper_proxy_route=paper_proxy_route,
    )
    if feasibility in {"direct_feasible", "proxy_only"}:
        return True
    if feasibility == "blocked":
        return False
    return UNKNOWN


def _paper_route_cost_bps(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
    paper_proxy_route: str | None = None,
) -> tuple[float, list[str]]:
    explicit = _numeric_field(
        opportunity,
        "route_cost_bps_paper",
        "paper_route_cost_bps",
        "estimated_route_cost_bps",
    )
    if explicit is not None:
        return (round(explicit, 4), ["explicit_route_cost_bps_paper"])
    blockers = blockers if blockers is not None else _route_blockers(opportunity)
    paper_proxy_route = paper_proxy_route or _paper_proxy_route(opportunity, blockers=blockers)
    borrow_required = _requires_spot_borrow(opportunity, blockers=blockers)
    requires_margin_permission = _requires_margin_permission(
        opportunity,
        blockers=blockers,
        borrow_required=borrow_required,
    )
    fee_per_side = _numeric_field(
        opportunity,
        "fee_bps_per_side_or_unknown",
        "fee_bps_per_side",
    )
    slippage_per_side = _numeric_field(
        opportunity,
        "slippage_bps_per_side_or_unknown",
        "slippage_bps_per_side",
    )
    borrow_fee = _numeric_field(
        opportunity,
        "borrow_fee_bps_estimate_or_unknown",
        "borrow_fee_bps_estimate",
        "borrow_fee_bps",
    )
    cost_bps = 0.0
    reason_codes: list[str] = []
    if fee_per_side is not None:
        cost_bps += fee_per_side * 2.0
        reason_codes.append("fees_two_sided")
    else:
        cost_bps += 4.0
        reason_codes.append("fees_unknown_penalty")
    if slippage_per_side is not None:
        cost_bps += slippage_per_side * 2.0
        reason_codes.append("slippage_two_sided")
    else:
        cost_bps += 6.0
        reason_codes.append("slippage_unknown_penalty")
    if borrow_required:
        if borrow_fee is not None:
            cost_bps += max(0.0, borrow_fee)
            reason_codes.append("borrow_cost")
        else:
            cost_bps += 15.0
            reason_codes.append("borrow_cost_unknown_penalty")
    if requires_margin_permission is True:
        cost_bps += 5.0
        reason_codes.append("margin_operational_drag")
    if paper_proxy_route != "not_applicable":
        cost_bps += 7.5
        reason_codes.append("paper_proxy_basis_risk")
    if _paper_feasibility(opportunity, blockers=blockers, paper_proxy_route=paper_proxy_route) == "blocked":
        cost_bps += 25.0
        reason_codes.append("blocked_route_penalty")
    return (round(cost_bps, 4), reason_codes)


def _route_blocker_playbook(blocker: str) -> dict[str, Any]:
    if blocker in {"prediction_markets_account", "venue_api_access", "jurisdiction_eligibility"}:
        return {
            "route_family": "prediction_market",
            "manual_requirements": [
                "confirm jurisdiction eligibility",
                "confirm venue account/API availability outside the runner",
                "review resolution rules and fee schedule",
            ],
            "unavailable_in_paper": [
                "credential_collection",
                "account_opening",
                "venue_api_key_storage",
                "live_order_submission",
            ],
        }
    if blocker in {"equity_short", "options_or_inverse_product"}:
        return {
            "route_family": "equity_short_or_options_proxy",
            "manual_requirements": [
                "confirm borrow, margin, or listed proxy availability externally",
                "verify market hours, fees, and locate constraints",
            ],
            "unavailable_in_paper": [
                "credential_collection",
                "borrow_locate_request",
                "broker_order_submission",
            ],
        }
    if blocker == "spot_borrow":
        return {
            "route_family": "crypto_spot_borrow_or_hedged_perp_proxy",
            "manual_requirements": [
                "confirm borrow/margin availability externally",
                "estimate borrow fees and minimum notional",
                "keep short-spot candidates shadow-only until borrow evidence is confirmed",
            ],
            "unavailable_in_paper": [
                "credential_collection",
                "margin_enablement",
                "private_borrow_api",
                "live_order_submission",
            ],
        }
    return {
        "route_family": "unknown_manual_route",
        "manual_requirements": ["research requirement externally before any live route can exist"],
        "unavailable_in_paper": ["credential_collection", "private_api_calls", "live_order_submission"],
    }


def _paper_proxy_route(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
) -> str:
    raw_proxy_route = _first_known(
        opportunity,
        "paper_testable_proxy",
        "paper_proxy_route",
        "proxy_route",
        "paper_route_proxy",
    )
    if isinstance(raw_proxy_route, str):
        proxy_route = raw_proxy_route.strip()
        if proxy_route and proxy_route.lower() != UNKNOWN:
            return proxy_route
    elif _bool_flag(raw_proxy_route) is True:
        return "paper_testable_proxy"

    blockers = blockers if blockers is not None else _route_blockers(opportunity)
    if "spot_borrow" in blockers and _bool_flag(_first_known(opportunity, "proxy_supported", "paper_proxy_supported")) is True:
        return "paper_testable_proxy"
    return "not_applicable"


def _paper_feasibility(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
    paper_proxy_route: str | None = None,
) -> str:
    explicit = str(opportunity.get("paper_feasibility") or "").strip().lower()
    if explicit in {"direct_feasible", "proxy_only", "assumption_sensitive", "blocked", UNKNOWN}:
        return explicit
    blockers = blockers if blockers is not None else _route_blockers(opportunity)
    paper_proxy_route = paper_proxy_route or _paper_proxy_route(opportunity, blockers=blockers)
    if _unconfirmed_frontier_spot_short_route(opportunity) or blockers:
        if paper_proxy_route != "not_applicable":
            return "proxy_only"
        return "blocked"
    return "direct_feasible"


def _build_route_requirement_row(opportunity: dict[str, Any]) -> dict[str, Any]:
    blockers = _route_blockers(opportunity)
    venue = _venue(opportunity)
    inst_id = str(opportunity.get("inst_id") or UNKNOWN)
    requirement_flags = list(blockers)
    explicit_permissions = _route_required_permissions(opportunity, requirement_flags)
    explicit_borrow_required = _bool_flag(opportunity.get("borrow_required")) is True
    permissions_require_borrow = any(
        "spot_borrow" in str(permission).lower()
        for permission in (
            (explicit_permissions,)
            if isinstance(explicit_permissions, str)
            else (explicit_permissions or [])
        )
    )
    if (
        _requires_frontier_spot_short_route(opportunity)
        or explicit_borrow_required
        or permissions_require_borrow
    ) and "spot_borrow" not in requirement_flags:
        requirement_flags.append("spot_borrow")
    borrow_required = "spot_borrow" in requirement_flags
    requires_margin_permission = _requires_margin_permission(
        opportunity,
        blockers=blockers,
        borrow_required=borrow_required,
    )
    account_requirements = _account_requirements(requirement_flags)
    route_status = _paper_route_status(opportunity, blockers=blockers)
    route_type = _route_type(opportunity, blockers=blockers)
    paper_proxy_route = _paper_proxy_route(opportunity, blockers=blockers)
    paper_feasibility = _paper_feasibility(opportunity, blockers=blockers, paper_proxy_route=paper_proxy_route)
    route_cost_bps_paper, route_cost_reason_codes = _paper_route_cost_bps(opportunity, blockers=blockers, paper_proxy_route=paper_proxy_route)
    short_required = "short" in str(opportunity.get("direction") or "").lower() or borrow_required
    shortability = _shortability_status(opportunity, short_required=short_required)
    margin_required = _margin_required(opportunity, borrow_required)
    margin_mode = _first_known(
        opportunity,
        "margin_mode",
        "margin_account_mode",
        "leverage_mode",
    )
    fee_tier = _first_known(
        opportunity,
        "fee_tier",
        "fee_tier_name",
        "maker_taker_fee_tier",
        "fee_class",
        "fee_model",
        "fee_model_status",
    )
    api_permission_status = _first_known(
        opportunity,
        "api_permission_status",
        "api_access_status",
        "venue_api_status",
        "endpoint_status",
    )
    order_type_support = _order_type_support(opportunity)

    return {
        "venue": venue,
        "inst_id": inst_id,
        "direction": str(opportunity.get("direction") or UNKNOWN),
        "route_status": route_status,
        "route_blockers": blockers,
        "required_account_type": "; ".join(account_requirements) if account_requirements else UNKNOWN,
        "required_permissions": _route_required_permissions(opportunity, requirement_flags),
        "shortability_status": shortability,
        "borrow_required": borrow_required,
        "borrow_asset": _borrow_asset(inst_id) if borrow_required else "not_applicable",
        "borrow_fee_bps_estimate_or_unknown": _first_known(
            opportunity,
            "borrow_fee_bps_estimate_or_unknown",
            "borrow_fee_bps_estimate",
            "borrow_fee_bps",
        ),
        "margin_required": margin_required,
        "margin_spot_constraints": {
            "margin_required": margin_required,
            "margin_mode": margin_mode,
            "spot_leg_required": "spot" in str(opportunity.get("direction") or "").lower()
            or "spot" in route_type.lower(),
            "required_account_modes": _string_list(
                _first_known(
                    opportunity,
                    "paper_route_required_account_modes",
                    "required_account_modes",
                )
            )
            or [UNKNOWN],
        },
        "fee_tier": fee_tier,
        "fee_tier_status": "observed" if fee_tier != UNKNOWN else UNKNOWN,
        "venue_api_requirement": _venue_api_requirement(blockers),
        "api_permission_status": api_permission_status,
        "endpoint_constraints": _endpoint_constraints(opportunity, blockers),
        "order_type_support": order_type_support,
        "jurisdiction_requirement": _jurisdiction_requirement(blockers),
        "fee_bps_per_side_or_unknown": _first_known(
            opportunity,
            "fee_bps_per_side_or_unknown",
            "fee_bps_per_side",
        ),
        "slippage_bps_per_side_or_unknown": _first_known(
            opportunity,
            "slippage_bps_per_side_or_unknown",
            "slippage_bps_per_side",
        ),
        "paper_route_only": True,
        "paper_feasibility": paper_feasibility,
        "paper_proxy_route": paper_proxy_route,
        "paper_proxy_not_live_equivalent": paper_proxy_route != "not_applicable",
        "route_type": route_type,
        "route_feasible_paper": _route_feasible_paper(
            opportunity,
            blockers=blockers,
            paper_proxy_route=paper_proxy_route,
        ),
        "route_cost_bps_paper": route_cost_bps_paper,
        "route_cost_reason_codes": route_cost_reason_codes,
        "api_surface_required": _api_surface_required(
            opportunity,
            blockers=blockers,
            borrow_required=borrow_required,
            requires_margin_permission=requires_margin_permission,
        ),
        "requires_spot_borrow": borrow_required,
        "requires_margin_permission": requires_margin_permission,
    }


def _route_priority_key(row: dict[str, Any]) -> tuple[int, int, str]:
    inst_id = str(row["inst_id"])
    blockers = set(row["route_blockers"])
    if "spot_borrow" in blockers and inst_id in _PRIORITY_SPOT_BORROW_INST_IDS:
        return (0, _PRIORITY_SPOT_BORROW_INST_IDS.index(inst_id), inst_id)
    if "spot_borrow" in blockers:
        return (1, 0, inst_id)
    if str(row["venue"]).upper() == "POLYMARKET":
        return (2, 0, inst_id)
    return (3, 0, inst_id)


def _paper_route_status(
    opportunity: dict[str, Any],
    *,
    blockers: list[str] | None = None,
) -> str:
    blockers = blockers if blockers is not None else _route_blockers(opportunity)
    if _unconfirmed_frontier_spot_short_route(opportunity) and _paper_proxy_route(opportunity, blockers=blockers) != "not_applicable":
        return "paper_testable_via_proxy"
    if _unconfirmed_frontier_spot_short_route(opportunity):
        return "unsupported_or_unknown"
    if blockers and _paper_proxy_route(opportunity, blockers=blockers) != "not_applicable":
        return "paper_testable_via_proxy"
    if blockers:
        return "blocked_until_requirements_confirmed"
    return "paper_observation_only"


def _unconfirmed_frontier_spot_short_route(opportunity: dict[str, Any]) -> bool:
    if not _requires_frontier_spot_short_route(opportunity):
        return False
    return not _frontier_spot_short_capability_confirmed(opportunity)


def _requires_frontier_spot_short_route(opportunity: dict[str, Any]) -> bool:
    market_key = str(
        _first_known(
            opportunity,
            "market_key",
            "scanner_key",
            "strategy_profile",
            "profile",
            "route_family",
            "thesis_type",
        )
        or ""
    ).lower()
    if "frontier" not in market_key:
        return False
    direction = str(_first_known(opportunity, "direction", "side") or "").lower()
    profile = str(
        _first_known(
            opportunity,
            "strategy_profile",
            "profile",
            "route_profile",
            "execution_route",
        )
        or ""
    ).lower()
    requires_short = (
        direction.startswith("short")
        or "spot_short" in profile
        or "conditional_spot_short" in profile
    )
    if not requires_short:
        return False
    instrument_type = str(
        _first_known(
            opportunity,
            "instrument_type",
            "market_type",
            "product_type",
            "contract_type",
        )
        or ""
    ).lower()
    if any(tag in instrument_type for tag in ("perp", "perpetual", "swap", "future", "option", "inverse")):
        return False
    return True


def _frontier_spot_short_capability_confirmed(opportunity: dict[str, Any]) -> bool:
    combined = _bool_flag(
        _first_known(
            opportunity,
            "margin_plus_borrow_supported",
            "spot_short_supported",
            "conditional_spot_short_supported",
        )
    )
    if combined is True:
        return True
    margin_supported = _bool_flag(_first_known(opportunity, "margin_supported", "spot_margin_supported", "route_margin_supported"))
    borrow_supported = _bool_flag(_first_known(opportunity, "borrow_supported", "spot_borrow_supported", "route_borrow_supported"))
    return margin_supported is True and borrow_supported is True


def _bool_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if not text or text == UNKNOWN:
        return None
    if text in {"1", "true", "yes", "y", "supported", "confirmed"}:
        return True
    if text in {"0", "false", "no", "n", "unsupported", "blocked"}:
        return False
    return None


def _shortability_status(
    opportunity: dict[str, Any],
    *,
    short_required: bool,
) -> str:
    """Return observed shortability without treating it as an entry gate."""

    value = _first_known(
        opportunity,
        "shortability_status",
        "shortable_status",
        "is_shortable",
        "instrument_shortable",
        "instrument_margin_shortable",
        "spot_short_supported",
        "supports_spot_short",
    )
    if value == UNKNOWN and isinstance(opportunity.get("venue_capabilities"), dict):
        value = _first_known(
            opportunity["venue_capabilities"],
            "supports_spot_short",
            "spot_short_supported",
            "shortability",
        )
    return _route_fact_status(
        value,
        required=short_required,
        unresolved=short_required,
    )


def _string_list(value: Any) -> list[str]:
    """Normalize declared route metadata lists without inferring support."""

    values = (value,) if isinstance(value, str) else (value or [])
    result: list[str] = []
    for item in values:
        text = str(item or "").strip().lower()
        if text and text not in result:
            result.append(text)
    return result


def _order_type_support(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Project declared order-type support from existing route metadata.

    This is intentionally a report of configuration and public observations;
    it never probes a private order API or implies permission to submit an
    order.  Unknown support stays visible as a non-blocking paper diagnostic.
    """

    required = _string_list(
        _first_known(
            opportunity,
            "required_order_types",
            "required_order_type",
            "route_required_order_types",
        )
    )
    supported = _string_list(
        _first_known(
            opportunity,
            "supported_order_types",
            "order_types_supported",
            "venue_order_types",
            "order_type_supports",
        )
    )
    capabilities = opportunity.get("venue_capabilities")
    if not supported and isinstance(capabilities, dict):
        supported = _string_list(
            _first_known(
                capabilities,
                "supported_order_types",
                "order_types_supported",
                "venue_order_types",
            )
        )
    explicit_status = _first_known(
        opportunity,
        "order_type_support_status",
        "order_type_status",
        "order_api_support_status",
    )
    if explicit_status != UNKNOWN:
        status = _route_fact_status(explicit_status, required=bool(required), unresolved=bool(required))
    elif not required:
        status = "not_required_for_paper_report"
    elif not supported:
        status = "unconfirmed"
    elif all(item in supported for item in required):
        status = "supported"
    else:
        status = "unsupported"
    return {
        "status": status,
        "required_order_types": required or [UNKNOWN],
        "supported_order_types": supported or [UNKNOWN],
        "source": "declared_route_metadata_only",
        "private_api_probe_performed": False,
        "paper_only": True,
        "non_blocking": True,
    }


def _route_blockers(opportunity: dict[str, Any]) -> list[str]:
    blockers = opportunity.get("route_blockers") or []
    if isinstance(blockers, str):
        blockers = [blockers]
    normalized = list(dict.fromkeys(str(blocker) for blocker in blockers if blocker))
    if _unconfirmed_frontier_spot_short_route(opportunity) and "spot_borrow" not in normalized:
        normalized.append("spot_borrow")
    return normalized


def _conditional_paper_policy_action(opportunity: dict[str, Any]) -> str:
    guard = _conditional_decay_flip_guard(opportunity)
    if not guard:
        if _unconfirmed_frontier_spot_short_route(opportunity):
            if str(opportunity.get("route_status") or "").lower() in {"blocked", "unsupported_or_unknown"}:
                return "reject_candidate_paper_only"
            return "apply_severe_ranking_penalty_paper_only"
        return "none"
    return str(guard.get("policy_action") or "none")


def _conditional_decay_flip_guard(opportunity: dict[str, Any]) -> dict[str, Any] | None:
    route_status = str(opportunity.get("route_status") or "").lower()
    direction = str(_first_known(opportunity, "direction", "side") or "").lower()
    if route_status != "conditional" or not direction.startswith("short"):
        return None

    prior_count = _numeric_first_known(
        opportunity,
        "prior_count",
        "paper_promotion_sample_count",
        "promotion_sample_count",
        "exploit_more_prior_count",
    )
    prior_avg_bps = _numeric_first_known(
        opportunity,
        "prior_avg_bps",
        "prior_expectancy_bps",
        "promotion_avg_bps",
        "exploit_more_prior_avg_bps",
    )
    current_count = _numeric_first_known(
        opportunity,
        "paper_count",
        "rolling_count",
        "sample_count",
        "observed_count",
    )
    rolling_expectancy_bps = _numeric_first_known(
        opportunity,
        "rolling_expectancy_bps",
        "paper_expectancy_bps",
        "realized_avg_bps",
        "rolling_avg_bps",
        "avg_bps",
    )
    drawdown_bps = _numeric_first_known(
        opportunity,
        "paper_drawdown_bps",
        "drawdown_bps",
        "rolling_drawdown_bps",
    )
    if prior_count is None or prior_avg_bps is None or current_count is None:
        return None

    min_confirm = float(CONDITIONAL_SHORT_DECAY_FLIP_GUARD["min_confirm_count_after_promotion"])
    negative_flip_threshold = float(CONDITIONAL_SHORT_DECAY_FLIP_GUARD["negative_flip_bps_threshold"])
    drawdown_guard_bps = float(CONDITIONAL_SHORT_DECAY_FLIP_GUARD["drawdown_guard_bps"])
    promoted_from_sparse_positive_history = 0 < prior_count < min_confirm and prior_avg_bps > negative_flip_threshold
    if not promoted_from_sparse_positive_history:
        return None

    if current_count < min_confirm:
        return {
            "triggered": False,
            "guard_state": "awaiting_expanded_sample_confirmation",
            "policy_action": "hold_conditional_paper_only",
            "exploit_more_eligible": False,
            "cooldown_cycles_remaining": 0,
            "min_confirm_count_after_promotion": int(min_confirm),
            "confirmation_progress_count": current_count,
            "prior_count": prior_count,
            "prior_avg_bps": prior_avg_bps,
            "rolling_expectancy_bps": rolling_expectancy_bps,
            "drawdown_bps": drawdown_bps,
        }

    negative_flip = rolling_expectancy_bps is not None and rolling_expectancy_bps < negative_flip_threshold
    drawdown_breach = drawdown_bps is not None and drawdown_bps >= drawdown_guard_bps
    if not negative_flip and not drawdown_breach:
        return None
    return {
        "triggered": True,
        "guard_state": "score_clamped_non_admissible",
        "policy_action": "score_clamped_non_admissible",
        "exploit_more_eligible": False,
        "cooldown_cycles_remaining": int(CONDITIONAL_SHORT_DECAY_FLIP_GUARD["cooldown_cycles"]),
        "negative_flip": negative_flip,
        "drawdown_breach": drawdown_breach,
        "min_confirm_count_after_promotion": int(min_confirm),
        "prior_count": prior_count,
        "prior_avg_bps": prior_avg_bps,
        "current_count": current_count,
        "rolling_expectancy_bps": rolling_expectancy_bps,
        "drawdown_bps": drawdown_bps,
    }


def _conditional_gate_reasons(opportunity: dict[str, Any]) -> list[str]:
    blockers = set(_route_blockers(opportunity))
    direction = str(opportunity.get("direction") or "").lower()
    route_status = str(opportunity.get("route_status") or "").lower()
    quality_action = str(opportunity.get("quality_action") or "").lower()
    anomalies = opportunity.get("anomaly_flags") or []
    if isinstance(anomalies, str):
        anomalies = [anomalies]
    anomaly_set = {str(item) for item in anomalies}
    reasons: list[str] = []
    if _unconfirmed_frontier_spot_short_route(opportunity):
        reasons.append("unsupported_or_unknown_frontier_spot_short_route")
    if route_status == "conditional" and ("spot_borrow" in blockers or direction.startswith("short")):
        reasons.append("unconfirmed_short_or_borrow_route")
    if quality_action == "shadow_only" or anomaly_set.intersection({"empty_book", "crossed_book", "one_sided_book"}):
        reasons.append("market_data_quality_shadow_only")
    edge = _numeric_first_known(
        opportunity,
        "edge_bps_estimate",
        "net_edge_after_borrow_cost_bps",
        "depth_adjusted_edge_bps",
    )
    if edge is not None and edge <= 0 and any("slippage" in anomaly for anomaly in anomaly_set):
        reasons.append("slippage_exceeds_nonpositive_edge")
    decay_flip_guard = _conditional_decay_flip_guard(opportunity)
    if decay_flip_guard:
        if decay_flip_guard.get("triggered"):
            reasons.append("decay_flip_guard_non_admissible")
        elif decay_flip_guard.get("guard_state") == "awaiting_expanded_sample_confirmation":
            reasons.append("expanded_sample_confirmation_pending")
    return list(dict.fromkeys(reasons))


def _numeric_first_known(opportunity: dict[str, Any], *keys: str) -> float | None:
    value = _first_known(opportunity, *keys)
    if value == UNKNOWN:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _venue(opportunity: dict[str, Any]) -> str:
    if opportunity.get("venue"):
        return str(opportunity["venue"])
    inst_id = str(opportunity.get("inst_id") or "")
    if ":" in inst_id:
        return inst_id.split(":", 1)[0]
    return UNKNOWN


def _borrow_asset(inst_id: str) -> str:
    if ":" in inst_id:
        inst_id = inst_id.split(":", 1)[1]
    for separator in ("_", "-", "/"):
        if separator in inst_id:
            return inst_id.split(separator, 1)[0] or UNKNOWN
    return inst_id or UNKNOWN


def _account_requirements(blockers: list[str]) -> list[str]:
    requirements = []
    if "spot_borrow" in blockers:
        requirements.append("margin_or_borrow_enabled_account_or_verified_borrow_source")
    if "prediction_markets_account" in blockers:
        requirements.append("externally_approved_prediction_markets_account")
    return requirements


def _required_permissions(blockers: list[str]) -> list[str]:
    permissions = []
    if "spot_borrow" in blockers:
        permissions.append("externally_confirmed_borrow_availability")
    if "venue_api_access" in blockers:
        permissions.append("externally_confirmed_venue_api_access_no_credentials_stored")
    if "jurisdiction_eligibility" in blockers:
        permissions.append("externally_confirmed_jurisdiction_eligibility")
    if "prediction_markets_account" in blockers:
        permissions.append("externally_confirmed_prediction_markets_account")
    return permissions or [UNKNOWN]


def _route_required_permissions(opportunity: dict[str, Any], blockers: list[str]) -> Any:
    """Prefer the resolver's explicit permission list when it is available."""

    explicit = opportunity.get("required_permissions")
    if explicit not in (None, "", [], {}):
        return explicit
    return _required_permissions(blockers)


def _venue_api_requirement(blockers: list[str]) -> str:
    if "venue_api_access" in blockers:
        return "external_venue_api_access_confirmation_required_no_credentials_stored"
    return "not_required_for_paper_report"


def _endpoint_constraints(opportunity: dict[str, Any], blockers: list[str]) -> Any:
    """Describe known endpoint limits without probing or calling an endpoint."""

    explicit = _first_known(
        opportunity,
        "endpoint_constraints",
        "api_endpoint_constraints",
        "order_endpoint_constraints",
        "endpoint_status",
    )
    if explicit != UNKNOWN:
        return explicit

    api_access_status = str(opportunity.get("api_access_status") or "").strip().lower()
    if api_access_status == "public_data_only":
        return "public_data_only_private_or_order_endpoint_unconfirmed"
    if api_access_status in {"unknown", "not_checked"}:
        return "endpoint_availability_unknown"
    if "venue_api_access" in blockers:
        return "venue_trade_endpoint_access_unconfirmed"
    return "public_market_data_only"


def _jurisdiction_requirement(blockers: list[str]) -> str:
    if "jurisdiction_eligibility" in blockers:
        return "external_jurisdiction_eligibility_confirmation_required"
    return "not_required_for_paper_report"


def _margin_required(opportunity: dict[str, Any], borrow_required: bool) -> Any:
    if "margin_required" in opportunity and opportunity["margin_required"] not in (None, ""):
        return opportunity["margin_required"]
    return UNKNOWN if borrow_required else False


def _first_known(opportunity: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in opportunity and opportunity[key] not in (None, ""):
            return opportunity[key]
    return UNKNOWN


def _route_requirement_opportunity(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Flatten resolver metadata for a report without changing the candidate.

    The resolver intentionally stores its route decision in nested packets so
    execution consumers have a single authoritative object.  Reports need the
    same constraints at the top level to make them visible before promotion.
    This copy-only projection never mutates the supplied candidate or route.
    """

    normalized = dict(opportunity)
    route = opportunity.get("execution_route")
    feasibility = opportunity.get("execution_feasibility")
    route = route if isinstance(route, dict) else {}
    feasibility = feasibility if isinstance(feasibility, dict) else {}

    for key in (
        "route_status",
        "required_permissions",
        "requirements",
        "borrow_required",
        "borrow_status",
        "margin_required",
        "margin_mode",
        "api_access_status",
        "api_permission_status",
        "fee_model_status",
        "fee_tier",
        "required_order_types",
        "required_order_type",
        "supported_order_types",
        "order_types_supported",
        "order_type_support_status",
        "route_id",
    ):
        if normalized.get(key) in (None, "", [], {}):
            value = route.get(key, feasibility.get(key))
            if value not in (None, "", [], {}):
                normalized[key] = value

    resolved_blockers = route.get("missing_permissions") or route.get("route_blockers")
    if not resolved_blockers:
        resolved_blockers = feasibility.get("route_blockers") or feasibility.get("missing_requirements")
    existing_blockers = normalized.get("route_blockers")
    existing_values = (
        [existing_blockers]
        if isinstance(existing_blockers, str)
        else list(existing_blockers or [])
    )
    resolved_values = (
        [resolved_blockers]
        if isinstance(resolved_blockers, str)
        else list(resolved_blockers or [])
    )
    combined_blockers = list(dict.fromkeys([*existing_values, *resolved_values]))
    if combined_blockers:
        normalized["route_blockers"] = combined_blockers

    return normalized


def _markdown_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value) if value else UNKNOWN
    return str(value).replace("|", "\\|")
