#!/usr/bin/env python3
"""Paper-only strategy reliability overlay for recurring manual task families.

This layer does not place trades, change credentials, or promote strategies.
It annotates candidates before deterministic review so weak slices can be moved
to shadow/probation while working slices stay visible for expansion trials.
"""

from __future__ import annotations

import collections
from collections.abc import Mapping
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

PAPER_FAMILY_QUARANTINE_FLAG_KEYS = (
    "paper_strategy_family_quarantine_enabled",
    "paper_family_quarantine_enabled",
    "strategy_family_quarantine_enabled",
)
PAPER_FAMILY_QUARANTINE_SCOPES = (
    "paper",
    "paper_policy",
    "strategy_reliability",
)
PAPER_MODE_CONFIG_KEYS = (
    "mode",
    "runtime_mode",
    "execution_mode",
    "trading_mode",
)
PAPER_MODE_VALUES = {"paper", "paper_only", "research", "simulation", "sim", "dry_run", "dryrun", "backtest"}
LIVE_MODE_VALUES = {"live", "production", "prod", "real", "broker"}
PAPER_CONTEXT_PROMOTION_FLAG_KEYS = (
    "paper_context_promotion_guard_enabled",
    "paper_cross_surface_scope_guard_enabled",
    "paper_scope_validator_enabled",
)
PAPER_CONTEXT_PROMOTION_SCOPES = (
    "paper",
    "paper_policy",
    "paper_runtime",
    "strategy_reliability",
)
_PAPER_CONTEXT_PROMOTION_SOURCE_FIELDS = (
    "promotion_source_context",
    "source_context",
    "lineage_source_context",
    "origin_context",
    "recommendation_context",
    "strategy_context_source",
    "paper_lineage_source_context",
)
_PAPER_CONTEXT_PROMOTION_RULE_FIELDS = (
    "promotion_compatibility_rule",
    "compatibility_rule",
    "cross_surface_compatibility_rule",
    "cross_context_compatibility_rule",
)


def _paper_context_promotion_guard_enabled(config: Mapping[str, Any] | bool | None = None) -> bool:
    if isinstance(config, bool):
        return config
    if not isinstance(config, Mapping):
        return True

    for key in PAPER_MODE_CONFIG_KEYS:
        mode = str(config.get(key) or "").strip().lower()
        if mode in LIVE_MODE_VALUES:
            return False

    for key in PAPER_CONTEXT_PROMOTION_FLAG_KEYS:
        if key in config:
            return _as_bool(config.get(key), True)

    for scope in PAPER_CONTEXT_PROMOTION_SCOPES:
        scoped = config.get(scope)
        if not isinstance(scoped, Mapping):
            continue
        for key in PAPER_MODE_CONFIG_KEYS:
            mode = str(scoped.get(key) or "").strip().lower()
            if mode in LIVE_MODE_VALUES:
                return False
        for key in PAPER_CONTEXT_PROMOTION_FLAG_KEYS:
            if key in scoped:
                return _as_bool(scoped.get(key), True)
    return True


