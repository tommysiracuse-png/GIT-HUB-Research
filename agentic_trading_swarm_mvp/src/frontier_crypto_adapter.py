#!/usr/bin/env python3
"""Frontier crypto venue adapter.

Public market-data only. This module expands the radar's venue map and creates
paper-only exploratory candidates from reachable public endpoints. It never
uses credentials, private/account APIs, or order endpoints.
"""

from __future__ import annotations


def classify_fiat_corridor(base_asset, quote_asset, venue_name=None, venue_notes=None):
    """
    Classify a public paper-only market into a broad liquidity corridor.

    This is metadata only: no routing, no execution, no trade decisions.
    """
    base = (base_asset or "").strip().upper()
    quote = (quote_asset or "").strip().upper()
    venue = (venue_name or "").strip().upper()
    notes = (venue_notes or "").strip().upper()

    if quote in {"USD", "USDT", "USDC"}:
        corridor_type = f"global_{quote.lower()}"
        corridor_confidence = 0.95
    elif quote in {"BTC", "ETH"}:
        corridor_type = f"{quote.lower()}_cross"
        corridor_confidence = 0.88
    else:
        corridor_type = "local_fiat"
        corridor_confidence = 0.72

    if any(token in venue for token in ("BINANCE", "COINBASE", "KRAKEN", "GATE", "KUCOIN", "MEXC", "BITGET")):
        corridor_confidence = min(0.99, corridor_confidence + 0.03)
    if "LOCAL" in notes or "FIAT" in notes:
        corridor_confidence = min(0.99, corridor_confidence + 0.02)

    return {
        "corridor_base": base,
        "corridor_quote": quote,
        "corridor_type": corridor_type,
        "corridor_confidence": round(corridor_confidence, 3),
    }


def apply_fiat_corridor_penalty(opportunity_score, corridor_type=None, liquidity_confidence=None,
                                depth_confidence=None, turnover_confidence=None):
    """
    Paper-only score adjustment for locally constrained fiat corridors.
    """
    score = float(opportunity_score or 0.0)
    corridor = (corridor_type or "").strip().lower()
    liquidity = float(liquidity_confidence or 0.0)
    depth = float(depth_confidence or 0.0)
    turnover = float(turnover_confidence or 0.0)

    strong_liquidity = liquidity >= 0.8 or (depth >= 0.75 and turnover >= 0.75)
    if corridor == "local_fiat" and not strong_liquidity:
        score *= 0.9
    return score

import argparse
import collections
import copy
import datetime as dt
import json
import math
import pathlib
import statistics
import time
import urllib.error
import urllib.request

from regional_fx_reference import get_regional_fx_references
from scan_batch import ScanBatch, normalize_observation

DEFAULT_PAPER_ONLY_EXECUTABLE_QUALITY_POLICY = {
    "fee_buffer_bps": 4.0,
    "slippage_buffer_bps": 6.0,
    "min_net_edge_bps": 12.0,
    "max_quote_age_ms": 1500.0,
    "max_spread_as_pct_of_edge": 0.35,
    "min_depth_multiple_of_paper_size": 3.0,
}


DEFAULT_PAPER_ONLY_CONFIDENCE_POLICY = {
    "min_confidence": 0.70,
    "trend_weight": 0.40,
    "momentum_weight": 0.35,
    "liquidity_weight": 0.25,
}


DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY = {
    "enabled": True,
    "min_closed_trades": 8,
    "min_confidence_to_allow": 0.55,
    "min_multiplier_to_allow": 1.0,
    "expectancy_scale_bps": 18.0,
    "max_abs_expectancy_contribution": 0.22,
    "max_abs_win_rate_contribution": 0.12,
    "max_abs_payoff_contribution": 0.10,
    "sample_size_pivot": 18,
    "multiplier_floor": 0.65,
    "multiplier_ceiling": 1.20,
    "block_on_negative_expectancy": True,
}

DEFAULT_PAPER_ONLY_FRONTIER_LONG_COHORT_POLICY = {
    "enabled": True,
    "min_closed_trades": 12,
    "min_recent_expectancy_bps": -6.0,
    "min_recent_win_rate": 0.42,
    "low_feasibility_max_share": 0.35,
    "decay_floor": 0.55,
    "suppress_floor": 0.60,
}

DEFAULT_PAPER_ONLY_FRONTIER_VENUE_DIRECTION_EXPECTANCY_REGISTRY = {
    "OKX_SPOT_LONG": {
        "enabled": True,
        "min_closed_trades": 8,
        "min_expectancy_bps": 0.0,
    },
    "BYBIT_SPOT_LONG": {
        "enabled": True,
        "min_closed_trades": 8,
        "min_expectancy_bps": 0.0,
    },
}


DEFAULT_PAPER_ONLY_CROSS_MARKET_RISK_GATE_POLICY = {
    "enabled": True,
    "min_divergence_bps": 0.0,
    "freshness_limit_ms": 1500.0,
    "mean_reversion_bps": 0.5,
    "stale_penalty_multiplier": 0.0,
    "record_multiplier": 1.0,
    "min_confidence": 0.72,
    "required_persistence_cycles": 2,
    "volatility_expansion_filter": True,
}


DEFAULT_PAPER_ONLY_CROSS_MARKET_SIGNAL_QUALITY_POLICY = {
    "enabled": True,
    "min_confidence": 0.68,
    "confirmation_window_ms": 15 * 60 * 1000.0,
    "require_primary_trigger": True,
    "require_related_market_confirmation": True,
    "below_threshold_state": "observe_only",
}


DEFAULT_PAPER_ONLY_ROUTE_FRESHNESS_POLICY = {
    "enabled": True,
    "quote_stale_threshold_ms": 750.0,
    "all_routes_stale_behavior": "suppress_fill",
}

DEFAULT_PAPER_ONLY_CONDITIONAL_SHORT_ROUTE_POLICY = {
    "enabled": True,
    "unsupported_behavior": "suppress",
    "unknown_behavior": "penalize",
    "unknown_multiplier": 0.75,
    "margin_permission_multiplier": 0.96,
    "borrow_check_multiplier": 0.94,
    "fee_bps_reference": 10.0,
    "max_fee_penalty_reduction": 0.06,
}

DEFAULT_PAPER_ONLY_CONDITIONAL_SHORT_ROUTE_REQUIREMENTS = {
    "GATE": {
        "supports_spot_short": True,
        "requires_margin_permission": True,
        "requires_borrow_check": True,
        "fee_bps_hint": 10.0,
        "margin_mode_hint": "cross_or_isolated_margin",
        "api_route_hint": "spot_margin",
    },
    "OKX": {
        "supports_spot_short": True,
        "requires_margin_permission": True,
        "requires_borrow_check": True,
        "fee_bps_hint": 8.0,
        "margin_mode_hint": "spot_margin",
        "api_route_hint": "margin_spot",
    },
    "BINANCE_US": {
        "supports_spot_short": False,
        "requires_margin_permission": None,
        "requires_borrow_check": None,
        "fee_bps_hint": None,
        "margin_mode_hint": "unsupported",
        "api_route_hint": "unsupported",
    },
    "BITSO": {},
}


def paper_only_build_governor_fields(
    *,
    category: str = "paper_scoring_logic",
    implementation_mode: str = "paper_policy",
    paper_only: bool = True,
    trade_effecting: bool = False,
) -> dict:
    """Standard paper-only build-governor metadata for report packets."""

    return {
        "category": str(category),
        "implementation_mode": str(implementation_mode),
        "paper_only": bool(paper_only),
        "trade_effecting": bool(trade_effecting),
    }


def paper_only_cross_market_risk_gate(
    *,
    divergence_bps: float | None = None,
    trigger_bps: float | None = None,
    source_a_freshness_ms: float | None = None,
    source_b_freshness_ms: float | None = None,
    freshness_limit_ms: float | None = None,
    mean_reversion_bps: float | None = None,
    enabled: bool = True,
) -> dict:
    """Paper-only gate for cross-market divergence observation and exit logic."""

    divergence = float(divergence_bps or 0.0)
    trigger = float(trigger_bps or 0.0)
    freshness_limit = float(freshness_limit_ms or DEFAULT_PAPER_ONLY_CROSS_MARKET_RISK_GATE_POLICY["freshness_limit_ms"])
    mean_reversion = float(mean_reversion_bps or DEFAULT_PAPER_ONLY_CROSS_MARKET_RISK_GATE_POLICY["mean_reversion_bps"])
    freshness_a = float(source_a_freshness_ms or 0.0)
    freshness_b = float(source_b_freshness_ms or 0.0)
    fresh = freshness_a <= freshness_limit and freshness_b <= freshness_limit
    exceeds_trigger = divergence > trigger
    mean_reverted = abs(divergence) <= mean_reversion
    allow_record = bool(enabled and fresh and exceeds_trigger)
    close_position = bool(enabled and (not fresh or mean_reverted))
    score_multiplier = 1.0 if allow_record else 0.0
    if close_position and not allow_record:
        score_multiplier = 0.0
    return {
        "enabled": bool(enabled),
        "allow_record": allow_record,
        "close_position": close_position,
        "fresh": fresh,
        "exceeds_trigger": exceeds_trigger,
        "mean_reverted": mean_reverted,
        "score_multiplier": max(0.0, min(1.0, score_multiplier)),
        "divergence_bps": divergence,
        "trigger_bps": trigger,
        "freshness_limit_ms": freshness_limit,
    }


def paper_only_cross_market_signal_quality_gate(
    *,
    confidence: float | None = None,
    primary_trigger_present: bool = False,
    related_market_confirmed: bool = False,
    signal_age_ms: float | None = None,
    confirmation_window_ms: float | None = None,
    enabled: bool = True,
    min_confidence: float | None = None,
    below_threshold_state: str | None = None,
) -> dict:
    """Paper-only signal ranking gate for cross-market confirmation."""

    threshold = float(
        min_confidence
        if min_confidence is not None
        else DEFAULT_PAPER_ONLY_CROSS_MARKET_SIGNAL_QUALITY_POLICY["min_confidence"]
    )
    window_ms = float(
        confirmation_window_ms
        if confirmation_window_ms is not None
        else DEFAULT_PAPER_ONLY_CROSS_MARKET_SIGNAL_QUALITY_POLICY["confirmation_window_ms"]
    )
    state = str(
        below_threshold_state
        if below_threshold_state is not None
        else DEFAULT_PAPER_ONLY_CROSS_MARKET_SIGNAL_QUALITY_POLICY["below_threshold_state"]
    )
    score = float(confidence or 0.0)
    age_ms = float(signal_age_ms or 0.0)
    within_window = age_ms <= window_ms
    promote = bool(
        enabled
        and primary_trigger_present
        and related_market_confirmed
        and within_window
        and score >= threshold
    )
    observe_only = bool(not promote)
    return {
        "enabled": bool(enabled),
        "promote": promote,
        "observe_only": observe_only,
        "primary_trigger_present": bool(primary_trigger_present),
        "related_market_confirmed": bool(related_market_confirmed),
        "within_confirmation_window": within_window,
        "confidence": score,
        "min_confidence": threshold,
        "signal_age_ms": age_ms,
        "confirmation_window_ms": window_ms,
        "state": "promoted" if promote else state,
    }


def paper_only_route_freshness_gate(
    routes: list[dict] | tuple[dict, ...] | None,
    *,
    quote_stale_threshold_ms: float | None = None,
    all_routes_stale_behavior: str | None = None,
    enabled: bool = True,
) -> dict:
    """Paper-only route freshness gate for stale-quote suppression."""

    threshold = float(
        quote_stale_threshold_ms
        if quote_stale_threshold_ms is not None
        else DEFAULT_PAPER_ONLY_ROUTE_FRESHNESS_POLICY["quote_stale_threshold_ms"]
    )
    stale_behavior = (all_routes_stale_behavior or DEFAULT_PAPER_ONLY_ROUTE_FRESHNESS_POLICY["all_routes_stale_behavior"]).strip().lower()
    candidates = [dict(route) for route in (routes or [])]
    ranked = []
    for route in candidates:
        age_ms = route.get("quote_age_ms")
        try:
            quote_age_ms = float(age_ms)
        except (TypeError, ValueError):
            quote_age_ms = math.inf
        route["quote_age_ms"] = quote_age_ms
        route["fresh"] = quote_age_ms <= threshold
        ranked.append(route)
    fresh_routes = [route for route in ranked if route["fresh"]]
    fresh_routes.sort(key=lambda item: (float(item.get("price", math.inf)), float(item.get("latency_ms", math.inf))))
    stale_routes = [route for route in ranked if not route["fresh"]]
    selected = fresh_routes[0] if fresh_routes else None
    suppress_fill = bool(enabled and selected is None and stale_behavior == "suppress_fill")
    return {
        "enabled": bool(enabled),
        "quote_stale_threshold_ms": threshold,
        "all_routes_stale_behavior": stale_behavior,
        "selected_route": selected,
        "eligible_routes": fresh_routes,
        "stale_routes": stale_routes,
        "suppress_fill": suppress_fill,
        "route_stale_no_fill": suppress_fill,
    }


def _paper_only_route_requirement_keys(venue: str) -> tuple[str, ...]:
    normalized = str(venue or "").strip().upper().replace("-", "_").replace("/", "_")
    if not normalized:
        return ("",)

    keys: list[str] = []

    def _append(value: str) -> None:
        if value and value not in keys:
            keys.append(value)

    _append(normalized)
    for suffix in ("_SPOT", "_PUBLIC"):
        if normalized.endswith(suffix):
            _append(normalized[: -len(suffix)])

    if normalized in {"GATEIO", "GATE_IO"}:
        _append("GATE")
    if normalized.startswith("OKX"):
        _append("OKX")
    if normalized in {"BINANCEUS", "BINANCE_US"}:
        _append("BINANCE_US")
    return tuple(keys)


def paper_only_conditional_short_route_requirements(
    *,
    venue: str,
    registry: dict | None = None,
) -> dict:
    """Resolve paper-only spot-short route requirements for a frontier venue."""

    registry = registry or DEFAULT_PAPER_ONLY_CONDITIONAL_SHORT_ROUTE_REQUIREMENTS
    resolved_key = None
    resolved_entry: dict = {}
    for key in _paper_only_route_requirement_keys(venue):
        candidate = registry.get(key)
        if isinstance(candidate, dict):
            resolved_key = key
            resolved_entry = copy.deepcopy(candidate)
            break

    normalized_venue = str(venue or "").strip().upper()
    supports_spot_short = resolved_entry.get("supports_spot_short")
    if supports_spot_short is True:
        support_status = "supported"
    elif supports_spot_short is False:
        support_status = "unsupported"
    else:
        support_status = "unknown"

    notes = []
    if resolved_entry.get("requires_margin_permission") is True:
        notes.append("margin_permission_required")
    if resolved_entry.get("requires_borrow_check") is True:
        notes.append("borrow_check_required")
    if support_status == "unknown":
        notes.append("support_unknown")
    elif support_status == "unsupported":
        notes.append("spot_short_unsupported")

    return {
        "venue": normalized_venue,
        "venue_key": resolved_key or normalized_venue,
        "supports_spot_short": resolved_entry.get("supports_spot_short"),
        "requires_margin_permission": resolved_entry.get("requires_margin_permission"),
        "requires_borrow_check": resolved_entry.get("requires_borrow_check"),
        "fee_bps_hint": resolved_entry.get("fee_bps_hint"),
        "margin_mode_hint": resolved_entry.get("margin_mode_hint"),
        "api_route_hint": resolved_entry.get("api_route_hint"),
        "support_status": support_status,
        "notes": notes,
    }


def _paper_only_is_conditional_short_context(direction: str, context_stats: dict | None) -> bool:
    normalized_direction = str(direction or "").strip().lower()
    if "short" not in normalized_direction:
        return False

    stats = context_stats or {}
    if bool(stats.get("conditional")):
        return True

    for key in (
        "opportunity_style",
        "route_type",
        "route_style",
        "execution_style",
        "entry_style",
        "context_key",
        "signal_key",
    ):
        value = stats.get(key)
        if value is not None and "conditional" in str(value).lower():
            return True
    return False


def paper_only_conditional_short_route_feasibility_gate(
    *,
    venue: str,
    direction: str,
    context_stats: dict | None = None,
    registry: dict | None = None,
    policy: dict | None = None,
    enabled: bool = True,
) -> dict:
    """Paper-only feasibility gate for conditional frontier spot shorts."""

    merged_policy = copy.deepcopy(DEFAULT_PAPER_ONLY_CONDITIONAL_SHORT_ROUTE_POLICY)
    if isinstance(policy, dict):
        merged_policy.update(policy)

    if not enabled or not bool(merged_policy.get("enabled", True)):
        return {
            "enabled": False,
            "applied": False,
            "allow": True,
            "suppressed": False,
            "score_multiplier": 1.0,
            "reason": "disabled",
            "reasons": ["disabled"],
            "route_requirements": None,
        }

    if not _paper_only_is_conditional_short_context(direction, context_stats):
        return {
            "enabled": True,
            "applied": False,
            "allow": True,
            "suppressed": False,
            "score_multiplier": 1.0,
            "reason": "not_applicable",
            "reasons": ["not_applicable"],
            "route_requirements": None,
        }

    requirements = paper_only_conditional_short_route_requirements(venue=venue, registry=registry)
    support_status = requirements.get("support_status")
    reasons: list[str] = []
    suppressed = False
    allow = True
    score_multiplier = 1.0

    if support_status == "unsupported":
        reasons.append("unsupported_spot_short")
        if str(merged_policy.get("unsupported_behavior", "suppress")).strip().lower() == "suppress":
            suppressed = True
            allow = False
            score_multiplier = 0.0
    elif support_status == "unknown":
        reasons.append("unknown_spot_short_support")
        if str(merged_policy.get("unknown_behavior", "penalize")).strip().lower() == "suppress":
            suppressed = True
            allow = False
            score_multiplier = 0.0
        else:
            score_multiplier *= max(0.0, min(1.0, float(merged_policy.get("unknown_multiplier", 0.75) or 0.75)))
    else:
        if requirements.get("requires_margin_permission") is True:
            reasons.append("margin_permission_required")
            score_multiplier *= max(
                0.0,
                min(1.0, float(merged_policy.get("margin_permission_multiplier", 0.96) or 0.96)),
            )
        if requirements.get("requires_borrow_check") is True:
            reasons.append("borrow_check_required")
            score_multiplier *= max(
                0.0,
                min(1.0, float(merged_policy.get("borrow_check_multiplier", 0.94) or 0.94)),
            )
        fee_bps_hint = requirements.get("fee_bps_hint")
        try:
            fee_bps = float(fee_bps_hint)
        except (TypeError, ValueError):
            fee_bps = 0.0
        if fee_bps > 0.0:
            fee_reference = max(float(merged_policy.get("fee_bps_reference", 10.0) or 10.0), 1.0)
            max_fee_penalty_reduction = max(
                0.0,
                min(0.25, float(merged_policy.get("max_fee_penalty_reduction", 0.06) or 0.06)),
            )
            score_multiplier *= 1.0 - (max_fee_penalty_reduction * min(fee_bps / fee_reference, 1.0))
            reasons.append("fee_hint_penalty")

    return {
        "enabled": True,
        "applied": True,
        "allow": allow,
        "suppressed": suppressed,
        "score_multiplier": max(0.0, min(1.0, score_multiplier)),
        "reason": reasons[0] if reasons else "supported",
        "reasons": reasons or ["supported"],
        "route_requirements": requirements,
    }


