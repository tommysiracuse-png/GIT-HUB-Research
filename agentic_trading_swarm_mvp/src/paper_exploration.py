"""Exploration-first paper admission helpers.

This module does not decide whether a candidate is good. It separates reasons a
trade would be unattractive or unexecutable from the small set of conditions
that make a paper PnL experiment impossible to measure.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from collections import defaultdict


SYNTHETIC_ROUTE_ID = "synthetic_research_paper"
SYNTHETIC_STATS_SCOPE = "synthetic_research"

_DIRECT_ROUTE_STATES = {"standard", "feasible", "executable", "paper_testable"}
_LONG_DIRECTIONS = {
    "long_frontier_spot",
    "long_frontier_perp",
    "long_proxy",
    "funding_capture_long_perp",
    "basis_mean_reversion_long_perp",
    "long_perp_short_spot",
    "buy_yes_event",
    "buy_no_event",
    "yes",
}
_SHORT_DIRECTIONS = {
    "short_frontier_spot",
    "short_frontier_perp",
    "short_proxy",
    "funding_capture_short_perp",
    "basis_mean_reversion_short_perp",
    "short_perp_long_spot",
    "no",
}
_MULTI_LEG_DIRECTIONS = {"long_perp_short_spot", "short_perp_long_spot"}
_CRITICAL_ANOMALIES = {
    "crossed_book",
    "empty_book",
    "one_sided_book",
    "invalid_book",
    "malformed_book",
    "invalid_price",
}


def exploration_config(settings: Mapping | None) -> dict:
    if not isinstance(settings, Mapping):
        return {}
    value = settings.get("paper_exploration") or {}
    return dict(value) if isinstance(value, Mapping) else {}


def exploration_enabled(settings: Mapping | None) -> bool:
    return bool(exploration_config(settings).get("enabled", False))


def _finite_positive(value: object) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0.0


def direction_has_pnl(direction: object) -> bool:
    text = str(direction or "").strip().lower()
    return text in _LONG_DIRECTIONS or text in _SHORT_DIRECTIONS or text.startswith(("long_", "short_", "buy_", "sell_"))


def execution_structure(candidate: Mapping) -> str:
    explicit = str(candidate.get("execution_structure") or "").strip().lower()
    if explicit:
        return explicit
    if str(candidate.get("direction") or "") in _MULTI_LEG_DIRECTIONS:
        return "perpetual_spot_pair"
    if str(candidate.get("trade_type") or "") == "perp_funding_basis":
        return "single_perpetual"
    return "single_instrument"


def _has_priced_legs(candidate: Mapping) -> bool:
    legs = candidate.get("paper_legs") or candidate.get("legs") or []
    if isinstance(legs, list) and len(legs) >= 2:
        return all(
            isinstance(leg, Mapping)
            and _finite_positive(leg.get("reference_price", leg.get("price", leg.get("last"))))
            for leg in legs[:2]
        )
    return _finite_positive(candidate.get("perp_last", candidate.get("last"))) and _finite_positive(
        candidate.get("spot_last")
    )


def _has_explicit_proxy(candidate: Mapping) -> bool:
    return bool(
        candidate.get("paper_proxy_activated")
        or candidate.get("synthetic_proxy_id")
        or candidate.get("paper_proxy_id")
        or candidate.get("explicit_synthetic_proxy")
    )


def immutable_rejection_reasons(candidate: Mapping, settings: Mapping | None) -> list[str]:
    cfg = exploration_config(settings)
    reasons: list[str] = []
    if not _finite_positive(candidate.get("last")):
        reasons.append("missing or invalid price")
    direction = candidate.get("direction")
    if not direction_has_pnl(direction) or str(direction or "").lower() == "watch_only":
        reasons.append("no defined direction or PnL calculation")
    try:
        stale_minutes = float(candidate.get("stale_minutes") or 0.0)
    except (TypeError, ValueError):
        stale_minutes = float("inf")
    if stale_minutes > float(cfg.get("max_stale_minutes", 90.0)):
        reasons.append(f"market data dangerously stale: {round(stale_minutes, 3)} minutes")
    anomalies = {str(item).strip().lower() for item in (candidate.get("anomaly_flags") or [])}
    critical = sorted(anomalies.intersection(_CRITICAL_ANOMALIES))
    if critical:
        reasons.append("critically broken market data: " + ", ".join(critical))
    if execution_structure(candidate) in {"perpetual_spot_pair", "multi_leg", "two_leg"}:
        if not _has_priced_legs(candidate) and not _has_explicit_proxy(candidate):
            reasons.append("multi-leg strategy has unavailable leg prices and no explicit proxy")
    return reasons


def direct_route_feasible(candidate: Mapping) -> bool:
    feasibility = candidate.get("execution_feasibility") or {}
    route = candidate.get("execution_route") or {}
    status = str(
        feasibility.get("route_status")
        or route.get("route_status")
        or feasibility.get("status")
        or candidate.get("route_status")
        or "unknown"
    ).lower()
    missing = (
        feasibility.get("missing_requirements")
        or route.get("missing_permissions")
        or candidate.get("route_blockers")
        or []
    )
    return status in _DIRECT_ROUTE_STATES and not missing


def _direct_signal_key(candidate: Mapping) -> str:
    feasibility = candidate.get("execution_feasibility") or {}
    route = candidate.get("execution_route") or {}
    status = str(
        feasibility.get("route_status")
        or route.get("route_status")
        or feasibility.get("status")
        or "unknown"
    )
    return "|".join(
        (
            str(candidate.get("venue") or "unknown"),
            str(candidate.get("trade_type") or "unknown"),
            str(candidate.get("direction") or "unknown"),
            status,
        )
    )


def prepare_candidate_for_exploration(candidate: dict, settings: Mapping | None) -> dict:
    """Annotate a candidate for direct or isolated synthetic paper execution."""
    if not exploration_enabled(settings):
        return candidate
    if candidate.get("synthetic_research_paper") and candidate.get("signal_stats_scope") == SYNTHETIC_STATS_SCOPE:
        return candidate
    cfg = exploration_config(settings)
    original_blocked = bool(candidate.get("paper_entry_blocked") or candidate.get("shadow_filtered"))
    prepared = dict(candidate)
    prepared["execution_structure"] = execution_structure(candidate)
    prepared["paper_exploration_enabled"] = True
    prepared["paper_exploration_immutable_rejections"] = immutable_rejection_reasons(candidate, settings)
    prepared["paper_exploration_prior_blocked"] = original_blocked
    if prepared["paper_exploration_immutable_rejections"]:
        return prepared

    prepared["paper_entry_blocked"] = False
    prepared["shadow_filtered"] = False
    prepared["paper_fill_allowed"] = True
    prepared["paper_action"] = "exploration_candidate"
    prepared["promotion_eligible"] = bool(candidate.get("promotion_eligible", True))
    prepared["_hunter_bucket"] = candidate.get("_hunter_bucket") or "explore"

    if candidate.get("paper_proxy_activated") and candidate.get("signal_stats_scope") == "paper_proxy":
        prepared["paper_execution_semantics"] = "proxy_not_live_equivalent"
        prepared["promotion_eligible"] = False
        prepared["_hunter_bucket"] = "diagnose"
        return prepared

    if direct_route_feasible(candidate):
        prepared["signal_stats_scope"] = candidate.get("signal_stats_scope") or "direct"
        prepared["paper_execution_semantics"] = candidate.get("paper_execution_semantics") or "direct_live_equivalent"
        return prepared

    direct_key = str(candidate.get("direct_signal_key") or _direct_signal_key(candidate))
    direct_feasibility = candidate.get("execution_feasibility") or {}
    direct_route = candidate.get("execution_route") or {}
    direct_blockers = list(
        dict.fromkeys(
            str(item)
            for item in (
                direct_feasibility.get("missing_requirements")
                or direct_route.get("missing_permissions")
                or candidate.get("route_blockers")
                or []
            )
            if item
        )
    )
    prepared["direct_signal_key"] = direct_key
    prepared["direct_execution_route"] = direct_route
    prepared["direct_execution_feasibility"] = direct_feasibility
    prepared["direct_missing_requirements"] = direct_blockers
    prepared["synthetic_route_blockers"] = direct_blockers
    prior_reasons = []
    if direct_blockers:
        prior_reasons.append("direct route unavailable: " + ", ".join(direct_blockers))
    if original_blocked:
        prior_reasons.append(
            str(candidate.get("candidate_reject_reason") or "an existing paper guard would block this candidate")
        )
    prepared["paper_exploration_would_block_reasons"] = list(dict.fromkeys(prior_reasons))
    prepared["execution_route"] = {
        "route_id": str(cfg.get("synthetic_route_id") or SYNTHETIC_ROUTE_ID),
        "route_status": "paper_testable_research",
        "missing_permissions": [],
        "notes": ["Synthetic research route; not evidence of real-world executability."],
    }
    prepared["execution_feasibility"] = {
        "status": "paper_testable_research",
        "route_status": "paper_testable_research",
        "route_id": str(cfg.get("synthetic_route_id") or SYNTHETIC_ROUTE_ID),
        "missing_requirements": [],
        "route_notes": ["Direct route blockers are preserved separately for research."],
    }
    prepared["route_id"] = str(cfg.get("synthetic_route_id") or SYNTHETIC_ROUTE_ID)
    prepared["route_status"] = "paper_testable_research"
    prepared["synthetic_research_paper"] = True
    prepared["synthetic_not_live_equivalent"] = True
    prepared["paper_execution_semantics"] = "synthetic_research_not_live_equivalent"
    prepared["signal_stats_scope"] = SYNTHETIC_STATS_SCOPE
    prepared["promotion_eligible"] = False
    prepared["paper_allocation_multiplier"] = max(
        float(prepared.get("paper_allocation_multiplier") or 0.0),
        float(cfg.get("synthetic_allocation_multiplier", 0.25)),
    )
    prepared["_hunter_bucket"] = "diagnose"
    return prepared


def limit_matched_policies(policies: list[dict], settings: Mapping | None) -> list[dict]:
    """In exploration mode retain at most one family and one contextual policy."""
    if not exploration_enabled(settings):
        return policies
    family: list[dict] = []
    contextual: list[dict] = []
    for policy in sorted(policies, key=lambda row: (-int(row.get("priority") or 0), str(row.get("policy_id") or ""))):
        payload = policy.get("policy") or {}
        (contextual if payload.get("context_filter") else family).append(policy)
    return family[:1] + contextual[:1]


def fair_lineage_order(candidates: list[dict], cycle_index: int, settings: Mapping | None) -> list[dict]:
    """Interleave signal lineages and rotate the first lineage each cycle."""
    if not exploration_enabled(settings) or len(candidates) < 2:
        return candidates
    grouped: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for candidate in candidates:
        key = (
            str(candidate.get("strategy_lab_id") or candidate.get("signal_lineage_key") or candidate.get("trade_type") or "unknown"),
            str(candidate.get("venue") or "unknown"),
            str(candidate.get("direction") or "unknown"),
            str(candidate.get("signal_variant_id") or candidate.get("strategy_lab_version") or "base"),
        )
        grouped[key].append(candidate)
    keys = sorted(grouped)
    offset = int(cycle_index) % len(keys)
    keys = keys[offset:] + keys[:offset]
    for rows in grouped.values():
        # The frontier quality score is a paper-only ordering input.  It never
        # removes a priceable candidate, but makes bounded review/execution
        # capacity favor independently corroborated, cost-aware dislocations.
        rows.sort(
            key=lambda row: float(
                row.get("paper_ranking_score")
                if row.get("trade_type") == "frontier_crypto_venue_map"
                and row.get("paper_ranking_score") is not None
                else row.get("score") or 0.0
            ),
            reverse=True,
        )
    ordered: list[dict] = []
    while keys:
        remaining = []
        for key in keys:
            rows = grouped[key]
            if rows:
                ordered.append(rows.pop(0))
            if rows:
                remaining.append(key)
        keys = remaining
    return ordered


def split_exploration_blocks(candidate: Mapping, hard_blocks: list[str], settings: Mapping | None) -> tuple[list[str], list[str]]:
    """Return immutable rejections and diagnostic would-block reasons."""
    if not exploration_enabled(settings):
        return list(hard_blocks), []
    immutable = immutable_rejection_reasons(candidate, settings)
    diagnostics = list(dict.fromkeys(str(item) for item in hard_blocks if item))
    for reason in candidate.get("paper_exploration_would_block_reasons") or []:
        if str(reason) not in diagnostics:
            diagnostics.append(str(reason))
    return immutable, diagnostics
