#!/usr/bin/env python3
"""Paper-only strategy reliability overlay for recurring manual task families.

This layer does not place trades, change credentials, or promote strategies.
It annotates candidates before deterministic review so weak slices can be moved
to shadow/probation while working slices stay visible for expansion trials.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import math
from typing import Any

from storage import RUNS_DIR, signal_key


REPORT_JSON = RUNS_DIR / "strategy_reliability_report.json"
REPORT_MD = RUNS_DIR / "strategy_reliability_report.md"

FRONTIER_REPAIR_VENUES = {"KRAKEN", "COINBASE", "MEXC", "GATE", "BINANCE_US", "BYBIT_SPOT", "KUCOIN"}
FRONTIER_STRICT_LONG_VENUES = {"KRAKEN", "COINBASE", "MEXC", "GATE", "BINANCE_US", "KUCOIN"}
FRONTIER_SHORT_PROBATION_VENUES = {"GATE", "MEXC", "BINANCE_US"}
USD_LIKE_QUOTES = {"usd_like", "same_venue_stablecoin_reference"}
CONTEXT_PENALTY_FLAG_KEYS = (
    "paper_only_context_penalties",
    "paper_only_context_penalty_enabled",
    "paper_context_penalties_enabled",
)
CONTEXT_PENALTY_SCOPES = (
    "paper",
    "paper_policy",
    "strategy_reliability",
)
CRITICAL_ANOMALY_TERMS = {"crossed_book", "locked_book", "one_sided_book", "empty_book", "stale_book", "critical"}

COVERED_IMPROVEMENT_TASK_IDS = [
    68036,
    55857,
    16489,
    13741,
    12494,
    6148,
    4069,
    13116,
    12982,
    8231,
    6164,
    3257,
    2778,
    134818,
    136386,
    136387,
    137407,
]

TASK_STATUS_BY_ID = {
    134818: "implemented_bybit_quality_decay_expansion_pack",
    137407: "implemented_bybit_quality_decay_expansion_pack",
    136386: "implemented_kucoin_long_repair_diagnostics",
    136387: "implemented_kucoin_long_repair_diagnostics",
}

COVERED_GROWTH_EXPERIMENT_IDS = [
    65537,
    65538,
    64906,
    64379,
    64905,
    61227,
    18294,
    16663,
    15555,
    14555,
    14356,
    14082,
    7810,
    5219,
    5217,
    4353,
    2885,
    52673,
    14795,
    14642,
    7826,
    3972,
    3017,
    1836,
    1832,
    63586,
    50186,
    14122,
    5292,
    4748,
    3774,
    2678,
    2609,
    1386,
    449,
    56737,
    2280,
    463,
]


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off", "disabled"}:
        return False
    return default


def _venue(candidate: dict) -> str:
    return str(candidate.get("venue") or candidate.get("source_venue") or "").upper()


def frontier_route_feasibility_record(candidate: dict) -> dict[str, Any]:
    """Expose normalized paper route feasibility for scoring and reporting."""
    try:
        from paper_order_router import frontier_route_feasibility_record as _router_route_feasibility_record
    except Exception:
        status = str(candidate.get("paper_route_status") or candidate.get("route_status") or "unknown")
        return {
            "paper_route_status": status,
            "execution_semantics": str(candidate.get("paper_route_type") or "unknown"),
            "paper_fill_allowed": _as_bool(candidate.get("paper_fill_allowed_by_route"), status != "blocked"),
            "paper_proxy_used": _as_bool(candidate.get("paper_proxy_used"), False),
            "paper_allocation_multiplier": _as_float(candidate.get("paper_allocation_multiplier"), 1.0),
            "route_blockers": list(candidate.get("route_blockers") or []),
            "venue": _venue(candidate),
        }

    record = dict(_router_route_feasibility_record(candidate))
    record.setdefault("venue", _venue(candidate))
    return record


def _route_status(candidate: dict) -> str:
    route_record = frontier_route_feasibility_record(candidate)
    normalized_status = str(route_record.get("paper_route_status") or "").strip()
    if normalized_status and normalized_status != "unknown":
        return normalized_status

    feasibility = candidate.get("execution_feasibility") or {}
    route = candidate.get("execution_route") or {}
    return str(feasibility.get("route_status") or feasibility.get("status") or route.get("route_status") or "unknown")


def _quote_status(candidate: dict) -> str:
    return str(candidate.get("quote_normalization_status") or "unknown")


def _cost_bucket(candidate: dict) -> str:
    cost = _as_float(candidate.get("estimated_round_trip_cost_bps"), 999.0)
    if cost <= 25:
        return "low"
    if cost <= 45:
        return "normal"
    if cost <= 90:
        return "high"
    return "extreme"


def _quality_bucket(candidate: dict) -> str:
    quality = _as_float(candidate.get("quality_score"), -1.0)
    if quality < 0:
        return "unknown"
    if quality >= 75:
        return "high"
    if quality >= 60:
        return "good"
    if quality >= 35:
        return "conditional"
    return "poor"


def _source_count(candidate: dict) -> int:
    return _as_int(candidate.get("source_venue_count") or candidate.get("unique_venue_count"), 0)


def _append_note(candidate: dict, note: str) -> None:
    notes = candidate.setdefault("risk_notes", [])
    if note not in notes:
        notes.append(note)


def _set_score(candidate: dict, delta: float) -> None:
    original = _as_float(candidate.get("score"), 0.0)
    candidate["score"] = round(max(0.0, min(100.0, original + delta)), 3)


def _maybe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _first_present_float(candidate: dict, fields: tuple[str, ...]) -> tuple[str | None, float | None]:
    for field in fields:
        numeric = _maybe_float(candidate.get(field))
        if numeric is not None:
            return field, numeric
    return None, None


def _normalize_fraction(value: float) -> float:
    if value > 1.0:
        value = value / 100.0 if value <= 100.0 else 1.0
    return max(0.0, min(1.0, value))


def _bounded_penalty(value: float) -> float:
    return max(0.85, min(1.0, value))


def _context_penalties_enabled(candidate: dict) -> bool:
    for key in CONTEXT_PENALTY_FLAG_KEYS:
        if key in candidate:
            return _as_bool(candidate.get(key), False)
    for scope in CONTEXT_PENALTY_SCOPES:
        scoped = candidate.get(scope)
        if not isinstance(scoped, dict):
            continue
        for key in CONTEXT_PENALTY_FLAG_KEYS:
            if key in scoped:
                return _as_bool(scoped.get(key), False)
    return False


def _context_penalty_record(candidate: dict) -> dict[str, Any] | None:
    if not _context_penalties_enabled(candidate):
        return None

    terms: dict[str, dict[str, Any]] = {}

    freshness_age_field, freshness_age = _first_present_float(
        candidate,
        (
            "supporting_market_data_age_seconds",
            "supporting_data_age_seconds",
            "context_data_age_seconds",
            "market_context_age_seconds",
            "cross_market_data_age_seconds",
        ),
    )
    freshness_window_field, freshness_window = _first_present_float(
        candidate,
        (
            "supporting_market_refresh_window_seconds",
            "supporting_data_refresh_window_seconds",
            "context_refresh_window_seconds",
            "market_context_refresh_window_seconds",
        ),
    )
    freshness_penalty = 1.0
    if freshness_age is not None and freshness_window is not None and freshness_window > 0.0:
        freshness_penalty = _bounded_penalty(1.0 - 0.15 * min(1.0, max(0.0, (freshness_age / freshness_window) - 1.0)))
    terms["freshness_penalty"] = {
        "multiplier": round(freshness_penalty, 3),
        "age_field": freshness_age_field,
        "age_seconds": round(freshness_age, 3) if freshness_age is not None else None,
        "window_field": freshness_window_field,
        "window_seconds": round(freshness_window, 3) if freshness_window is not None else None,
    }

    regime_vol_field, regime_vol = _first_present_float(
        candidate,
        (
            "realized_volatility",
            "realized_volatility_24h",
            "regime_realized_volatility",
            "cross_market_realized_volatility",
        ),
    )
    regime_threshold_field, regime_threshold = _first_present_float(
        candidate,
        (
            "volatility_stress_threshold",
            "paper_volatility_stress_threshold",
            "regime_stress_threshold",
        ),
    )
    regime_penalty = 1.0
    if regime_vol is not None and regime_threshold is not None and regime_threshold > 0.0:
        regime_penalty = _bounded_penalty(1.0 - 0.15 * min(1.0, max(0.0, (regime_vol / regime_threshold) - 1.0)))
    terms["regime_penalty"] = {
        "multiplier": round(regime_penalty, 3),
        "volatility_field": regime_vol_field,
        "realized_volatility": round(regime_vol, 6) if regime_vol is not None else None,
        "threshold_field": regime_threshold_field,
        "stress_threshold": round(regime_threshold, 6) if regime_threshold is not None else None,
    }

    confirmation_samples: list[float] = []
    confirmation_fields: list[str] = []
    for field in ("correlated_markets_confirmed", "cross_market_confirmed", "correlation_confirmed"):
        if field in candidate:
            confirmation_fields.append(field)
            confirmation_samples.append(1.0 if _as_bool(candidate.get(field), False) else 0.0)
    for field in ("market_breadth_confirmed", "breadth_confirmed"):
        if field in candidate:
            confirmation_fields.append(field)
            confirmation_samples.append(1.0 if _as_bool(candidate.get(field), False) else 0.0)
    for field in ("correlation_confirmation_score", "breadth_confirmation_score", "cross_market_confirmation_score"):
        numeric = _maybe_float(candidate.get(field))
        if numeric is None:
            continue
        confirmation_fields.append(field)
        confirmation_samples.append(_normalize_fraction(numeric))
    confirmation_mean = sum(confirmation_samples) / len(confirmation_samples) if confirmation_samples else None
    confirmation_penalty = 1.0
    if confirmation_mean is not None:
        confirmation_penalty = _bounded_penalty(1.0 - 0.15 * (1.0 - confirmation_mean))
    terms["confirmation_penalty"] = {
        "multiplier": round(confirmation_penalty, 3),
        "evidence_fields": confirmation_fields,
        "confirmation_score": round(confirmation_mean, 6) if confirmation_mean is not None else None,
    }

    concentration_field, concentration_count = _first_present_float(
        candidate,
        (
            "independent_feature_count",
            "supporting_feature_count",
            "signal_feature_count",
            "feature_count",
        ),
    )
    concentration_penalty = 1.0
    if concentration_count is not None and concentration_count > 0.0:
        capped_count = max(1.0, min(3.0, concentration_count))
        concentration_penalty = _bounded_penalty(0.85 + ((capped_count - 1.0) / 2.0) * 0.15)
    terms["concentration_penalty"] = {
        "multiplier": round(concentration_penalty, 3),
        "feature_count_field": concentration_field,
        "independent_feature_count": round(concentration_count, 3) if concentration_count is not None else None,
    }

    total_multiplier = 1.0
    dominant_penalty_reason = None
    dominant_penalty_value = 1.0
    for name, detail in terms.items():
        multiplier = _maybe_float(detail.get("multiplier")) or 1.0
        total_multiplier *= multiplier
        if multiplier < dominant_penalty_value:
            dominant_penalty_value = multiplier
            dominant_penalty_reason = name

    base_score = round(_as_float(candidate.get("score"), 0.0), 3)
    final_score = round(max(0.0, min(100.0, base_score * total_multiplier)), 3)
    return {
        "paper_only": True,
        "enabled": True,
        "applied": total_multiplier < 0.999999,
        "base_score": base_score,
        "final_score": final_score,
        "total_multiplier": round(total_multiplier, 6),
        "dominant_penalty_reason": dominant_penalty_reason,
        "terms": terms,
    }


def _apply_context_penalty(candidate: dict) -> dict[str, Any] | None:
    detail = _context_penalty_record(candidate)
    if detail is None:
        return None
    if "score" in candidate or detail["base_score"] > 0.0:
        candidate["score"] = detail["final_score"]
    candidate["paper_score_context_penalty"] = detail
    if detail.get("applied") and detail.get("dominant_penalty_reason"):
        _append_note(candidate, f"paper_context_penalty:{detail['dominant_penalty_reason']}")
    return detail


def _base_profile(candidate: dict) -> dict:
    seen_at = str(candidate.get("seen_at") or "")
    hour_utc = None
    try:
        hour_utc = dt.datetime.fromisoformat(seen_at.replace("Z", "+00:00")).hour if seen_at else None
    except ValueError:
        hour_utc = None
    return {
        "signal_key": signal_key(candidate),
        "trade_type": candidate.get("trade_type"),
        "inst_id": candidate.get("inst_id"),
        "base": candidate.get("base"),
        "direction": candidate.get("direction"),
        "venue": _venue(candidate),
        "route_status": _route_status(candidate),
        "quality_bucket": _quality_bucket(candidate),
        "quality_score": candidate.get("quality_score"),
        "source_venue_count": _source_count(candidate),
        "cost_bucket": _cost_bucket(candidate),
        "round_trip_cost_bps": candidate.get("estimated_round_trip_cost_bps"),
        "edge_bps_estimate": candidate.get("edge_bps_estimate"),
        "spread_bps": candidate.get("spread_bps"),
        "dislocation_bucket": candidate.get("dislocation_bucket") or _dislocation_bucket(candidate),
        "hour_utc": hour_utc,
        "recent_decay_status": candidate.get("recent_decay_status") or candidate.get("decay_status"),
        "quote_normalization_status": _quote_status(candidate),
    }


def _annotate(
    candidate: dict,
    *,
    profile: str,
    action: str,
    reasons: list[str],
    score_delta: float = 0.0,
    allocation_multiplier: float = 1.0,
    shadow_only: bool = False,
    protect: bool = False,
) -> dict:
    allocation_multiplier = max(0.0, min(1.0, allocation_multiplier))
    reliability = _base_profile(candidate)
    reliability.update(
        {
            "profile": profile,
            "action": action,
            "reasons": reasons,
            "score_delta": round(score_delta, 3),
            "allocation_multiplier": allocation_multiplier,
            "protect_working_slice": bool(protect),
        }
    )
    candidate["strategy_reliability"] = reliability
    candidate["strategy_reliability_action"] = action
    candidate["strategy_reliability_allocation_multiplier"] = allocation_multiplier
    candidate["strategy_reliability_reasons"] = reasons
    if shadow_only:
        candidate["paper_entry_blocked"] = True
        candidate["promotion_eligible"] = False
        candidate.setdefault("candidate_reject_reason", f"{profile}:{action}")
    elif allocation_multiplier < 1.0:
        candidate["quality_action"] = candidate.get("quality_action") or "conditional"
    if score_delta:
        _set_score(candidate, score_delta)
    context_penalty = _apply_context_penalty(candidate)
    if context_penalty is not None:
        reliability["paper_score_context_penalty"] = context_penalty
        candidate["strategy_reliability_context_penalty"] = context_penalty
    candidate["strategy_reliability"] = reliability
    _append_note(candidate, f"strategy_reliability:{profile}:{action}")
    return reliability


def _frontier_reasons(
    candidate: dict,
    *,
    min_quality: float,
    min_sources: int,
    min_edge: float,
    max_cost: float,
    require_book: bool,
    route_statuses: set[str],
    quote_statuses: set[str],
) -> list[str]:
    reasons = []
    quality = _as_float(candidate.get("quality_score"), -1.0)
    edge = _as_float(candidate.get("edge_bps_estimate"), 0.0)
    cost = _as_float(candidate.get("estimated_round_trip_cost_bps"), 999.0)
    sources = _source_count(candidate)
    if quality < min_quality:
        reasons.append(f"quality_score<{min_quality:g}")
    if sources < min_sources:
        reasons.append(f"source_venue_count<{min_sources}")
    if edge < min_edge:
        reasons.append(f"depth_adjusted_edge<{min_edge:g}bps")
    if cost > max_cost:
        reasons.append(f"round_trip_cost>{max_cost:g}bps")
    if require_book and candidate.get("frontier_cost_source") != "public_order_book":
        reasons.append("missing_public_order_book_cost")
    if _route_status(candidate) not in route_statuses:
        reasons.append(f"route_status={_route_status(candidate)}")
    if _quote_status(candidate) not in quote_statuses:
        reasons.append(f"quote_normalization={_quote_status(candidate)}")
    if candidate.get("cross_quote_reference"):
        reasons.append("cross_quote_reference_contamination")
    reasons.extend(_critical_quality_reasons(candidate))
    return reasons


def _dislocation_bucket(candidate: dict) -> str:
    edge = abs(_as_float(candidate.get("edge_bps_estimate"), 0.0))
    if edge >= 100:
        return "extreme"
    if edge >= 50:
        return "large"
    if edge >= 20:
        return "medium"
    if edge > 0:
        return "small"
    return "unknown"


def _anomaly_flags(candidate: dict) -> list[str]:
    raw = candidate.get("anomaly_flags") or candidate.get("quality_anomaly_flags") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(flag).lower() for flag in raw if str(flag).strip()]


def _critical_quality_reasons(candidate: dict) -> list[str]:
    reasons = []
    flags = set(_anomaly_flags(candidate))
    critical = sorted(flag for flag in flags if flag in CRITICAL_ANOMALY_TERMS or flag.startswith("critical"))
    if critical:
        reasons.append(f"critical_anomaly_flags={critical[:5]}")
    if str(candidate.get("quality_status") or "").lower() not in {"verified", "good", ""}:
        reasons.append(f"quality_status={candidate.get('quality_status')}")
    age = _as_float(candidate.get("book_age_seconds") or candidate.get("depth_age_seconds"), 0.0)
    if age > 90.0:
        reasons.append("book_data_stale")
    return reasons


def _decay_reasons(candidate: dict) -> list[str]:
    status = str(candidate.get("recent_decay_status") or candidate.get("decay_status") or "").lower()
    recent_delta = _as_float(candidate.get("recent_60m_uplift_bps") or candidate.get("recent_60m_delta_bps"), 0.0)
    reasons = []
    if status in {"decayed", "deteriorating", "worsening", "below_incumbent"}:
        reasons.append(f"recent_decay_status={status}")
    if recent_delta < -5.0:
        reasons.append(f"recent_60m_delta_bps={recent_delta:g}")
    return reasons


def _repair_frontier_candidate(candidate: dict) -> dict | None:
    venue = _venue(candidate)
    direction = str(candidate.get("direction") or "")
    if venue not in FRONTIER_REPAIR_VENUES:
        return None

    if direction == "long_frontier_spot" and venue == "BYBIT_SPOT":
        reasons = _frontier_reasons(
            candidate,
            min_quality=75.0,
            min_sources=3,
            min_edge=8.0,
            max_cost=35.0,
            require_book=True,
            route_statuses={"standard"},
            quote_statuses=USD_LIKE_QUOTES,
        )
        decay_reasons = _decay_reasons(candidate)
        if reasons or decay_reasons:
            return _annotate(
                candidate,
                profile="bybit_quality_decay_expansion_pack",
                action="bybit_shadow_until_quality_decay_gate",
                reasons=[*reasons, *decay_reasons] or ["BYBIT long awaits quality/decay evidence"],
                score_delta=-8.0,
                allocation_multiplier=0.0,
                shadow_only=True,
            )
        return _annotate(
            candidate,
            profile="bybit_quality_decay_expansion_pack",
            action="bybit_probation_quality_expansion",
            reasons=["BYBIT long passed quality, source-count, route, cost, anomaly, and decay gates"],
            score_delta=-1.0,
            allocation_multiplier=0.25,
        )

    if direction == "long_frontier_spot" and venue == "KUCOIN":
        reasons = _frontier_reasons(
            candidate,
            min_quality=80.0,
            min_sources=4,
            min_edge=12.0,
            max_cost=30.0,
            require_book=True,
            route_statuses={"standard"},
            quote_statuses=USD_LIKE_QUOTES,
        )
        if reasons:
            return _annotate(
                candidate,
                profile="kucoin_long_repair_diagnostics",
                action="kucoin_shadow_diagnostic_gate",
                reasons=reasons,
                score_delta=-14.0,
                allocation_multiplier=0.0,
                shadow_only=True,
            )
        return _annotate(
            candidate,
            profile="kucoin_long_repair_diagnostics",
            action="kucoin_small_recovery_probe",
            reasons=["KUCOIN long passed strict diagnostic recovery gate"],
            score_delta=-5.0,
            allocation_multiplier=0.1,
        )

    if direction == "long_frontier_spot" and venue in FRONTIER_STRICT_LONG_VENUES:
        reasons = _frontier_reasons(
            candidate,
            min_quality=75.0,
            min_sources=4,
            min_edge=10.0,
            max_cost=35.0,
            require_book=True,
            route_statuses={"standard"},
            quote_statuses={"usd_like"},
        )
        if reasons:
            return _annotate(
                candidate,
                profile="frontier_long_repair",
                action="shadow_only_long_strict_gate",
                reasons=reasons,
                score_delta=-18.0,
                allocation_multiplier=0.0,
                shadow_only=True,
            )
        return _annotate(
            candidate,
            profile="frontier_long_repair",
            action="strict_long_recovery_probe",
            reasons=["long signal passed strict venue/direction repair gate"],
            score_delta=-4.0,
            allocation_multiplier=0.25,
        )

    if direction == "short_frontier_spot" and venue in FRONTIER_SHORT_PROBATION_VENUES:
        reasons = _frontier_reasons(
            candidate,
            min_quality=60.0 if venue == "GATE" else 65.0,
            min_sources=3,
            min_edge=6.0 if venue == "GATE" else 8.0,
            max_cost=45.0,
            require_book=True,
            route_statuses={"standard", "conditional"},
            quote_statuses=USD_LIKE_QUOTES,
        )
        if reasons:
            return _annotate(
                candidate,
                profile="frontier_short_repair",
                action="shadow_only_short_probation_gate",
                reasons=reasons,
                score_delta=-10.0,
                allocation_multiplier=0.0,
                shadow_only=True,
            )
        return _annotate(
            candidate,
            profile="frontier_short_repair",
            action="probation_short_expansion",
            reasons=["short signal passed quality/depth/route probation gate"],
            score_delta=-2.0,
            allocation_multiplier=0.25,
        )
    return None


def _okx_basis_gate(candidate: dict) -> list[str]:
    direction = str(candidate.get("direction") or "")
    basis = _as_float(candidate.get("basis_bps"), 0.0)
    funding = _as_float(candidate.get("funding_bps"), 0.0)
    spread = _as_float(candidate.get("spread_bps"), 999.0)
    liquidity = _as_float(candidate.get("liquidity_score"), 0.0)
    move = abs(_as_float(candidate.get("change_24h_pct"), 0.0))
    reasons = []
    if abs(basis) < 75.0:
        reasons.append("basis_not_extreme")
    if move > 20.0:
        reasons.append("momentum_not_cooling")
    if spread > 4.0:
        reasons.append("spread_not_normal")
    if liquidity < 0.45:
        reasons.append("liquidity_below_regime_gate")
    if direction.endswith("short_perp") and basis > 0 and funding > 3.0:
        reasons.append("funding_reinforces_positive_basis")
    if direction.endswith("long_perp") and basis < 0 and funding < -3.0:
        reasons.append("funding_reinforces_negative_basis")
    return reasons


def _repair_okx_candidate(candidate: dict) -> dict | None:
    direction = str(candidate.get("direction") or "")
    if candidate.get("trade_type") != "perp_funding_basis":
        return None

    funding = abs(_as_float(candidate.get("funding_bps"), 0.0))
    basis = abs(_as_float(candidate.get("basis_bps"), 0.0))
    spread = _as_float(candidate.get("spread_bps"), 999.0)
    liquidity = _as_float(candidate.get("liquidity_score"), 0.0)
    route_status = _route_status(candidate)

    if direction in {"funding_capture_short_perp", "funding_capture_long_perp"}:
        if funding >= 3.0 and spread <= 4.0 and liquidity >= 0.45:
            return _annotate(
                candidate,
                profile="okx_funding_capture",
                action="protect_working_funding_slice",
                reasons=["funding magnitude, spread, and liquidity agree"],
                score_delta=3.0,
                allocation_multiplier=1.0,
                protect=True,
            )
        return _annotate(
            candidate,
            profile="okx_funding_capture",
            action="funding_capture_observe",
            reasons=["funding capture lacks full protected-slice evidence"],
            score_delta=-2.0,
            allocation_multiplier=0.5,
        )

    if direction == "short_perp_long_spot":
        if (funding >= 3.0 or basis >= 50.0) and spread <= 5.0 and liquidity >= 0.35:
            return _annotate(
                candidate,
                profile="okx_cash_and_carry",
                action="protected_high_edge_short_perp_long_spot",
                reasons=["high-edge protected cash-and-carry slice"],
                score_delta=1.0,
                allocation_multiplier=0.5,
                protect=True,
            )
        return _annotate(
            candidate,
            profile="okx_cash_and_carry",
            action="cash_and_carry_probation",
            reasons=["cash-and-carry edge not strong enough for full allocation"],
            score_delta=-5.0,
            allocation_multiplier=0.25,
        )

    if direction == "long_perp_short_spot":
        reasons = []
        if route_status != "standard":
            reasons.append(f"borrow_or_route_status={route_status}")
        if funding < 8.0 and basis < 75.0:
            reasons.append("reverse_basis_not_extreme")
        if reasons:
            return _annotate(
                candidate,
                profile="okx_reverse_basis",
                action="reverse_basis_shadow_only",
                reasons=reasons,
                score_delta=-12.0,
                allocation_multiplier=0.0,
                shadow_only=True,
            )
        return _annotate(
            candidate,
            profile="okx_reverse_basis",
            action="capped_reverse_basis_recovery",
            reasons=["reverse basis passed extreme evidence and route gate"],
            score_delta=-4.0,
            allocation_multiplier=0.25,
        )

    if direction.startswith("basis_mean_reversion"):
        reasons = _okx_basis_gate(candidate)
        if reasons:
            return _annotate(
                candidate,
                profile="okx_basis_mean_reversion",
                action="basis_regime_shadow_only",
                reasons=reasons,
                score_delta=-18.0,
                allocation_multiplier=0.0,
                shadow_only=True,
            )
        return _annotate(
            candidate,
            profile="okx_basis_mean_reversion",
            action="capped_basis_regime_probe",
            reasons=["basis is extreme, momentum is cooling, spread/funding regime is acceptable"],
            score_delta=-6.0,
            allocation_multiplier=0.25,
        )
    return None


def _repair_proxy_candidate(candidate: dict) -> dict | None:
    if candidate.get("trade_type") not in {"global_proxy_momentum", "global_market_discovery_proxy"}:
        return None
    direction = str(candidate.get("direction") or "")
    move = _as_float(candidate.get("change_24h_pct"), 0.0)
    short_return = _as_float(candidate.get("short_return_pct"), move)
    edge = _as_float(candidate.get("edge_bps_estimate"), 0.0)
    spread = _as_float(candidate.get("spread_bps"), 999.0)
    liquidity = _as_float(candidate.get("liquidity_score"), 0.0)
    stale = _as_float(candidate.get("stale_minutes"), 0.0)

    if direction == "short_proxy":
        reasons = []
        if move > -2.0 and short_return > -1.0:
            reasons.append("short_proxy_needs_stronger_reversal")
        if edge < 8.0:
            reasons.append("short_proxy_edge_below_confirmation")
        if spread > 6.0:
            reasons.append("short_proxy_cost_too_high")
        if liquidity < 0.65:
            reasons.append("short_proxy_liquidity_weak")
        if stale > 60.0:
            reasons.append("short_proxy_data_stale")
        if reasons:
            return _annotate(
                candidate,
                profile="yahoo_proxy_short_repair",
                action="short_proxy_shadow_confirmation",
                reasons=reasons,
                score_delta=-12.0,
                allocation_multiplier=0.0,
                shadow_only=True,
            )
        return _annotate(
            candidate,
            profile="yahoo_proxy_short_repair",
            action="short_proxy_confirmation_probe",
            reasons=["short proxy passed reversal, edge, cost, liquidity, and freshness gates"],
            score_delta=-3.0,
            allocation_multiplier=0.25,
        )

    if direction == "long_proxy":
        reasons = ["long proxy remains eligible; track regime/hour/instrument context before expansion"]
        if spread > 6.0 or stale > 60.0:
            reasons.append("long_proxy_quality_reduced_allocation")
            return _annotate(
                candidate,
                profile="yahoo_proxy_long_context",
                action="long_proxy_context_probation",
                reasons=reasons,
                score_delta=-2.0,
                allocation_multiplier=0.5,
            )
        return _annotate(
            candidate,
            profile="yahoo_proxy_long_context",
            action="long_proxy_context_tracking",
            reasons=reasons,
            score_delta=0.0,
            allocation_multiplier=1.0,
            protect=True,
        )
    return None


def _apply_one(candidate: dict) -> dict | None:
    trade_type = candidate.get("trade_type")
    if trade_type == "frontier_crypto_venue_map":
        return _repair_frontier_candidate(candidate)
    if trade_type == "perp_funding_basis":
        return _repair_okx_candidate(candidate)
    if trade_type in {"global_proxy_momentum", "global_market_discovery_proxy"}:
        return _repair_proxy_candidate(candidate)
    return None


def _summarize(items: list[dict], candidates: list[dict]) -> dict:
    by_action = collections.Counter(item["action"] for item in items)
    by_profile = collections.Counter(item["profile"] for item in items)
    by_signal = collections.Counter(item["signal_key"] for item in items)
    by_direction = collections.Counter(item["direction"] for item in items)
    by_venue = collections.Counter(item["venue"] for item in items if item.get("venue"))
    blocked = sum(1 for item in items if item["action"].startswith("shadow") or "shadow" in item["action"])
    protected = sum(1 for item in items if item.get("protect_working_slice"))
    return {
        "candidate_count": len(candidates),
        "annotated_count": len(items),
        "shadow_or_blocked_count": blocked,
        "protected_working_slice_count": protected,
        "by_action": dict(by_action),
        "by_profile": dict(by_profile),
        "by_signal": dict(by_signal.most_common(20)),
        "by_direction": dict(by_direction),
        "by_venue": dict(by_venue),
        "manual_repair_focus": _manual_repair_focus(items),
    }


def _manual_repair_focus(items: list[dict]) -> dict:
    focus_profiles = {"bybit_quality_decay_expansion_pack", "kucoin_long_repair_diagnostics"}
    rows = [item for item in items if item.get("profile") in focus_profiles]
    by_profile: dict[str, dict] = {}
    for profile in focus_profiles:
        selected = [item for item in rows if item.get("profile") == profile]
        by_profile[profile] = {
            "count": len(selected),
            "by_action": dict(collections.Counter(item.get("action") for item in selected)),
            "by_quality_bucket": dict(collections.Counter(item.get("quality_bucket") for item in selected)),
            "by_cost_bucket": dict(collections.Counter(item.get("cost_bucket") for item in selected)),
            "by_source_count": dict(collections.Counter(str(item.get("source_venue_count")) for item in selected)),
            "by_dislocation_bucket": dict(collections.Counter(item.get("dislocation_bucket") for item in selected)),
            "by_hour_utc": dict(collections.Counter(str(item.get("hour_utc")) for item in selected if item.get("hour_utc") is not None)),
            "top_instruments": [
                {
                    "inst_id": item.get("inst_id"),
                    "base": item.get("base"),
                    "action": item.get("action"),
                    "quality_score": item.get("quality_score"),
                    "source_venue_count": item.get("source_venue_count"),
                    "edge_bps_estimate": item.get("edge_bps_estimate"),
                    "round_trip_cost_bps": item.get("round_trip_cost_bps"),
                    "reasons": item.get("reasons"),
                }
                for item in selected[:15]
            ],
            "classification": _manual_repair_classification(profile, selected),
        }
    return by_profile


def _manual_repair_classification(profile: str, items: list[dict]) -> str:
    if not items:
        return "no_current_candidates"
    actions = collections.Counter(item.get("action") for item in items)
    if profile == "bybit_quality_decay_expansion_pack":
        if actions.get("bybit_probation_quality_expansion"):
            return "conditional_expand_quality_slice_only"
        return "shadow_until_quality_or_decay_evidence_improves"
    if actions.get("kucoin_small_recovery_probe"):
        return "rare_strict_recovery_probe_available"
    return "rare_winners_vs_noisy_entries_shadow_diagnostics"


def _cleanup_manual_tasks(conn: Any | None) -> dict:
    if conn is None:
        return {"enabled": False, "updated": 0, "statuses": {}}
    updated_by_status: dict[str, int] = {}
    for task_id, status in TASK_STATUS_BY_ID.items():
        try:
            cur = conn.execute(
                "update improvement_tasks set status = ? where id = ? and status = 'open'",
                (status, task_id),
            )
        except Exception:  # noqa: BLE001
            continue
        if cur.rowcount:
            updated_by_status[status] = updated_by_status.get(status, 0) + int(cur.rowcount)
    try:
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    return {"enabled": True, "updated": sum(updated_by_status.values()), "statuses": updated_by_status}


def _report_markdown(report: dict) -> str:
    summary = report.get("summary", {})
    lines = [
        "# Strategy Reliability Report",
        "",
        "Paper-only venue/direction reliability layer for the recurring manual self-improvement queue.",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Candidates reviewed: `{summary.get('candidate_count', 0)}`",
        f"- Annotated candidates: `{summary.get('annotated_count', 0)}`",
        f"- Shadow/blocked by reliability layer: `{summary.get('shadow_or_blocked_count', 0)}`",
        f"- Protected working slices: `{summary.get('protected_working_slice_count', 0)}`",
        f"- Actions: `{summary.get('by_action', {})}`",
        f"- Profiles: `{summary.get('by_profile', {})}`",
        f"- Manual repair focus: `{summary.get('manual_repair_focus', {})}`",
        "",
        "## Top Adjustments",
        "",
    ]
    adjustments = report.get("top_adjustments", [])
    if not adjustments:
        lines.append("No strategy-reliability adjustments this loop.")
    for item in adjustments[:20]:
        lines.append(
            f"- `{item.get('signal_key')}` `{item.get('direction')}` `{item.get('inst_id')}` "
            f"action=`{item.get('action')}` allocation=`{item.get('allocation_multiplier')}` "
            f"score_delta=`{item.get('score_delta')}` reasons={item.get('reasons')}"
        )
    lines.extend(
        [
            "",
            "## Covered Manual Queue",
            "",
            f"- Improvement tasks: `{report.get('covered_improvement_task_ids', [])}`",
            f"- Growth experiments: `{report.get('covered_growth_experiment_ids', [])}`",
            f"- Task cleanup: `{report.get('task_cleanup', {})}`",
            "",
            "## Hard Limits",
            "",
            "- Paper-only candidate annotation.",
            "- No live trading, credentials, broker writes, dependency installation, or startup changes.",
            "- Promotions still require the existing reliable-label gates.",
        ]
    )
    return "\n".join(lines) + "\n"


def apply_strategy_reliability(
    candidates: list[dict],
    settings: dict | None = None,
    conn: Any | None = None,
) -> tuple[list[dict], dict]:
    """Annotate candidates with bounded paper-only reliability controls."""

    if settings is not None and not settings.get("strategy_reliability", {}).get("enabled", True):
        return candidates, {"enabled": False, "generated_at": _utc_now(), "summary": {"candidate_count": len(candidates)}}

    adjusted = []
    for candidate in candidates:
        reliability = _apply_one(candidate)
        if reliability:
            adjusted.append(reliability)
    candidates.sort(key=lambda row: row.get("score", 0), reverse=True)

    top_adjustments = sorted(
        adjusted,
        key=lambda row: (abs(_as_float(row.get("score_delta"))), row.get("signal_key", "")),
        reverse=True,
    )[:50]
    report = {
        "enabled": True,
        "generated_at": _utc_now(),
        "summary": _summarize(adjusted, candidates),
        "top_adjustments": top_adjustments,
        "covered_improvement_task_ids": COVERED_IMPROVEMENT_TASK_IDS,
        "covered_growth_experiment_ids": COVERED_GROWTH_EXPERIMENT_IDS,
        "task_cleanup": _cleanup_manual_tasks(conn),
        "hard_limits": [
            "paper_only",
            "no_live_trading",
            "no_credentials",
            "promotion_requires_existing_reliable_label_gates",
        ],
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_report_markdown(report), encoding="utf-8")
    return candidates, report