def paper_only_frontier_score_adjustment(
    *,
    venue: str,
    direction: str,
    context_stats: dict | None = None,
    registry: dict | None = None,
    long_cohort_closed_trade_count: int | None = None,
    long_cohort_recent_expectancy_bps: float | None = None,
    long_cohort_recent_win_rate: float | None = None,
    long_cohort_low_feasibility_share: float | None = None,
    route_feasibility_policy: dict | None = None,
    enabled: bool = True,
) -> dict:
    """Paper-only frontier score adjustment for safe, reportable gating."""

    stats = context_stats or {}
    cohort_closed = int(
        long_cohort_closed_trade_count
        if long_cohort_closed_trade_count is not None
        else stats.get("closed_trade_count")
        or stats.get("closed_trades")
        or 0
    )
    cohort_expectancy = float(
        long_cohort_recent_expectancy_bps
        if long_cohort_recent_expectancy_bps is not None
        else stats.get("recent_expectancy_bps")
        or stats.get("expectancy_bps")
        or 0.0
    )
    cohort_win_rate = float(
        long_cohort_recent_win_rate
        if long_cohort_recent_win_rate is not None
        else stats.get("recent_win_rate")
        or stats.get("win_rate")
        or 0.0
    )
    cohort_low_feasibility_share = float(
        long_cohort_low_feasibility_share
        if long_cohort_low_feasibility_share is not None
        else stats.get("low_feasibility_share")
        or 0.0
    )

    gate = paper_only_frontier_venue_direction_expectancy_gate(
        venue=venue,
        direction=direction,
        context_stats=stats,
        registry=registry,
        enabled=enabled,
    )
    cohort_gate = paper_only_frontier_long_cohort_gate(
        closed_trade_count=cohort_closed,
        recent_expectancy_bps=cohort_expectancy,
        recent_win_rate=cohort_win_rate,
        low_feasibility_share=cohort_low_feasibility_share,
        enabled=enabled,
    )
    cross_market_gate = paper_only_cross_market_risk_gate(
        divergence_bps=stats.get("cross_market_divergence_bps"),
        trigger_bps=stats.get("cross_market_trigger_bps"),
        source_a_freshness_ms=stats.get("source_a_freshness_ms"),
        source_b_freshness_ms=stats.get("source_b_freshness_ms"),
        freshness_limit_ms=stats.get("freshness_limit_ms"),
        mean_reversion_bps=stats.get("mean_reversion_bps"),
        enabled=enabled,
    )
    route_feasibility_gate = paper_only_conditional_short_route_feasibility_gate(
        venue=venue,
        direction=direction,
        context_stats=stats,
        policy=route_feasibility_policy,
        enabled=enabled,
    )
    allow = bool(gate.get("allow", False))
    suppressed = bool(cohort_gate.get("suppressed", False))

    score_multiplier = 1.0
    if gate.get("allow", False):
        score_multiplier *= float(gate.get("score_multiplier", 1.0) or 1.0)
    else:
        score_multiplier *= 0.0

    if not cohort_gate.get("suppressed", False):
        score_multiplier *= float(cohort_gate.get("score_multiplier", 1.0) or 1.0)
    else:
        score_multiplier *= 0.0

    if cross_market_gate.get("enabled", False):
        score_multiplier *= float(cross_market_gate.get("score_multiplier", 1.0) or 1.0)

    if route_feasibility_gate.get("enabled", False) and route_feasibility_gate.get("applied", False):
        score_multiplier *= float(route_feasibility_gate.get("score_multiplier", 1.0) or 1.0)
        if route_feasibility_gate.get("suppressed", False):
            allow = False
            suppressed = True

    return {
        "enabled": bool(enabled),
        "allow": allow,
        "suppressed": suppressed,
        "cross_market_gate": cross_market_gate,
        "score_multiplier": max(0.0, min(1.0, score_multiplier)),
        "route_feasibility_gate": route_feasibility_gate,
        "venue_direction_gate": gate,
        "long_cohort_gate": cohort_gate,
    }


def _paper_frontier_venue_direction_key(venue: str, direction: str) -> str:
    return f"{str(venue).strip().upper()}_{str(direction).strip().upper()}"


def paper_only_frontier_venue_direction_expectancy_gate(
    *,
    venue: str,
    direction: str,
    context_stats: dict | None = None,
    registry: dict | None = None,
    enabled: bool = True,
    min_closed_trades: int = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY["min_closed_trades"],
    min_confidence_to_allow: float = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY["min_confidence_to_allow"],
    min_multiplier_to_allow: float = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY["min_multiplier_to_allow"],
    block_on_negative_expectancy: bool = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY["block_on_negative_expectancy"],
) -> dict:
    """Paper-only gate for frontier spot venue-direction entries."""

    key = _paper_frontier_venue_direction_key(venue, direction)
    registry = registry or DEFAULT_PAPER_ONLY_FRONTIER_VENUE_DIRECTION_EXPECTANCY_REGISTRY
    entry = copy.deepcopy(registry.get(key, {}))
    stats = context_stats or {}

    if not enabled:
        return {"enabled": False, "allow": False, "key": key, "reason": "disabled"}

    if not entry.get("enabled", False):
        return {"enabled": True, "allow": False, "key": key, "reason": "not_allowlisted"}

    closed_trades = int(stats.get("closed_trade_count") or stats.get("closed_trades") or 0)
    expectancy_bps = float(stats.get("recent_expectancy_bps") or stats.get("expectancy_bps") or 0.0)
    confidence = float(stats.get("confidence") or stats.get("paper_confidence") or 0.0)
    multiplier = float(stats.get("score_multiplier") or 0.0)

    if closed_trades < int(entry.get("min_closed_trades", min_closed_trades)):
        return {"enabled": True, "allow": False, "key": key, "reason": "insufficient_closed_trades"}

    if confidence and confidence < float(min_confidence_to_allow):
        return {"enabled": True, "allow": False, "key": key, "reason": "low_confidence"}

    if multiplier and multiplier < float(min_multiplier_to_allow):
        return {"enabled": True, "allow": False, "key": key, "reason": "low_multiplier"}

    min_expectancy = float(entry.get("min_expectancy_bps", 0.0))
    if block_on_negative_expectancy and expectancy_bps < min_expectancy:
        return {"enabled": True, "allow": False, "key": key, "reason": "negative_expectancy"}

    return {"enabled": True, "allow": True, "key": key, "reason": "allowlisted"}


def paper_only_frontier_long_cohort_gate(
    *,
    closed_trade_count: int,
    recent_expectancy_bps: float,
    recent_win_rate: float,
    low_feasibility_share: float,
    enabled: bool = DEFAULT_PAPER_ONLY_FRONTIER_LONG_COHORT_POLICY["enabled"],
    min_closed_trades: int = DEFAULT_PAPER_ONLY_FRONTIER_LONG_COHORT_POLICY["min_closed_trades"],
    min_recent_expectancy_bps: float = DEFAULT_PAPER_ONLY_FRONTIER_LONG_COHORT_POLICY["min_recent_expectancy_bps"],
    min_recent_win_rate: float = DEFAULT_PAPER_ONLY_FRONTIER_LONG_COHORT_POLICY["min_recent_win_rate"],
    low_feasibility_max_share: float = DEFAULT_PAPER_ONLY_FRONTIER_LONG_COHORT_POLICY["low_feasibility_max_share"],
    decay_floor: float = DEFAULT_PAPER_ONLY_FRONTIER_LONG_COHORT_POLICY["decay_floor"],
    suppress_floor: float = DEFAULT_PAPER_ONLY_FRONTIER_LONG_COHORT_POLICY["suppress_floor"],
) -> dict:
    """Paper-only gate for frontier long cohorts based on recent closed-trade quality."""

    if not enabled:
        return {
            "enabled": False,
            "suppressed": False,
            "score_multiplier": 1.0,
            "reasons": ["disabled"],
        }

    if int(closed_trade_count) < int(min_closed_trades):
        return {
            "enabled": True,
            "suppressed": False,
            "score_multiplier": 1.0,
            "reasons": ["insufficient_closed_trades"],
        }

    expectancy = float(recent_expectancy_bps)
    win_rate = float(recent_win_rate)
    feasibility_share = float(low_feasibility_share)

    score_multiplier = 1.0
    reasons = []

    if expectancy <= float(min_recent_expectancy_bps) and win_rate <= float(min_recent_win_rate):
        score_multiplier *= float(decay_floor)
        reasons.append("negative_expectancy_and_weak_win_rate")

    if feasibility_share >= float(low_feasibility_max_share):
        score_multiplier *= 0.85
        reasons.append("low_feasibility_share")

    suppressed = (
        expectancy <= float(min_recent_expectancy_bps)
        and win_rate <= float(min_recent_win_rate)
        and feasibility_share >= float(low_feasibility_max_share)
        and score_multiplier <= float(suppress_floor)
    )
    if (
        not suppressed
        and expectancy <= float(min_recent_expectancy_bps)
        and win_rate <= float(min_recent_win_rate)
        and feasibility_share >= max(float(low_feasibility_max_share), 0.50)
    ):
        suppressed = True

    if suppressed:
        reasons.append("cohort_suppressed")
        score_multiplier = 0.0

    return {
        "enabled": True,
        "suppressed": suppressed,
        "score_multiplier": score_multiplier,
        "reasons": reasons,
    }


def _clamp_paper_score(value: float, minimum: float, maximum: float) -> float:
    return max(float(minimum), min(float(maximum), float(value)))


def _paper_stat_value(stats: dict, *keys: str) -> float | None:
    for key in keys:
        value = stats.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _paper_venue_direction_context_stats(
    stats_by_context: dict | None,
    *,
    venue: str,
    direction: str,
) -> dict | None:
    if not isinstance(stats_by_context, dict):
        return None

    normalized_venue = str(venue or "").upper()
    normalized_direction = str(direction or "").lower()
    composite_keys = (
        f"{normalized_venue}|{normalized_direction}",
        f"{normalized_venue}:{normalized_direction}",
        f"{normalized_venue}/{normalized_direction}",
    )
    for key in composite_keys:
        value = stats_by_context.get(key)
        if isinstance(value, dict):
            return value

    venue_bucket = stats_by_context.get(normalized_venue) or stats_by_context.get(str(venue or ""))
    if isinstance(venue_bucket, dict):
        for key in (normalized_direction, str(direction or ""), normalized_direction.upper()):
            value = venue_bucket.get(key)
            if isinstance(value, dict):
                return value
    return None


def paper_only_venue_direction_expectancy_gate(
    *,
    venue: str,
    direction: str,
    stats_by_context: dict | None,
    enabled: bool = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY["enabled"],
    min_closed_trades: int = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY["min_closed_trades"],
    min_confidence_to_allow: float = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY[
        "min_confidence_to_allow"
    ],
    min_multiplier_to_allow: float = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY[
        "min_multiplier_to_allow"
    ],
    expectancy_scale_bps: float = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY["expectancy_scale_bps"],
    max_abs_expectancy_contribution: float = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY[
        "max_abs_expectancy_contribution"
    ],
    max_abs_win_rate_contribution: float = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY[
        "max_abs_win_rate_contribution"
    ],
    max_abs_payoff_contribution: float = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY[
        "max_abs_payoff_contribution"
    ],
    sample_size_pivot: int = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY["sample_size_pivot"],
    multiplier_floor: float = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY["multiplier_floor"],
    multiplier_ceiling: float = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY["multiplier_ceiling"],
    block_on_negative_expectancy: bool = DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY[
        "block_on_negative_expectancy"
    ],
) -> dict:
    """Paper-only venue-direction gate using realized expectancy with shrinkage to neutral."""

    context_key = f"{str(venue or '').upper()}|{str(direction or '').lower()}"
    if not enabled:
        return {
            "enabled": False,
            "blocked": False,
            "score_multiplier": 1.0,
            "confidence": 0.0,
            "closed_trade_count": 0,
            "context_key": context_key,
            "reasons": ["disabled"],
        }

    context_stats = _paper_venue_direction_context_stats(stats_by_context, venue=venue, direction=direction)
    if not isinstance(context_stats, dict):
        return {
            "enabled": True,
            "blocked": False,
            "score_multiplier": 1.0,
            "confidence": 0.0,
            "closed_trade_count": 0,
            "context_key": context_key,
            "reasons": ["missing_context_stats"],
        }

    closed_trade_count = int(round(_paper_stat_value(context_stats, "closed_trade_count", "closed_trades", "sample_size") or 0.0))
    if closed_trade_count < int(min_closed_trades):
        return {
            "enabled": True,
            "blocked": False,
            "score_multiplier": 1.0,
            "confidence": 0.0,
            "closed_trade_count": closed_trade_count,
            "context_key": context_key,
            "reasons": ["insufficient_closed_trades"],
        }

    win_rate = _paper_stat_value(context_stats, "recent_win_rate", "win_rate")
    wins = _paper_stat_value(context_stats, "wins", "win_count")
    if win_rate is None and closed_trade_count > 0 and wins is not None:
        win_rate = wins / max(closed_trade_count, 1)
    win_rate = _clamp_paper_score(win_rate if win_rate is not None else 0.5, 0.0, 1.0)

    avg_win_bps = _paper_stat_value(context_stats, "avg_win_bps", "average_win_bps")
    avg_loss_bps = _paper_stat_value(context_stats, "avg_loss_bps", "average_loss_bps")
    expectancy_bps = _paper_stat_value(context_stats, "recent_expectancy_bps", "expectancy_bps")
    if expectancy_bps is None and avg_win_bps is not None and avg_loss_bps is not None:
        expectancy_bps = (win_rate * float(avg_win_bps)) - ((1.0 - win_rate) * abs(float(avg_loss_bps)))
    expectancy_bps = float(expectancy_bps if expectancy_bps is not None else 0.0)

    payoff_ratio = _paper_stat_value(context_stats, "payoff_ratio")
    if payoff_ratio is None and avg_win_bps is not None and avg_loss_bps not in (None, 0.0):
        payoff_ratio = abs(float(avg_win_bps) / float(avg_loss_bps))
    payoff_ratio = max(0.0, float(payoff_ratio if payoff_ratio is not None else 1.0))

    confidence = closed_trade_count / float(max(closed_trade_count + int(sample_size_pivot), 1))
    expectancy_component = _clamp_paper_score(expectancy_bps / max(float(expectancy_scale_bps), 1.0), -1.0, 1.0)
    expectancy_component *= float(max_abs_expectancy_contribution)
    win_rate_component = _clamp_paper_score((win_rate - 0.5) / 0.20, -1.0, 1.0)
    win_rate_component *= float(max_abs_win_rate_contribution)
    payoff_component = _clamp_paper_score((payoff_ratio - 1.0) / 0.75, -1.0, 1.0)
    payoff_component *= float(max_abs_payoff_contribution)
    multiplier = 1.0 + (confidence * (expectancy_component + win_rate_component + payoff_component))
    multiplier = _clamp_paper_score(multiplier, multiplier_floor, multiplier_ceiling)

    reasons = []
    if confidence < float(min_confidence_to_allow):
        reasons.append("low_sample_confidence")
    if multiplier <= float(min_multiplier_to_allow):
        reasons.append("multiplier_not_above_neutral")
    if bool(block_on_negative_expectancy) and expectancy_bps < 0.0 and multiplier <= 1.0:
        reasons.append("negative_expectancy")

    return {
        "enabled": True,
        "blocked": bool(reasons),
        "score_multiplier": multiplier,
        "confidence": confidence,
        "closed_trade_count": closed_trade_count,
        "context_key": context_key,
        "expectancy_bps": expectancy_bps,
        "win_rate": win_rate,
        "payoff_ratio": payoff_ratio,
        "reasons": list(dict.fromkeys(reasons)),
    }


def paper_only_confidence_score(
    *,
    trend_score: float,
    momentum_score: float,
    liquidity_score: float,
    min_confidence: float = DEFAULT_PAPER_ONLY_CONFIDENCE_POLICY["min_confidence"],
    trend_weight: float = DEFAULT_PAPER_ONLY_CONFIDENCE_POLICY["trend_weight"],
    momentum_weight: float = DEFAULT_PAPER_ONLY_CONFIDENCE_POLICY["momentum_weight"],
    liquidity_weight: float = DEFAULT_PAPER_ONLY_CONFIDENCE_POLICY["liquidity_weight"],
) -> dict:
    """Compute a normalized paper-only confidence score and threshold gate."""

    weights = [float(trend_weight), float(momentum_weight), float(liquidity_weight)]
    raw_score = (
        float(trend_score) * weights[0]
        + float(momentum_score) * weights[1]
        + float(liquidity_score) * weights[2]
    )
    weight_total = sum(weights) or 1.0
    confidence = max(0.0, min(1.0, raw_score / weight_total))
    blocked = confidence < float(min_confidence)
    return {
        "confidence": confidence,
        "min_confidence": float(min_confidence),
        "blocked": blocked,
        "alert_allowed": not blocked,
    }


def paper_only_long_entry_confirmation(
    *,
    price: float,
    ema_20: float,
    rsi_1h: float,
    volume: float,
    avg_volume_20: float,
    min_rsi: float = 55.0,
    min_volume_ratio: float = 1.2,
) -> dict:
    """Paper-only long entry gate requiring momentum and participation confirmation."""

    price = float(price)
    ema_20 = float(ema_20)
    rsi_1h = float(rsi_1h)
    volume = float(volume)
    avg_volume_20 = float(avg_volume_20)
    min_rsi = float(min_rsi)
    min_volume_ratio = float(min_volume_ratio)

    volume_ratio = volume / avg_volume_20 if avg_volume_20 > 0 else 0.0
    price_above_ema = price > ema_20
    rsi_ok = rsi_1h > min_rsi
    volume_ok = volume_ratio >= min_volume_ratio
    allowed = price_above_ema and rsi_ok and volume_ok

    reasons = []
    if not price_above_ema:
        reasons.append("price_below_ema20")
    if not rsi_ok:
        reasons.append("rsi_below_min")
    if not volume_ok:
        reasons.append("volume_below_min_ratio")

    return {
        "allowed": allowed,
        "price_above_ema20": price_above_ema,
        "rsi_ok": rsi_ok,
        "volume_ok": volume_ok,
        "volume_ratio": volume_ratio,
        "min_rsi": min_rsi,
        "min_volume_ratio": min_volume_ratio,
        "reasons": reasons,
    }