def _paper_context_value(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text or None


def _coerce_context_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if isinstance(parsed, Mapping):
                return dict(parsed)
    return None


def _paper_context_bucket(candidate: Mapping[str, Any]) -> dict[str, str]:
    trade_family = (
        candidate.get("trade_family")
        or candidate.get("signal_family")
        or candidate.get("trade_type")
    )
    bucket = {
        "venue": _paper_context_value(candidate.get("venue")),
        "direction": _paper_context_value(candidate.get("direction")),
        "trade_family": _paper_context_value(trade_family),
        "market_surface": _paper_context_value(candidate.get("market_surface")),
        "market_key": _paper_context_value(candidate.get("market_key")),
    }
    return {field: value for field, value in bucket.items() if value}


def _paper_context_source_context(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    for field in _PAPER_CONTEXT_PROMOTION_SOURCE_FIELDS:
        record = _coerce_context_mapping(candidate.get(field))
        if record:
            record.setdefault("context_source_field", field)
            return record

    for container_field in ("paper_lineage_context", "lineage_context", "recommendation_lineage"):
        container = candidate.get(container_field)
        if not isinstance(container, Mapping):
            continue
        for field in ("source_context",) + _PAPER_CONTEXT_PROMOTION_SOURCE_FIELDS:
            record = _coerce_context_mapping(container.get(field))
            if record:
                record.setdefault("context_source_field", f"{container_field}.{field}")
                return record
    return None


def _paper_context_compatibility_rule(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    for field in _PAPER_CONTEXT_PROMOTION_RULE_FIELDS:
        value = candidate.get(field)
        if value in (None, "", False):
            continue
        if isinstance(value, Mapping):
            record = dict(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            if text[:1] in "[{":
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError, json.JSONDecodeError):
                    record = {"rule": text}
                else:
                    if isinstance(parsed, Mapping):
                        record = dict(parsed)
                    elif isinstance(parsed, list):
                        record = {"fields": [str(item) for item in parsed if item not in (None, "")]}
                    else:
                        record = {"rule": text}
            else:
                record = {"rule": text}
        elif isinstance(value, (list, tuple, set)):
            record = {"fields": [str(item) for item in value if item not in (None, "")]}
        elif value is True:
            record = {"allowed": True}
        else:
            continue
        record.setdefault("rule_source_field", field)
        return record
    return None


def _paper_context_rule_allows(rule: Mapping[str, Any] | None, mismatched_fields: list[str]) -> bool:
    if not isinstance(rule, Mapping) or not mismatched_fields:
        return False

    control_values = (
        rule.get("allow_cross_context"),
        rule.get("allow_promotion"),
        rule.get("compatible"),
        rule.get("allowed"),
        rule.get("enabled"),
    )
    explicit_allow = any(_as_bool(value, False) for value in control_values if value is not None)
    if not explicit_allow and all(value is None for value in control_values):
        explicit_allow = True

    allowed_fields: set[str] = set()
    for field_name in ("fields", "compatible_fields", "dimensions", "scopes"):
        values = rule.get(field_name)
        if isinstance(values, str):
            values = [part.strip() for part in values.replace("|", ",").split(",")]
        if not isinstance(values, (list, tuple, set)):
            continue
        for value in values:
            normalized = _paper_context_value(value)
            if normalized:
                allowed_fields.add(normalized)
    return explicit_allow and (not allowed_fields or set(mismatched_fields).issubset(allowed_fields))


def paper_context_promotion_guard_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    if not isinstance(candidate, Mapping) or not _paper_context_promotion_guard_enabled(config):
        return None

    source_context = _paper_context_source_context(candidate)
    if not source_context:
        return None

    source_bucket = _paper_context_bucket(source_context)
    if not source_bucket:
        return None

    destination_bucket = _paper_context_bucket(candidate)
    compared_fields = [field for field in ("venue", "direction", "trade_family", "market_surface", "market_key") if source_bucket.get(field)]
    if not compared_fields:
        return None

    matching_fields: list[str] = []
    mismatched_fields: list[str] = []
    for field in compared_fields:
        if destination_bucket.get(field) == source_bucket.get(field):
            matching_fields.append(field)
        else:
            mismatched_fields.append(field)

    compatibility_rule = _paper_context_compatibility_rule(candidate)
    allowed_by_rule = _paper_context_rule_allows(compatibility_rule, mismatched_fields)
    eligible = not mismatched_fields or allowed_by_rule
    return {
        "guard": "paper_context_promotion_scope",
        "reason": None if eligible else "paper_context_promotion_mismatch",
        "paper_only": True,
        "eligible": eligible,
        "promotion_blocked": bool(mismatched_fields) and not allowed_by_rule,
        "compatibility_rule_logged": compatibility_rule is not None,
        "compatibility_rule": compatibility_rule,
        "source_context": source_bucket,
        "destination_context": destination_bucket,
        "matching_fields": matching_fields,
        "mismatched_fields": mismatched_fields,
        "paper_score_multiplier": 1.0 if eligible else 0.0,
        "paper_fill_allowed": eligible,
    }


def _paper_family_quarantine_enabled(config: Mapping[str, Any] | bool | None = None) -> bool:
    if isinstance(config, bool):
        return config
    if not isinstance(config, Mapping):
        return True

    for key in PAPER_FAMILY_QUARANTINE_FLAG_KEYS:
        if key in config:
            return _as_bool(config.get(key), True)

    for scope in PAPER_FAMILY_QUARANTINE_SCOPES:
        scoped = config.get(scope)
        if not isinstance(scoped, Mapping):
            continue
        for key in PAPER_FAMILY_QUARANTINE_FLAG_KEYS:
            if key in scoped:
                return _as_bool(scoped.get(key), True)
    return True


def _paper_family_quarantine_applies_in_context(config: Mapping[str, Any] | bool | None = None) -> bool:
    if isinstance(config, bool) or not isinstance(config, Mapping):
        return True

    containers: list[Mapping[str, Any]] = [config]
    for scope in PAPER_FAMILY_QUARANTINE_SCOPES:
        scoped = config.get(scope)
        if isinstance(scoped, Mapping):
            containers.append(scoped)

    saw_explicit_mode = False
    for container in containers:
        for key in PAPER_MODE_CONFIG_KEYS:
            raw_mode = container.get(key)
            if raw_mode in (None, ""):
                continue
            saw_explicit_mode = True
            normalized = str(raw_mode).strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in LIVE_MODE_VALUES:
                return False
            if normalized in PAPER_MODE_VALUES:
                return True
    return not saw_explicit_mode or True


def _paper_family_quarantine_match(candidate: Mapping[str, Any]) -> tuple[str, str] | None:
    texts: list[str] = []
    for field in (
        "market_key",
        "market_surface",
        "signal_family",
        "signal_key",
        "strategy",
        "strategy_id",
        "variant",
        "variant_id",
        "context_key",
        "trade_type",
    ):
        value = candidate.get(field)
        if value not in (None, ""):
            texts.append(str(value).strip().lower())

    try:
        derived_signal_key = signal_key(candidate)
    except Exception:
        derived_signal_key = None
    if derived_signal_key:
        texts.append(str(derived_signal_key).strip().lower())

    combined = " | ".join(texts)
    for descendant_key in QUARANTINED_DESCENDANT_KEYS:
        normalized = str(descendant_key).strip().lower()
        if normalized and normalized in combined:
            return ("descendant_key", normalized)

    for lineage_terms in QUARANTINED_BASE_LINEAGE_TERMS:
        normalized_terms = tuple(str(term).strip().lower() for term in lineage_terms if str(term).strip())
        if normalized_terms and all(term in combined for term in normalized_terms):
            return ("base_lineage", "|".join(normalized_terms))
    return None


def paper_family_quarantine_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    if not _paper_family_quarantine_enabled(config) or not _paper_family_quarantine_applies_in_context(config):
        return None
    if not isinstance(candidate, Mapping):
        return None
    matched = _paper_family_quarantine_match(candidate)
    if matched is None:
        return None
    match_type, matched_value = matched
    return {
        "reason": "paper_strategy_family_quarantine",
        "guard": "paper_strategy_family_quarantine",
        "paper_only": True,
        "eligible": False,
        "paper_fill_allowed": False,
        "paper_score_multiplier": 0.0,
        "paper_allocation_multiplier": 0.0,
        "quarantine_action": "monitor_only",
        "family_key": "YAHOO_PROXY|global_proxy_momentum",
        "reentry_rule": (
            "Keep quarantined until rolling paper performance is positive on both long and short branches "
            "with materially improved win rate and sample depth."
        ),
        "matched_on": {"type": match_type, "value": matched_value},
        "evidence": {
            "long_proxy_standard_bps": -16.225,
            "long_proxy_standard_closes": 176,
            "long_proxy_standard_win_rate_pct": 31.8,
            "short_proxy_conditional_bps": -24.614,
            "short_proxy_conditional_closes": 171,
            "short_proxy_conditional_win_rate_pct": 32.2,
            "short_proxy_standard_bps": -72.504,
            "short_proxy_standard_closes": 7,
        },
    }

QUARANTINED_BASE_LINEAGE_TERMS = (("yahoo_proxy", "global_proxy_momentum"),)
QUARANTINED_DESCENDANT_KEYS = {
    "YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
    "yahoo_proxy_global_proxy_momentum",
    "YAHOO_PROXY|GLOBAL_PROXY_MOMENTUM",
    "yahoo_proxy|global_proxy_momentum",
    "strategy_lab|gate_yahoo_momentum_to_fresh_tight_high_quality_proxies_3342a7f1",
    "strategy_lab|red_team_yahoo_proxy_momentum_sanity_check_c6d14fc0",
    "strategy_lab|route_rich_frontier_long_filter_2942c975",
    "gate_yahoo_momentum_to_fresh_tight_high_quality_proxies_3342a7f1",
    "red_team_yahoo_proxy_momentum_sanity_check_c6d14fc0",
    "route_rich_frontier_long_filter_2942c975",
}
QUARANTINE_RELEASE_CONDITION = (
    "Only lift quarantine for a new candidate if it is not lineage-derived from the "
    "quarantined family and it demonstrates fresh positive paper results across "
    "independent horizons with stable sign and without relying on Yahoo proxy "
    "momentum inputs."
)

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

PAPER_CELL_SCOPE_FLAG_KEYS = (
    "paper_cell_policy",
    "paper_only_cell_policy",
    "strategy_reliability_cell_policy",
)
PAPER_CELL_SCOPE_SCOPES = (
    "paper",
    "paper_policy",
    "strategy_reliability",
)

PAPER_LINEAGE_ID_FIELDS = (
    "lineage_id",
    "strategy_lineage_id",
    "lineage_key",
    "strategy_lineage_key",
    "family_lineage_id",
)
PAPER_LINEAGE_OBSERVATION_COUNT_FIELDS = (
    "target_context_paper_observation_count",
    "paper_context_observation_count",
    "paper_observation_count",
    "completed_paper_trades",
    "paper_trade_count",
)
PAPER_LINEAGE_WIN_COUNT_FIELDS = (
    "target_context_paper_win_count",
    "paper_context_win_count",
    "paper_win_count",
    "completed_paper_wins",
    "paper_positive_outcome_count",
)
PAPER_HOLDING_PROFILE_FIELDS = (
    "holding_profile",
    "paper_holding_profile",
    "holding_period_profile",
    "holding_period_bucket",
    "horizon_profile",
)

PAPER_FRONTIER_EXECUTION_QUALITY_FLAG_KEYS = (
    "paper_frontier_execution_quality_gate_enabled",
    "paper_frontier_execution_quality_enabled",
    "frontier_execution_quality_gate_enabled",
)
PAPER_FRONTIER_EXECUTION_QUALITY_SCOPES = (
    "paper",
    "paper_policy",
    "strategy_reliability",
    "paper_order_router",
    "frontier",
)
PAPER_FRONTIER_EXECUTION_QUALITY_MARKETS = {"OKX_SPOT"}
PAPER_FRONTIER_ROUTE_RICHNESS_MIN = 2
PAPER_FRONTIER_SPREAD_BPS_MAX = 12.0
PAPER_FRONTIER_FRESHNESS_SECONDS_MAX = 20.0
PAPER_FRONTIER_LIQUIDITY_SCORE_MIN = 0.5
PAPER_FRONTIER_DEPTH_USD_MIN = 25000.0


OKX_BASIS_PAPER_CARRY_FLAG_KEYS = (
    "paper_okx_basis_carry_gate_enabled",
    "okx_basis_paper_carry_gate_enabled",
    "paper_okx_basis_carry_enabled",
)
OKX_BASIS_PAPER_CARRY_SCOPES = (
    "paper",
    "paper_policy",
    "strategy_reliability",
    "paper_order_router",
)
OKX_BASIS_PAPER_TARGET_FIELDS = (
    "market_key",
    "market_surface",
    "signal_key",
    "trade_type",
    "strategy",
    "strategy_id",
    "variant",
    "variant_id",
    "context_key",
    "notes",
    "thesis",
)
OKX_BASIS_FUNDING_FIELDS = (
    "net_funding_bps",
    "net_funding_edge_bps",
    "expected_net_funding_bps",
    "forward_net_funding_bps",
    "funding_edge_bps",
    "carry_edge_bps",
    "expected_carry_bps",
    "expected_funding_bps",
    "forward_funding_bps",
    "forecast_funding_bps",
    "funding_rate_bps",
)
OKX_BASIS_EXPLICIT_NET_FIELDS = (
    "net_funding_bps",
    "net_funding_edge_bps",
    "expected_net_funding_bps",
    "forward_net_funding_bps",
    "net_edge_bps_estimate",
)
OKX_BASIS_ROUND_TRIP_COST_FIELDS = (
    "estimated_round_trip_cost_bps",
    "round_trip_cost_bps",
    "total_cost_bps",
    "estimated_total_cost_bps",
)
OKX_BASIS_FEE_FIELDS = (
    "estimated_fee_bps",
    "fee_bps",
    "fees_bps",
)
OKX_BASIS_SLIPPAGE_FIELDS = (
    "estimated_slippage_bps",
    "slippage_bps",
)
OKX_BASIS_LEVEL_FIELDS = (
    "basis_bps",
    "basis_spread_bps",
    "premium_bps",
    "perp_premium_bps",
)
OKX_BASIS_ELEVATED_TERMS = ("rich", "elevated", "crowded")
OKX_BASIS_WEAK_FUNDING_TERMS = ("weak_funding", "funding_weak", "negative_funding", "funding_negative")
OKX_BASIS_UNSTABLE_FUNDING_TERMS = ("unstable_funding", "funding_unstable", "funding_flip", "flip_risk")
OKX_BASIS_RICH_BPS_MIN = 12.0
OKX_BASIS_RICH_MIN_NET_FUNDING_BPS = 1.0


def okx_basis_paper_carry_gate_enabled(
    config: Mapping[str, Any] | bool | None = None,
) -> bool:
    if isinstance(config, bool):
        return config
    if not isinstance(config, Mapping):
        return True

    for key in OKX_BASIS_PAPER_CARRY_FLAG_KEYS:
        if key in config:
            return _as_bool(config.get(key), True)

    for scope in OKX_BASIS_PAPER_CARRY_SCOPES:
        scoped = config.get(scope)
        if not isinstance(scoped, Mapping):
            continue
        for key in OKX_BASIS_PAPER_CARRY_FLAG_KEYS:
            if key in scoped:
                return _as_bool(scoped.get(key), True)
    return True


def _okx_basis_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(_okx_basis_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_okx_basis_text(item) for item in value)
    return str(value)


def _okx_basis_first_present_float(
    candidate: Mapping[str, Any],
    fields: tuple[str, ...],
) -> tuple[str | None, float | None]:
    for container in (
        candidate,
        candidate.get("paper_metrics"),
        candidate.get("analysis"),
        candidate.get("thesis"),
        candidate.get("metadata"),
        candidate.get("paper_policy"),
    ):
        if not isinstance(container, Mapping):
            continue
        for field in fields:
            value = container.get(field)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric):
                return field, numeric
    return None, None


def _okx_basis_paper_carry_target(candidate: Mapping[str, Any]) -> bool:
    haystack = " ".join(_okx_basis_text(candidate.get(field)) for field in OKX_BASIS_PAPER_TARGET_FIELDS).lower()
    return "okx" in haystack and "basis" in haystack and any(term in haystack for term in ("swap", "perp", "carry", "funding"))


def okx_basis_paper_carry_gate_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    if not okx_basis_paper_carry_gate_enabled(config):
        return None
    if not _okx_basis_paper_carry_target(candidate):
        return None

    descriptor = " ".join(_okx_basis_text(candidate.get(field)) for field in OKX_BASIS_PAPER_TARGET_FIELDS).lower()
    funding_field, funding_bps = _okx_basis_first_present_float(candidate, OKX_BASIS_FUNDING_FIELDS)
    explicit_net_field, explicit_net_funding_bps = _okx_basis_first_present_float(candidate, OKX_BASIS_EXPLICIT_NET_FIELDS)
    round_trip_field, round_trip_cost_bps = _okx_basis_first_present_float(candidate, OKX_BASIS_ROUND_TRIP_COST_FIELDS)
    fee_field, fee_bps = _okx_basis_first_present_float(candidate, OKX_BASIS_FEE_FIELDS)
    slippage_field, slippage_bps = _okx_basis_first_present_float(candidate, OKX_BASIS_SLIPPAGE_FIELDS)
    basis_field, basis_bps = _okx_basis_first_present_float(candidate, OKX_BASIS_LEVEL_FIELDS)

    estimated_cost_bps = round_trip_cost_bps
    if estimated_cost_bps is None and fee_bps is not None and slippage_bps is not None:
        estimated_cost_bps = fee_bps + slippage_bps

    net_funding_bps = explicit_net_funding_bps
    if net_funding_bps is None and funding_bps is not None:
        if estimated_cost_bps is not None:
            net_funding_bps = funding_bps - estimated_cost_bps
        else:
            net_funding_bps = funding_bps

    basis_rich = (basis_bps is not None and basis_bps >= OKX_BASIS_RICH_BPS_MIN) or any(
        term in descriptor for term in OKX_BASIS_ELEVATED_TERMS
    )
    weak_funding = any(term in descriptor for term in OKX_BASIS_WEAK_FUNDING_TERMS)
    unstable_funding = any(term in descriptor for term in OKX_BASIS_UNSTABLE_FUNDING_TERMS)

    checks: list[dict[str, Any]] = []
    failed_checks: list[dict[str, Any]] = []
    if funding_bps is None and explicit_net_funding_bps is None:
        checks.append({"code": "funding_expectation_missing"})
    elif net_funding_bps is not None and net_funding_bps <= 0.0:
        failed_checks.append({"code": "non_positive_net_funding_expectation", "value": net_funding_bps, "field": explicit_net_field or funding_field})
    elif basis_rich and net_funding_bps is not None and net_funding_bps <= OKX_BASIS_RICH_MIN_NET_FUNDING_BPS:
        failed_checks.append({"code": "weak_carry_for_rich_basis", "value": net_funding_bps, "field": explicit_net_field or funding_field})

    if weak_funding and (net_funding_bps is None or net_funding_bps <= OKX_BASIS_RICH_MIN_NET_FUNDING_BPS):
        failed_checks.append({"code": "rich_basis_with_weak_funding", "value": net_funding_bps, "field": explicit_net_field or funding_field})
    if unstable_funding and (net_funding_bps is None or net_funding_bps <= OKX_BASIS_RICH_MIN_NET_FUNDING_BPS):
        failed_checks.append({"code": "unstable_funding_carry_regime", "value": net_funding_bps, "field": explicit_net_field or funding_field})

    return {
        "guard": "paper_okx_basis_carry_gate",
        "paper_only": True,
        "eligible": not failed_checks,
        "conviction_cap": "full" if not failed_checks else "hold",
        "checks": checks,
        "failed_checks": failed_checks,
        "funding_field": funding_field,
        "estimated_cost_field": round_trip_field or fee_field or slippage_field,
        "basis_field": basis_field,
        "funding_bps": funding_bps,
        "estimated_carry_cost_bps": estimated_cost_bps,
        "net_funding_bps": net_funding_bps,
        "basis_bps": basis_bps,
        "basis_rich": basis_rich,
        "weak_or_unstable_funding_context": weak_funding or unstable_funding,
        "suppression_action": "hold_no_trade" if failed_checks else "allow",
    }


def paper_frontier_execution_quality_gate_enabled(
    config: Mapping[str, Any] | bool | None = None,
) -> bool:
    if isinstance(config, bool):
        return config
    if not isinstance(config, Mapping):
        return True

    for key in PAPER_FRONTIER_EXECUTION_QUALITY_FLAG_KEYS:
        if key in config:
            return _as_bool(config.get(key), True)

    for scope in PAPER_FRONTIER_EXECUTION_QUALITY_SCOPES:
        scoped = config.get(scope)
        if not isinstance(scoped, Mapping):
            continue
        for key in PAPER_FRONTIER_EXECUTION_QUALITY_FLAG_KEYS:
            if key in scoped:
                return _as_bool(scoped.get(key), True)
    return True


def _paper_frontier_execution_quality_target(candidate: Mapping[str, Any]) -> bool:
    market_key = _paper_cell_text(candidate.get("market_key") or candidate.get("market_surface"), "").upper()
    if market_key not in PAPER_FRONTIER_EXECUTION_QUALITY_MARKETS and "OKX_SPOT" not in market_key:
        return False

    haystack = " ".join(
        _paper_cell_text(candidate.get(field), "")
        for field in (
            "direction",
            "signal_key",
            "trade_type",
            "strategy",
            "strategy_id",
            "variant",
            "variant_id",
            "context_key",
        )
    ).lower()
    if "frontier_crypto_venue_map" not in haystack:
        return False
    return "short_frontier_spot" in haystack or ("short" in haystack and "spot" in haystack)


def _paper_frontier_first_present_float(
    candidate: Mapping[str, Any],
    fields: tuple[str, ...],
) -> tuple[str | None, float | None]:
    for field in fields:
        value = candidate.get(field)
        if value in (None, ""):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            return field, numeric
    return None, None


def _paper_frontier_first_present_int(
    candidate: Mapping[str, Any],
    fields: tuple[str, ...],
) -> tuple[str | None, int | None]:
    field, value = _paper_frontier_first_present_float(candidate, fields)
    if field is None or value is None:
        return None, None
    return field, int(value)


def _paper_frontier_flag_values(value: Any) -> set[str]:
    if value in (None, ""):
        return set()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return set()
        if text[:1] in "[{":
            try:
                return _paper_frontier_flag_values(json.loads(text))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        parts = text.replace("|", ",").split(",")
        return {part.strip().strip("'\"[] ") for part in parts if part.strip().strip("'\"[] ")}
    if isinstance(value, Mapping):
        return {str(key) for key, flagged in value.items() if flagged}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item) for item in value if item not in (None, "")}
    return {str(value)}


