"""Paper-only LLM state packet helpers.

This module composes JSON-serializable fragments from existing paper/research
reports.  It has no side effects: no file writes, credential collection,
private API calls, order routing, or live trading enablement.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

try:  # Support both direct ``src`` imports and package-style imports.
    from route_intelligence import build_route_requirements_report
except ImportError:  # pragma: no cover - package import fallback
    from .route_intelligence import build_route_requirements_report


def _normalize_segment_value(value: Any, *, upper: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text.upper() if upper else text.lower()


def _segment_from_mapping(mapping: dict[str, Any]) -> dict[str, str | None]:
    return {
        "venue": _normalize_segment_value(
            mapping.get("venue") or mapping.get("exchange"),
            upper=True,
        ),
        "surface": _normalize_segment_value(
            mapping.get("surface")
            or mapping.get("execution_surface")
            or mapping.get("strategy_surface")
            or mapping.get("trade_type")
        ),
        "direction": _normalize_segment_value(mapping.get("direction")),
    }


def _prefixed_segment(mapping: dict[str, Any], prefix: str) -> dict[str, str | None]:
    return _segment_from_mapping(
        {
            "venue": mapping.get(f"{prefix}_venue") or mapping.get(f"{prefix}_exchange"),
            "surface": (
                mapping.get(f"{prefix}_surface")
                or mapping.get(f"{prefix}_execution_surface")
                or mapping.get(f"{prefix}_strategy_surface")
                or mapping.get(f"{prefix}_trade_type")
            ),
            "direction": mapping.get(f"{prefix}_direction"),
        }
    )


def _segment_present(segment: dict[str, str | None]) -> bool:
    return any(segment.values())


def _segment_complete(segment: dict[str, str | None]) -> bool:
    return bool(segment.get("venue") and segment.get("surface") and segment.get("direction"))


def _segment_key(segment: dict[str, str | None]) -> tuple[str | None, str | None, str | None]:
    return (
        segment.get("venue"),
        segment.get("surface"),
        segment.get("direction"),
    )


def _extract_segment(
    item: dict[str, Any],
    nested_keys: tuple[str, ...],
    prefixes: tuple[str, ...],
    *,
    fallback_to_current: bool = False,
) -> dict[str, str | None]:
    for key in nested_keys:
        nested = item.get(key)
        if isinstance(nested, dict):
            segment = _segment_from_mapping(nested)
            if _segment_present(segment):
                return segment
    for prefix in prefixes:
        segment = _prefixed_segment(item, prefix)
        if _segment_present(segment):
            return segment
    if fallback_to_current:
        return _segment_from_mapping(item)
    return {"venue": None, "surface": None, "direction": None}


def _collect_segment_evidence(item: dict[str, Any]) -> list[dict[str, str | None]]:
    candidates: list[dict[str, str | None]] = []

    def _consume(value: Any) -> None:
        if isinstance(value, dict):
            if any(
                key in value
                for key in (
                    "venue",
                    "exchange",
                    "surface",
                    "execution_surface",
                    "strategy_surface",
                    "trade_type",
                    "direction",
                )
            ):
                segment = _segment_from_mapping(value)
                if _segment_present(segment):
                    candidates.append(segment)
                return
            for nested_key in (
                "segments",
                "segment_evidence",
                "paper_evidence_segments",
                "supported_segments",
                "evidence",
                "matches",
            ):
                if nested_key in value:
                    _consume(value.get(nested_key))
        elif isinstance(value, (list, tuple, set)):
            for entry in value:
                _consume(entry)

    for key in (
        "strategy_lab_segment_evidence",
        "segment_evidence",
        "paper_segment_evidence",
        "paper_evidence_segments",
    ):
        _consume(item.get(key))
    paper_stats = item.get("paper_stats")
    if isinstance(paper_stats, dict):
        _consume(paper_stats)

    unique: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for segment in candidates:
        key = _segment_key(segment)
        if key in seen:
            continue
        unique.append(segment)
        seen.add(key)
    return unique


def _is_strategy_lab_candidate(item: dict[str, Any]) -> bool:
    return bool(
        item.get("strategy_lab_id")
        or item.get("lab_id")
        or item.get("strategy_lab")
        or str(item.get("candidate_source") or "").strip().lower() == "strategy_lab"
        or str(item.get("source") or "").strip().lower() == "strategy_lab"
    )


def assess_strategy_lab_promotion_guard(opportunity: dict[str, Any]) -> dict[str, Any] | None:
    """Return a paper-only Strategy Lab promotion assessment for an opportunity."""

    item = dict(opportunity or {})
    if not _is_strategy_lab_candidate(item):
        return None

    target_segment = _extract_segment(
        item,
        ("target_segment", "strategy_lab_target_segment"),
        ("target", "strategy_lab_target"),
        fallback_to_current=True,
    )
    source_segment = _extract_segment(
        item,
        ("source_segment", "origin_segment", "strategy_lab_source_segment"),
        ("source", "origin", "strategy_lab_source"),
    )
    if not _segment_present(source_segment):
        source_segment = dict(target_segment)

    differences = [
        f"{field}_mismatch"
        for field in ("venue", "surface", "direction")
        if source_segment.get(field)
        and target_segment.get(field)
        and source_segment.get(field) != target_segment.get(field)
    ]
    missing_fields = [
        f"missing_target_{field}"
        for field in ("venue", "surface", "direction")
        if not target_segment.get(field)
    ]

    evidence_segments = _collect_segment_evidence(item)
    target_key = _segment_key(target_segment)
    evidence_match = any(
        _segment_complete(segment) and _segment_key(segment) == target_key
        for segment in evidence_segments
    )

    assessment = {
        "strategy_lab_id": str(item.get("strategy_lab_id") or item.get("lab_id") or "").strip() or None,
        "source_segment": source_segment,
        "target_segment": target_segment,
        "source_target_differences": differences,
        "evidence_segments": evidence_segments,
        "target_segment_evidence_found": evidence_match,
    }

    if _segment_complete(source_segment) and _segment_complete(target_segment) and not differences:
        assessment.update(
            {
                "guard_status": "promotable_exact_segment",
                "promotion_scope": "exact_segment",
                "recommended_action": "promote",
                "blocker_reasons": [],
            }
        )
        return assessment

    if _segment_complete(target_segment) and evidence_match:
        assessment.update(
            {
                "guard_status": "promotable_with_segment_evidence",
                "promotion_scope": "validated_target_segment",
                "recommended_action": "promote",
                "blocker_reasons": [],
            }
        )
        return assessment

    blocker_reasons = differences + missing_fields
    if not evidence_match:
        blocker_reasons.append("missing_target_segment_evidence")
    assessment.update(
        {
            "guard_status": "blocked_pending_segment_evidence",
            "promotion_scope": "local_only",
            "recommended_action": "keep_local",
            "blocker_reasons": blocker_reasons,
        }
    )
    return assessment


def build_strategy_lab_promotion_guard_fragment(
    opportunities: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize Strategy Lab promotion-guard outcomes for a paper-only packet."""

    assessments: list[dict[str, Any]] = []
    for index, opportunity in enumerate(opportunities):
        assessment = assess_strategy_lab_promotion_guard(opportunity)
        if assessment is None:
            continue
        assessment["opportunity_index"] = index
        assessments.append(assessment)

    status_counts = Counter(item["guard_status"] for item in assessments)
    blocked_candidates = [
        {
            "strategy_lab_id": item.get("strategy_lab_id"),
            "opportunity_index": item.get("opportunity_index"),
            "promotion_scope": item.get("promotion_scope"),
            "blocker_reasons": list(item.get("blocker_reasons") or []),
            "target_segment": dict(item.get("target_segment") or {}),
        }
        for item in assessments
        if item.get("guard_status") == "blocked_pending_segment_evidence"
    ]
    return {
        "enabled": bool(assessments),
        "paper_only": True,
        "candidate_count": len(assessments),
        "promotable_count": sum(
            1 for item in assessments if str(item.get("recommended_action")) == "promote"
        ),
        "blocked_count": len(blocked_candidates),
        "status_counts": dict(status_counts),
        "blocked_candidates": blocked_candidates,
        "candidates": assessments,
    }


def build_route_intelligence_packet_fragment(
    opportunities: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Return a paper-only route-intelligence fragment for an LLM packet.

    The fragment is read-only and derived entirely from caller-supplied paper
    opportunities.  It intentionally does not collect credentials, call broker
    APIs, mutate order/fill state, or enable any live execution path.
    """

    opportunities = list(opportunities)
    return {
        "paper_only": True,
        "safety_constraints": [
            "read_only_output_only",
            "no_credentials",
            "no_live_trading",
        ],
        "route_intelligence_report": build_route_requirements_report(opportunities),
        "strategy_lab_promotion_guard": build_strategy_lab_promotion_guard_fragment(opportunities),
    }