def paper_only_executable_quality_check(
    *,
    expected_edge_bps: float,
    quoted_spread_bps: float,
    top_of_book_depth: float | None = None,
    paper_order_size: float | None = None,
    recent_trade_volume: float | None = None,
    baseline_trade_volume: float | None = None,
    quote_age_ms: float | None = None,
    fee_buffer_bps: float = DEFAULT_PAPER_ONLY_EXECUTABLE_QUALITY_POLICY["fee_buffer_bps"],
    slippage_buffer_bps: float = DEFAULT_PAPER_ONLY_EXECUTABLE_QUALITY_POLICY["slippage_buffer_bps"],
    min_net_edge_bps: float = DEFAULT_PAPER_ONLY_EXECUTABLE_QUALITY_POLICY["min_net_edge_bps"],
    max_quote_age_ms: float = DEFAULT_PAPER_ONLY_EXECUTABLE_QUALITY_POLICY["max_quote_age_ms"],
    max_spread_as_pct_of_edge: float = DEFAULT_PAPER_ONLY_EXECUTABLE_QUALITY_POLICY["max_spread_as_pct_of_edge"],
    min_depth_multiple_of_paper_size: float = DEFAULT_PAPER_ONLY_EXECUTABLE_QUALITY_POLICY[
        "min_depth_multiple_of_paper_size"
    ],
    venue_direction_gate: dict | None = None,
    min_recent_volume_multiple_vs_baseline: float = 1.25,
) -> dict:
    """Paper-only executable quality filter for cross-market observations."""

    reasons = []
    edge_after_costs = float(expected_edge_bps) - float(fee_buffer_bps) - float(slippage_buffer_bps)
    spread_limit_bps = max(0.0, float(expected_edge_bps) * float(max_spread_as_pct_of_edge))

    if edge_after_costs < float(min_net_edge_bps):
        reasons.append("net_edge_below_minimum")
    if float(quoted_spread_bps) > spread_limit_bps:
        reasons.append("spread_exceeds_edge_fraction")
    if quote_age_ms is not None and float(quote_age_ms) > float(max_quote_age_ms):
        reasons.append("quote_stale")
    if top_of_book_depth is not None and paper_order_size is not None:
        required_depth = float(paper_order_size) * float(min_depth_multiple_of_paper_size)
        if float(top_of_book_depth) < required_depth:
            reasons.append("insufficient_depth")
    if recent_trade_volume is not None and baseline_trade_volume is not None:
        required_volume = float(baseline_trade_volume) * float(min_recent_volume_multiple_vs_baseline)
        if float(recent_trade_volume) < required_volume:
            reasons.append("insufficient_recent_volume")

    confidence_inputs = {
        "trend_score": 1.0 if float(expected_edge_bps) > 0.0 else 0.0,
        "momentum_score": min(1.0, max(0.0, float(expected_edge_bps) / max(float(min_net_edge_bps), 1.0))),
        "liquidity_score": 1.0
        if top_of_book_depth is None or paper_order_size is None
        else min(1.0, max(0.0, float(top_of_book_depth) / max(float(paper_order_size), 1.0) / 3.0)),
    }
    confidence = paper_only_confidence_score(
        **confidence_inputs,
        min_confidence=0.70,
    )
    applied_venue_direction_gate = (
        venue_direction_gate
        if isinstance(venue_direction_gate, dict)
        else {"enabled": False, "blocked": False, "score_multiplier": 1.0, "reasons": []}
    )
    if bool(applied_venue_direction_gate.get("enabled")) and float(applied_venue_direction_gate.get("score_multiplier", 1.0)) < 1.0:
        reasons.append("venue_direction_expectancy_below_neutral")
    if bool(applied_venue_direction_gate.get("blocked")):
        reasons.append("venue_direction_expectancy_gate")

    passed = not reasons
    return {
        "passed": passed,
        "venue_direction_gate": applied_venue_direction_gate,
        "reasons": reasons,
        "edge_after_costs_bps": edge_after_costs,
        "spread_limit_bps": spread_limit_bps,
        "score_multiplier": 1.0 if passed else 0.0,
        "confidence": confidence["confidence"],
        "confidence_gate": confidence,
        "alert_blocked": not passed or confidence["blocked"],
    }


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
RUNS_DIR = ROOT / "runs"
REPORT_JSON = RUNS_DIR / "frontier_crypto_venues_latest.json"
REPORT_MD = RUNS_DIR / "frontier_crypto_venues_report.md"
CUSTOM_REGISTRY_PATH = CONFIG_DIR / "frontier_crypto_venues.json"
EXAMPLE_REGISTRY_PATH = CONFIG_DIR / "frontier_crypto_venues.example.json"

USD_LIKE_QUOTES = {"USD", "USDT", "USDC"}
REGIONAL_FIAT_QUOTES = {
    "ZAR",
    "NGN",
    "GHS",
    "KES",
    "TZS",
    "UGX",
    "IDR",
    "THB",
    "SGD",
    "MYR",
    "PHP",
    "MXN",
    "BRL",
    "CLP",
    "COP",
    "PEN",
    "ARS",
}
LATAM_FIAT_QUOTES = {"MXN", "BRL", "CLP", "COP", "PEN", "ARS"}
PAPER_ONLY_REVIEW_FIAT_QUOTES = LATAM_FIAT_QUOTES | {"TZS", "UGX"}
QUOTE_ASSETS = USD_LIKE_QUOTES | REGIONAL_FIAT_QUOTES
STABLE_OR_FIAT_BASES = {
    "USD",
    "USDT",
    "USDC",
    "DAI",
    "TUSD",
    "FDUSD",
    "BUSD",
    "USDP",
    "PYUSD",
    "EUR",
    "GBP",
    *REGIONAL_FIAT_QUOTES,
}

DEFAULT_ROUTE_FEASIBILITY_POLICY = {
    "enabled": True,
    "default_status": "uncertain",
    "default_reason": "missing_route_rule",
    "uncertain_action": "downweight",
    "uncertain_confidence_multiplier": 0.6,
    "infeasible_action": "suppress",
    "log_all_reviews": True,
    "rules": [
        {
            "rule_id": "watch_only_yellow_card",
            "venue": "YELLOW_CARD",
            "instrument_type": "rfq_rail",
            "static_status": "watch_only",
            "status": "infeasible",
            "reason_code": "watch_only_route",
        },
        {
            "rule_id": "watch_only_bitnob",
            "venue": "BITNOB",
            "instrument_type": "rfq_rail",
            "static_status": "watch_only",
            "status": "infeasible",
            "reason_code": "watch_only_route",
        },
        {
            "rule_id": "conditional_spot_short_requires_borrow",
            "venue": "*",
            "instrument_type": "spot",
            "directionality": ["short", "short_spot"],
            "strategy_family": ["conditional", "basis", "cash_carry", "pair_trade"],
            "status": "infeasible",
            "reason_code": "borrow_or_margin_unverified",
        },
        {
            "rule_id": "multi_leg_basis_needs_fee_and_api_review",
            "venue": "*",
            "instrument_type": ["spot", "perp"],
            "directionality": ["hedged", "market_neutral"],
            "strategy_family": ["basis", "cash_carry", "pair_trade"],
            "status": "uncertain",
            "reason_code": "multi_leg_fee_tier_or_api_support_unverified",
        },
        {
            "rule_id": "latam_public_spot_review_only",
            "venue": ["BITSO", "MERCADO_BITCOIN", "BUDA"],
            "instrument_type": "spot",
            "directionality": "long",
            "strategy_family": ["standard", "momentum", "breakout"],
            "status": "uncertain",
            "reason_code": "regional_fiat_manual_review",
        },
        {
            "rule_id": "public_spot_long_supported",
            "venue": [
                "KUCOIN",
                "GATE",
                "MEXC",
                "BITGET",
                "BINANCE_US",
                "COINBASE",
                "KRAKEN",
                "OKX_SPOT",
                "BYBIT_SPOT",
                "LUNO",
                "VALR",
                "QUIDAX",
                "INDODAX",
                "BITKUB",
            ],
            "instrument_type": "spot",
            "directionality": "long",
            "strategy_family": ["standard", "momentum", "breakout"],
            "status": "feasible",
            "reason_code": "public_spot_long_supported",
        },
    ],
}

DEFAULT_PAPER_TRADE_POLICY = {
    "market_key": "paper.signal_confirmation.v1",
    "mode": "paper_only",
    "execution": "simulated",
    "summary": "Convert the recommendation into a paper-only gated setup that stays flat unless trend, liquidity, and related-market direction confirm.",
    "min_confirmation_score": 0.70,
    "divergence_block": "enabled",
    "high_volatility_posture": "monitor_first",
    "single_asset_override": "disabled",
    "cross_market_confirmation_enabled": True,
    "cross_market_confirmation_source": "related_market_direction",
    "cross_market_confirmation_window": "15m",
    "cross_market_confirmation_alignment": "same_direction",
    "cross_market_confirmation_on_miss": "monitor",
    "state_if_unconfirmed": "flat",
    "state_if_cross_market_unconfirmed": "monitor",
    "state_if_divergent": "monitor",
    "cross_market_regime_filter_enabled": True,
    "cross_market_regime_proxy": "risk_proxy",
    "cross_market_regime_ma_fast": 20,
    "cross_market_regime_ma_slow": 50,
    "entry_rule": "Enter paper long only if price closes above the prior session high, current volume is greater than the 20-session average volume, and the selected related market confirms direction within the observation window; otherwise remain flat or monitor.",
    "exit_rule": "Exit the paper position on a close below the prior session low or after 3 trading sessions, whichever comes first.",
    "risk_limit": "Cap paper risk at 0.50 percent of notional per simulated trade and do not pyramid.",
    "review_rule": "Keep manual review enabled for any suppressed high-volatility event.",
    "fractional_risk": 0.005,
    "sizing": "fixed_fractional",
    "shadow_evaluation": {
        "enabled": False,
        "scope": "paper_only_shadow",
        "target_market_keys": ["YAHOO_PROXY|global_proxy_momentum"],
        "control_mode": "paper_baseline",
        "candidate_mode": "freshness_and_session_gate",
        "freshness_gate_seconds": 90,
        "freshness_action": "suppress_new_entries",
        "session_boundary_block_minutes": 15,
        "session_boundary_action": "suppress_new_entries",
        "session_boundary_reference": "local_market_session",
        "log_fields": [
            "proxy_age_seconds",
            "session_state",
            "signal_timestamp_delta_seconds",
            "suppressed_reason",
            "shadow_outcome_tag",
        ],
    },
    "venue_direction_expectancy_gate": DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY,
    "route_feasibility": DEFAULT_ROUTE_FEASIBILITY_POLICY,
    "pyramiding": "disabled",
}


