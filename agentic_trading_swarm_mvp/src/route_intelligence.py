"""Paper-only route requirement intelligence.

This module is intentionally read-only: it creates route requirement rows and
report text from already-observed paper opportunities.  It does not request or
store credentials, call private APIs, or change any execution path.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


UNKNOWN = "unknown"

PAPER_ONLY_CONSTRAINTS = (
    "read_only_output_only",
    "no_credentials",
    "no_private_api_calls",
    "no_live_trading",
    "no_order_execution_changes",
)

ROUTE_REQUIREMENT_FIELDS = (
    "venue",
    "inst_id",
    "direction",
    "route_status",
    "route_blockers",
    "required_account_type",
    "required_permissions",
    "borrow_required",
    "borrow_asset",
    "borrow_fee_bps_estimate_or_unknown",
    "margin_required",
    "venue_api_requirement",
    "jurisdiction_requirement",
    "fee_bps_per_side_or_unknown",
    "slippage_bps_per_side_or_unknown",
    "paper_route_only",
    "paper_feasibility",
    "paper_proxy_route",
    "feasibility_state",
    "route_friction_bps",
    "paper_proxy_not_live_equivalent",
    "route_type",
    "route_feasible_paper",
    "route_cost_bps_paper",
    "route_cost_reason_codes",
    "api_surface_required",
    "requires_spot_borrow",
    "requires_margin_permission",
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
        row = _build_route_requirement_row(opportunity)
        rows.append(_annotate_route_feasibility_fields(opportunity, row))
    return sorted(rows, key=_route_priority_key)


def build_route_requirements_report(
    opportunities: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Return a JSON-serializable paper-only route requirements report."""

    opportunities = list(opportunities)
    return {
        "paper_only": True,
        "safety_constraints": list(PAPER_ONLY_CONSTRAINTS),
        "fields": list(ROUTE_REQUIREMENT_FIELDS),
        "routes": build_route_requirements_matrix(opportunities),
        "playbook_summary": build_route_playbook_summary(opportunities),
        "paper_feasibility_summary": build_route_feasibility_summary(opportunities),
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
        edge_bps_estimate = _first_known(
            opportunity,
            "edge_bps_estimate",
            "net_edge_after_borrow_cost_bps",
            "depth_adjusted_edge_bps",
        )
        edge_bps_number = _float_or_none(edge_bps_estimate)
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
        if proxy_route != "not_applicable":
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
    return row


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
    if _requires_frontier_spot_short_route(opportunity) and "spot_borrow" not in requirement_flags:
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

    return {
        "venue": venue,
        "inst_id": inst_id,
        "direction": str(opportunity.get("direction") or UNKNOWN),
        "route_status": route_status,
        "route_blockers": blockers,
        "required_account_type": "; ".join(account_requirements) if account_requirements else UNKNOWN,
        "required_permissions": _required_permissions(requirement_flags),
        "borrow_required": borrow_required,
        "borrow_asset": _borrow_asset(inst_id) if borrow_required else "not_applicable",
        "borrow_fee_bps_estimate_or_unknown": _first_known(
            opportunity,
            "borrow_fee_bps_estimate_or_unknown",
            "borrow_fee_bps_estimate",
            "borrow_fee_bps",
        ),
        "margin_required": _margin_required(opportunity, borrow_required),
        "venue_api_requirement": _venue_api_requirement(blockers),
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


def _venue_api_requirement(blockers: list[str]) -> str:
    if "venue_api_access" in blockers:
        return "external_venue_api_access_confirmation_required_no_credentials_stored"
    return "not_required_for_paper_report"


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


def _markdown_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value) if value else UNKNOWN
    return str(value).replace("|", "\\|")
