"""Read-only frontier quality dashboard helpers.

This module intentionally produces report metrics only.  It does not mutate
candidates, change scoring, filter signals, size orders, resolve routes, call
networks, or require credentials.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Sequence


_QUALITY_COUNT_KEYS = (
    "known_quality_count",
    "depth_enriched_count",
    "quality_known_count",
)
_TOTAL_COUNT_KEYS = (
    "observation_count",
    "total_observation_count",
    "candidate_count",
)
_UNKNOWN_COUNT_KEYS = (
    "unknown_quality_count",
    "quality_unknown_count",
)
_RATE_KEYS = (
    "known_quality_rate",
    "quality_coverage_rate",
)
_CANDIDATE_KEYS = (
    "candidates",
    "candidate_reports",
    "top_dislocations",
    "frontier_candidates",
)
_OUTCOME_KEYS = (
    "outcome_relationship_60m",
    "quality_bucket_outcomes_60m",
    "quality_bucket_outcomes",
)
_IDENTITY_KEYS = (
    "market_key",
    "candidate_id",
    "venue_symbol",
    "symbol",
    "instrument",
    "pair",
    "market",
    "route_key",
)
_DEGRADED_FLAG_KEYS = (
    "degraded",
    "is_degraded",
    "quality_degraded",
)
_SHADOW_FLAG_KEYS = (
    "shadow_only",
    "is_shadow_only",
)
_DEGRADED_STATUS_KEYS = (
    "status",
    "quality_status",
    "frontier_quality_status",
    "route_status",
    "flags",
    "quality_flags",
    "warnings",
)
_SHADOW_STATUS_KEYS = (
    "status",
    "route_status",
    "mode",
    "execution_mode",
    "paper_status",
    "flags",
    "quality_flags",
    "warnings",
)
_WARNING_FLAG_KEYS = (
    "simulated_slippage_exceeds_edge",
    "round_trip_cost_exceeds_edge",
    "cost_exceeds_edge",
    "losing_after_costs",
)
_EDGE_KEYS = (
    "gross_edge_bps",
    "expected_gross_edge_bps",
    "edge_bps",
    "expected_edge_bps",
    "gross_edge",
)
_COST_KEYS = (
    "modeled_round_trip_cost_bps",
    "round_trip_cost_bps",
    "total_cost_bps",
    "simulated_slippage_bps",
    "cost_bps",
)
_NET_EDGE_KEYS = (
    "net_edge_bps",
    "edge_after_cost_bps",
    "after_cost_edge_bps",
)
_KNOWN_QUALITY_KEYS = (
    "quality_score",
    "depth_quality_score",
    "quality_bucket",
    "quality",
)
_KNOWN_QUALITY_FLAGS = (
    "quality_known",
    "known_quality",
    "depth_enriched",
)


def summarize_frontier_quality(
    report: Optional[Mapping[str, Any]] = None,
    candidates: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return paper-only dashboard metrics for supplied frontier dictionaries.

    ``report`` and ``candidates`` are treated as read-only inputs.  The returned
    summary is suitable for dashboards or logs and deliberately has no impact on
    ranking, route eligibility, sizing, or paper order generation.
    """

    report_dict: Mapping[str, Any] = report or {}
    if not isinstance(report_dict, Mapping):
        raise TypeError("report must be a mapping or None")

    sections = _report_sections(report_dict)
    candidate_rows = _candidate_rows(report_dict, candidates)

    supplied_unknown_count = _first_int(sections, _UNKNOWN_COUNT_KEYS)
    known_count = _first_int(sections, _QUALITY_COUNT_KEYS)
    total_count = _first_int(sections, _TOTAL_COUNT_KEYS)

    if total_count is None and candidate_rows:
        total_count = len(candidate_rows)
    if known_count is None and total_count is not None and supplied_unknown_count is not None:
        known_count = max(total_count - supplied_unknown_count, 0)
    if known_count is None and candidate_rows:
        known_count = sum(1 for row in candidate_rows if _has_known_quality(row))

    if total_count is not None and known_count is not None:
        unknown_count = max(total_count - known_count, 0)
    elif supplied_unknown_count is not None:
        unknown_count = supplied_unknown_count
    elif candidate_rows:
        unknown_count = sum(1 for row in candidate_rows if not _has_known_quality(row))
    else:
        unknown_count = 0

    if known_count is not None and total_count:
        known_quality_rate = round(float(known_count) / float(total_count), 4)
    else:
        supplied_rate = _first_float(sections, _RATE_KEYS)
        known_quality_rate = round(supplied_rate, 4) if supplied_rate is not None else 0.0

    degraded_shadow_only = [
        _candidate_name(row) for row in candidate_rows if _is_degraded_shadow_only(row)
    ]
    cost_warnings = [
        _candidate_name(row) for row in candidate_rows if _cost_exceeds_edge(row)
    ]

    candidate_count = len(candidate_rows)
    if candidate_count == 0:
        candidate_count = _first_int(sections, ("candidate_count",)) or 0

    return {
        "candidate_count": candidate_count,
        "observation_count": total_count or 0,
        "known_quality_count": known_count or 0,
        "known_quality_rate": known_quality_rate,
        "quality_coverage_rate": known_quality_rate,
        "unknown_quality_count": unknown_count,
        "degraded_shadow_only_count": len(degraded_shadow_only),
        "degraded_shadow_only_candidates": degraded_shadow_only,
        "simulated_slippage_exceeds_edge_count": len(cost_warnings),
        "simulated_slippage_exceeds_edge_candidates": cost_warnings,
        "outcome_relationship_60m": _outcome_rows(report_dict),
    }