DEFAULT_REGISTRY = {
    "filters": {
        "quote_assets": sorted(QUOTE_ASSETS),
        "exclude_base_assets": sorted(STABLE_OR_FIAT_BASES),
        "paper_trade_policy_enabled": True,
        "top_volume_per_venue": 80,
        "frontier_symbols_per_venue": 40,
        "frontier_max_listing_count": 3,
        "min_frontier_quote_volume_usd": 25_000,
        "min_cross_venue_count": 2,
        "regional_fx_normalization_enabled": True,
        "regional_fx_require_fresh_reference": True,
        "regional_fx_max_age_seconds": 21_600,
        "regional_fx_stale_confidence_haircut": 0.35,
    },
    "paper_trade_policy": DEFAULT_PAPER_TRADE_POLICY,
    "route_feasibility": DEFAULT_ROUTE_FEASIBILITY_POLICY,
    "venues": [
        {
            "venue": "KUCOIN",
            "enabled": True,
            "market_type": "spot",
            "route_id": "kucoin_spot_public",
            "url": "https://api.kucoin.com/api/v1/market/allTickers",
            "parser": "kucoin_all_tickers",
            "depth": {
                "url_template": "https://api.kucoin.com/api/v1/market/orderbook/level2_20?symbol={symbol}",
                "parser": "kucoin_level2",
                "max_levels": 20,
                "timestamp_capability": "exchange",
            },
        },
        {
            "venue": "GATE",
            "enabled": True,
            "market_type": "spot",
            "route_id": "gate_spot_public",
            "url": "https://api.gateio.ws/api/v4/spot/tickers",
            "parser": "gate_spot_tickers",
            "depth": {
                "url_template": "https://api.gateio.ws/api/v4/spot/order_book?currency_pair={symbol}&limit={limit}&with_id=true",
                "parser": "gate_order_book",
                "max_levels": 50,
                "timestamp_capability": "exchange",
            },
        },
        {
            "venue": "MEXC",
            "enabled": True,
            "market_type": "spot",
            "route_id": "mexc_spot_public",
            "url": "https://api.mexc.com/api/v3/ticker/24hr",
            "parser": "mexc_24hr",
            "depth": {
                "url_template": "https://api.mexc.com/api/v3/depth?symbol={symbol}&limit={limit}",
                "parser": "mexc_depth",
                "max_levels": 50,
                "timestamp_capability": "response_received",
            },
        },
        {
            "venue": "BITGET",
            "enabled": True,
            "market_type": "spot",
            "route_id": "bitget_spot_public",
            "url": "https://api.bitget.com/api/v2/spot/market/tickers",
            "parser": "bitget_spot_tickers",
            "depth": {
                "url_template": "https://api.bitget.com/api/v2/spot/market/orderbook?symbol={symbol}&type=step0&limit={limit}",
                "parser": "bitget_orderbook",
                "max_levels": 50,
                "timestamp_capability": "exchange",
            },
        },
        {
            "venue": "BINANCE_US",
            "enabled": True,
            "market_type": "spot",
            "route_id": "binance_us_spot_public",
            "url": "https://api.binance.us/api/v3/ticker/24hr",
            "parser": "binance_24hr",
            "depth": {
                "url_template": "https://api.binance.us/api/v3/depth?symbol={symbol}&limit={limit}",
                "parser": "binance_depth",
                "max_levels": 50,
                "timestamp_capability": "response_received",
            },
        },
        {
            "venue": "COINBASE",
            "enabled": True,
            "market_type": "spot",
            "route_id": "coinbase_spot_public",
            "url": "https://api.exchange.coinbase.com/products",
            "parser": "coinbase_products",
            "max_product_tickers": 50,
            "depth": {
                "url_template": "https://api.exchange.coinbase.com/products/{symbol}/book?level=2",
                "parser": "coinbase_book",
                "max_levels": 50,
                "timestamp_capability": "response_received",
            },
        },
        {
            "venue": "KRAKEN",
            "enabled": True,
            "market_type": "spot",
            "route_id": "kraken_spot_public",
            "url": "https://api.kraken.com/0/public/Ticker",
            "asset_pairs_url": "https://api.kraken.com/0/public/AssetPairs",
            "parser": "kraken_all_tickers",
            "depth": {
                "url_template": "https://api.kraken.com/0/public/Depth?pair={symbol}&count={limit}",
                "parser": "kraken_depth",
                "max_levels": 50,
                "timestamp_capability": "exchange",
            },
        },
        {
            "venue": "OKX",
            "enabled": True,
            "market_type": "perp",
            "symbol": "BTC-USDT-SWAP",
            "route_id": "okx_perp_public",
            "url": "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP",
            "funding_url": "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP",
            "parser": "okx_swap_ticker",
            "depth": {
                "url_template": "https://www.okx.com/api/v5/market/books?instId={symbol}&sz={limit}",
                "parser": "okx_books",
                "max_levels": 50,
                "timestamp_capability": "exchange",
            },
        },
        {
            "venue": "OKX_SPOT",
            "enabled": True,
            "market_type": "spot",
            "route_id": "okx_spot_public",
            "url": "https://www.okx.com/api/v5/market/tickers?instType=SPOT",
            "parser": "okx_spot_tickers",
            "depth": {
                "url_template": "https://www.okx.com/api/v5/market/books?instId={symbol}&sz={limit}",
                "parser": "okx_books",
                "max_levels": 50,
                "timestamp_capability": "exchange",
            },
        },
        {
            "venue": "BYBIT",
            "enabled": True,
            "market_type": "perp",
            "symbol": "BTCUSDT",
            "route_id": "bybit_perp_public",
            "url": "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
            "parser": "bybit_linear_ticker",
        },
        {
            "venue": "BYBIT_SPOT",
            "enabled": True,
            "market_type": "spot",
            "route_id": "bybit_spot_public",
            "url": "https://api.bybit.com/v5/market/tickers?category=spot",
            "parser": "bybit_spot_tickers",
            "depth": {
                "url_template": "https://api.bybit.com/v5/market/orderbook?category=spot&symbol={symbol}&limit={limit}",
                "parser": "bybit_orderbook",
                "max_levels": 50,
                "timestamp_capability": "exchange",
            },
        },
        {
            "venue": "LUNO",
            "enabled": True,
            "market_type": "spot",
            "route_id": "luno_spot_public",
            "region": "Africa",
            "url": "https://api.luno.com/api/1/tickers",
            "parser": "luno_tickers",
            "depth": {
                "url_template": "https://api.luno.com/api/1/orderbook_top?pair={symbol}",
                "parser": "luno_orderbook",
                "max_levels": 50,
                "timestamp_capability": "exchange",
            },
        },
        {
            "venue": "VALR",
            "enabled": True,
            "market_type": "spot",
            "route_id": "valr_spot_public",
            "region": "Africa",
            "url": "https://api.valr.com/v1/public/marketsummary",
            "parser": "valr_market_summary",
            "depth": {
                "url_template": "https://api.valr.com/v1/public/{symbol}/orderbook",
                "parser": "valr_orderbook",
                "max_levels": 50,
                "timestamp_capability": "response_received",
            },
        },
        {
            "venue": "QUIDAX",
            "enabled": True,
            "market_type": "spot",
            "route_id": "quidax_spot_public",
            "region": "Africa",
            "url": "https://app.quidax.io/api/v1/markets/tickers",
            "parser": "quidax_tickers",
            "depth": {
                "url_template": "https://app.quidax.io/api/v1/markets/{symbol}/depth",
                "parser": "quidax_depth",
                "max_levels": 50,
                "timestamp_capability": "response_received",
            },
        },
        {
            "venue": "INDODAX",
            "enabled": True,
            "market_type": "spot",
            "route_id": "indodax_spot_public",
            "region": "Southeast Asia",
            "url": "https://indodax.com/api/ticker_all",
            "parser": "indodax_ticker_all",
            "depth": {
                "url_template": "https://indodax.com/api/depth/{symbol}",
                "parser": "indodax_depth",
                "max_levels": 50,
                "timestamp_capability": "response_received",
            },
        },
        {
            "venue": "BITKUB",
            "enabled": True,
            "market_type": "spot",
            "route_id": "bitkub_spot_public",
            "region": "Southeast Asia",
            "url": "https://api.bitkub.com/api/v3/market/ticker",
            "parser": "bitkub_ticker",
            "depth": {
                "url_template": "https://api.bitkub.com/api/v3/market/depth?sym={symbol}&lmt={limit}",
                "parser": "bitkub_depth",
                "max_levels": 50,
                "timestamp_capability": "response_received",
            },
        },
        {
            "venue": "YELLOW_CARD",
            "enabled": True,
            "market_type": "rfq_rail",
            "route_id": "yellow_card_watch_only",
            "region": "Africa",
            "symbol": "YELLOW_CARD_RAIL",
            "url": "https://docs.yellowcard.engineering/docs/getting-started",
            "static_status": "watch_only",
            "parser": "watch_only_rail",
            "notes": "Watch-only stablecoin/fiat rail research. No public order-book endpoint is configured.",
        },
        {
            "venue": "BITNOB",
            "enabled": True,
            "market_type": "rfq_rail",
            "route_id": "bitnob_watch_only",
            "region": "Africa",
            "symbol": "BITNOB_RAIL",
            "url": "https://bitnob.dev/",
            "static_status": "watch_only",
            "parser": "watch_only_rail",
            "notes": "Watch-only stablecoin/fiat rail research. No public order-book endpoint is configured.",
        },
        {
            "venue": "BITSO",
            "enabled": True,
            "market_type": "spot",
            "route_id": "bitso_spot_public",
            "region": "LATAM",
            "url": "https://api.bitso.com/v3/available_books/",
            "parser": "bitso_available_books",
            "max_product_tickers": 60,
            "depth": {
                "url_template": "https://api.bitso.com/v3/order_book/?book={symbol}",
                "parser": "bitso_order_book",
                "max_levels": 50,
                "timestamp_capability": "exchange",
            },
        },
        {
            "venue": "MERCADO_BITCOIN",
            "enabled": True,
            "market_type": "spot",
            "route_id": "mercado_bitcoin_spot_public",
            "region": "LATAM",
            "url": "https://api.mercadobitcoin.net/api/v4/symbols",
            "parser": "mercado_bitcoin_symbols",
            "max_product_tickers": 50,
            "depth": {
                "url_template": "https://api.mercadobitcoin.net/api/v4/{symbol}/orderbook",
                "parser": "mercado_bitcoin_orderbook",
                "max_levels": 50,
                "timestamp_capability": "response_received",
            },
        },
        {
            "venue": "BUDA",
            "enabled": True,
            "market_type": "spot",
            "route_id": "buda_spot_public",
            "region": "LATAM",
            "url": "https://www.buda.com/api/v2/markets",
            "parser": "buda_markets",
            "max_product_tickers": 60,
            "depth": {
                "url_template": "https://www.buda.com/api/v2/markets/{symbol}/order_book",
                "parser": "buda_order_book",
                "max_levels": 50,
                "timestamp_capability": "response_received",
            },
        },
    ],
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def as_float(value: object, default: float | None = 0.0) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _unix_ms_to_iso(value: object) -> str | None:
    try:
        if value in (None, ""):
            return None
        return dt.datetime.fromtimestamp(int(value) / 1000.0, tz=dt.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def bps(new: float, old: float) -> float:
    if old <= 0:
        return 0.0
    return (new / old - 1.0) * 10_000.0


def liquidity_score(quote_volume: float | None) -> float:
    quote_volume = float(quote_volume or 0.0)
    if quote_volume <= 0:
        return 0.35
    return max(0.0, min(1.0, (math.log10(quote_volume) - 5.0) / 4.0))


def spread_bps(bid: float | None, ask: float | None, last: float | None) -> float:
    bid = float(bid or 0.0)
    ask = float(ask or 0.0)
    last = float(last or 0.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
    if ask > bid > 0 and mid > 0:
        return max(0.0, (ask - bid) / mid * 10_000.0)
    return 6.0


def _status_from_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, urllib.error.HTTPError):
        status = f"HTTP {exc.code}: {exc.reason}"
        if exc.code in {401, 403, 451}:
            return "blocked", status
        return "unavailable", status
    return "unavailable", str(exc)[:300]


def fetch_json(url: str, timeout: int = 8) -> dict:
    started = time.perf_counter()
    received_at = _utc_now()
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 inefficiency-radar/0.1",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            received_at = _utc_now()
            body = response.read().decode("utf-8")
            return {
                "ok": True,
                "data_status": "reachable",
                "http_status": str(response.status),
                "received_at": received_at,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "payload": json.loads(body),
            }
    except Exception as exc:  # noqa: BLE001
        data_status, http_status = _status_from_error(exc)
        return {
            "ok": False,
            "data_status": data_status,
            "received_at": received_at,
            "http_status": http_status,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
            "payload": None,
        }


def _deep_merge_dict(base: dict, overrides: dict) -> dict:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge_dict(base[key], value)
            continue
        base[key] = copy.deepcopy(value)
    return base


def _route_feasibility_policy_from_loaded_registry(loaded: dict | None = None) -> dict:
    policy = copy.deepcopy(DEFAULT_ROUTE_FEASIBILITY_POLICY)
    if not isinstance(loaded, dict):
        return policy
    top_level = loaded.get("route_feasibility")
    if isinstance(top_level, dict):
        _deep_merge_dict(policy, top_level)
    loaded_policy = loaded.get("paper_trade_policy")
    if isinstance(loaded_policy, dict):
        nested = loaded_policy.get("route_feasibility")
        if isinstance(nested, dict):
            _deep_merge_dict(policy, nested)
    rules = policy.get("rules")
    policy["rules"] = [copy.deepcopy(rule) for rule in rules if isinstance(rule, dict)] if isinstance(rules, list) else []
    return policy


def _route_feasibility_value_matches(expected: object, observed: object) -> bool:
    if expected in (None, "", "*"):
        return True
    if isinstance(expected, (list, tuple, set, frozenset)):
        return any(_route_feasibility_value_matches(item, observed) for item in expected)
    observed_values = observed if isinstance(observed, (list, tuple, set, frozenset)) else [observed]
    normalized_observed = {str(value).strip().upper() for value in observed_values if value not in (None, "")}
    normalized_expected = str(expected).strip().upper()
    if normalized_expected in {"", "*", "ANY"}:
        return True
    return normalized_expected in normalized_observed


def paper_route_feasibility_review(
    observation: dict,
    loaded_registry: dict | None = None,
    strategy_family: str | None = None,
    directionality: str | None = None,
) -> dict:
    policy = _route_feasibility_policy_from_loaded_registry(loaded_registry)
    if not policy.get("enabled", True):
        return {
            "enabled": False,
            "status": "feasible",
            "reason_code": "route_feasibility_disabled",
            "action": "allow",
            "confidence_multiplier": 1.0,
            "matched_rule_id": None,
            "instrument_type": str(observation.get("instrument_type") or observation.get("market_type") or "spot"),
            "directionality": str(directionality or observation.get("directionality") or observation.get("direction") or "long"),
            "strategy_family": str(strategy_family or observation.get("strategy_family") or observation.get("strategy") or observation.get("setup_type") or "standard"),
            "observation_key": f"{observation.get('venue', 'UNKNOWN')}|{observation.get('symbol', 'UNKNOWN')}",
        }
    instrument_type = observation.get("instrument_type") or observation.get("market_type") or observation.get("instrument") or "spot"
    res

def _paper_trade_policy_from_loaded_registry(loaded: dict | None = None) -> dict:
    policy = copy.deepcopy(DEFAULT_PAPER_TRADE_POLICY)
    if not isinstance(loaded, dict):
        return policy
    loaded_policy = loaded.get("paper_trade_policy")
    if isinstance(loaded_policy, dict):
        _deep_merge_dict(policy, loaded_policy)
        shadow = policy.get("shadow_evaluation")
        if isinstance(shadow, dict) and isinstance(shadow.get("target_market_keys"), str):
            shadow["target_market_keys"] = [shadow["target_market_keys"]]
    return policy


def load_venue_registry() -> dict:
    path = CUSTOM_REGISTRY_PATH if CUSTOM_REGISTRY_PATH.exists() else EXAMPLE_REGISTRY_PATH
    if not path.exists():
        registry = copy.deepcopy(DEFAULT_REGISTRY)
    else:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        registry = {
            "filters": {**DEFAULT_REGISTRY.get("filters", {}), **loaded.get("filters", {})},
            "paper_trade_policy": _paper_trade_policy_from_loaded_registry(loaded),
            "venues": loaded.get("venues", DEFAULT_REGISTRY["venues"]),
        }
    registry.setdefault("paper_trade_policy", copy.deepcopy(DEFAULT_PAPER_TRADE_POLICY))
    policy_enabled = bool(registry.get("filters", {}).get("paper_trade_policy_enabled", True))
    if not policy_enabled:
        return registry
    venues = []
    for venue in registry.get("venues", []):
        if not isinstance(venue, dict):
            venues.append(venue)
            continue
        venue_copy = copy.deepcopy(venue)
        venue_copy.setdefault(
            "paper_trade_policy",
            copy.deepcopy(registry["paper_trade_policy"]),
        )
        venues.append(venue_copy)
    registry["venues"] = venues
    return registry


def _split_symbol(symbol: str, quote_assets: set[str]) -> tuple[str | None, str | None]:
    clean = symbol.upper().replace("_SPBL", "")
    for separator in ("-", "_", "/"):
        if separator in clean:
            parts = [item for item in clean.split(separator) if item]
            if len(parts) >= 2:
                base, quote = parts[0], parts[1]
                if quote in quote_assets:
                    return _canonical_asset(base), quote
    for quote in sorted(quote_assets, key=len, reverse=True):
        if clean.endswith(quote) and len(clean) > len(quote):
            return _canonical_asset(clean[: -len(quote)]), quote
    return None, None


def _canonical_asset(value: str | None) -> str | None:
    if not value:
        return None
    upper = str(value).upper()
    aliases = {"XBT": "BTC", "BCC": "BCH"}
    return aliases.get(upper, upper)


def _is_latam_fiat_quote(quote: str | None) -> bool:
    return str(quote or "").upper() in LATAM_FIAT_QUOTES


def _is_paper_only_review_fiat_quote(quote: str | None) -> bool:
    return str(quote or "").upper() in PAPER_ONLY_REVIEW_FIAT_QUOTES


VALR_PRIORITY_MARKETS = ("BTCZAR", "ETHZAR", "USDTZAR")


def _valr_payload_rows(payload: object) -> list[dict]:
    body = payload
    if isinstance(body, dict) and "payload" in body and any(
        key in body for key in ("ok", "data_status", "http_status", "latency_ms")
    ):
        body = body.get("payload")
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("marketsummary", "marketSummary", "market_summaries", "marketSummaries", "data"):
        rows = body.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _valr_market_quote_volume(row: dict) -> float:
    quote_volume = as_float(
        row.get("quoteVolume")
        or row.get("quoteCurrencyVolume")
        or row.get("volumeQuote")
        or row.get("quote_volume"),
        None,
    )
    if quote_volume is not None and quote_volume > 0:
        return float(quote_volume)
    last_price = as_float(
        row.get("lastTradedPrice") or row.get("lastPrice") or row.get("last") or row.get("price"),
        None,
    )
    base_volume = as_float(
        row.get("baseVolume")
        or row.get("volume")
        or row.get("baseCurrencyVolume")
        or row.get("volume24Hour"),
        None,
    )
    if last_price is not None and last_price > 0 and base_volume is not None and base_volume > 0:
        return float(last_price * base_volume)
    return 0.0


def _valr_timestamp_ms(row: dict) -> str | None:
    for key in ("timestamp", "createdTimestamp", "lastTradedTimestamp"):
        value = as_float(row.get(key), None)
        if value is not None and value > 0:
            return str(int(value))
    for key in ("created", "createdAt", "updatedAt", "lastTradedAt", "tradedAt"):
        value = row.get(key)
        if value in (None, ""):
            continue
        numeric = as_float(value, None)
        if numeric is not None and numeric > 0:
            return str(int(numeric))
        if isinstance(value, str):
            try:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return str(int(parsed.timestamp() * 1000.0))
    return None


def _valr_as_luno_payload(payload: object) -> dict:
    rows = _valr_payload_rows(payload)
    selected: dict[str, dict] = {}
    usd_like_choice: tuple[str, dict] | None = None
    usd_like_metric = -1.0
    for row in rows:
        raw_symbol = str(
            row.get("currencyPair") or row.get("symbol") or row.get("pair") or row.get("market") or ""
        ).upper()
        symbol = raw_symbol.replace("-", "").replace("_", "").replace("/", "")
        if not symbol:
            continue
        base_asset, quote_asset = _split_symbol(symbol, QUOTE_ASSETS)
        if symbol in VALR_PRIORITY_MARKETS:
            selected[symbol] = row
            continue
        if quote_asset in USD_LIKE_QUOTES and base_asset and base_asset not in STABLE_OR_FIAT_BASES:
            metric = _valr_market_quote_volume(row)
            if metric > usd_like_metric:
                usd_like_metric = metric
                usd_like_choice = (symbol, row)
    ordered_symbols = list(VALR_PRIORITY_MARKETS)
    if usd_like_choice is not None:
        selected[usd_like_choice[0]] = usd_like_choice[1]
        ordered_symbols.append(usd_like_choice[0])
    tickers = []
    for symbol in ordered_symbols:
        row = selected.get(symbol)
        if not isinstance(row, dict):
            continue
        last_trade = row.get("lastTradedPrice") or row.get("lastPrice") or row.get("last") or row.get("price")
        rolling_volume = (
            row.get("baseVolume")
            or row.get("volume")
            or row.get("baseCurrencyVolume")
            or row.get("volume24Hour")
        )
        if rolling_volume in (None, "", 0, "0"):
            quote_volume = _valr_market_quote_volume(row)
            last_price = as_float(last_trade, None)
            if quote_volume > 0 and last_price is not None and last_price > 0:
                rolling_volume = quote_volume / last_price
        ticker = {
            "pair": symbol,
            "bid": row.get("bidPrice") or row.get("bid") or row.get("bestBid"),
            "ask": row.get("askPrice") or row.get("ask") or row.get("bestAsk"),
            "last_trade": last_trade,
            "rolling_24_hour_volume": rolling_volume or "0",
            "status": "ACTIVE",
        }
        timestamp = _valr_timestamp_ms(row)
        if timestamp is not None:
            ticker["timestamp"] = timestamp
        tickers.append(ticker)
    return {"tickers": tickers}


def _parse_valr_market_summary(payload: object, *args, **kwargs) -> list[dict]:
    transformed = _valr_as_luno_payload(payload)
    candidates = [transformed]
    if isinstance(payload, dict) and "payload" in payload and any(
        key in payload for key in ("ok", "data_status", "http_status", "latency_ms")
    ):
        wrapped = dict(payload)
        wrapped["payload"] = transformed
        candidates.insert(0, wrapped)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return _parse_luno_tickers(candidate, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error is not None:
        raise last_error
    return []


def _append_note(row: dict, note: str) -> None:
    notes = row.setdefault("notes", [])
    if note not in notes:
        notes.append(note)


def _apply_paper_only_review_policy(row: dict) -> dict:
    quote = str(row.get("quote") or "").upper()
    if not _is_paper_only_review_fiat_quote(quote):
        return row
    row["local_quote_observe_only"] = True
    row["paper_only_review_scope"] = "frontier_candidate_review"
    normalized_last = as_float(row.get("usd_normalized_last"), default=None)
    if normalized_last is not None and normalized_last > 0 and row.get("quote_normalization_source"):
        _append_note(row, "usd_normalized_via_reference_fx")
    else:
        _append_note(row, "review_only_pending_usd_normalization")
    return row


def _target_quote_assets(target: dict) -> set[str]:
    return {str(item).upper() for item in target.get("quote_assets", sorted(QUOTE_ASSETS))}


def _instrument_id(target: dict, symbol: str) -> str:
    return f"{target['venue']}:{symbol}"


def _base_observation(target: dict, result: dict, symbol: str | None = None) -> dict:
    symbol = symbol or target.get("symbol") or "ALL"
    base, quote = _split_symbol(symbol, _target_quote_assets(target))
    return {
        "venue": target["venue"],
        "market_type": target.get("market_type", "spot"),
        "region": target.get("region"),
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "comparison_key": base,
        "instrument_id": _instrument_id(target, symbol),
        "route_id": target.get("route_id", f"{target['venue'].lower()}_public"),
        "paper_trade_policy": copy.deepcopy(target.get("paper_trade_policy"))
        if isinstance(target.get("paper_trade_policy"), dict)
        else None,
        "data_status": result["data_status"],
        "http_status": result["http_status"],
        "latency_ms": result["latency_ms"],
        "last_checked_at": _utc_now(),
        "bid": None,
        "ask": None,
        "last": None,
        "mark_price": None,
        "index_price": None,
        "funding_rate": None,
        "next_funding_time": None,
        "quote_volume_24h": None,
        "spread_bps": None,
        "usd_normalized_last": None,
        "quote_normalization_status": "not_normalized",
        "quote_normalization_source": None,
        "local_quote_observe_only": False,
        "source_url": target["url"],
        "paper_only_review_scope": None,
        "notes": [],
    }


def _finalize_observation(row: dict) -> dict:
    if not row.get("last"):
        row["last"] = row.get("mark_price") or row.get("index_price")
    row["spread_bps"] = round(spread_bps(row.get("bid"), row.get("ask"), row.get("last")), 3)
    if not row.get("base") or not row.get("quote"):
        base, quote = _split_symbol(str(row.get("symbol") or ""), QUOTE_ASSETS)
        row["base"] = row.get("base") or base
        row["quote"] = row.get("quote") or quote
    row["base"] = _canonical_asset(row.get("base"))
    row["comparison_key"] = row.get("base")
    return _apply_paper_only_review_policy(row)


def _max_product_tickers(target: dict, default: int = 50) -> int:
    try:
        return max(1, min(120, int(target.get("max_product_tickers", default))))
    except (TypeError, ValueError):
        return default


def _is_target_quote(target: dict, quote: str | None) -> bool:
    return str(quote or "").upper() in _target_quote_assets(target)


def _eligible_symbol_for_subfetch(target: dict, symbol: str) -> bool:
    base, quote = _split_symbol(symbol, _target_quote_assets(target))
    if not base or not quote or not _is_target_quote(target, quote):
        return False
    if base in STABLE_OR_FIAT_BASES and not (base in USD_LIKE_QUOTES and quote in REGIONAL_FIAT_QUOTES):
        return False
    return True


def _top_symbols_for_subfetch(target: dict, symbols: list[str]) -> list[str]:
    preferred_bases = [
        "BTC",
        "ETH",
        "SOL",
        "XRP",
        "BNB",
        "DOGE",
        "LINK",
        "AVAX",
        "ADA",
        "USDT",
        "USDC",
    ]
    eligible = [symbol for symbol in symbols if _eligible_symbol_for_subfetch(target, symbol)]
    preferred = []
    remaining = []
    for symbol in eligible:
        base, _ = _split_symbol(symbol, _target_quote_assets(target))
        if base in preferred_bases:
            preferred.append(symbol)
        else:
            remaining.append(symbol)
    ordered = [*preferred, *remaining]
    seen = set()
    deduped = []
    for symbol in ordered:
        if symbol in seen:
            continue
        seen.add(symbol)
        deduped.append(symbol)
    return deduped[: _max_product_tickers(target)]


def _parse_coinbase(target: dict, result: dict) -> list[dict]:
    row = _base_observation(target, result, target.get("symbol"))
    data = result["payload"]
    row.update(
        {
            "bid": as_float(data.get("bid")),
            "ask": as_float(data.get("ask")),
            "last": as_float(data.get("price")),
            "quote_volume_24h": (as_float(data.get("volume"), 0.0) or 0.0) * (as_float(data.get("price"), 0.0) or 0.0),
        }
    )
    return [_finalize_observation(row)]


def _parse_coinbase_products(target: dict, result: dict) -> list[dict]:
    products = result.get("payload") or []
    symbols = [
        str(item.get("id") or "")
        for item in products
        if isinstance(item, dict)
        and not item.get("trading_disabled")
        and str(item.get("status") or "").lower() == "online"
        and _is_target_quote(target, item.get("quote_currency"))
    ]
    observations = []
    for symbol in _top_symbols_for_subfetch(target, symbols):
        ticker = fetch_json(f"https://api.exchange.coinbase.com/products/{urllib.request.pathname2url(symbol)}/ticker")
        if not ticker["ok"]:
            row = _base_observation(target, ticker, symbol)
            row["notes"].append(f"Coinbase ticker fetch failed: {ticker['http_status']}")
            observations.append(_finalize_observation(row))
            continue
        row = _base_observation(target, ticker, symbol)
        data = ticker.get("payload") or {}
        price = as_float(data.get("price"))
        row.update(
            {
                "bid": as_float(data.get("bid")),
                "ask": as_float(data.get("ask")),
                "last": price,
                "quote_volume_24h": (as_float(data.get("volume"), 0.0) or 0.0) * float(price or 0.0),
                "latency_ms": round(float(result.get("latency_ms") or 0.0) + float(ticker.get("latency_ms") or 0.0), 3),
            }
        )
        observations.append(_finalize_observation(row))
    return observations or [_finalize_observation(_base_observation(target, result, target.get("symbol") or "ALL"))]


def _parse_kraken(target: dict, result: dict) -> list[dict]:
    row = _base_observation(target, result, target.get("symbol"))
    payload = result["payload"]
    values = list((payload.get("result") or {}).values())
    if not values:
        row["data_status"] = "degraded"
        row["notes"].append("Kraken payload had no ticker result.")
        return [_finalize_observation(row)]
    data = values[0]
    last = as_float((data.get("c") or [None])[0])
    volume_base = as_float((data.get("v") or [None, None])[1], 0.0) or 0.0
    row.update(
        {
            "bid": as_float((data.get("b") or [None])[0]),
            "ask": as_float((data.get("a") or [None])[0]),
            "last": last,
            "quote_volume_24h": volume_base * float(last or 0.0),
        }
    )
    if row["symbol"] == "XBTUSD":
        row["base"] = "BTC"
        row["quote"] = "USD"
        row["comparison_key"] = "BTC"
    return [_finalize_observation(row)]


def _parse_kraken_all_tickers(target: dict, result: dict) -> list[dict]:
    mapping: dict[str, dict] = {}
    pairs_url = target.get("asset_pairs_url")
    if pairs_url:
        pairs = fetch_json(str(pairs_url))
        for key, value in ((pairs.get("payload") or {}).get("result") or {}).items():
            if not isinstance(value, dict):
                continue
            altname = str(value.get("altname") or key)
            wsname = str(value.get("wsname") or "")
            if "/" in wsname:
                base, quote = [part.upper() for part in wsname.split("/", 1)]
            else:
                base, quote = _split_symbol(altname, _target_quote_assets(target))
            if base and quote:
                mapping[str(key)] = {
                    "symbol": altname,
                    "base": _canonical_asset(base),
                    "quote": quote,
                }
    observations = []
    for key, data in ((result.get("payload") or {}).get("result") or {}).items():
        meta = mapping.get(str(key), {})
        symbol = str(meta.get("symbol") or key)
        row = _base_observation(target, result, symbol)
        if meta:
            row["base"] = meta["base"]
            row["quote"] = meta["quote"]
            row["comparison_key"] = meta["base"]
        if not _is_target_quote(target, row.get("quote")):
            continue
        last = as_float((data.get("c") or [None])[0])
        volume_base = as_float((data.get("v") or [None, None])[1], 0.0) or 0.0
        row.update(
            {
                "bid": as_float((data.get("b") or [None])[0]),
                "ask": as_float((data.get("a") or [None])[0]),
                "last": last,
                "quote_volume_24h": volume_base * float(last or 0.0),
            }
        )
        observations.append(_finalize_observation(row))
    observations.sort(key=lambda row: float(row.get("quote_volume_24h") or 0.0), reverse=True)
    return observations[: _max_product_tickers(target, 80)] or [_finalize_observation(_base_observation(target, result, target.get("symbol") or "ALL"))]


def _parse_binance_24hr(target: dict, result: dict) -> list[dict]:
    payload = result["payload"]
    rows = payload if isinstance(payload, list) else [payload]
    observations = []
    for data in rows:
        symbol = str(data.get("symbol") or target.get("symbol") or "")
        row = _base_observation(target, result, symbol)
        row.update(
            {
                "bid": as_float(data.get("bidPrice")),
                "ask": as_float(data.get("askPrice")),
                "last": as_float(data.get("lastPrice")),
                "quote_volume_24h": as_float(data.get("quoteVolume")),
            }
        )
        observations.append(_finalize_observation(row))
    return observations


def _parse_kucoin_all_tickers(target: dict, result: dict) -> list[dict]:
    rows = ((result.get("payload") or {}).get("data") or {}).get("ticker") or []
    observations = []
    for data in rows:
        symbol = str(data.get("symbol") or data.get("symbolName") or "")
        row = _base_observation(target, result, symbol)
        row.update(
            {
                "bid": as_float(data.get("buy")),
                "ask": as_float(data.get("sell")),
                "last": as_float(data.get("last")),
                "quote_volume_24h": as_float(data.get("volValue")),
            }
        )
        observations.append(_finalize_observation(row))
    return observations


def _parse_gate_spot_tickers(target: dict, result: dict) -> list[dict]:
    observations = []
    for data in result.get("payload") or []:
        symbol = str(data.get("currency_pair") or "")
        row = _base_observation(target, result, symbol)
        row.update(
            {
                "bid": as_float(data.get("highest_bid")),
                "ask": as_float(data.get("lowest_ask")),
                "last": as_float(data.get("last")),
                "quote_volume_24h": as_float(data.get("quote_volume")),
            }
        )
        observations.append(_finalize_observation(row))
    return observations


def _parse_mexc_24hr(target: dict, result: dict) -> list[dict]:
    payload = result["payload"]
    rows = payload if isinstance(payload, list) else [payload]
    observations = []
    for data in rows:
        symbol = str(data.get("symbol") or "")
        row = _base_observation(target, result, symbol)
        row.update(
            {
                "bid": as_float(data.get("bidPrice")),
                "ask": as_float(data.get("askPrice")),
                "last": as_float(data.get("lastPrice")),
                "quote_volume_24h": as_float(data.get("quoteVolume")),
            }
        )
        observations.append(_finalize_observation(row))
    return observations


def _parse_bitget_spot_tickers(target: dict, result: dict) -> list[dict]:
    payload = result.get("payload") or {}
    rows = payload.get("data") or []
    observations = []
    for data in rows:
        symbol = str(data.get("symbol") or "")
        row = _base_observation(target, result, symbol)
        row.update(
            {
                "bid": as_float(data.get("bestBid") or data.get("bidPr")),
                "ask": as_float(data.get("bestAsk") or data.get("askPr")),
                "last": as_float(data.get("close") or data.get("lastPr")),
                "quote_volume_24h": as_float(data.get("quoteVol") or data.get("quoteVolume")),
            }
        )
        observations.append(_finalize_observation(row))
    return observations


def _parse_okx_swap(target: dict, result: dict) -> list[dict]:
    row = _base_observation(target, result, target.get("symbol"))
    rows = (result["payload"].get("data") or []) if result.get("payload") else []
    if not rows:
        row["data_status"] = "degraded"
        row["notes"].append("OKX payload had no ticker row.")
        return [_finalize_observation(row)]
    data = rows[0]
    row.update(
        {
            "bid": as_float(data.get("bidPx")),
            "ask": as_float(data.get("askPx")),
            "last": as_float(data.get("last")),
            "mark_price": as_float(data.get("last")),
            "quote_volume_24h": as_float(data.get("volCcy24h")),
        }
    )
    funding_url = target.get("funding_url")
    if funding_url:
        funding = fetch_json(funding_url)
        if funding["ok"] and funding.get("payload", {}).get("data"):
            frow = funding["payload"]["data"][0]
            row["funding_rate"] = as_float(frow.get("fundingRate"))
            row["next_funding_time"] = _unix_ms_to_iso(frow.get("nextFundingTime") or frow.get("fundingTime"))
        else:
            row["notes"].append(f"Funding fetch {funding['http_status']}")
    return [_finalize_observation(row)]


def _parse_okx_spot_tickers(target: dict, result: dict) -> list[dict]:
    observations = []
    for data in (result.get("payload") or {}).get("data") or []:
        symbol = str(data.get("instId") or "")
        row = _base_observation(target, result, symbol)
        row.update(
            {
                "bid": as_float(data.get("bidPx")),
                "ask": as_float(data.get("askPx")),
                "last": as_float(data.get("last")),
                "quote_volume_24h": as_float(data.get("volCcy24h") or data.get("vol24h")),
            }
        )
        observations.append(_finalize_observation(row))
    observations.sort(key=lambda row: float(row.get("quote_volume_24h") or 0.0), reverse=True)
    return observations


def _parse_bybit_linear(target: dict, result: dict) -> list[dict]:
    row = _base_observation(target, result, target.get("symbol"))
    rows = (((result.get("payload") or {}).get("result") or {}).get("list") or [])
    if not rows:
        row["data_status"] = "degraded"
        row["notes"].append("Bybit payload had no ticker row.")
        return [_finalize_observation(row)]
    data = rows[0]
    row.update(
        {
            "bid": as_float(data.get("bid1Price")),
            "ask": as_float(data.get("ask1Price")),
            "last": as_float(data.get("lastPrice")),
            "mark_price": as_float(data.get("markPrice")),
            "index_price": as_float(data.get("indexPrice")),
            "funding_rate": as_float(data.get("fundingRate")),
            "next_funding_time": _unix_ms_to_iso(data.get("nextFundingTime")),
            "quote_volume_24h": as_float(data.get("turnover24h")),
        }
    )
    return [_finalize_observation(row)]


def _parse_bybit_spot_tickers(target: dict, result: dict) -> list[dict]:
    observations = []
    rows = (((result.get("payload") or {}).get("result") or {}).get("list") or [])
    for data in rows:
        symbol = str(data.get("symbol") or "")
        row = _base_observation(target, result, symbol)
        row.update(
            {
                "bid": as_float(data.get("bid1Price")),
                "ask": as_float(data.get("ask1Price")),
                "last": as_float(data.get("lastPrice")),
                "quote_volume_24h": as_float(data.get("turnover24h")),
            }
        )
        observations.append(_finalize_observation(row))
    observations.sort(key=lambda row: float(row.get("quote_volume_24h") or 0.0), reverse=True)
    return observations


def _parse_luno_tickers(target: dict, result: dict) -> list[dict]:
    payload = result.get("payload") or {}
    rows = payload.get("tickers") or []
    observations = []
    for data in rows:
        symbol = str(data.get("pair") or "")
        row = _base_observation(target, result, symbol)
        last = as_float(data.get("last_trade"))
        volume_base = as_float(data.get("rolling_24_hour_volume"), 0.0) or 0.0
        row.update(
            {
                "bid": as_float(data.get("bid")),
                "ask": as_float(data.get("ask")),
                "last": last,
                "quote_volume_24h": volume_base * float(last or 0.0),
            }
        )
        observations.append(_finalize_observation(row))
    return observations


def _parse_valr_market_summary(target: dict, result: dict) -> list[dict]:
    payload = result.get("payload") or []
    rows = payload if isinstance(payload, list) else payload.get("data", [])
    observations = []
    for data in rows:
        symbol = str(data.get("currencyPair") or data.get("pair") or "")
        row = _base_observation(target, result, symbol)
        row.update(
            {
                "bid": as_float(data.get("bidPrice") or data.get("bid")),
                "ask": as_float(data.get("askPrice") or data.get("ask")),
                "last": as_float(data.get("lastTradedPrice") or data.get("last")),
                "quote_volume_24h": as_float(data.get("quoteVolume") or data.get("quote_volume")),
            }
        )
        if row.get("quote_volume_24h") is None:
            row["quote_volume_24h"] = (as_float(data.get("baseVolume"), 0.0) or 0.0) * float(row.get("last") or 0.0)
        observations.append(_finalize_observation(row))
    return observations


def _quidax_rows(payload: object) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        if isinstance(data.get("tickers"), list):
            return data["tickers"]
        return [
            {**value, "market": key}
            for key, value in data.items()
            if isinstance(value, dict)
        ]
    return data if isinstance(data, list) else []


def _parse_quidax_tickers(target: dict, result: dict) -> list[dict]:
    observations = []
    for data in _quidax_rows(result.get("payload") or {}):
        symbol = str(data.get("market") or data.get("currency") or data.get("id") or data.get("symbol") or "")
        symbol = symbol.upper()
        row = _base_observation(target, result, symbol)
        last = as_float(data.get("last") or data.get("last_price") or data.get("price"))
        row.update(
            {
                "bid": as_float(data.get("buy") or data.get("bid")),
                "ask": as_float(data.get("sell") or data.get("ask")),
                "last": last,
                "quote_volume_24h": as_float(data.get("quote_volume") or data.get("volume_quote")),
            }
        )
        if row.get("quote_volume_24h") is None:
            row["quote_volume_24h"] = (as_float(data.get("volume"), 0.0) or 0.0) * float(last or 0.0)
        observations.append(_finalize_observation(row))
    return observations


def _parse_indodax_ticker_all(target: dict, result: dict) -> list[dict]:
    tickers = (result.get("payload") or {}).get("tickers") or {}
    observations = []
    for symbol, data in tickers.items():
        row = _base_observation(target, result, str(symbol).upper())
        quote = row.get("quote")
        volume_key = f"vol_{str(quote or '').lower()}"
        row.update(
            {
                "bid": as_float(data.get("buy")),
                "ask": as_float(data.get("sell")),
                "last": as_float(data.get("last")),
                "quote_volume_24h": as_float(data.get(volume_key)),
            }
        )
        observations.append(_finalize_observation(row))
    return observations


def _parse_bitkub_ticker(target: dict, result: dict) -> list[dict]:
    payload = result.get("payload") or {}
    data = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    observations = []
    for symbol, row_data in data.items():
        if not isinstance(row_data, dict):
            continue
        parts = [part for part in str(symbol).upper().split("_") if part]
        row = _base_observation(target, result, str(symbol).upper())
        if len(parts) == 2:
            row["quote"] = parts[0]
            row["base"] = _canonical_asset(parts[1])
            row["comparison_key"] = row["base"]
        last = as_float(row_data.get("last"))
        row.update(
            {
                "bid": as_float(row_data.get("highestBid") or row_data.get("bid")),
                "ask": as_float(row_data.get("lowestAsk") or row_data.get("ask")),
                "last": last,
                "quote_volume_24h": as_float(row_data.get("quoteVolume")),
            }
        )
        if row.get("quote_volume_24h") is None:
            row["quote_volume_24h"] = (as_float(row_data.get("baseVolume") or row_data.get("volume"), 0.0) or 0.0) * float(last or 0.0)
        observations.append(_finalize_observation(row))
    return observations


def _parse_bitso_available_books(target: dict, result: dict) -> list[dict]:
    books = [
        str(item.get("book") or "").upper()
        for item in (result.get("payload") or {}).get("payload") or []
        if isinstance(item, dict) and item.get("book")
    ]
    observations = []
    for symbol in _top_symbols_for_subfetch(target, books):
        book = symbol.lower()
        ticker = fetch_json(f"https://api.bitso.com/v3/ticker/?book={book}")
        if not ticker["ok"]:
            row = _base_observation(target, ticker, symbol)
            row["notes"].append(f"Bitso ticker fetch failed: {ticker['http_status']}")
            observations.append(_finalize_observation(row))
            continue
        data = (ticker.get("payload") or {}).get("payload") or {}
        row = _base_observation(target, ticker, symbol)
        last = as_float(data.get("last"))
        row.update(
            {
                "bid": as_float(data.get("bid")),
                "ask": as_float(data.get("ask")),
                "last": last,
                "quote_volume_24h": (as_float(data.get("volume"), 0.0) or 0.0) * float(last or 0.0),
            }
        )
        observations.append(_finalize_observation(row))
    return observations


def _parse_mercado_bitcoin_symbols(target: dict, result: dict) -> list[dict]:
    payload = result.get("payload") or {}
    symbols = [str(symbol).upper() for symbol in payload.get("symbol") or []]
    observations = []
    for symbol in _top_symbols_for_subfetch(target, symbols):
        book = fetch_json(f"https://api.mercadobitcoin.net/api/v4/{symbol}/orderbook")
        if not book["ok"]:
            row = _base_observation(target, book, symbol)
            row["notes"].append(f"Mercado Bitcoin orderbook fetch failed: {book['http_status']}")
            observations.append(_finalize_observation(row))
            continue
        data = book.get("payload") or {}
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        bid = as_float((bids[0] or [None])[0]) if bids else None
        ask = as_float((asks[0] or [None])[0]) if asks else None
        last = (float(bid) + float(ask)) / 2.0 if bid and ask else bid or ask
        row = _base_observation(target, book, symbol)
        depth_quote = 0.0
        for level in [*bids[:10], *asks[:10]]:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price = as_float(level[0])
                amount = as_float(level[1])
                if price and amount:
                    depth_quote += price * amount
        row.update(
            {
                "bid": bid,
                "ask": ask,
                "last": last,
                "quote_volume_24h": round(depth_quote, 3) if depth_quote > 0 else None,
                "notes": [*list(row.get("notes") or []), "Ticker inferred from public order book; 24h volume unavailable."],
            }
        )
        observations.append(_finalize_observation(row))
    return observations


def _parse_buda_markets(target: dict, result: dict) -> list[dict]:
    symbols = [
        str(item.get("id") or item.get("name") or "").upper()
        for item in (result.get("payload") or {}).get("markets") or []
        if isinstance(item, dict) and not item.get("disabled")
    ]
    observations = []
    for symbol in _top_symbols_for_subfetch(target, symbols):
        market_id = symbol.lower()
        ticker = fetch_json(f"https://www.buda.com/api/v2/markets/{market_id}/ticker")
        if not ticker["ok"]:
            row = _base_observation(target, ticker, symbol)
            row["notes"].append(f"Buda ticker fetch failed: {ticker['http_status']}")
            observations.append(_finalize_observation(row))
            continue
        data = (ticker.get("payload") or {}).get("ticker") or {}
        last_pair = data.get("last_price") or []
        bid_pair = data.get("max_bid") or []
        ask_pair = data.get("min_ask") or []
        volume_pair = data.get("quote_volume") or data.get("volume") or []
        row = _base_observation(target, ticker, symbol)
        row.update(
            {
                "bid": as_float(bid_pair[0] if bid_pair else None),
                "ask": as_float(ask_pair[0] if ask_pair else None),
                "last": as_float(last_pair[0] if last_pair else None),
                "quote_volume_24h": as_float(volume_pair[0] if volume_pair else None),
            }
        )
        observations.append(_finalize_observation(row))
    return observations


PARSERS = {
    "coinbase_ticker": _parse_coinbase,
    "coinbase_products": _parse_coinbase_products,
    "kraken_ticker": _parse_kraken,
    "kraken_all_tickers": _parse_kraken_all_tickers,
    "binance_24hr": _parse_binance_24hr,
    "kucoin_all_tickers": _parse_kucoin_all_tickers,
    "gate_spot_tickers": _parse_gate_spot_tickers,
    "mexc_24hr": _parse_mexc_24hr,
    "bitget_spot_tickers": _parse_bitget_spot_tickers,
    "okx_swap_ticker": _parse_okx_swap,
    "okx_spot_tickers": _parse_okx_spot_tickers,
    "bybit_linear_ticker": _parse_bybit_linear,
    "bybit_spot_tickers": _parse_bybit_spot_tickers,
    "luno_tickers": _parse_luno_tickers,
    "valr_market_summary": _parse_valr_market_summary,
    "quidax_tickers": _parse_quidax_tickers,
    "indodax_ticker_all": _parse_indodax_ticker_all,
    "bitkub_ticker": _parse_bitkub_ticker,
    "bitso_available_books": _parse_bitso_available_books,
    "mercado_bitcoin_symbols": _parse_mercado_bitcoin_symbols,
    "buda_markets": _parse_buda_markets,
}


def _quote_assets(registry: dict) -> set[str]:
    return {str(item).upper() for item in registry.get("filters", {}).get("quote_assets", sorted(QUOTE_ASSETS))}


def _excluded_bases(registry: dict) -> set[str]:
    return {str(item).upper() for item in registry.get("filters", {}).get("exclude_base_assets", sorted(STABLE_OR_FIAT_BASES))}


def _is_supported_observation(row: dict, registry: dict) -> bool:
    if row.get("data_status") != "reachable":
        return True
    if not row.get("base") or not row.get("quote"):
        return False
    if row["quote"] not in _quote_assets(registry):
        return False
    if row["base"] in _excluded_bases(registry):
        return False
    if float(row.get("last") or 0.0) <= 0:
        return False
    return True


def _comparison_price(row: dict) -> float:
    if row.get("usd_normalized_last") not in (None, ""):
        return float(row.get("usd_normalized_last") or 0.0)
    return float(row.get("last") or 0.0)


def _normalize_regional_quotes(
    observations: list[dict],
    fx_references: dict[str, dict] | None = None,
) -> list[dict]:
    fx_references = fx_references or {}
    by_venue_quote: dict[tuple[str, str], dict] = {}
    for row in observations:
        if row.get("data_status") != "reachable":
            continue
        base = str(row.get("base") or "")
        quote = str(row.get("quote") or "")
        last = float(row.get("last") or 0.0)
        if base in USD_LIKE_QUOTES and quote in REGIONAL_FIAT_QUOTES and last > 0:
            key = (str(row.get("venue")), quote)
            previous = by_venue_quote.get(key)
            if previous is None or base == "USDT":
                by_venue_quote[key] = row

    normalized = []
    for row in observations:
        output = dict(row)
        quote = str(output.get("quote") or "")
        base = str(output.get("base") or "")
        last = float(output.get("last") or 0.0)
        output["comparison_price"] = last
        if quote in USD_LIKE_QUOTES:
            output["usd_normalized_last"] = last
            output["quote_normalization_status"] = "usd_like"
            output["quote_normalization_source"] = quote
            output["local_quote_observe_only"] = False
        elif quote in REGIONAL_FIAT_QUOTES:
            fx = by_venue_quote.get((str(output.get("venue")), quote))
            if fx and last > 0 and float(fx.get("last") or 0.0) > 0:
                fx_price = float(fx["last"])
                output["usd_normalized_last"] = round(last / fx_price, 12)
                output["comparison_price"] = output["usd_normalized_last"]
                output["quote_normalization_status"] = "same_venue_stablecoin_reference"
                output["quote_normalization_source"] = fx.get("instrument_id")
                output["local_quote_observe_only"] = base in STABLE_OR_FIAT_BASES
                if output.get("quote_volume_24h") is not None:
                    output["local_quote_volume_24h"] = output.get("quote_volume_24h")
                    output["quote_volume_24h"] = round(float(output["quote_volume_24h"]) / fx_price, 3)
            elif quote in fx_references and last > 0 and float(fx_references[quote].get("rate") or 0.0) > 0:
                ref = fx_references[quote]
                fx_price = float(ref["rate"])
                output["usd_normalized_last"] = round(last / fx_price, 12)
                output["comparison_price"] = output["usd_normalized_last"]
                output["quote_normalization_status"] = "external_fx_reference"
                output["quote_normalization_source"] = f"{ref.get('provider')}:USD/{quote}"
                output["fx_reference_rate"] = fx_price
                output["fx_reference_provider"] = ref.get("provider")
                output["fx_reference_age_seconds"] = ref.get("age_seconds")
                output["fx_reference_source_url"] = ref.get("source_url")
                output["local_quote_observe_only"] = base in STABLE_OR_FIAT_BASES
                if output.get("quote_volume_24h") is not None:
                    output["local_quote_volume_24h"] = output.get("quote_volume_24h")
                    output["quote_volume_24h"] = round(float(output["quote_volume_24h"]) / fx_price, 3)
            else:
                output["usd_normalized_last"] = None
                output["quote_normalization_status"] = "missing_same_venue_stablecoin_reference"
                output["quote_normalization_source"] = None
                output["local_quote_observe_only"] = True
                output["notes"] = [
                    *list(output.get("notes") or []),
                    "Regional fiat quote observed, but no same-venue stablecoin/fiat reference was available.",
                ]
        else:
            output["quote_normalization_status"] = "unsupported_quote"
            output["local_quote_observe_only"] = True
        normalized.append(output)
    return normalized


def _select_observations(observations: list[dict], registry: dict) -> list[dict]:
    filters = registry.get("filters", {})
    top_n = int(filters.get("top_volume_per_venue", 80))
    frontier_n = int(filters.get("frontier_symbols_per_venue", 40))
    frontier_max_listing_count = int(filters.get("frontier_max_listing_count", 3))
    min_frontier_quote_volume = float(filters.get("min_frontier_quote_volume_usd", 25_000))
    supported = [row for row in observations if _is_supported_observation(row, registry)]
    listing_counts = collections.Counter(
        row.get("comparison_key")
        for row in supported
        if row.get("data_status") == "reachable" and row.get("comparison_key")
    )
    selected_ids: set[tuple[str, str, str]] = set()
    selected: list[dict] = []
    by_venue: dict[str, list[dict]] = collections.defaultdict(list)
    for row in supported:
        if row.get("data_status") != "reachable":
            selected.append(row)
            continue
        by_venue[row["venue"]].append(row)
    for venue_rows in by_venue.values():
        venue_rows.sort(key=lambda item: float(item.get("quote_volume_24h") or 0.0), reverse=True)
        top_rows = venue_rows[:top_n]
        frontier_rows = [
            row
            for row in venue_rows[top_n:]
            if float(row.get("quote_volume_24h") or 0.0) >= min_frontier_quote_volume
            and listing_counts.get(row.get("comparison_key"), 0) <= frontier_max_listing_count
        ][:frontier_n]
        for row in [*top_rows, *frontier_rows]:
            key = (row["venue"], row["market_type"], row["symbol"])
            if key in selected_ids:
                continue
            selected_ids.add(key)
            selected.append(row)
    selected.sort(
        key=lambda item: (
            item.get("data_status") != "reachable",
            item.get("venue", ""),
            -float(item.get("quote_volume_24h") or 0.0),
        )
    )
    return selected


def scan_venues(
    settings: dict | None = None,
    selected_only: bool = True,
    required_inst_ids: set[str] | None = None,
    conn=None,
) -> list[dict]:
    cfg = (settings or {}).get("frontier_crypto_adapter", {})
    timeout = int(cfg.get("timeout_seconds", 8))
    registry = load_venue_registry()
    observations = []
    for target in registry.get("venues", []):
        if not target.get("enabled", True):
            continue
        if target.get("static_status"):
            result = {
                "ok": False,
                "data_status": str(target.get("static_status")),
                "http_status": "static_research_target",
                "latency_ms": 0.0,
                "payload": None,
            }
            parsed = [_base_observation(target, result, target.get("symbol"))]
            parsed[0]["notes"].append(str(target.get("notes") or "Watch-only research target."))
            observations.extend(_finalize_observation(row) for row in parsed)
            continue
        result = fetch_json(target["url"], timeout=timeout)
        if result["ok"]:
            parser = PARSERS[target["parser"]]
            try:
                parsed = parser(target, result)
            except Exception as exc:  # noqa: BLE001
                parsed = [_base_observation(target, result, target.get("symbol"))]
                parsed[0]["data_status"] = "degraded"
                parsed[0]["notes"].append(f"Parser failed: {exc}")
        else:
            parsed = [_base_observation(target, result, target.get("symbol"))]
            if parsed[0]["data_status"] == "blocked":
                parsed[0]["notes"].append("Public endpoint blocked from this machine; captured as access evidence.")
        observations.extend(_finalize_observation(row) for row in parsed)
    fx_references = get_regional_fx_references(conn, settings or {})
    observations = _normalize_regional_quotes(observations, fx_references=fx_references)
    required_inst_ids = required_inst_ids or set()
    supported = [
        row
        for row in observations
        if _is_supported_observation(row, registry)
        or (
            row.get("instrument_id") in required_inst_ids
            and row.get("data_status") == "reachable"
            and float(row.get("last") or 0.0) > 0
        )
    ]
    return _select_observations(supported, registry) if selected_only else supported


def _reference_prices(observations: list[dict], settings: dict) -> dict[str, float]:
    cfg = settings.get("frontier_crypto_adapter", {})
    min_cross_venue = int(cfg.get("min_cross_venue_count", load_venue_registry().get("filters", {}).get("min_cross_venue_count", 2)))
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    venues_by_key: dict[str, set[str]] = collections.defaultdict(set)
    for row in observations:
        if row.get("data_status") != "reachable" or row.get("market_type") != "spot":
            continue
        if row.get("local_quote_observe_only"):
            continue
        key = row.get("comparison_key")
        last = _comparison_price(row)
        if not key or last <= 0:
            continue
        grouped[key].append(last)
        venues_by_key[key].add(row["venue"])
    return {
        key: statistics.median(prices)
        for key, prices in grouped.items()
        if len(venues_by_key.get(key, set())) >= min_cross_venue and len(prices) >= min_cross_venue
    }


def _variant_reference(
    observation: dict,
    observations: list[dict],
    config: dict,
) -> tuple[float | None, int]:
    grouping = str(config.get("reference_grouping", "base"))
    key = (
        (observation.get("base"), observation.get("quote"))
        if grouping == "base_quote"
        else observation.get("base")
    )
    by_venue: dict[str, dict] = {}
    for row in observations:
        if row.get("data_status") != "reachable" or row.get("market_type") != "spot":
            continue
        if row.get("local_quote_observe_only"):
            continue
        row_key = (
            (row.get("base"), row.get("quote"))
            if grouping == "base_quote"
            else row.get("base")
        )
        if row_key != key or _comparison_price(row) <= 0:
            continue
        venue = str(row.get("venue"))
        previous = by_venue.get(venue)
        if previous is None or float(row.get("quote_volume_24h") or 0.0) > float(
            previous.get("quote_volume_24h") or 0.0
        ):
            by_venue[venue] = row
    unique_venues = len(by_venue)
    if unique_venues < int(config.get("min_unique_venues", 2)):
        return None, unique_venues
    peers = list(by_venue.values())
    if config.get("leave_one_out", False):
        peers = [row for row in peers if row.get("venue") != observation.get("venue")]
    prices = [_comparison_price(row) for row in peers if _comparison_price(row) > 0]
    if not prices:
        return None, unique_venues
    return statistics.median(prices), unique_venues


def build_variant_candidates(
    observations: list[dict],
    settings: dict,
    variant_id: str,
    config: dict,
) -> list[dict]:
    variant_settings = copy.deepcopy(settings)
    variant_settings.setdefault("frontier_crypto_adapter", {})["min_dislocation_bps"] = float(
        config.get("min_dislocation_bps", 12.0)
    )
    variant_settings.setdefault("risk", {})["max_spread_bps"] = float(
        config.get("max_spread_bps", settings.get("risk", {}).get("max_spread_bps", 8.0))
    )
    variant_settings["risk"]["taker_fee_bps_per_leg"] = float(
        config.get("fee_bps_per_side", settings.get("risk", {}).get("taker_fee_bps_per_leg", 5.0))
    )
    variant_settings["risk"]["slippage_bps_per_leg"] = float(
        config.get("slippage_bps_per_side", settings.get("risk", {}).get("slippage_bps_per_leg", 3.0))
    )
    min_liquidity = float(config.get("min_liquidity_score", 0.0))
    direction_mode = str(config.get("direction_mode", "both"))
    allowed_venues = {str(item).upper() for item in config.get("allowed_venues", [])}
    blocked_venues = {str(item).upper() for item in config.get("blocked_venues", [])}
    allowed_directions = set(config.get("allowed_directions", []))
    allowed_route_statuses = set(config.get("allowed_route_statuses", []))
    allowed_quote_normalization = set(config.get("allowed_quote_normalization_statuses", []))
    min_quality_score = float(config.get("min_quality_score", 0.0))
    min_depth_edge = float(config.get("min_depth_adjusted_edge_bps", 0.0))
    min_source_venues = int(config.get("min_source_venue_count", config.get("min_unique_venues", 2)))
    max_round_trip_cost = float(config.get("max_round_trip_cost_bps", 1000.0))
    require_public_book = bool(config.get("require_public_order_book", False))
    allow_regional_quotes = bool(config.get("allow_regional_quotes", True))
    candidates = []
    for observation in observations:
        if observation.get("data_status") != "reachable" or observation.get("market_type") != "spot":
            continue
        venue = str(observation.get("venue") or "").upper()
        if allowed_venues and venue not in allowed_venues:
            continue
        if venue in blocked_venues:
            continue
        reference, unique_venues = _variant_reference(observation, observations, config)
        if reference is None:
            continue
        candidate = _candidate_from_observation(
            observation,
            variant_settings,
            reference,
            unique_venues,
        )
        reject_reason = candidate.get("candidate_reject_reason")
        route_status = str((candidate.get("execution_feasibility") or {}).get("status") or "unknown")
        if candidate.get("liquidity_score", 0.0) < min_liquidity:
            reject_reason = "liquidity_below_variant_minimum"
        if direction_mode == "short_only" and candidate.get("direction") != "short_frontier_spot":
            reject_reason = "direction_not_enabled"
        if direction_mode == "long_only" and candidate.get("direction") != "long_frontier_spot":
            reject_reason = "direction_not_enabled"
        if allowed_directions and candidate.get("direction") not in allowed_directions:
            reject_reason = "direction_not_enabled"
        if allowed_route_statuses and route_status not in allowed_route_statuses:
            reject_reason = "route_status_not_enabled"
        if allowed_quote_normalization and candidate.get("quote_normalization_status") not in allowed_quote_normalization:
            reject_reason = "quote_normalization_not_enabled"
        if not allow_regional_quotes and candidate.get("region"):
            reject_reason = "regional_quote_not_enabled"
        if int(candidate.get("source_venue_count") or 0) < min_source_venues:
            reject_reason = "source_venue_count_below_variant_minimum"
        if float(candidate.get("quality_score") or 0.0) < min_quality_score:
            reject_reason = "quality_below_variant_minimum"
        if float(candidate.get("edge_bps_estimate") or 0.0) < min_depth_edge:
            reject_reason = "depth_adjusted_edge_below_variant_minimum"
        if float(candidate.get("estimated_round_trip_cost_bps") or 0.0) > max_round_trip_cost:
            reject_reason = "round_trip_cost_above_variant_maximum"
        if require_public_book and candidate.get("frontier_cost_source") != "public_order_book":
            reject_reason = "public_order_book_required"
        if reject_reason:
            candidate["direction"] = "watch_only"
            candidate["candidate_reject_reason"] = reject_reason
            candidate["score"] = min(float(candidate.get("score") or 0.0), 25.0)
            candidate["paper_entry_blocked"] = True
            candidate["promotion_eligible"] = False
            candidate["execution_feasibility"] = _preliminary_feasibility(
                "watch_only",
                observation["market_type"],
                observation["data_status"],
                variant_settings,
            )
        candidate["signal_variant_id"] = variant_id
        candidate["variant_reference_grouping"] = config.get("reference_grouping", "base")
        candidate["variant_leave_one_out"] = bool(config.get("leave_one_out", False))
        candidate["variant_unique_venue_count"] = unique_venues
        candidate["variant_route_status"] = route_status
        candidate["variant_min_quality_score"] = min_quality_score
        candidate["variant_min_depth_adjusted_edge_bps"] = min_depth_edge
        candidate["variant_min_source_venue_count"] = min_source_venues
        candidates.append(candidate)
    candidates.sort(key=lambda row: row.get("score", 0.0), reverse=True)
    return candidates


def _preliminary_feasibility(direction: str, market_type: str, data_status: str, settings: dict) -> dict:
    caps = settings.get("account_capabilities", {})
    if data_status in {"blocked", "unavailable", "degraded"}:
        return {
            "status": "blocked",
            "requires_short_spot": False,
            "legs": [],
            "route_blockers": ["venue_api_access"],
            "notes": ["Public market data is not currently reachable enough for paper execution."],
        }
    if direction == "watch_only":
        return {"status": "watch_only", "requires_short_spot": False, "legs": [], "route_blockers": [], "notes": ["No actionable dislocation."]}
    if market_type == "spot" and direction == "long_frontier_spot":
        allowed = bool(caps.get("crypto_spot", False))
        return {
            "status": "standard" if allowed else "conditional",
            "requires_short_spot": False,
            "legs": ["buy spot on observed venue"],
            "route_blockers": [] if allowed else ["crypto_spot"],
            "notes": ["Paper-only spot venue candidate from public data."],
        }
    if market_type == "spot" and direction == "short_frontier_spot":
        allowed = bool(caps.get("crypto_spot", False) and caps.get("spot_borrow", False))
        blockers = []
        if not caps.get("crypto_spot", False):
            blockers.append("crypto_spot")
        if not caps.get("spot_borrow", False):
            blockers.append("spot_borrow")
        return {
            "status": "standard" if allowed else "conditional",
            "requires_short_spot": True,
            "legs": ["borrow and short spot or use equivalent margin route"],
            "route_blockers": blockers,
            "notes": ["Short spot route requires confirmed borrow or margin support."],
        }
    if market_type == "perp":
        allowed = bool(caps.get("crypto_derivatives", False))
        return {
            "status": "standard" if allowed else "conditional",
            "requires_short_spot": False,
            "legs": ["paper perpetual exposure"],
            "route_blockers": [] if allowed else ["crypto_derivatives"],
            "notes": ["Paper-only derivatives venue candidate from public data."],
        }
    return {"status": "route_unknown", "requires_short_spot": False, "legs": [], "route_blockers": ["route_model"], "notes": ["No route model."]}


def _direction_for_observation(observation: dict, reference_price: float | None, settings: dict) -> tuple[str, float, str | None]:
    cfg = settings.get("frontier_crypto_adapter", {})
    risk = settings.get("risk", {})
    min_dislocation_bps = float(cfg.get("min_dislocation_bps", 12.0))
    max_spread = float(risk.get("max_spread_bps", 8.0))
    last = _comparison_price(observation)
    if observation.get("local_quote_observe_only"):
        return "watch_only", 0.0, "local_quote_observe_only"
    if observation.get("data_status") != "reachable" or not reference_price or last <= 0:
        return "watch_only", 0.0, "no_reliable_reference"
    deviation = bps(last, reference_price)
    if float(observation.get("spread_bps") or 999.0) > max_spread:
        return "watch_only", round(deviation, 3), "spread_too_wide"
    if abs(deviation) < min_dislocation_bps:
        return "watch_only", round(deviation, 3), "below_dislocation_threshold"
    if observation.get("market_type") == "perp":
        return ("short_frontier_perp" if deviation > 0 else "long_frontier_perp"), round(deviation, 3), None
    return ("short_frontier_spot" if deviation > 0 else "long_frontier_spot"), round(deviation, 3), None


def _candidate_from_observation(observation: dict, settings: dict, reference_price: float | None, source_venue_count: int) -> dict:
    risk = settings.get("risk", {})
    quality_cfg = settings.get("frontier_data_quality", {})
    direction, deviation, reject_reason = _direction_for_observation(observation, reference_price, settings)
    last = float(observation.get("last") or 0.0)
    comparison_price = _comparison_price(observation)
    spread = round(float(observation.get("spread_bps") or spread_bps(observation.get("bid"), observation.get("ask"), last)), 3)
    liq = round(liquidity_score(observation.get("quote_volume_24h")), 3)
    fee_bps_per_side = float(quality_cfg.get("conservative_fee_bps_per_side", 10.0))
    fills = observation.get("simulated_fills") or {}
    buy_fill = ((fills.get("buy") or {}).get("1000") or {})
    sell_fill = ((fills.get("sell") or {}).get("1000") or {})
    buy_slippage = buy_fill.get("slippage_bps")
    sell_slippage = sell_fill.get("slippage_bps")
    fallback_slippage = float(risk.get("slippage_bps_per_leg", 3.0))
    if buy_slippage is not None and sell_slippage is not None:
        round_trip_cost_bps = float(buy_slippage) + float(sell_slippage) + fee_bps_per_side * 2.0
        cost_source = "public_order_book"
    else:
        round_trip_cost_bps = (fee_bps_per_side + fallback_slippage) * 2.0
        cost_source = "conservative_fallback"
    entry_slippage = (
        float(buy_slippage)
        if direction in {"long_frontier_spot", "long_frontier_perp"} and buy_slippage is not None
        else float(sell_slippage)
        if direction not in {"long_frontier_spot", "long_frontier_perp"} and sell_slippage is not None
        else fallback_slippage
    )
    exit_slippage = (
        float(sell_slippage)
        if direction in {"long_frontier_spot", "long_frontier_perp"} and sell_slippage is not None
        else float(buy_slippage)
        if buy_slippage is not None
        else fallback_slippage
    )
    gross_edge = abs(deviation)
    edge = max(0.0, gross_edge - round_trip_cost_bps)
    anomaly_flags = list(observation.get("anomaly_flags") or [])
    critical_anomalies = list(observation.get("critical_anomaly_flags") or [])
    if round_trip_cost_bps >= gross_edge and direction != "watch_only":
        anomaly_flags.append("simulated_slippage_exceeds_edge")
    if gross_edge > 250.0 and source_venue_count <= 2:
        anomaly_flags.append("unsupported_single_venue_extreme")
    anomaly_flags = sorted(set(anomaly_flags))
    quality_status = str(observation.get("quality_status") or "unknown")
    quality_score = observation.get("quality_score")
    freshness_age = observation.get("freshness_age_seconds")
    shadow_threshold = float(quality_cfg.get("shadow_below_score", 35.0))
    conditional_threshold = float(quality_cfg.get("conditional_below_score", 60.0))
    block_stale = float(quality_cfg.get("block_stale_seconds", 90.0))
    if (
        quality_status in {"unknown", "blocked"}
        or critical_anomalies
        or (freshness_age is not None and float(freshness_age) > block_stale)
        or quality_score is None
        or float(quality_score) < shadow_threshold
    ):
        quality_action = "shadow_only"
        paper_entry_blocked = True
        quality_allocation_multiplier = 0.0
    elif float(quality_score) < conditional_threshold:
        quality_action = "conditional"
        paper_entry_blocked = False
        quality_allocation_multiplier = 0.25
    else:
        quality_action = "normal"
        paper_entry_blocked = False
        quality_allocation_multiplier = 1.0
    promotion_eligible = bool(
        quality_status in {"verified", "degraded"}
        and quality_score is not None
        and not critical_anomalies
        and (freshness_age is None or float(freshness_age) <= block_stale)
    )
    regional_candidate_gate_status = "not_applicable"
    regional_quote = observation.get("quote") in REGIONAL_FIAT_QUOTES
    if regional_quote and observation.get("quote_normalization_status") == "external_fx_reference":
        required_snapshots = int(quality_cfg.get("min_verified_snapshots_for_regional_candidate", 3))
        min_regional_quality = float(quality_cfg.get("min_regional_quality_score", 70.0))
        verified_count = int(observation.get("verified_depth_snapshot_count") or 0)
        if verified_count < required_snapshots:
            regional_candidate_gate_status = "insufficient_verified_depth_snapshots"
        elif quality_score is None or float(quality_score) < min_regional_quality:
            regional_candidate_gate_status = "regional_quality_below_minimum"
        elif quality_status != "verified":
            regional_candidate_gate_status = "regional_depth_not_verified"
        else:
            regional_candidate_gate_status = "passed"
        if regional_candidate_gate_status != "passed":
            quality_action = "shadow_only"
            paper_entry_blocked = True
            quality_allocation_multiplier = 0.0
            promotion_eligible = False
            anomaly_flags = sorted(set([*anomaly_flags, regional_candidate_gate_status]))
    actionable = direction != "watch_only" and observation.get("data_status") == "reachable" and not reject_reason
    score = 0.0
    if actionable:
        score = min(100.0, 24.0 + edge * 1.25 + liq * 18.0 - min(spread * 1.2, 20.0) + min(source_venue_count, 8))
        if quality_score is not None:
            score += (float(quality_score) - 50.0) * 0.25
        score = max(0.0, min(100.0, score))
    elif observation.get("data_status") == "reachable":
        score = min(25.0, 8.0 + abs(deviation) * 0.3 + liq * 10.0)
    feasibility = _preliminary_feasibility(direction, observation["market_type"], observation["data_status"], settings)
    funding_bps = (float(observation.get("funding_rate") or 0.0) * 10_000.0) if observation.get("funding_rate") is not None else 0.0
    return {
        "seen_at": observation["last_checked_at"],
        "venue": observation["venue"],
        "inst_id": observation["instrument_id"],
        "symbol": observation["symbol"],
        "region": observation.get("region"),
        "base": observation.get("base"),
        "quote": observation.get("quote"),
        "comparison_key": observation.get("comparison_key"),
        "source_venue_count": source_venue_count,
        "asset_class": "crypto_derivatives" if observation["market_type"] == "perp" else "crypto_spot",
        "trade_type": "frontier_crypto_venue_map",
        "direction": direction,
        "execution_feasibility": feasibility,
        "thesis": "frontier crypto venue map price/funding dislocation candidate",
        "last": round(last, 8),
        "usd_normalized_last": observation.get("usd_normalized_last"),
        "comparison_price": round(comparison_price, 8) if comparison_price else None,
        "reference_price": round(float(reference_price or 0.0), 8),
        "quote_normalization_status": observation.get("quote_normalization_status"),
        "quote_normalization_source": observation.get("quote_normalization_source"),
        "fx_reference_rate": observation.get("fx_reference_rate"),
        "fx_reference_provider": observation.get("fx_reference_provider"),
        "fx_reference_age_seconds": observation.get("fx_reference_age_seconds"),
        "fx_reference_source_url": observation.get("fx_reference_source_url"),
        "local_quote_observe_only": bool(observation.get("local_quote_observe_only")),
        "regional_candidate_gate_status": regional_candidate_gate_status,
        "verified_depth_snapshot_count": observation.get("verified_depth_snapshot_count"),
        "venue_deviation_bps": round(deviation, 3),
        "funding_rate": observation.get("funding_rate"),
        "funding_bps": round(funding_bps, 3),
        "next_funding_time": observation.get("next_funding_time"),
        "basis_bps": round(deviation, 3) if observation["market_type"] == "perp" else 0.0,
        "gross_edge_bps_estimate": round(gross_edge, 3),
        "edge_bps_estimate": round(edge, 3),
        "estimated_round_trip_cost_bps": round(round_trip_cost_bps, 3),
        "estimated_fee_bps_per_side": round(fee_bps_per_side, 3),
        "entry_slippage_bps_estimate": round(entry_slippage, 3),
        "exit_slippage_bps_estimate": round(exit_slippage, 3),
        "frontier_cost_source": cost_source,
        "change_24h_pct": 0.0,
        "quote_volume_24h": round(float(observation.get("quote_volume_24h") or 0.0), 3),
        "liquidity_score": liq,
        "spread_bps": spread,
        "score": round(max(0.0, score), 3),
        "data_status": observation["data_status"],
        "http_status": observation["http_status"],
        "latency_ms": observation["latency_ms"],
        "route_id": observation["route_id"],
        "candidate_reject_reason": reject_reason,
        "quality_status": quality_status,
        "quality_score": quality_score,
        "quality_components": observation.get("quality_components") or {},
        "quality_action": quality_action,
        "quality_allocation_multiplier": quality_allocation_multiplier,
        "paper_entry_blocked": paper_entry_blocked,
        "promotion_eligible": promotion_eligible,
        "freshness_age_seconds": freshness_age,
        "freshness_basis": observation.get("freshness_basis"),
        "depth_latency_ms": observation.get("depth_latency_ms"),
        "depth_usd": observation.get("depth_usd") or {},
        "simulated_fills": fills,
        "book_imbalance_10bps": observation.get("book_imbalance_10bps"),
        "depth_concentration_25bps": observation.get("depth_concentration_25bps"),
        "anomaly_flags": anomaly_flags,
        "critical_anomaly_flags": critical_anomalies,
        "risk_notes": [
            "paper-trade only",
            "public endpoint data may be delayed, blocked, or venue-specific",
            "cross-venue price differences can reflect USD/USDT/USDC, fees, withdrawal friction, and index methodology",
            "regional fiat quotes require same-venue stablecoin normalization before paper entry",
            "frontier fees use a conservative global estimate until read-only account-specific fee data is configured",
            "live execution remains blocked until route, credentials, legal access, and limits are configured",
        ],
        "data_source": {
            "provider": f"{observation['venue']} public REST",
            "url": observation["source_url"],
            "data_status": observation["data_status"],
            "http_status": observation["http_status"],
            "notes": observation.get("notes", []),
        },
    }


def build_scan_batch(
    settings: dict,
    limit: int | None = None,
    required_inst_ids: set[str] | None = None,
    conn=None,
) -> ScanBatch:
    all_observations = scan_venues(
        settings,
        selected_only=False,
        required_inst_ids=required_inst_ids,
        conn=conn,
    )
    observations = _select_observations(all_observations, load_venue_registry())
    selected_ids = {row.get("instrument_id") for row in observations}
    for row in all_observations:
        if row.get("instrument_id") in (required_inst_ids or set()) and row.get("instrument_id") not in selected_ids:
            observations.append(row)
            selected_ids.add(row.get("instrument_id"))
    refs = _reference_prices(observations, settings)
    venue_counts = collections.Counter(
        row.get("comparison_key")
        for row in observations
        if row.get("data_status") == "reachable" and row.get("comparison_key") and not row.get("local_quote_observe_only")
    )
    candidates = [
        _candidate_from_observation(row, settings, refs.get(str(row.get("comparison_key"))), venue_counts.get(row.get("comparison_key"), 0))
        for row in observations
        if row.get("data_status") == "reachable" and row.get("comparison_key") in refs
    ]
    candidates.sort(key=lambda row: row.get("score", 0.0), reverse=True)
    if limit:
        candidates = candidates[: int(limit)]
    write_outputs(observations, candidates, settings)
    price_observations = [
        normalize_observation(row, source=f"{row.get('venue')} public REST")
        for row in all_observations
        if row.get("data_status") == "reachable" and float(row.get("last") or 0.0) > 0
    ]
    return ScanBatch(
        source="Frontier crypto public REST",
        candidates=candidates,
        observations=price_observations,
        metadata={
            "selected_observations": observations,
            "all_observation_count": len(all_observations),
            "report": str(REPORT_JSON),
        },
    )


def build_candidates(settings: dict, limit: int | None = None) -> list[dict]:
    return build_scan_batch(settings, limit=limit).candidates


def _distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "median": round(statistics.median(ordered), 3),
        "p95": round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))], 3),
        "max": round(ordered[-1], 3),
    }


