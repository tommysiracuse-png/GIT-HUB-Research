"""Canonical paper-only quality evidence for public proxy signals.

Yahoo chart payloads do not provide an executable order book.  The scanners
therefore expose recent traded notional as an explicitly labelled depth proxy;
admission must never mistake it for top-of-book depth.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


PROXY_TRADE_TYPES = {"global_proxy_momentum", "global_market_discovery_proxy"}
DEFAULT_MAX_FRESHNESS_AGE_SECONDS = 3600.0
DEFAULT_MIN_DEPTH_NOTIONAL_USD = 1.0
DEFAULT_MIN_LIQUIDITY_SCORE = 0.65
HEALTHY_VENUE_STATES = {"healthy", "normal", "open", "reachable", "verified"}


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _first_number(record: Mapping[str, Any], fields: tuple[str, ...]) -> tuple[str | None, float | None]:
    for field in fields:
        if field not in record:
            continue
        value = _number(record.get(field))
        if value is not None:
            return field, value
    return None, None


def _gate_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    direct = config.get("proxy_short_quality_gate")
    if isinstance(direct, Mapping):
        return dict(direct)
    for section in ("market_admission", "strategy_reliability"):
        nested = config.get(section)
        if isinstance(nested, Mapping) and isinstance(nested.get("proxy_short_quality_gate"), Mapping):
            return dict(nested["proxy_short_quality_gate"])
    return {}


def is_proxy_short(record: Mapping[str, Any]) -> bool:
    return (
        str(record.get("direction") or "").strip().lower() == "short_proxy"
        and str(record.get("trade_type") or "").strip().lower() in PROXY_TRADE_TYPES
    )


def enrich_parsed_proxy_quality(candidate: dict[str, Any]) -> dict[str, Any]:
    """Attach normalized quality fields after a public proxy payload parsed."""

    trade_type = str(candidate.get("trade_type") or "").strip().lower()
    if trade_type not in PROXY_TRADE_TYPES:
        return candidate

    freshness_field, freshness_age = _first_number(
        candidate,
        ("freshness_age_seconds", "provider_age_seconds", "quote_age_seconds", "data_age_seconds"),
    )
    if freshness_age is None:
        stale_minutes = _number(candidate.get("stale_minutes"))
        if stale_minutes is not None:
            freshness_field = "stale_minutes"
            freshness_age = stale_minutes * 60.0
    if freshness_age is not None:
        candidate["freshness_age_seconds"] = round(max(0.0, freshness_age), 3)
        candidate["proxy_freshness_basis"] = freshness_field

    depth_field, depth_notional = _first_number(
        candidate,
        ("proxy_depth_notional_usd", "quote_volume_24h"),
    )
    if depth_notional is not None:
        candidate["proxy_depth_notional_usd"] = round(max(0.0, depth_notional), 2)
        candidate["proxy_depth_basis"] = (
            candidate.get("proxy_depth_basis")
            or ("recent_traded_notional" if depth_field == "quote_volume_24h" else depth_field)
        )

    if not candidate.get("proxy_venue_health_status"):
        data_status = str(candidate.get("data_status") or "").strip().lower()
        if data_status:
            candidate["proxy_venue_health_status"] = data_status
            candidate["proxy_venue_health_basis"] = "adapter_data_status"
        elif _number(candidate.get("last")) not in {None, 0.0}:
            candidate["proxy_venue_health_status"] = "healthy"
            candidate["proxy_venue_health_basis"] = "successful_public_chart_parse"
    return candidate


def proxy_short_quality_review(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail-closed freshness/depth/venue-health review for proxy shorts."""

    applies = is_proxy_short(candidate)
    cfg = _gate_config(config)
    enabled = bool(cfg.get("enabled", True))
    max_age = _number(cfg.get("max_freshness_age_seconds"))
    min_depth = _number(cfg.get("min_depth_notional_usd"))
    min_liquidity = _number(cfg.get("min_liquidity_score"))
    max_age = DEFAULT_MAX_FRESHNESS_AGE_SECONDS if max_age is None else max(0.0, max_age)
    min_depth = DEFAULT_MIN_DEPTH_NOTIONAL_USD if min_depth is None else max(0.0, min_depth)
    min_liquidity = DEFAULT_MIN_LIQUIDITY_SCORE if min_liquidity is None else max(0.0, min_liquidity)

    freshness_field, freshness_age = _first_number(
        candidate,
        ("freshness_age_seconds", "provider_age_seconds", "quote_age_seconds", "data_age_seconds"),
    )
    if freshness_age is None:
        stale_minutes = _number(candidate.get("stale_minutes"))
        if stale_minutes is not None:
            freshness_field = "stale_minutes"
            freshness_age = stale_minutes * 60.0
    depth_field, depth_notional = _first_number(
        candidate,
        (
            "proxy_depth_notional_usd",
            "top_of_book_depth_usd",
            "book_depth_usd",
            "local_depth_usd",
            "quote_volume_24h",
        ),
    )
    liquidity_field, liquidity_score = _first_number(candidate, ("liquidity_score", "liquidity_proxy_score"))
    health_field = next(
        (
            field
            for field in ("proxy_venue_health_status", "venue_health_status", "provider_health_status", "data_status")
            if str(candidate.get(field) or "").strip()
        ),
        None,
    )
    health_status = str(candidate.get(health_field) or "").strip().lower() if health_field else None

    failure_reasons: list[str] = []
    if applies and enabled:
        if freshness_age is None:
            failure_reasons.append("proxy_short_quality_missing_freshness")
        elif freshness_age > max_age:
            failure_reasons.append("proxy_short_quality_stale")
        if depth_notional is None:
            failure_reasons.append("proxy_short_quality_missing_depth")
        elif depth_notional < min_depth:
            failure_reasons.append("proxy_short_quality_insufficient_depth")
        if liquidity_score is None:
            failure_reasons.append("proxy_short_quality_missing_liquidity")
        elif liquidity_score < min_liquidity:
            failure_reasons.append("proxy_short_quality_liquidity_weak")
        if health_status is None:
            failure_reasons.append("proxy_short_quality_missing_venue_health")
        elif health_status not in HEALTHY_VENUE_STATES:
            failure_reasons.append("proxy_short_quality_venue_unhealthy")

    return {
        "paper_only": True,
        "applies": applies,
        "enabled": enabled,
        "eligible": not (applies and enabled and failure_reasons),
        "quality_failure_reason": failure_reasons[0] if failure_reasons else None,
        "quality_failure_reasons": failure_reasons,
        "freshness": {
            "field": freshness_field,
            "age_seconds": round(freshness_age, 3) if freshness_age is not None else None,
            "max_age_seconds": max_age,
        },
        "depth": {
            "field": depth_field,
            "notional_usd": round(depth_notional, 2) if depth_notional is not None else None,
            "basis": candidate.get("proxy_depth_basis"),
            "min_notional_usd": min_depth,
        },
        "liquidity": {
            "field": liquidity_field,
            "score": round(liquidity_score, 6) if liquidity_score is not None else None,
            "min_score": min_liquidity,
        },
        "venue_health": {
            "field": health_field,
            "status": health_status,
            "basis": candidate.get("proxy_venue_health_basis"),
        },
    }
