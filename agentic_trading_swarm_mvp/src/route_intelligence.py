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
)

_PRIORITY_SPOT_BORROW_INST_IDS = (
    "GATE:ARC_USDT",
    "GATE:DEXE_USDT",
    "COINBASE:XRP-USDT",
)


def build_route_requirements_matrix(
    opportunities: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a paper-only route requirements matrix.

    The input is expected to be previously collected paper/research
    opportunities.  The output is a list of dictionaries containing only
    requirement metadata and explicit unknown placeholders.
    """

    rows = [_build_route_requirement_row(opportunity) for opportunity in opportunities]
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
    }


def build_conditional_paper_quality_gate(opportunities: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize paper-only opportunity gates caused by route or data quality.

    This is diagnostic output only. It does not alter route capabilities, place
    orders, or enable live execution.
    """

    gated: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    for opportunity in opportunities:
        reasons = _conditional_gate_reasons(opportunity)
        if not reasons:
            continue
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        gated.append(
            {
                "inst_id": str(opportunity.get("inst_id") or opportunity.get("instrument") or UNKNOWN),
                "venue": _venue(opportunity),
                "direction": str(opportunity.get("direction") or UNKNOWN),
                "route_status": str(opportunity.get("route_status") or UNKNOWN),
                "route_blockers": _route_blockers(opportunity),
                "quality_action": str(opportunity.get("quality_action") or UNKNOWN),
                "edge_bps_estimate": _first_known(
                    opportunity,
                    "edge_bps_estimate",
                    "net_edge_after_borrow_cost_bps",
                    "depth_adjusted_edge_bps",
                ),
                "reasons": reasons,
                "paper_only": True,
            }
        )
    gated.sort(key=lambda item: (-len(item["reasons"]), str(item["inst_id"])))
    return {
        "paper_only": True,
        "gate_count": len(gated),
        "reason_counts": dict(sorted(reason_counts.items())),
        "top_examples": gated[:10],
        "hard_limits": [
            "Diagnostic gate only.",
            "No credentials, broker writes, account changes, or live orders.",
        ],
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


def _build_route_requirement_row(opportunity: dict[str, Any]) -> dict[str, Any]:
    blockers = _route_blockers(opportunity)
    venue = _venue(opportunity)
    inst_id = str(opportunity.get("inst_id") or UNKNOWN)
    borrow_required = "spot_borrow" in blockers
    account_requirements = _account_requirements(blockers)

    return {
        "venue": venue,
        "inst_id": inst_id,
        "direction": str(opportunity.get("direction") or UNKNOWN),
        "route_status": (
            "blocked_until_requirements_confirmed"
            if blockers
            else "paper_observation_only"
        ),
        "route_blockers": blockers,
        "required_account_type": "; ".join(account_requirements) if account_requirements else UNKNOWN,
        "required_permissions": _required_permissions(blockers),
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


def _route_blockers(opportunity: dict[str, Any]) -> list[str]:
    blockers = opportunity.get("route_blockers") or []
    if isinstance(blockers, str):
        blockers = [blockers]
    return list(dict.fromkeys(str(blocker) for blocker in blockers if blocker))


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