def _quality_rates(observations: list[dict], key: str) -> dict:
    grouped: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for row in observations:
        label = str(row.get(key) or "unknown")
        status = row.get("quality_status") or "unknown"
        grouped[label]["total"] += 1
        if status in {"verified", "degraded"}:
            grouped[label]["known"] += 1
    return {
        label: {
            "total": counts["total"],
            "known": counts["known"],
            "known_quality_rate": round(counts["known"] / counts["total"], 4) if counts["total"] else 0.0,
        }
        for label, counts in sorted(grouped.items())
    }


def _starved_venue_coverage(observations: list[dict], depth_summary: dict) -> dict:
    starved = {str(venue).upper() for venue in depth_summary.get("starved_venues", [])}
    selected_by_venue = depth_summary.get("selected_by_venue", {}) or {}
    coverage: dict[str, dict] = {}
    for venue in sorted(starved):
        rows = [row for row in observations if str(row.get("venue") or "").upper() == venue]
        if not rows:
            coverage[venue] = {
                "observation_count": 0,
                "known_quality_count": 0,
                "known_quality_rate": 0.0,
                "selected_this_cycle": int(selected_by_venue.get(venue, 0) or 0),
                "starved_venue_status": "not_observed",
            }
            continue
        known = sum(1 for row in rows if row.get("quality_status") in {"verified", "degraded"})
        selected = int(selected_by_venue.get(venue, 0) or 0)
        known_rate = known / len(rows) if rows else 0.0
        if known_rate < 0.1:
            status = "coverage_starved"
        elif selected == 0:
            status = "observed_not_selected"
        else:
            status = "being_enriched"
        coverage[venue] = {
            "observation_count": len(rows),
            "known_quality_count": known,
            "known_quality_rate": round(known_rate, 4),
            "selected_this_cycle": selected,
            "starved_venue_status": status,
        }
    return coverage


