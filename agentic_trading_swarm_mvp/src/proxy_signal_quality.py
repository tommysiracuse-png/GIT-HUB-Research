"""Canonical paper-only quality evidence for public proxy signals.

Yahoo chart payloads do not provide an executable order book.  The scanners
therefore expose recent traded notional as an explicitly labelled depth proxy;
admission must never mistake it for top-of-book depth.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from typing import Any


PROXY_TRADE_TYPES = {
    "global_proxy_momentum",
    "global_market_discovery_proxy",
    "global_proxy_shock_reversal",
}
DEFAULT_MAX_FRESHNESS_AGE_SECONDS = 3600.0
DEFAULT_MIN_DEPTH_NOTIONAL_USD = 1.0
DEFAULT_MIN_LIQUIDITY_SCORE = 0.65
HEALTHY_VENUE_STATES = {"healthy", "normal", "open", "reachable", "verified"}
DEFAULT_PROXY_MOMENTUM_CONTEXT_POLICY = {
    "enabled": True,
    "paper_only": True,
    "minimum_move_strength_bps": 40.0,
    "minimum_tradable_followthrough_bps": 5.0,
    "max_freshness_age_seconds": 900.0,
    "minimum_volatility_normalized_persistence": 0.75,
    "ranking_penalty_points": 15.0,
    "minimum_counterfactual_allocation_multiplier": 0.25,
}


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "open", "active"}:
        return True
    if text in {"false", "0", "no", "n", "closed", "inactive"}:
        return False
    return None


def _safe_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _first_number(record: Mapping[str, Any], fields: tuple[str, ...]) -> tuple[str | None, float | None]:
    for field in fields:
        if field not in record:
            continue
        value = _number(record.get(field))
        if value is not None:
            return field, value
    return None, None


def _lookup_nested(*records: Mapping[str, Any] | None, keys: tuple[str, ...]) -> Any:
    for record in records:
        if not isinstance(record, Mapping):
            continue
        for key in keys:
            if key not in record:
                continue
            value = record.get(key)
            if value not in (None, ""):
                return value
    return None


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
    numeric = _number(value)
    if numeric is not None:
        if abs(numeric) > 10_000_000_000.0:
            numeric /= 1000.0
        return dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def is_proxy_trade(record: Mapping[str, Any]) -> bool:
    trade_type = str(record.get("trade_type") or "").strip().lower()
    venue = str(record.get("venue") or "").strip().upper()
    return trade_type in PROXY_TRADE_TYPES or venue == "YAHOO_PROXY"


def proxy_quote_age_seconds(
    candidate: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
) -> float | None:
    direct = _number(
        _lookup_nested(
            candidate,
            context,
            review,
            keys=(
                "source_quote_age_seconds",
                "quote_age_seconds",
                "proxy_quote_age_seconds",
                "freshness_age_seconds",
                "provider_age_seconds",
                "data_age_seconds",
            ),
        )
    )
    if direct is not None:
        return max(0.0, direct)
    stale_minutes = _number(_lookup_nested(candidate, context, review, keys=("stale_minutes", "freshness_minutes")))
    if stale_minutes is not None:
        return max(0.0, stale_minutes * 60.0)
    seen_at = _parse_timestamp(
        _lookup_nested(candidate, context, review, keys=("decision_time_utc", "seen_at", "opened_at", "opened_time_utc"))
    )
    source_at = _parse_timestamp(
        _lookup_nested(
            candidate,
            context,
            review,
            keys=("source_quote_timestamp", "source_bar_end_utc", "last_bar_utc", "last_trade_timestamp"),
        )
    )
    if seen_at is not None and source_at is not None:
        return max(0.0, (seen_at - source_at).total_seconds())
    return None


def proxy_quote_staleness_bucket(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "unknown"
    if age_seconds <= 15.0 * 60.0:
        return "fresh_le_15m"
    if age_seconds <= 60.0 * 60.0:
        return "aging_15m_to_60m"
    return "stale_gt_60m"


def proxy_holding_horizon_bucket(minutes: int | float | None) -> str:
    horizon = _number(minutes)
    if horizon is None:
        return "unknown"
    if horizon <= 15.0:
        return "scalp_le_15m"
    if horizon <= 60.0:
        return "intraday_16m_to_60m"
    if horizon <= 240.0:
        return "swing_61m_to_240m"
    return "position_gt_240m"


def proxy_session_bucket(
    candidate: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
) -> str:
    session_text = str(
        _lookup_nested(
            candidate,
            context,
            review,
            keys=("source_session_status", "proxy_session_status", "session_status", "market_session_status"),
        )
        or ""
    ).strip().lower().replace("-", "_")
    explicit_open = _as_bool(
        _lookup_nested(candidate, context, review, keys=("source_session_open", "proxy_session_open"))
    )
    if session_text in {"pre", "pre_market", "premarket", "pre_open"}:
        return "pre_market"
    if session_text in {"post", "post_market", "postpost", "after_hours"}:
        return "after_hours"
    if explicit_open is True or session_text in {"open", "regular", "regular_hours", "trading", "active"}:
        return "open"
    if explicit_open is False or session_text in {"closed", "off_session", "halted", "holiday", "weekend"}:
        return "closed"
    return "unknown"


def proxy_time_of_day_bucket(
    candidate: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
) -> str:
    moment = _parse_timestamp(
        _lookup_nested(
            candidate,
            context,
            review,
            keys=("decision_time_utc", "seen_at", "opened_at", "opened_time_utc", "source_quote_timestamp"),
        )
    )
    if moment is None:
        return "unknown"
    hour = moment.hour
    if hour < 6:
        return "overnight_00_to_06"
    if hour < 10:
        return "morning_06_to_10"
    if hour < 14:
        return "midday_10_to_14"
    if hour < 18:
        return "afternoon_14_to_18"
    return "evening_18_to_24"


def proxy_spread_regime_bucket(
    candidate: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
) -> tuple[float | None, str]:
    spread = _number(
        _lookup_nested(
            candidate,
            context,
            review,
            keys=("spread_bps", "effective_spread_bps", "best_bid_ask_spread_bps", "current_spread_bps"),
        )
    )
    if spread is None:
        return None, "unknown"
    spread = max(0.0, spread)
    if spread <= 3.0:
        return spread, "tight_le_3bps"
    if spread <= 8.0:
        return spread, "normal_3_to_8bps"
    if spread <= 15.0:
        return spread, "wide_8_to_15bps"
    return spread, "extreme_gt_15bps"


def proxy_routing_path_bucket(
    candidate: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None, str]:
    route_surface = _safe_text(
        _lookup_nested(candidate, context, review, keys=("route_surface", "execution_surface", "source_surface"))
    )
    signal_scope = str(
        _lookup_nested(candidate, context, review, keys=("signal_stats_scope",))
        or "direct"
    ).strip().lower()
    semantics = _safe_text(
        _lookup_nested(candidate, context, review, keys=("paper_execution_semantics", "execution_semantics"))
    )
    lowered_semantics = str(semantics or "").strip().lower()
    proxy_not_live_equivalent = bool(
        _as_bool(_lookup_nested(candidate, context, review, keys=("paper_proxy_not_live_equivalent",)))
    )
    lowered_surface = str(route_surface or "").strip().lower()
    if signal_scope == "synthetic_research":
        if "proxy_not_live_equivalent" in lowered_semantics or proxy_not_live_equivalent:
            return route_surface, semantics, "synthetic_proxy_not_live_equivalent"
        if lowered_surface:
            return route_surface, semantics, f"synthetic_{lowered_surface}"
        return route_surface, semantics, "synthetic_research"
    if signal_scope == "paper_proxy":
        if lowered_surface:
            return route_surface, semantics, f"paper_proxy_{lowered_surface}"
        return route_surface, semantics, "paper_proxy"
    if proxy_not_live_equivalent:
        if lowered_surface:
            return route_surface, semantics, f"proxy_not_live_equivalent_{lowered_surface}"
        return route_surface, semantics, "proxy_not_live_equivalent"
    if lowered_surface in {"proxy", "cross_surface", "auction_reference", "nav_reference"}:
        return route_surface, semantics, f"direct_{lowered_surface}"
    return route_surface, semantics, "direct_base"


def proxy_paper_trade_diagnostic_tags(
    candidate: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
    selected_hold_minutes: int | None = None,
    outcome_horizon_minutes: int | None = None,
) -> dict[str, Any]:
    if not is_proxy_trade(candidate):
        merged: dict[str, Any] = {}
        if isinstance(context, Mapping):
            merged.update(context)
        if isinstance(review, Mapping):
            merged.update(review)
        merged.update(candidate)
        if not is_proxy_trade(merged):
            return {}
    quote_age_seconds = proxy_quote_age_seconds(candidate, context=context, review=review)
    spread_bps, spread_bucket = proxy_spread_regime_bucket(candidate, context=context, review=review)
    route_surface, semantics, routing_bucket = proxy_routing_path_bucket(
        candidate,
        context=context,
        review=review,
    )
    session_bucket = proxy_session_bucket(candidate, context=context, review=review)
    time_bucket = proxy_time_of_day_bucket(candidate, context=context, review=review)
    selected_bucket = proxy_holding_horizon_bucket(selected_hold_minutes)
    outcome_bucket = proxy_holding_horizon_bucket(outcome_horizon_minutes)
    tags = {
        "tag_version": 1,
        "quote_age_seconds": round(quote_age_seconds, 3) if quote_age_seconds is not None else None,
        "quote_staleness_bucket": proxy_quote_staleness_bucket(quote_age_seconds),
        "session_bucket": session_bucket,
        "time_of_day_bucket": time_bucket,
        "session_time_bucket": f"{session_bucket}|{time_bucket}",
        "spread_bps": round(spread_bps, 3) if spread_bps is not None else None,
        "spread_regime_bucket": spread_bucket,
        "routing_path_bucket": routing_bucket,
        "route_surface": route_surface,
        "paper_execution_semantics": semantics,
        "signal_stats_scope": str(
            _lookup_nested(candidate, context, review, keys=("signal_stats_scope",))
            or "direct"
        ),
        "paper_proxy_not_live_equivalent": bool(
            _as_bool(_lookup_nested(candidate, context, review, keys=("paper_proxy_not_live_equivalent",)))
        ),
        "selected_hold_minutes": int(selected_hold_minutes) if _number(selected_hold_minutes) is not None else None,
        "selected_holding_horizon_bucket": selected_bucket,
        "outcome_horizon_minutes": int(outcome_horizon_minutes) if _number(outcome_horizon_minutes) is not None else None,
        "outcome_holding_horizon_bucket": outcome_bucket,
    }
    return tags


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


def proxy_momentum_context_review(
    candidate: Mapping[str, Any],
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score Yahoo momentum confirmation without suppressing paper research.

    A weak proxy move can remain a priceable native paper observation.  This
    review only lowers its ranking and paper allocation while preserving the
    raw candidate and its diagnostics for counterfactual measurement.
    """

    configured = (settings or {}).get("yahoo_proxy_momentum_context", {})
    policy = dict(DEFAULT_PROXY_MOMENTUM_CONTEXT_POLICY)
    if isinstance(configured, Mapping):
        policy.update({key: value for key, value in configured.items() if value is not None})
    if configured is False:
        policy["enabled"] = False

    trade_type = str(candidate.get("trade_type") or "").strip().lower()
    venue = str(candidate.get("venue") or "").strip().upper()
    mode = str(candidate.get("execution_mode") or candidate.get("mode") or "paper").strip().lower()
    paper_mode = mode in {"paper", "paper_only", "simulation", "sim", "review"}
    source_text = " ".join(
        str(value or "")
        for value in (
            candidate.get("data_source"),
            (candidate.get("data_source") or {}).get("provider")
            if isinstance(candidate.get("data_source"), Mapping)
            else None,
        )
    ).lower()
    yahoo_source = bool(
        venue == "YAHOO_PROXY"
        or isinstance(candidate.get("proxy_reuse_gate"), Mapping)
        or "yahoo" in source_text
    )
    applies = bool(
        policy.get("enabled", True)
        and policy.get("paper_only", True)
        and paper_mode
        and yahoo_source
        and trade_type in {"global_proxy_momentum", "global_market_discovery_proxy"}
    )
    if not applies:
        return {
            "enabled": bool(policy.get("enabled", True)),
            "applicable": False,
            "paper_only": True,
            "emission_action": "unchanged",
        }

    direction = str(candidate.get("direction") or "").strip().lower()
    move_pct = _number(candidate.get("change_24h_pct"))
    followthrough_pct = _number(candidate.get("short_return_pct"))
    move_bps = move_pct * 100.0 if move_pct is not None else None
    followthrough_bps = followthrough_pct * 100.0 if followthrough_pct is not None else None
    freshness_age = _number(
        candidate.get("freshness_age_seconds", candidate.get("provider_age_seconds"))
    )
    volatility_bps = _number(candidate.get("recent_volatility_bps"))
    min_move = max(0.001, float(policy["minimum_move_strength_bps"]))
    min_followthrough = max(0.001, float(policy["minimum_tradable_followthrough_bps"]))
    max_age = max(0.001, float(policy["max_freshness_age_seconds"]))
    min_persistence = max(0.001, float(policy["minimum_volatility_normalized_persistence"]))

    # A large proxy move in the opposite direction is not confirmation.  The
    # older magnitude-only check could label a falling proxy as confirmation
    # for a long paper candidate whenever the very short return happened to
    # be positive.  Keep the raw observation, but require the source move and
    # the proposed direction to agree before using it as primary-route
    # context.
    expected_sign = -1.0 if direction == "short_proxy" else 1.0
    signed_move = expected_sign * move_bps if move_bps is not None else None
    signed_followthrough = expected_sign * followthrough_bps if followthrough_bps is not None else None
    persistence = (
        abs(followthrough_bps) / volatility_bps
        if followthrough_bps is not None and volatility_bps is not None and volatility_bps > 0.0
        else None
    )
    components = {
        "proxy_move_strength": (
            min(100.0, 100.0 * signed_move / min_move)
            if signed_move is not None and signed_move > 0.0
            else 0.0
        ),
        "tradable_followthrough": (
            min(100.0, 100.0 * signed_followthrough / min_followthrough)
            if signed_followthrough is not None and signed_followthrough > 0.0
            else 0.0
        ),
        "freshness": (
            max(0.0, 100.0 * (1.0 - freshness_age / max_age))
            if freshness_age is not None
            else 50.0
        ),
        "volatility_normalized_persistence": (
            min(100.0, 100.0 * persistence / min_persistence)
            if persistence is not None
            else 0.0
        ),
    }
    diagnostics: list[str] = []
    if signed_move is None:
        diagnostics.append("proxy_move_strength_unavailable")
    elif signed_move < min_move:
        diagnostics.append("proxy_move_strength_below_confirmation")
    if signed_followthrough is None:
        diagnostics.append("tradable_followthrough_unavailable")
    elif signed_followthrough < min_followthrough:
        diagnostics.append("tradable_followthrough_not_confirmed")
    if freshness_age is None:
        diagnostics.append("proxy_freshness_unavailable")
    elif freshness_age > max_age:
        diagnostics.append("proxy_freshness_degraded")
    if persistence is None or persistence < min_persistence:
        diagnostics.append("volatility_normalized_persistence_not_confirmed")

    score = sum(components.values()) / len(components)
    confirmed = not diagnostics
    counterfactual_floor = max(
        0.01,
        min(1.0, float(policy["minimum_counterfactual_allocation_multiplier"])),
    )
    allocation_multiplier = 1.0 if confirmed else max(counterfactual_floor, score / 100.0)
    ranking_penalty = max(0.0, float(policy["ranking_penalty_points"])) * (1.0 - score / 100.0)
    return {
        "enabled": True,
        "applicable": True,
        "paper_only": True,
        "score": round(max(0.0, min(100.0, score)), 3),
        "components": {key: round(value, 3) for key, value in components.items()},
        "diagnostics": diagnostics,
        "confirmed": confirmed,
        "emission_action": "primary_simulated_route" if confirmed else "counterfactual_guard_value",
        "allocation_multiplier": round(allocation_multiplier, 6),
        "ranking_penalty_points": round(ranking_penalty, 6),
        "proxy_move_bps": round(move_bps, 6) if move_bps is not None else None,
        "directional_proxy_move_bps": round(signed_move, 6) if signed_move is not None else None,
        "minimum_move_strength_bps": min_move,
        "tradable_followthrough_bps": round(followthrough_bps, 6) if followthrough_bps is not None else None,
        "minimum_tradable_followthrough_bps": min_followthrough,
        "freshness_age_seconds": round(freshness_age, 6) if freshness_age is not None else None,
        "max_freshness_age_seconds": max_age,
        "volatility_bps": round(volatility_bps, 6) if volatility_bps is not None else None,
        "volatility_normalized_persistence": round(persistence, 6) if persistence is not None else None,
        "minimum_volatility_normalized_persistence": min_persistence,
    }