def _report_sections(report: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    sections: List[Mapping[str, Any]] = []
    frontier_quality = report.get("frontier_quality")
    if isinstance(frontier_quality, Mapping):
        sections.append(frontier_quality)
    sections.append(report)
    return sections


def _candidate_rows(
    report: Mapping[str, Any],
    candidates: Optional[Iterable[Mapping[str, Any]]],
) -> List[Mapping[str, Any]]:
    if candidates is not None:
        if isinstance(candidates, Mapping):
            return [candidates]
        return [row for row in candidates if isinstance(row, Mapping)]

    for section in _report_sections(report):
        for key in _CANDIDATE_KEYS:
            rows = section.get(key)
            if isinstance(rows, Mapping):
                mapped_rows = [
                    row for row in rows.values() if isinstance(row, Mapping)
                ]
                if mapped_rows:
                    return mapped_rows
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                listed_rows = [row for row in rows if isinstance(row, Mapping)]
                if listed_rows:
                    return listed_rows
    return []


def _outcome_rows(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    for section in _report_sections(report):
        for key in _OUTCOME_KEYS:
            rows = section.get(key)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _first_int(
    sections: Iterable[Mapping[str, Any]],
    keys: Iterable[str],
) -> Optional[int]:
    value = _first_float(sections, keys)
    return int(value) if value is not None else None


def _first_float(
    sections: Iterable[Mapping[str, Any]],
    keys: Iterable[str],
) -> Optional[float]:
    for section in sections:
        for key in keys:
            value = _to_float(section.get(key))
            if value is not None:
                return value
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _truthy_flag(row: Mapping[str, Any], keys: Iterable[str]) -> bool:
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool):
            if value:
                return True
            continue
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes", "y"}:
                return True
            continue
        if value:
            return True
    return False


def _value_contains(value: Any, needle: str) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, set)):
        return any(_value_contains(item, needle) for item in value)
    text = str(value).lower().replace("-", "_")
    return needle in text


def _contains_status(
    row: Mapping[str, Any],
    keys: Iterable[str],
    needle: str,
) -> bool:
    return any(_value_contains(row.get(key), needle) for key in keys)


def _is_degraded_shadow_only(row: Mapping[str, Any]) -> bool:
    degraded = _truthy_flag(row, _DEGRADED_FLAG_KEYS) or _contains_status(
        row, _DEGRADED_STATUS_KEYS, "degraded"
    )
    shadow_only = _truthy_flag(row, _SHADOW_FLAG_KEYS) or _contains_status(
        row, _SHADOW_STATUS_KEYS, "shadow_only"
    )
    return degraded and shadow_only


def _cost_exceeds_edge(row: Mapping[str, Any]) -> bool:
    if _truthy_flag(row, _WARNING_FLAG_KEYS):
        return True

    cost = _first_float((row,), _COST_KEYS)
    edge = _first_float((row,), _EDGE_KEYS)
    if cost is not None and edge is not None:
        return cost > edge

    net_edge = _first_float((row,), _NET_EDGE_KEYS)
    return net_edge is not None and net_edge < 0


def _has_known_quality(row: Mapping[str, Any]) -> bool:
    if _truthy_flag(row, _KNOWN_QUALITY_FLAGS):
        return True
    for key in _KNOWN_QUALITY_KEYS:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip().lower() in {"", "unknown", "none", "nan"}:
            continue
        return True
    return False


def _candidate_name(row: Mapping[str, Any]) -> str:
    for key in _IDENTITY_KEYS:
        value = row.get(key)
        if value:
            return str(value)

    venue = row.get("venue") or row.get("exchange")
    symbol = row.get("base_symbol") or row.get("asset")
    if venue and symbol:
        return f"{venue}:{symbol}"
    return "<unknown>"