def summarize(
    observations: list[dict],
    candidates: list[dict],
    quality_summary: dict | None = None,
) -> dict:
    by_status = collections.Counter(row.get("data_status", "unknown") for row in observations)
    by_market_type = collections.Counter(row.get("market_type", "unknown") for row in observations)
    by_venue = collections.Counter(row.get("venue", "unknown") for row in observations)
    by_region = collections.Counter(row.get("region", "global") or "global" for row in observations)
    by_quote = collections.Counter(row.get("quote", "unknown") for row in observations if row.get("quote"))
    by_quote_normalization = collections.Counter(
        row.get("quote_normalization_status", "unknown") for row in observations
    )
    route_status = collections.Counter((row.get("execution_feasibility") or {}).get("status", "unknown") for row in candidates)
    route_blockers: collections.Counter[str] = collections.Counter()
    quality_statuses = collections.Counter(row.get("quality_status", "unknown") for row in observations)
    anomaly_counts: collections.Counter[str] = collections.Counter()
    freshness_values = []
    depth_latency_values = []
    buy_slippage_values = []
    sell_slippage_values = []
    for row in observations:
        anomaly_counts.update(
            flag
            for flag in (row.get("anomaly_flags") or [])
            if flag != "not_selected_for_depth"
        )
        if row.get("freshness_age_seconds") is not None:
            freshness_values.append(float(row["freshness_age_seconds"]))
        if row.get("depth_latency_ms") is not None:
            depth_latency_values.append(float(row["depth_latency_ms"]))
        fills = row.get("simulated_fills") or {}
        buy = (((fills.get("buy") or {}).get("1000") or {}).get("slippage_bps"))
        sell = (((fills.get("sell") or {}).get("1000") or {}).get("slippage_bps"))
        if buy is not None:
            buy_slippage_values.append(float(buy))
        if sell is not None:
            sell_slippage_values.append(float(sell))
    for row in candidates:
        for blocker in (row.get("execution_feasibility") or {}).get("route_blockers", []):
            route_blockers[blocker] += 1
    quality_known_count = sum(
        count for status, count in quality_statuses.items() if status in {"verified", "degraded"}
    )
    depth_summary = quality_summary or {}
    depth_selected = int(depth_summary.get("selected_count", 0) or 0)
    depth_enriched = int(depth_summary.get("enriched_count", 0) or 0)
    observation_count = len(observations)
    starved_coverage = _starved_venue_coverage(observations, depth_summary)
    known_quality_rate = round(quality_known_count / observation_count, 4) if observation_count else 0.0
    known_quality_target = 0.25
    expansion_map = {
        "market_surface": "frontier_crypto_venue_map",
        "observation_count": observation_count,
        "candidate_count": len(candidates),
        "venue_count": len(by_venue),
        "symbol_count": len({row.get("comparison_key") for row in observations if row.get("comparison_key")}),
        "regional_observation_count": sum(1 for row in observations if row.get("quote") in REGIONAL_FIAT_QUOTES),
        "depth_selected_count": depth_selected,
        "depth_enriched_count": depth_enriched,
        "depth_selected_rate": round(depth_selected / observation_count, 4) if observation_count else 0.0,
        "depth_enriched_rate": round(depth_enriched / observation_count, 4) if observation_count else 0.0,
        "known_quality_count": quality_known_count,
        "unknown_quality_count": quality_statuses.get("unknown", 0),
        "known_quality_rate": known_quality_rate,
        "known_quality_rate_target": known_quality_target,
        "known_quality_rate_target_progress": round(min(1.0, known_quality_rate / known_quality_target), 4),
        "quality_target_escalation": depth_summary.get("selection_escalation", {}),
        "depth_selection_buckets": depth_summary.get("selection_bucket_counts", {}),
        "market_testing_progress": depth_summary.get("market_testing_progress", {}),
        "selected_by_venue": depth_summary.get("selected_by_venue", {}),
        "starved_selected_by_venue": depth_summary.get("starved_selected_by_venue", {}),
        "starved_venue_coverage": starved_coverage,
        "by_region": dict(by_region),
        "by_quote": dict(by_quote),
        "by_quote_normalization": dict(by_quote_normalization),
        "by_route_blocker": dict(route_blockers),
        "known_quality_by_region": _quality_rates(observations, "region"),
        "known_quality_by_venue": _quality_rates(observations, "venue"),
        "known_quality_by_quote": _quality_rates(observations, "quote"),
        "known_quality_by_quote_normalization": _quality_rates(observations, "quote_normalization_status"),
    }
    unknown_quality_backlog = [
        {
            "inst_id": row.get("instrument_id"),
            "venue": row.get("venue"),
            "base": row.get("base"),
            "quote": row.get("quote"),
            "region": row.get("region"),
            "quote_volume_24h": row.get("quote_volume_24h"),
            "quote_normalization_status": row.get("quote_normalization_status"),
        }
        for row in sorted(
            [
                item
                for item in observations
                if item.get("data_status") == "reachable"
                and item.get("quality_status") == "unknown"
                and item.get("instrument_id")
            ],
            key=lambda item: float(item.get("quote_volume_24h") or 0.0),
            reverse=True,
        )[:25]
    ]
    regional_candidate_blockers = collections.Counter(
        row.get("regional_candidate_gate_status", "unknown")
        for row in candidates
        if row.get("quote") in REGIONAL_FIAT_QUOTES
    )
    active_candidate_count = sum(
        1
        for row in candidates
        if row.get("direction") != "watch_only"
        and not row.get("paper_entry_blocked")
        and not row.get("candidate_reject_reason")
    )
    shadow_only_candidate_count = sum(
        1
        for row in candidates
        if row.get("direction") == "watch_only"
        or row.get("paper_entry_blocked")
        or row.get("quality_action") == "shadow_only"
        or bool(row.get("candidate_reject_reason"))
    )
    candidate_activity = {
        "active_paper_review_candidates": active_candidate_count,
        "shadow_or_observe_only_candidates": shadow_only_candidate_count,
        "regional_admitted_candidates": regional_candidate_blockers.get("passed", 0),
        "regional_blocked_candidates": sum(
            count
            for gate, count in regional_candidate_blockers.items()
            if gate not in {"passed", "not_applicable"}
        ),
    }
    expansion_map.update(
        {
            "candidate_activity": candidate_activity,
            "active_paper_review_candidates": active_candidate_count,
            "shadow_or_observe_only_candidates": shadow_only_candidate_count,
            "venue_quota_report": depth_summary.get("venue_quota_report", {}),
            "selection_limits": depth_summary.get("selection_limits", {}),
            "worker_count": depth_summary.get("worker_count"),
        }
    )
    top_dislocations = [
        {
            "inst_id": row.get("inst_id"),
            "venue": row.get("venue"),
            "base": row.get("base"),
            "quote": row.get("quote"),
            "region": row.get("region"),
            "direction": row.get("direction"),
            "score": row.get("score"),
            "venue_deviation_bps": row.get("venue_deviation_bps"),
            "edge_bps_estimate": row.get("edge_bps_estimate"),
            "gross_edge_bps_estimate": row.get("gross_edge_bps_estimate"),
            "estimated_round_trip_cost_bps": row.get("estimated_round_trip_cost_bps"),
            "quality_score": row.get("quality_score"),
            "quality_status": row.get("quality_status"),
            "quality_action": row.get("quality_action"),
            "anomaly_flags": row.get("anomaly_flags", []),
            "route_status": (row.get("execution_feasibility") or {}).get("status"),
            "route_blockers": (row.get("execution_feasibility") or {}).get("route_blockers", []),
            "quote_normalization_status": row.get("quote_normalization_status"),
        }
        for row in candidates[:20]
    ]
    return {
        "observation_count": len(observations),
        "candidate_count": len(candidates),
        "venue_count": len(by_venue),
        "symbol_count": len({row.get("comparison_key") for row in observations if row.get("comparison_key")}),
        "reachable_venue_count": len({row["venue"] for row in observations if row.get("data_status") == "reachable"}),
        "blocked_venue_count": len({row["venue"] for row in observations if row.get("data_status") == "blocked"}),
        "degraded_venue_count": len({row["venue"] for row in observations if row.get("data_status") == "degraded"}),
        "by_data_status": dict(by_status),
        "by_market_type": dict(by_market_type),
        "by_venue": dict(by_venue),
        "by_region": dict(by_region),
        "by_quote": dict(by_quote),
        "by_quote_normalization": dict(by_quote_normalization),
        "regional_observation_count": sum(1 for row in observations if row.get("quote") in REGIONAL_FIAT_QUOTES),
        "regional_candidate_count": sum(1 for row in candidates if row.get("quote") in REGIONAL_FIAT_QUOTES),
        "active_paper_review_candidate_count": active_candidate_count,
        "shadow_or_observe_only_candidate_count": shadow_only_candidate_count,
        "candidate_activity": candidate_activity,
        "by_preliminary_route_status": dict(route_status),
        "by_route_blocker": dict(route_blockers),
        "top_dislocations": top_dislocations,
        "reachable_venues": sorted({row["venue"] for row in observations if row.get("data_status") == "reachable"}),
        "blocked_venues": sorted({row["venue"] for row in observations if row.get("data_status") == "blocked"}),
        "degraded_venues": sorted({row["venue"] for row in observations if row.get("data_status") == "degraded"}),
        "depth_enrichment": quality_summary or {},
        "market_testing_progress": depth_summary.get("market_testing_progress", {}),
        "by_quality_status": dict(quality_statuses),
        "known_quality_by_region": _quality_rates(observations, "region"),
        "known_quality_by_venue": _quality_rates(observations, "venue"),
        "known_quality_by_quote": _quality_rates(observations, "quote"),
        "known_quality_by_quote_normalization": _quality_rates(observations, "quote_normalization_status"),
        "starved_venue_coverage": starved_coverage,
        "venue_quota_report": depth_summary.get("venue_quota_report", {}),
        "top_unknown_quality_backlog": unknown_quality_backlog,
        "regional_candidate_gate_counts": dict(regional_candidate_blockers),
        "anomaly_counts": dict(anomaly_counts.most_common()),
        "freshness_age_seconds": _distribution(freshness_values),
        "depth_latency_ms": _distribution(depth_latency_values),
        "buy_slippage_1000_bps": _distribution(buy_slippage_values),
        "sell_slippage_1000_bps": _distribution(sell_slippage_values),
        "expansion_map": expansion_map,
        "candidates_losing_edge_after_costs": [
            {
                "inst_id": row.get("inst_id"),
                "gross_edge_bps": row.get("gross_edge_bps_estimate"),
                "round_trip_cost_bps": row.get("estimated_round_trip_cost_bps"),
                "quality_score": row.get("quality_score"),
            }
            for row in candidates
            if float(row.get("gross_edge_bps_estimate") or 0.0) > 0
            and float(row.get("edge_bps_estimate") or 0.0) <= 0
        ][:20],
    }