def paper_frontier_execution_quality_gate_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    if not paper_frontier_execution_quality_gate_enabled(config):
        return None
    if not isinstance(candidate, Mapping) or not _paper_frontier_execution_quality_target(candidate):
        return None

    checks: list[dict[str, Any]] = []
    failed_checks: list[dict[str, Any]] = []

    def _record_check(name: str, passed: bool, field: str | None, observed: Any, threshold: Any) -> None:
        record = {
            "name": name,
            "field": field,
            "observed": observed,
            "threshold": threshold,
            "passed": passed,
        }
        checks.append(record)
        if not passed:
            failed_checks.append(record)

    quality_action = str(candidate.get("quality_action") or "").strip().lower().replace("-", "_")
    if quality_action == "shadow_only":
        _record_check("quality_action", False, "quality_action", candidate.get("quality_action"), "not_shadow_only")

    anomaly_flags = {
        str(flag).strip().lower().replace(" ", "_")
        for flag in _paper_frontier_flag_values(candidate.get("anomaly_flags"))
        if str(flag).strip()
    }
    critical_flags = sorted(
        flag for flag in anomaly_flags if any(term in flag for term in CRITICAL_ANOMALY_TERMS)
    )
    if critical_flags:
        _record_check("critical_anomaly_flags", False, "anomaly_flags", critical_flags, "no_critical_flags")

    route_field, route_count = _paper_frontier_first_present_int(
        candidate,
        ("route_richness", "route_count", "executable_route_count", "venue_route_count", "supporting_venue_count"),
    )
    if route_count is not None:
        _record_check(
            "route_richness",
            route_count >= PAPER_FRONTIER_ROUTE_RICHNESS_MIN,
            route_field,
            route_count,
            f">={PAPER_FRONTIER_ROUTE_RICHNESS_MIN}",
        )

    spread_field, spread_bps = _paper_frontier_first_present_float(
        candidate,
        ("spread_bps", "top_of_book_spread_bps", "quoted_spread_bps", "local_spread_bps"),
    )
    if spread_bps is not None:
        _record_check(
            "spread_bps",
            spread_bps <= PAPER_FRONTIER_SPREAD_BPS_MAX,
            spread_field,
            round(spread_bps, 6),
            f"<={PAPER_FRONTIER_SPREAD_BPS_MAX}",
        )

    freshness_field, freshness_seconds = _paper_frontier_first_present_float(
        candidate,
        ("book_age_seconds", "quote_age_seconds", "local_quote_age_seconds", "top_of_book_age_seconds", "source_age_seconds"),
    )
    if freshness_seconds is not None:
        _record_check(
            "freshness_seconds",
            freshness_seconds <= PAPER_FRONTIER_FRESHNESS_SECONDS_MAX,
            freshness_field,
            round(freshness_seconds, 6),
            f"<={PAPER_FRONTIER_FRESHNESS_SECONDS_MAX}",
        )

    liquidity_field, liquidity_score = _paper_frontier_first_present_float(
        candidate,
        ("local_liquidity_score", "liquidity_proxy_score", "book_depth_ratio", "top_of_book_size_ratio"),
    )
    if liquidity_score is not None:
        _record_check(
            "liquidity_score",
            liquidity_score >= PAPER_FRONTIER_LIQUIDITY_SCORE_MIN,
            liquidity_field,
            round(liquidity_score, 6),
            f">={PAPER_FRONTIER_LIQUIDITY_SCORE_MIN}",
        )

    depth_field, depth_usd = _paper_frontier_first_present_float(
        candidate,
        ("top_of_book_notional_usd", "book_depth_usd", "local_depth_usd"),
    )
    if depth_usd is not None:
        _record_check(
            "book_depth_usd",
            depth_usd >= PAPER_FRONTIER_DEPTH_USD_MIN,
            depth_field,
            round(depth_usd, 6),
            f">={PAPER_FRONTIER_DEPTH_USD_MIN}",
        )

    favorable_context = not failed_checks and sum(1 for check in checks if check.get("passed")) >= 2
    eligible = not failed_checks
    return {
        "guard": "paper_frontier_execution_quality_gate",
        "paper_only": True,
        "applies": True,
        "eligible": eligible,
        "paper_fill_allowed": eligible,
        "favorable_context": favorable_context,
        "paper_score_multiplier": 1.0 if favorable_context else (0.85 if eligible else 0.0),
        "checks": checks,
        "failed_checks": failed_checks,
        "summary": "favorable_context_confirmed" if favorable_context else ("blocked_low_execution_quality" if failed_checks else "no_explicit_quality_failure"),
    }