def write_outputs(
    observations: list[dict],
    candidates: list[dict],
    settings: dict | None = None,
    quality_summary: dict | None = None,
) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": _utc_now(),
        "mode": (settings or {}).get("mode", "paper"),
        "live_trading_allowed": bool((settings or {}).get("allow_live_trading", False)),
        "summary": summarize(observations, candidates, quality_summary=quality_summary),
        "observations": observations,
        "candidates": candidates,
        "hard_limits": [
            "Public market-data only.",
            "No credentials, account APIs, order APIs, or live trading.",
            "Blocked venues are captured as evidence and do not create executable candidates.",
        ],
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")
    return report


def _markdown(report: dict) -> str:
    summary = report.get("summary", {})
    lines = [
        "# Frontier Crypto Venue Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Live trading allowed: `{report.get('live_trading_allowed')}`",
        f"- Observations: `{summary.get('observation_count', 0)}`",
        f"- Candidates: `{summary.get('candidate_count', 0)}`",
        f"- Active paper-review candidates: `{summary.get('active_paper_review_candidate_count', 0)}`",
        f"- Shadow/observe-only candidates: `{summary.get('shadow_or_observe_only_candidate_count', 0)}`",
        f"- Venues: `{summary.get('venue_count', 0)}`",
        f"- Symbols: `{summary.get('symbol_count', 0)}`",
        f"- Regional observations: `{summary.get('regional_observation_count', 0)}`",
        f"- Regional candidates: `{summary.get('regional_candidate_count', 0)}`",
        f"- Reachable venues: `{', '.join(summary.get('reachable_venues', [])) or 'none'}`",
        f"- Blocked venues: `{', '.join(summary.get('blocked_venues', [])) or 'none'}`",
        f"- Degraded venues: `{', '.join(summary.get('degraded_venues', [])) or 'none'}`",
        "",
        "## Venue Counts",
        "",
    ]
    for venue, count in sorted(summary.get("by_venue", {}).items(), key=lambda item: item[0]):
        lines.append(f"- `{venue}`: `{count}`")
    lines.extend(["", "## Regional Quote Coverage", ""])
    lines.append(f"- Regions: `{summary.get('by_region', {})}`")
    lines.append(f"- Quotes: `{summary.get('by_quote', {})}`")
    lines.append(f"- Quote normalization: `{summary.get('by_quote_normalization', {})}`")
    lines.extend(["", "## Route Blockers", ""])
    blockers = summary.get("by_route_blocker", {})
    if not blockers:
        lines.append("No preliminary route blockers in candidate set.")
    for blocker, count in sorted(blockers.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- `{blocker}`: `{count}`")
    lines.extend(["", "## Executable Data Quality", ""])
    lines.append(f"- Quality statuses: `{summary.get('by_quality_status', {})}`")
    lines.append(f"- Depth enrichment: `{summary.get('depth_enrichment', {})}`")
    lines.append(f"- Freshness: `{summary.get('freshness_age_seconds', {})}`")
    lines.append(f"- Depth latency: `{summary.get('depth_latency_ms', {})}`")
    lines.append(f"- $1,000 buy slippage: `{summary.get('buy_slippage_1000_bps', {})}`")
    lines.append(f"- $1,000 sell slippage: `{summary.get('sell_slippage_1000_bps', {})}`")
    lines.append(f"- Anomalies: `{summary.get('anomaly_counts', {})}`")
    expansion = summary.get("expansion_map", {})
    lines.extend(["", "## Expansion Map", ""])
    lines.append(f"- Known quality rate: `{expansion.get('known_quality_rate')}`")
    lines.append(f"- Known quality target progress: `{expansion.get('known_quality_rate_target_progress')}`")
    lines.append(f"- Quality target escalation: `{expansion.get('quality_target_escalation', {})}`")
    lines.append(f"- Selection limits: `{expansion.get('selection_limits', {})}`")
    lines.append(f"- Worker count: `{expansion.get('worker_count')}`")
    lines.append(f"- Candidate activity: `{expansion.get('candidate_activity', {})}`")
    lines.append(f"- Unknown quality count: `{expansion.get('unknown_quality_count')}`")
    lines.append(f"- Depth selected rate: `{expansion.get('depth_selected_rate')}`")
    lines.append(f"- Depth enriched rate: `{expansion.get('depth_enriched_rate')}`")
    lines.append(f"- Depth selection buckets: `{expansion.get('depth_selection_buckets', {})}`")
    lines.append(f"- Markets tested: `{expansion.get('market_testing_progress', {})}`")
    lines.append(f"- Selected by venue: `{expansion.get('selected_by_venue', {})}`")
    lines.append(f"- Venue quota report: `{expansion.get('venue_quota_report', {})}`")
    lines.append(f"- Route blockers: `{expansion.get('by_route_blocker', {})}`")
    lines.append(f"- Known quality by region: `{expansion.get('known_quality_by_region', {})}`")
    lines.append(f"- Known quality by quote normalization: `{expansion.get('known_quality_by_quote_normalization', {})}`")
    lines.append(f"- Starved venue coverage: `{expansion.get('starved_venue_coverage', {})}`")
    lines.append(f"- Regional candidate gates: `{summary.get('regional_candidate_gate_counts', {})}`")
    lines.extend(["", "## Unknown Quality Backlog", ""])
    backlog = summary.get("top_unknown_quality_backlog", [])
    if not backlog:
        lines.append("No reachable unknown-quality backlog.")
    for row in backlog[:15]:
        lines.append(
            f"- `{row.get('inst_id')}` venue=`{row.get('venue')}` quote=`{row.get('quote')}` "
            f"region=`{row.get('region')}` volume=`{row.get('quote_volume_24h')}` "
            f"norm=`{row.get('quote_normalization_status')}`"
        )
    leaderboard = (summary.get("depth_enrichment") or {}).get("venue_quality_leaderboard", [])
    lines.extend(["", "## Venue Quality Leaderboard", ""])
    if not leaderboard:
        lines.append("No venue quality snapshots yet.")
    for row in leaderboard:
        lines.append(
            f"- `{row.get('venue')}` score=`{row.get('venue_quality_score')}` "
            f"quality=`{row.get('median_instrument_quality')}` reach=`{row.get('reachability_rate')}` "
            f"anomaly_free=`{row.get('anomaly_free_rate')}` latency=`{row.get('median_latency_ms')}`ms"
        )
    lines.extend(["", "## Top Dislocations", ""])
    top = summary.get("top_dislocations", [])
    if not top:
        lines.append("No cross-venue dislocations above the current report cutoff.")
    for row in top:
        lines.append(
            f"- `{row.get('inst_id')}` {row.get('direction')} score=`{row.get('score')}` "
            f"dev=`{row.get('venue_deviation_bps')}`bps edge=`{row.get('edge_bps_estimate')}`bps "
            f"quality=`{row.get('quality_score')}` action=`{row.get('quality_action')}` "
            f"quote_norm=`{row.get('quote_normalization_status')}` "
            f"route=`{row.get('route_status')}` blockers={row.get('route_blockers')}"
        )
    lines.extend(["", "## Venue Health Sample", ""])
    for row in report.get("observations", [])[:60]:
        lines.append(
            f"- `{row['venue']}` `{row['symbol']}` `{row['market_type']}` "
            f"status=`{row['data_status']}` http=`{row['http_status']}` latency=`{row['latency_ms']}`ms "
            f"last=`{row.get('last')}` spread=`{row.get('spread_bps')}`bps volume=`{row.get('quote_volume_24h')}` "
            f"quality=`{row.get('quality_score')}` qstatus=`{row.get('quality_status')}` anomalies={row.get('anomaly_flags', [])}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Scan frontier crypto public venues.")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args(argv)
    from settings import load_settings

    settings = load_settings()
    candidates = build_candidates(settings, limit=args.top)
    print(f"Wrote {REPORT_JSON}")
    print(f"Wrote {REPORT_MD}")
    for row in candidates[: args.top]:
        print(
            f"{row['inst_id']:<28} {row['direction']:<22} score={row['score']:<6} "
            f"edge={row['edge_bps_estimate']:<7} dev={row['venue_deviation_bps']:<8} "
            f"data={row['data_status']} route={(row.get('execution_feasibility') or {}).get('status')}"
        )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