def _paper_cell_text(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text or default


def _paper_lineage_text(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text.replace(" ", "_")


def _paper_first_present_int(candidate: dict[str, Any], fields: tuple[str, ...]) -> int | None:
    for field in fields:
        value = candidate.get(field)
        if value in (None, ""):
            continue
        try:
            numeric = int(float(value))
        except (TypeError, ValueError):
            continue
        return numeric
    return None


def _paper_lineage_id(candidate: dict[str, Any]) -> str:
    for field in PAPER_LINEAGE_ID_FIELDS:
        text = _paper_lineage_text(candidate.get(field), default="")
        if text:
            return text
    for field in ("signal_family", "strategy_id", "strategy", "signal_key", "variant_id", "variant"):
        text = _paper_lineage_text(candidate.get(field), default="")
        if text:
            return text
    return "unknown"


def _paper_holding_profile(candidate: dict[str, Any]) -> str:
    for field in PAPER_HOLDING_PROFILE_FIELDS:
        text = _paper_lineage_text(candidate.get(field), default="")
        if text:
            return text

    minutes = _paper_first_present_int(
        candidate,
        ("expected_hold_minutes", "max_holding_minutes", "holding_minutes", "paper_holding_minutes"),
    )
    if minutes is not None:
        if minutes <= 240:
            return "intraday"
        if minutes <= 1440:
            return "overnight"
        if minutes <= 4320:
            return "swing"
        return "multi_day"

    haystack = " ".join(
        str(candidate.get(field) or "")
        for field in ("signal_key", "trade_type", "strategy", "variant", "context_key")
    ).lower()
    for profile in ("scalp", "intraday", "overnight", "swing", "multi_day", "multi-day"):
        if profile in haystack:
            return profile.replace("-", "_")
    return "unknown"


def paper_lineage_context(candidate: dict[str, Any], minimum_threshold: int = 1) -> dict[str, Any]:
    """Return a strict paper-only lineage partition record for score carryover."""
    threshold = max(int(minimum_threshold or 1), 1)
    observation_count = _paper_first_present_int(candidate, PAPER_LINEAGE_OBSERVATION_COUNT_FIELDS) or 0
    win_count = _paper_first_present_int(candidate, PAPER_LINEAGE_WIN_COUNT_FIELDS) or 0
    record = {
        "lineage_id": _paper_lineage_id(candidate),
        "venue": _paper_lineage_text(candidate.get("venue")),
        "trade_type": _paper_lineage_text(candidate.get("trade_type") or candidate.get("market_surface")),
        "direction": _paper_cell_direction(candidate),
        "holding_profile": _paper_holding_profile(candidate),
    }
    record["context_key"] = "|".join(
        (record["lineage_id"], record["venue"], record["trade_type"], record["direction"], record["holding_profile"])
    )
    record["minimum_observation_threshold"] = threshold
    record["target_context_observation_count"] = observation_count
    record["target_context_positive_count"] = win_count
    record["has_independent_target_context_observations"] = observation_count >= threshold
    record["has_target_context_win_quality"] = win_count > 0
    record["inherited_score_boost_allowed"] = observation_count >= threshold and win_count > 0
    return record


def _paper_cell_direction(candidate: dict[str, Any]) -> str:
    direct = _paper_cell_text(candidate.get("direction"), default="")
    if direct:
        return direct.lower()
    haystack = " ".join(
        str(candidate.get(field) or "")
        for field in ("signal_key", "trade_type", "strategy", "variant", "context_key")
    ).lower()
    if " short" in f" {haystack} " or haystack.endswith("_short") or "|short|" in haystack:
        return "short"
    if " long" in f" {haystack} " or haystack.endswith("_long") or "|long|" in haystack:
        return "long"
    return "unknown"


def _paper_cell_route_status(candidate: dict[str, Any]) -> str:
    for container in (
        candidate,
        candidate.get("frontier_route_feasibility"),
        candidate.get("execution_feasibility"),
        candidate.get("execution_route"),
    ):
        if not isinstance(container, dict):
            continue
        for field in ("paper_route_status", "route_status", "status", "execution_status"):
            text = _paper_cell_text(container.get(field), default="")
            if text:
                return text.lower().replace("-", "_").replace(" ", "_")
    return "unknown"


def paper_signal_cell(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical paper-only execution cell for a candidate or trade record."""
    signal_identity = _paper_cell_text(candidate.get("signal_key"), default="")
    if not signal_identity:
        try:
            signal_identity = _paper_cell_text(signal_key(candidate), default="")
        except Exception:
            signal_identity = ""

    strategy = _paper_cell_text(candidate.get("strategy") or candidate.get("strategy_id"))
    variant = _paper_cell_text(candidate.get("variant") or candidate.get("variant_id"))
    venue = _paper_cell_text(candidate.get("venue")).upper()
    direction = _paper_cell_direction(candidate)
    route_status = _paper_cell_route_status(candidate)
    signal_family = _paper_cell_text(
        candidate.get("signal_family") or candidate.get("market_surface") or candidate.get("trade_type") or strategy
    )
    if not signal_identity:
        signal_identity = "|".join((signal_family, strategy, variant))

    cell_key = "|".join(
        (
            signal_identity,
            venue,
            direction,
            route_status,
        )
    )
    return {
        "scope": "paper_signal_cell_v1",
        "signal_family": signal_family,
        "signal_key": signal_identity,
        "strategy": strategy,
        "variant": variant,
        "venue": venue,
        "direction": direction,
        "paper_route_status": route_status,
        "cell_key": cell_key,
    }


def paper_signal_cell_key(candidate: dict[str, Any]) -> str:
    return str(paper_signal_cell(candidate).get("cell_key") or "")


def evaluate_paper_cell_policy(
    record: dict[str, Any],
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Evaluate a paper-only promotion/probation/rollback decision for one cell."""
    settings: dict[str, Any] = {}
    if isinstance(config, dict):
        for key in PAPER_CELL_SCOPE_FLAG_KEYS:
            value = config.get(key)
            if isinstance(value, dict):
                settings.update(value)
        for scope in PAPER_CELL_SCOPE_SCOPES:
            scoped = config.get(scope)
            if not isinstance(scoped, dict):
                continue
            for key in PAPER_CELL_SCOPE_FLAG_KEYS:
                value = scoped.get(key)
                if isinstance(value, dict):
                    settings.update(value)

    closed_count = _as_int(record.get("closed_count", record.get("closed_trades", record.get("trades", 0))))
    avg_pnl_bps = _as_float(record.get("avg_pnl_bps", record.get("pnl_bps", 0.0)))
    win_rate_raw = record.get("win_rate")
    win_rate = _as_float(win_rate_raw, default=-1.0) if win_rate_raw is not None else None
    min_closed_trades = max(1, _as_int(settings.get("min_closed_trades"), 3))
    probation_ttl_days = max(1, _as_int(settings.get("probation_ttl_days"), 7))
    promote_min_avg_pnl_bps = _as_float(settings.get("promote_min_avg_pnl_bps"), 1.0)
    promote_min_win_rate = _as_float(settings.get("promote_min_win_rate"), 0.5)
    revert_avg_pnl_bps = _as_float(settings.get("revert_avg_pnl_bps"), -5.0)
    prior_state = _paper_cell_text(record.get("prior_state") or record.get("state"), default="new").lower()
    probation_started_at = record.get("probation_started_at") or record.get("first_reviewed_at") or record.get("reviewed_at")
    current_now = dt.datetime.fromisoformat(now) if now else dt.datetime.now(dt.timezone.utc)
    probation_expired = False
    if probation_started_at:
        try:
            probation_started = dt.datetime.fromisoformat(str(probation_started_at))
            probation_expired = (current_now - probation_started).days >= probation_ttl_days
        except (TypeError, ValueError):
            probation_expired = False

    if closed_count >= min_closed_trades and avg_pnl_bps <= revert_avg_pnl_bps:
        decision = "reverted"
        action = "rollback_cell"
    elif closed_count >= min_closed_trades and avg_pnl_bps >= promote_min_avg_pnl_bps and (win_rate is None or win_rate >= promote_min_win_rate):
        decision = "promoted"
        action = "promote_cell"
    elif probation_expired and avg_pnl_bps < 0.0 and prior_state in {"probation", "new"}:
        decision = "reverted"
        action = "rollback_cell"
    else:
        decision = "probation"
        action = "retain_cell_probation"

    cell = paper_signal_cell(record)
    return {
        "scope": "paper_signal_cell_policy_v1",
        "cell": cell,
        "cell_key": cell.get("cell_key"),
        "decision": decision,
        "action": action,
        "closed_count": closed_count,
        "avg_pnl_bps": avg_pnl_bps,
        "win_rate": None if win_rate is None or win_rate < 0.0 else win_rate,
        "prior_state": prior_state,
        "probation_ttl_days": probation_ttl_days,
        "probation_expired": probation_expired,
        "reviewed_at": current_now.isoformat(),
    }


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


def _flag_override(source: Any, keys: tuple[str, ...], scopes: tuple[str, ...]) -> bool | None:
    if isinstance(source, bool):
        return source
    if not isinstance(source, dict):
        return None

    for key in keys:
        if key in source:
            return _as_bool(source.get(key), True)

    for scope in scopes:
        scoped = source.get(scope)
        if not isinstance(scoped, dict):
            continue
        for key in keys:
            if key in scoped:
                return _as_bool(scoped.get(key), True)
    return None


def paper_family_quarantine_enabled(
    candidate: dict[str, Any] | None = None,
    config: dict[str, Any] | bool | None = None,
) -> bool:
    override = _flag_override(config, PAPER_FAMILY_QUARANTINE_FLAG_KEYS, PAPER_FAMILY_QUARANTINE_SCOPES)
    if override is not None:
        return override
    override = _flag_override(candidate, PAPER_FAMILY_QUARANTINE_FLAG_KEYS, PAPER_FAMILY_QUARANTINE_SCOPES)
    if override is not None:
        return override
    return True


def _lineage_texts(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, dict):
        texts: list[str] = []
        for key, nested in value.items():
            texts.extend(_lineage_texts(key))
            texts.extend(_lineage_texts(nested))
        return texts
    if isinstance(value, (list, tuple, set)):
        texts: list[str] = []
        for nested in value:
            texts.extend(_lineage_texts(nested))
        return texts
    text = str(value).strip().lower()
    return [text] if text else []


def paper_family_quarantine_record(
    candidate: dict[str, Any],
    config: dict[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    if not paper_family_quarantine_enabled(candidate, config):
        return None

    searchable_fields = (
        "market_key",
        "market_surface",
        "signal_family",
        "signal_key",
        "family",
        "strategy",
        "strategy_id",
        "variant",
        "variant_id",
        "lineage",
        "lineage_tags",
        "tags",
        "candidate_tags",
        "contexts",
        "parent_strategy",
        "parent_strategy_id",
        "parent_variant",
        "parent_signal_key",
    )

    field_texts: dict[str, str] = {}
    normalized_joined: list[str] = []
    for field in searchable_fields:
        texts = _lineage_texts(candidate.get(field))
        if not texts:
            continue
        raw_text = " ".join(texts)
        field_texts[field] = raw_text
        normalized_joined.append(raw_text.replace("|", " ").replace("/", " ").replace(";", " ").replace(",", " "))

    combined_raw = " ".join(field_texts.values())
    combined_normalized = " ".join(normalized_joined)
    base_match = any(all(term in combined_normalized for term in terms) for terms in QUARANTINED_BASE_LINEAGE_TERMS)
    matched_descendants = sorted(descendant for descendant in QUARANTINED_DESCENDANT_KEYS if descendant in combined_raw)
    if not base_match and not matched_descendants:
        return None

    matched_fields = sorted(
        field
        for field, raw_text in field_texts.items()
        if any(term in raw_text for group in QUARANTINED_BASE_LINEAGE_TERMS for term in group)
        or any(descendant in raw_text for descendant in QUARANTINED_DESCENDANT_KEYS)
    )
    return {
        "reason": "quarantined_family_decay",
        "paper_only": True,
        "paper_fill_allowed": False,
        "guard": "paper_strategy_family_quarantine",
        "quarantine_family": "YAHOO_PROXY global_proxy_momentum",
        "release_condition": QUARANTINE_RELEASE_CONDITION,
        "matched_fields": matched_fields,
        "matched_descendants": matched_descendants,
    }


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
    route_eligibility = candidate.get("paper_route_eligibility") or {}
    route_eligibility_blocked = bool(route_eligibility.get("suppressed", False))
    requested_score_delta = score_delta
    reasons = list(reasons)
    if route_eligibility_blocked:
        score_delta = 0.0
        allocation_multiplier = 0.0
        shadow_only = True
        protect = False
        if "paper_route_eligibility_blocked" not in reasons:
            reasons.append("paper_route_eligibility_blocked")
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
    if route_eligibility_blocked:
        reliability["route_eligibility_enforced"] = True
        reliability["requested_score_delta"] = round(requested_score_delta, 3)
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
    if route_eligibility_blocked:
        candidate.setdefault("pre_route_eligibility_score", candidate.get("score", 0.0))
        candidate["score"] = 0.0
        candidate["paper_entry_blocked"] = True
        candidate["promotion_eligible"] = False
        candidate["paper_route_allocation_multiplier"] = 0.0
        candidate["paper_route_score_clamped"] = True
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
    venue = str(candidate.get("venue") or "").upper()
    funding_profile = "okx_funding_capture" if venue == "OKX" else "public_perpetual_funding_capture"

    if direction in {"funding_capture_short_perp", "funding_capture_long_perp"}:
        if funding >= 3.0 and spread <= 4.0 and liquidity >= 0.45:
            return _annotate(
                candidate,
                profile=funding_profile,
                action="protect_working_funding_slice",
                reasons=["funding magnitude, spread, and liquidity agree"],
                score_delta=3.0,
                allocation_multiplier=1.0,
                protect=True,
            )
        return _annotate(
            candidate,
            profile=funding_profile,
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
