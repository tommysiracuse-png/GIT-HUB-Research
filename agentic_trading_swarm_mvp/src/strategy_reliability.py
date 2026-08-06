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

from paper_context_cost import realized_paper_cost_audit
from paper_decay_quarantine import (
    apply_score_policy as apply_okx_basis_decay_score_policy,
    quarantine_record as okx_basis_decay_quarantine_record,
)
from proxy_signal_quality import PROXY_TRADE_TYPES, proxy_short_quality_review
from storage import RUNS_DIR, signal_key
from frontier_data_quality import (
    paper_only_proxy_frontier_target_evidence_review,
    paper_only_yahoo_proxy_cross_surface_alignment_guard,
)


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
PAPER_CONTEXT_PRIOR_POLICY_KEY = "paper_context_priors"
PAPER_CONTEXT_PRIOR_SCOPES = (
    "paper",
    "paper_policy",
    "strategy_reliability",
)
PAPER_CONTEXT_PRIOR_DEFAULTS = {
    "enabled": True,
    "paper_only": True,
    "exceptional_base_signal_score": 85.0,
    "feasibility_standard_prior": 6.0,
    "feasibility_conditional_prior": -10.0,
    "top_rank_min_closed_trades": 25,
    "top_rank_min_avg_pnl_bps": 0.0,
    "top_rank_score_cap": 75.0,
    "conditional_rank_score_cap": 35.0,
    "realized_context_window_closed_trades": 30,
    "realized_context_min_closed_trades": 6,
    "realized_context_positive_scale": 0.2,
    "realized_context_negative_scale": 0.3,
    "realized_context_max_positive_prior": 12.0,
    "realized_context_max_negative_prior": -18.0,
    "realized_context_persistent_negative_closed_trades": 8,
    "realized_context_conditional_penalty_multiplier": 1.75,
    "realized_context_persistent_negative_multiplier": 1.5,
    "strong_liquidity_score": 0.70,
    "strong_liquidity_prior": 4.0,
    "weak_liquidity_score": 0.45,
    "weak_liquidity_prior": -8.0,
    "venue_direction_feasibility_priors": {
        "OKX_SPOT|long|standard": 8.0,
        "OKX_SPOT|long|conditional": 1.0,
        "BYBIT_SPOT|long|standard": 6.0,
        "BYBIT_SPOT|long|conditional": -3.0,
        "GATE|short|standard": 3.0,
        "GATE|short|conditional": -2.0,
        "GATE|long|standard": -7.0,
        "GATE|long|conditional": -10.0,
        "KRAKEN|long|standard": -4.0,
        "KRAKEN|long|conditional": -7.0,
        "MEXC|long|standard": -12.0,
        "MEXC|long|conditional": -15.0,
    },
}
PAPER_CONTEXT_RANK_GATE_TRADE_TYPES = {
    "frontier_crypto_venue_map",
    "perp_funding_basis",
    "spot_carry",
    "basis_mean_reversion",
}
PAPER_CONTEXT_RANK_GATE_FAMILIES = {
    "carry_or_funding_capture",
    "convergence_or_mean_reversion",
}
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
PAPER_CONTEXT_LOSS_QUARANTINE_POLICY_KEY = "paper_context_loss_quarantine"
PAPER_CONTEXT_LOSS_QUARANTINE_DEFAULTS = {
    "enabled": True,
    "rolling_window_closed_trades": 30,
    "min_closed_trades": 12,
    "max_expectancy_bps": 0.0,
    "max_win_rate": 0.45,
    "max_tail_average_bps": -20.0,
    "max_worst_loss_bps": -80.0,
    "cooldown_hours": 24,
    "recovery_min_closed_trades": 8,
    "recovery_min_expectancy_bps": 0.0,
    "recovery_min_win_rate": 0.50,
    "recovery_min_tail_average_bps": -20.0,
}
PAPER_PORTABILITY_QUARANTINE_FLAG_KEYS = (
    "paper_portability_quarantine_enabled",
    "cross_surface_portability_quarantine_enabled",
    "paper_cross_family_portability_guard_enabled",
)
PAPER_PORTABILITY_QUARANTINE_SCOPES = (
    "paper_portability_quarantine",
    "paper_policy",
    "strategy_reliability",
    "paper",
)
PAPER_PORTABILITY_MIN_CLOSED_COUNT = 20
PAPER_PORTABILITY_MIN_EXPECTANCY_NET_BPS = 0.0
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
PAPER_TRANSLATED_ROUTE_OBSERVATION_MULTIPLIER = 0.15
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


def _route_local_confirmation_flag(candidate: Mapping[str, Any], names: tuple[str, ...]) -> bool:
    """Read an explicit route-local confirmation without inferring it from quality.

    Price action and liquidity are deliberately separate checks: a strong proxy
    signal, a tight spread, or a high aggregate quality score is not a
    substitute for same-market confirmation.
    """
    containers: list[Mapping[str, Any]] = [candidate]
    for field in (
        "route_local_confirmation",
        "local_confirmation",
        "native_route_confirmation",
        "same_market_confirmation",
    ):
        nested = candidate.get(field)
        if isinstance(nested, Mapping):
            containers.append(nested)
    for container in containers:
        for name in names:
            if name in container:
                return _as_bool(container.get(name), False)
    return False


def _route_local_confirmation(candidate: Mapping[str, Any]) -> dict[str, Any]:
    price_action_confirmed = _route_local_confirmation_flag(
        candidate,
        (
            "native_price_action_confirmed",
            "same_market_price_action_confirmed",
            "route_local_price_action_confirmed",
            "local_price_action_confirmed",
            "price_action_confirmed",
        ),
    )
    liquidity_confirmed = _route_local_confirmation_flag(
        candidate,
        (
            "native_liquidity_confirmed",
            "same_market_liquidity_confirmed",
            "route_local_liquidity_confirmed",
            "local_liquidity_confirmed",
            "liquidity_confirmed",
            "liquidity_checks_passed",
        ),
    )
    return {
        "native_price_action_confirmed": price_action_confirmed,
        "native_liquidity_confirmed": liquidity_confirmed,
        "confirmed": bool(price_action_confirmed and liquidity_confirmed),
        "required_checks": ["native_price_action", "native_liquidity"],
    }


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
    families = _portability_families(candidate)
    translated_family = bool(
        families.get("source_family")
        and families.get("destination_family")
        and families["source_family"] != families["destination_family"]
    )
    if translated_family and "market_family" not in mismatched_fields:
        mismatched_fields.append("market_family")
    translated_route = bool(mismatched_fields)
    local_confirmation = _route_local_confirmation(candidate)
    eligible = not translated_route or local_confirmation["confirmed"]
    return {
        "guard": "paper_route_lineage_confirmation",
        "reason": None if eligible else "route_local_confirmation_missing",
        "paper_only": True,
        "eligible": eligible,
        "translated_route": translated_route,
        "lineage_state": "native" if not translated_route else "confirmed" if eligible else "observation_only",
        "promotion_blocked": translated_route and not eligible,
        "compatibility_rule_logged": compatibility_rule is not None,
        "compatibility_rule_allows_translation": allowed_by_rule,
        "compatibility_rule": compatibility_rule,
        "source_context": source_bucket,
        "destination_context": destination_bucket,
        "matching_fields": matching_fields,
        "mismatched_fields": mismatched_fields,
        "source_market_family": families.get("source_family"),
        "destination_market_family": families.get("destination_family"),
        "route_local_confirmation": local_confirmation,
        "paper_score_multiplier": 1.0 if eligible else PAPER_TRANSLATED_ROUTE_OBSERVATION_MULTIPLIER,
        # This guard changes promotion and ranking treatment only.  It never
        # suppresses a priceable paper experiment.
        "paper_fill_allowed": True,
        "observation_only": translated_route and not eligible,
    }


def paper_route_lineage_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any]:
    """Attach route lineage to every paper candidate, including native ideas."""
    source_context = _paper_context_source_context(candidate)
    destination_context = _paper_context_bucket(candidate)
    promotion = paper_context_promotion_guard_record(candidate, config=config)
    if promotion is not None:
        return {
            "lineage_type": "translated" if promotion["translated_route"] else "native",
            "source_context_present": True,
            "source_context": promotion["source_context"],
            "destination_context": promotion["destination_context"],
            "confirmation": promotion["route_local_confirmation"],
            "confirmation_required": promotion["translated_route"],
            "confirmation_status": "confirmed" if promotion["eligible"] else "missing",
            "observation_only": promotion["observation_only"],
            "promotion_guard": promotion,
        }
    # Some scanners carry only source/destination market families rather than
    # a full source route context.  That is still enough to identify a
    # translated thesis, but never enough to claim local confirmation.
    families = _portability_families(candidate)
    source_family = families.get("source_family")
    destination_family = families.get("destination_family")
    translated_family = bool(
        source_family and destination_family and source_family != destination_family
    )
    confirmation = _route_local_confirmation(candidate)
    if translated_family:
        family_source = {"market_family": source_family}
        guard = {
            "guard": "paper_route_lineage_confirmation",
            "reason": None if confirmation["confirmed"] else "route_local_confirmation_missing",
            "paper_only": True,
            "eligible": confirmation["confirmed"],
            "translated_route": True,
            "lineage_state": "confirmed" if confirmation["confirmed"] else "observation_only",
            "promotion_blocked": not confirmation["confirmed"],
            "compatibility_rule_logged": False,
            "compatibility_rule_allows_translation": False,
            "compatibility_rule": None,
            "source_context": family_source,
            "destination_context": destination_context,
            "matching_fields": [],
            "mismatched_fields": ["market_family"],
            "route_local_confirmation": confirmation,
            "paper_score_multiplier": (
                1.0 if confirmation["confirmed"] else PAPER_TRANSLATED_ROUTE_OBSERVATION_MULTIPLIER
            ),
            "paper_fill_allowed": True,
            "observation_only": not confirmation["confirmed"],
        }
        return {
            "lineage_type": "translated",
            "source_context_present": True,
            "source_context": family_source,
            "destination_context": destination_context,
            "confirmation": confirmation,
            "confirmation_required": True,
            "confirmation_status": "confirmed" if confirmation["confirmed"] else "missing",
            "observation_only": not confirmation["confirmed"],
            "promotion_guard": guard,
            "source_market_family": source_family,
            "destination_market_family": destination_family,
        }
    return {
        "lineage_type": "native",
        "source_context_present": source_context is not None,
        "source_context": _paper_context_bucket(source_context) if source_context else {},
        "destination_context": destination_context,
        "confirmation": _route_local_confirmation(candidate),
        "confirmation_required": False,
        "confirmation_status": "not_required",
        "observation_only": False,
        "promotion_guard": None,
    }


QUARANTINED_PAPER_FAMILY_KEY = "YAHOO_PROXY|global_proxy_momentum"
QUARANTINED_PAPER_SOURCE_FAMILY = "yahoo_proxy"
QUARANTINED_PAPER_STRATEGY_FAMILY = "global_proxy_momentum"
QUARANTINED_STRATEGY_LAB_PREFIXES = (
    "gate_yahoo_momentum_to_fresh_tight_high_quality_proxies_3342a7f1",
    "lab_yahoo_proxy_momentum_freshness_quality_gate_v1",
    "red_team_yahoo_proxy_momentum_sanity_check_c6d14fc0",
    "route_rich_frontier_long_filter_2942c975",
    "tighten_entry_confirmation_and_add_paper_only_cooldown_65825268",
)
QUARANTINE_RELEASE_CONDITION = (
    "Lift only after the source family and its immediate descendants each show "
    "sustained non-negative paper expectancy with acceptable freshness and "
    "execution-quality diagnostics."
)
YAHOO_PROXY_FRESHNESS_SHADOW_POLICY_KEY = "yahoo_proxy_momentum_freshness_shadow_gate"
YAHOO_PROXY_FRESHNESS_SHADOW_SCOPES = (
    "paper",
    "paper_policy",
    "strategy_reliability",
)
YAHOO_PROXY_FRESHNESS_SHADOW_DEFAULTS = {
    "enabled": True,
    "max_quote_age_seconds": 20.0 * 60.0,
    "max_last_trade_age_seconds": 20.0 * 60.0,
    "min_tick_observations": 2,
    "min_tick_move_bps": 3.0,
    "min_alignment_ratio": 0.5,
}
SOURCE_VETO_POLICY_KEY = "yahoo_proxy_momentum_source_veto"
SOURCE_VETO_DEFAULT_MIN_WINDOWS = 3
SOURCE_VETO_DEFAULT_MIN_SAMPLES_PER_WINDOW = 10
SOURCE_VETO_DEFAULT_MIN_DIAGNOSTIC_PASS_RATE = 0.90
LINEAGE_SOURCE_HEALTH_POLICY_KEY = "lineage_source_health_guard"
LINEAGE_SOURCE_HEALTH_DEFAULT_MIN_CLOSED_COUNT = 10
LINEAGE_SOURCE_HEALTH_DEFAULT_PENALTY_MIN_CLOSED_COUNT = 3
LINEAGE_SOURCE_HEALTH_DEFAULT_PENALTY_MULTIPLIER = 0.50
LINEAGE_SOURCE_HEALTH_FIELDS = (
    "lineage_source_health",
    "parent_signal_health",
    "source_signal_health",
    "upstream_signal_health",
    "parent_signal_stats",
    "source_signal_stats",
    "upstream_signal_stats",
)
LINEAGE_SOURCE_SIGNAL_KEY_FIELDS = (
    "parent_signal_key",
    "source_signal_key",
    "strategy_lab_source_signal_key",
    "upstream_signal_key",
    "origin_signal_key",
)
YAHOO_PROXY_TRANSFER_SOURCE_PREFIX = "YAHOO_PROXY|global_proxy_momentum"
YAHOO_PROXY_TRANSFER_OKX_SURFACES = {"OKX_SPOT", "OKX_PERP"}
YAHOO_PROXY_TRANSFER_DELAY_BUCKETS = (
    (60.0, "under_1m"),
    (300.0, "1m_to_5m"),
    (900.0, "5m_to_15m"),
    (float("inf"), "over_15m"),
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


def _paper_cell_asymmetric_direction_reasons(record: dict[str, Any], cell: dict[str, Any]) -> list[str]:
    """Identify short proxy/discovery cells that need direction-specific evidence."""
    direction = str(cell.get("direction") or "").lower()
    short_direction = "short" in direction
    haystack = " ".join(
        str(value or "").lower()
        for value in (
            cell.get("signal_key"),
            cell.get("signal_family"),
            cell.get("strategy"),
            cell.get("variant"),
            cell.get("paper_route_status"),
            record.get("trade_type"),
            record.get("market_surface"),
            record.get("context_key"),
        )
    )
    reasons = []
    if short_direction and "proxy" in haystack:
        reasons.append("short_proxy")
    if short_direction and "discovery" in haystack:
        reasons.append("short_discovery")
    if (
        short_direction
        and "frontier" in haystack
        and "conditional" in haystack
    ):
        reasons.append("conditional_frontier_short")
    return reasons


def _paper_cell_promotion_confidence_review(
    closed_count: int,
    settings: dict[str, Any],
    asymmetric_direction_reasons: list[str],
    negative_adjustment_evidence_floor: int,
) -> dict[str, Any]:
    """Measure conditional-frontier-short sample confidence for promotion only.

    This is intentionally a promotion/probation diagnostic.  It never changes
    paper entry eligibility, candidate emission, or routing.
    """

    applies = "conditional_frontier_short" in asymmetric_direction_reasons
    if not applies:
        return {
            "applies": False,
            "sample_confidence": 1.0,
            "minimum_confidence": None,
            "target_closed_trades": None,
            "confidence_penalty_bps": 0.0,
        }

    target_closed_trades = max(
        1,
        _as_int(settings.get("conditional_frontier_short_confidence_target_closed_trades"), 20),
    )
    minimum_confidence = max(
        0.0,
        min(1.0, _as_float(settings.get("conditional_frontier_short_min_promotion_confidence"), 0.8)),
    )
    max_penalty_bps = max(
        0.0,
        _as_float(settings.get("conditional_frontier_short_confidence_penalty_bps"), 2.0),
    )
    sample_confidence = min(1.0, max(0.0, closed_count / target_closed_trades))
    raw_penalty_bps = max_penalty_bps * (1.0 - sample_confidence)
    evidence_floor_met = closed_count >= negative_adjustment_evidence_floor
    # A sparse outcome set can lower confidence, but it must not impose a
    # score penalty that makes a paper cell look conclusively weak.  The
    # regular promotion minimum still keeps the cell in probation until it
    # has enough observations.
    applied_penalty_bps = raw_penalty_bps if evidence_floor_met else 0.0
    return {
        "applies": True,
        "sample_confidence": round(sample_confidence, 3),
        "minimum_confidence": minimum_confidence,
        "target_closed_trades": target_closed_trades,
        "negative_adjustment_evidence_floor": negative_adjustment_evidence_floor,
        "negative_adjustment_evidence_floor_met": evidence_floor_met,
        "raw_confidence_penalty_bps": round(raw_penalty_bps, 3),
        "confidence_penalty_bps": round(applied_penalty_bps, 3),
        "confidence_penalty_deferred": bool(raw_penalty_bps and not evidence_floor_met),
        "confidence_status": (
            "confirmed"
            if sample_confidence >= minimum_confidence
            else "evidence_limited"
            if not evidence_floor_met
            else "developing"
        ),
    }


def _paper_cell_quality_audit(
    record: dict[str, Any],
    cell: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    direction = str(cell.get("direction") or record.get("direction") or "").lower()
    haystack = " ".join(
        str(value or "").lower()
        for value in (cell.get("signal_key"), cell.get("signal_family"), record.get("trade_type"))
    )
    if direction == "short_proxy" or ("short" in direction and "proxy" in haystack):
        floor_name = "proxy_short"
    elif direction.startswith("long_frontier"):
        floor_name = "frontier_long"
    else:
        return {"applies": False, "passed": True, "reasons": []}

    liquidity = _as_float(record.get("liquidity_score"), -1.0)
    freshness_field = "freshness_age_seconds"
    freshness = record.get(freshness_field)
    if freshness is None and record.get("stale_minutes") is not None:
        freshness_field = "stale_minutes"
        freshness = _as_float(record.get("stale_minutes")) * 60.0
    freshness_age = _as_float(freshness, -1.0)
    min_liquidity = _as_float(settings.get(f"{floor_name}_min_liquidity_score"), 0.65 if floor_name == "proxy_short" else 0.35)
    max_freshness = _as_float(settings.get(f"{floor_name}_max_freshness_age_seconds"), 900.0 if floor_name == "proxy_short" else 90.0)
    allowed_routes = {
        str(value).strip().lower()
        for value in settings.get("allowed_promotion_route_statuses", ["standard", "feasible", "executable"])
    }
    route_status = str(cell.get("paper_route_status") or "unknown").lower()
    reasons: list[str] = []
    if liquidity < 0.0:
        reasons.append("missing_promotion_liquidity_evidence")
    elif liquidity < min_liquidity:
        reasons.append("promotion_liquidity_below_floor")
    if freshness_age < 0.0:
        reasons.append("missing_promotion_freshness_evidence")
    elif freshness_age > max_freshness:
        reasons.append("promotion_freshness_above_ceiling")
    if route_status not in allowed_routes:
        reasons.append("promotion_route_status_not_standard")
    return {
        "applies": True,
        "name": floor_name,
        "passed": not reasons,
        "liquidity_score": None if liquidity < 0.0 else liquidity,
        "min_liquidity_score": min_liquidity,
        "freshness_field": freshness_field if freshness_age >= 0.0 else None,
        "freshness_age_seconds": None if freshness_age < 0.0 else freshness_age,
        "max_freshness_age_seconds": max_freshness,
        "route_status": route_status,
        "allowed_route_statuses": sorted(allowed_routes),
        "reasons": reasons,
    }


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
    reported_avg_pnl_bps = _as_float(record.get("avg_pnl_bps", record.get("pnl_bps", 0.0)))
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

    cell = paper_signal_cell(record)
    quality_audit = _paper_cell_quality_audit(record, cell, settings)
    gross_avg = record.get("gross_avg_pnl_bps")
    cost_basis = str(record.get("avg_pnl_cost_basis") or record.get("pnl_cost_basis") or "").lower()
    needs_cost_backfill = gross_avg is not None or cost_basis in {"gross", "price_only", "legacy_unadjusted"}
    cost_audit = realized_paper_cost_audit(
        record,
        gross_avg if gross_avg is not None else reported_avg_pnl_bps,
        charged_cost_bps=record.get("realized_cost_bps", record.get("charged_cost_bps", 0.0)),
        settings=config,
        already_backfilled=not needs_cost_backfill,
    )
    avg_pnl_bps = _as_float(cost_audit.get("adjusted_pnl_bps"), reported_avg_pnl_bps)
    portability = paper_portability_quarantine_record(record, config=config)
    asymmetric_direction_reasons = _paper_cell_asymmetric_direction_reasons(record, cell)
    promotion_min_closed_trades = min_closed_trades
    if any(reason in {"short_proxy", "short_discovery"} for reason in asymmetric_direction_reasons):
        promotion_min_closed_trades = max(
            promotion_min_closed_trades,
            max(1, _as_int(settings.get("asymmetric_direction_min_closed_trades"), 20)),
        )
        # Generic exploration thresholds may be loosened, but short proxy and
        # discovery cells must retain positive realized after-cost expectancy.
        promote_min_avg_pnl_bps = max(
            promote_min_avg_pnl_bps,
            max(1.0, _as_float(settings.get("asymmetric_direction_min_avg_pnl_bps"), 1.0)),
        )
    if "conditional_frontier_short" in asymmetric_direction_reasons:
        promotion_min_closed_trades = max(
            promotion_min_closed_trades,
            max(1, _as_int(settings.get("conditional_frontier_short_min_closed_trades"), 12)),
        )
        promote_min_avg_pnl_bps = max(
            promote_min_avg_pnl_bps,
            max(1.0, _as_float(settings.get("conditional_frontier_short_min_avg_pnl_bps"), 1.0)),
        )
    # Discovery and conditional-frontier cells already need this many closed
    # paper outcomes before promotion.  Use the same floor before applying a
    # strong negative retention decision so a small, noisy cohort remains
    # observable.  Keep the legacy generic short-proxy probation-expiry
    # behavior unchanged.
    negative_adjustment_evidence_applies = any(
        reason in {"short_discovery", "conditional_frontier_short"}
        for reason in asymmetric_direction_reasons
    )
    negative_adjustment_evidence_floor = (
        promotion_min_closed_trades if negative_adjustment_evidence_applies else 0
    )
    negative_adjustment_evidence_floor_met = closed_count >= negative_adjustment_evidence_floor
    evidence_confidence = (
        min(1.0, closed_count / negative_adjustment_evidence_floor)
        if negative_adjustment_evidence_floor
        else 1.0
    )
    promotion_confidence = _paper_cell_promotion_confidence_review(
        closed_count,
        settings,
        asymmetric_direction_reasons,
        negative_adjustment_evidence_floor,
    )
    promotion_avg_pnl_bps = avg_pnl_bps - _as_float(
        promotion_confidence.get("confidence_penalty_bps"),
        0.0,
    )
    score_components = {
        "pre_sample_size_adjustment_bps": round(avg_pnl_bps, 3),
        "raw_sample_size_penalty_bps": _as_float(
            promotion_confidence.get("raw_confidence_penalty_bps"), 0.0
        ),
        "applied_sample_size_penalty_bps": _as_float(
            promotion_confidence.get("confidence_penalty_bps"), 0.0
        ),
        "post_sample_size_adjustment_bps": round(promotion_avg_pnl_bps, 3),
        "negative_adjustment_evidence_floor": negative_adjustment_evidence_floor,
        "negative_adjustment_evidence_floor_met": negative_adjustment_evidence_floor_met,
        "evidence_confidence": round(evidence_confidence, 3),
        "confidence_status": (
            promotion_confidence.get("confidence_status", "confirmed")
            if promotion_confidence["applies"]
            else "evidence_limited"
            if not negative_adjustment_evidence_floor_met
            else "confirmed"
        ),
    }

    promotion_blockers = []
    if closed_count < promotion_min_closed_trades:
        promotion_blockers.append(
            "insufficient_direction_specific_closed_trades"
            if asymmetric_direction_reasons
            else "insufficient_closed_trades"
        )
    if promotion_confidence["applies"] and (
        promotion_confidence["sample_confidence"] < promotion_confidence["minimum_confidence"]
    ) and negative_adjustment_evidence_floor_met:
        promotion_blockers.append("conditional_frontier_short_promotion_confidence_below_floor")
    if promotion_avg_pnl_bps < promote_min_avg_pnl_bps:
        promotion_blockers.append(
            "conditional_frontier_short_confidence_adjusted_edge_below_floor"
            if promotion_confidence["applies"]
            else "direction_specific_realized_edge_below_floor"
            if asymmetric_direction_reasons
            else "realized_edge_below_floor"
        )
    if win_rate is not None and win_rate < promote_min_win_rate:
        promotion_blockers.append("win_rate_below_floor")
    promotion_blockers.extend(quality_audit["reasons"])
    verified_cost_basis = cost_basis in {
        "net",
        "after_cost",
        "after_costs",
        "after_modeled_context_cost",
    } or bool(cost_audit.get("backfill_applied"))
    if quality_audit["applies"] and not verified_cost_basis:
        promotion_blockers.append("unverified_realized_cost_basis")
    if portability is not None and not portability["promotion_eligible"]:
        promotion_blockers.append(portability["reason"])

    negative_retention_signal = avg_pnl_bps <= revert_avg_pnl_bps
    probation_negative_signal = (
        probation_expired and avg_pnl_bps < 0.0 and prior_state in {"probation", "new"}
    )
    negative_retention_deferred = bool(
        not negative_adjustment_evidence_floor_met
        and (negative_retention_signal or probation_negative_signal)
    )
    retention_audit = {
        "pre_adjustment_avg_pnl_bps": round(avg_pnl_bps, 3),
        "revert_avg_pnl_bps": revert_avg_pnl_bps,
        "negative_retention_signal": negative_retention_signal,
        "probation_negative_signal": probation_negative_signal,
        "negative_adjustment_evidence_floor": negative_adjustment_evidence_floor,
        "negative_adjustment_evidence_floor_met": negative_adjustment_evidence_floor_met,
        "evidence_confidence": round(evidence_confidence, 3),
        "confidence_status": score_components["confidence_status"],
        "negative_adjustment_deferred": negative_retention_deferred,
        "post_adjustment_decision": "probation" if negative_retention_deferred else None,
    }

    if (
        closed_count >= min_closed_trades
        and negative_retention_signal
        and negative_adjustment_evidence_floor_met
    ):
        decision = "reverted"
        action = "rollback_cell"
    elif (
        not promotion_blockers
        and closed_count >= promotion_min_closed_trades
        and promotion_avg_pnl_bps >= promote_min_avg_pnl_bps
        and (win_rate is None or win_rate >= promote_min_win_rate)
    ):
        decision = "promoted"
        action = "promote_cell"
    elif probation_negative_signal and negative_adjustment_evidence_floor_met:
        decision = "reverted"
        action = "rollback_cell"
    else:
        decision = "probation"
        action = "retain_cell_probation"

    return {
        "scope": "paper_signal_cell_policy_v1",
        "cell": cell,
        "cell_key": cell.get("cell_key"),
        "decision": decision,
        "action": action,
        "closed_count": closed_count,
        "avg_pnl_bps": avg_pnl_bps,
        "promotion_avg_pnl_bps": promotion_avg_pnl_bps,
        "promotion_score_components": score_components,
        "reported_avg_pnl_bps": reported_avg_pnl_bps,
        "win_rate": None if win_rate is None or win_rate < 0.0 else win_rate,
        "prior_state": prior_state,
        "probation_ttl_days": probation_ttl_days,
        "probation_expired": probation_expired,
        "promotion_gate": {
            "paper_only": True,
            "direction_asymmetric": bool(asymmetric_direction_reasons),
            "direction_asymmetric_reasons": asymmetric_direction_reasons,
            "min_closed_trades": promotion_min_closed_trades,
            "min_avg_pnl_bps": promote_min_avg_pnl_bps,
            "min_win_rate": promote_min_win_rate,
            "promotion_confidence": promotion_confidence,
            "promotion_score_components": score_components,
            "negative_retention_audit": retention_audit,
            "blockers": promotion_blockers,
            "quality_audit": quality_audit,
            "realized_cost_audit": cost_audit,
            "realized_cost_basis_verified": verified_cost_basis,
            "portability_quarantine": portability,
        },
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


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
    numeric = _finite_float(value)
    if numeric is not None:
        if abs(numeric) > 10_000_000_000.0:
            numeric /= 1000.0
        try:
            return dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def _float_list(value: Any) -> list[float]:
    if isinstance(value, str):
        text = value.strip()
        if text[:1] == "[":
            try:
                value = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        else:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    numbers: list[float] = []
    for item in value:
        numeric = _finite_float(item)
        if numeric is not None:
            numbers.append(numeric)
    return numbers


def _paper_context_loss_policy(config: Mapping[str, Any] | bool | None) -> dict[str, Any]:
    policy = dict(PAPER_CONTEXT_LOSS_QUARANTINE_DEFAULTS)
    if isinstance(config, bool):
        policy["enabled"] = config
        return policy
    if not isinstance(config, Mapping):
        return policy
    for container in (
        config,
        config.get("paper"),
        config.get("paper_policy"),
        config.get("strategy_reliability"),
    ):
        if isinstance(container, Mapping) and isinstance(
            container.get(PAPER_CONTEXT_LOSS_QUARANTINE_POLICY_KEY), Mapping
        ):
            policy.update(container[PAPER_CONTEXT_LOSS_QUARANTINE_POLICY_KEY])
    return policy


def _paper_context_loss_enabled(config: Mapping[str, Any] | bool | None) -> bool:
    if isinstance(config, Mapping):
        for container in (
            config,
            config.get("paper"),
            config.get("paper_policy"),
            config.get("strategy_reliability"),
        ):
            if not isinstance(container, Mapping):
                continue
            for key in PAPER_MODE_CONFIG_KEYS:
                if str(container.get(key) or "").strip().lower() in LIVE_MODE_VALUES:
                    return False
    return _as_bool(_paper_context_loss_policy(config).get("enabled"), True)


def _paper_context_loss_value(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text or default


def paper_context_loss_key(candidate: Mapping[str, Any]) -> str:
    """Return the paper-only venue/surface/type/direction evidence key."""
    asset_surface = (
        candidate.get("asset_surface")
        or candidate.get("execution_surface")
        or candidate.get("market_surface")
        or candidate.get("market_type")
        or candidate.get("trade_type")
    )
    return "|".join(
        (
            _paper_context_loss_value(candidate.get("venue")),
            _paper_context_loss_value(asset_surface),
            _paper_context_loss_value(candidate.get("trade_type")),
            _paper_context_loss_value(candidate.get("direction")),
        )
    )


def _paper_context_loss_stats(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    key = paper_context_loss_key(candidate)
    for field in ("paper_context_loss_stats", "paper_context_loss_statistics"):
        value = candidate.get(field)
        if not isinstance(value, Mapping):
            continue
        if any(name in value for name in ("closed_count", "closed_trades", "sample_size")):
            return dict(value)
        scoped = value.get(key)
        if isinstance(scoped, Mapping):
            return dict(scoped)
    policy_stats = _paper_context_loss_policy(config).get("stats_by_context")
    if isinstance(policy_stats, Mapping):
        scoped = policy_stats.get(key)
        if isinstance(scoped, Mapping):
            return dict(scoped)
    return None


def _paper_context_loss_metric(stats: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        value = stats.get(name)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _paper_context_loss_recovery_passes(stats: Mapping[str, Any] | None, policy: Mapping[str, Any]) -> bool:
    if not isinstance(stats, Mapping):
        return False
    count = _as_int(stats.get("closed_count", stats.get("closed_trades", stats.get("sample_size"))), 0)
    expectancy = _paper_context_loss_metric(stats, "expectancy_bps", "avg_pnl_bps", "recent_expectancy_bps")
    win_rate = _paper_context_loss_metric(stats, "win_rate", "recent_win_rate")
    tail_average = _paper_context_loss_metric(stats, "tail_average_bps", "tail_avg_bps", "average_tail_loss_bps")
    return bool(
        count >= max(1, _as_int(policy.get("recovery_min_closed_trades"), 8))
        and expectancy is not None
        and expectancy > _as_float(policy.get("recovery_min_expectancy_bps"), 0.0)
        and win_rate is not None
        and win_rate >= _as_float(policy.get("recovery_min_win_rate"), 0.50)
        and tail_average is not None
        and tail_average >= _as_float(policy.get("recovery_min_tail_average_bps"), -20.0)
    )


def paper_context_loss_quarantine_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    """Evaluate a durable paper-only context quarantine from closed-trade evidence."""
    if not isinstance(candidate, Mapping) or not _paper_context_loss_enabled(config):
        return None
    policy = _paper_context_loss_policy(config)
    context_key = paper_context_loss_key(candidate)
    stats = _paper_context_loss_stats(candidate, config=config)
    state = candidate.get("paper_context_loss_quarantine_state")
    state = dict(state) if isinstance(state, Mapping) else {}
    if not isinstance(stats, Mapping):
        if str(state.get("status") or "").lower() not in {"active", "cooldown"}:
            return None
        stats = {}

    closed_count = _as_int(stats.get("closed_count", stats.get("closed_trades", stats.get("sample_size"))), 0)
    expectancy = _paper_context_loss_metric(stats, "expectancy_bps", "avg_pnl_bps", "recent_expectancy_bps")
    win_rate = _paper_context_loss_metric(stats, "win_rate", "recent_win_rate")
    tail_average = _paper_context_loss_metric(stats, "tail_average_bps", "tail_avg_bps", "average_tail_loss_bps")
    worst_loss = _paper_context_loss_metric(stats, "worst_bps", "worst_loss_bps", "minimum_pnl_bps")
    tail_negative = bool(
        (tail_average is not None and tail_average <= _as_float(policy.get("max_tail_average_bps"), -20.0))
        or (worst_loss is not None and worst_loss <= _as_float(policy.get("max_worst_loss_bps"), -80.0))
    )
    failure = bool(
        closed_count >= max(1, _as_int(policy.get("min_closed_trades"), 12))
        and expectancy is not None
        and expectancy < _as_float(policy.get("max_expectancy_bps"), 0.0)
        and win_rate is not None
        and win_rate < _as_float(policy.get("max_win_rate"), 0.45)
        and tail_negative
    )

    now = dt.datetime.now(dt.timezone.utc)
    cooldown_complete = _as_bool(stats.get("cooldown_complete"), False)
    cooldown_until = state.get("cooldown_until")
    if cooldown_until:
        try:
            cooldown_complete = cooldown_complete or dt.datetime.fromisoformat(
                str(cooldown_until).replace("Z", "+00:00")
            ) <= now
        except ValueError:
            pass
    recovery = candidate.get("paper_context_recovery_stats")
    recovery = recovery if isinstance(recovery, Mapping) else stats.get("recovery_stats")
    active_state = str(state.get("status") or "").lower() in {"active", "cooldown"}
    recovered = bool(active_state and cooldown_complete and _paper_context_loss_recovery_passes(recovery, policy))
    quarantined = bool((failure or active_state) and not recovered)
    if not quarantined and not failure and not active_state:
        return None

    reason = "paper_context_loss_quarantine" if failure else "paper_context_loss_quarantine_active"
    if active_state and not cooldown_complete:
        reason = "paper_context_loss_quarantine_cooldown"
    if recovered:
        reason = "paper_context_loss_quarantine_recovered"
    return {
        "guard": PAPER_CONTEXT_LOSS_QUARANTINE_POLICY_KEY,
        "reason": reason,
        "paper_only": True,
        "context_key": context_key,
        "context": {
            "venue": context_key.split("|")[0],
            "asset_surface": context_key.split("|")[1],
            "trade_type": context_key.split("|")[2],
            "direction": context_key.split("|")[3],
        },
        "stats": dict(stats),
        "state": state,
        "thresholds": dict(policy),
        "failure_signature": {
            "closed_count": closed_count,
            "expectancy_bps": expectancy,
            "win_rate": win_rate,
            "tail_average_bps": tail_average,
            "worst_loss_bps": worst_loss,
            "tail_negative": tail_negative,
        },
        "quarantined": quarantined,
        "paper_fill_allowed": not quarantined,
        "paper_score_eligible": not quarantined,
        "paper_rank_eligible": not quarantined,
        "paper_score_multiplier": 0.0 if quarantined else 1.0,
        "paper_allocation_multiplier": 0.0 if quarantined else 1.0,
        "promotion_eligible": False,
        "cooldown_complete": cooldown_complete,
        "recovery_required": active_state or failure,
        "recovered": recovered,
        "state_transition": "released" if recovered else "activate" if failure and not active_state else None,
        "release_condition": (
            "After the cooldown, require a new paper sample with positive expectancy, "
            "acceptable win rate, and acceptable tail losses."
        ),
    }


def _portability_policy(config: Mapping[str, Any] | bool | None) -> dict[str, Any]:
    policy: dict[str, Any] = {}
    if isinstance(config, Mapping):
        for container in (
            config,
            config.get("paper"),
            config.get("paper_policy"),
            config.get("strategy_reliability"),
        ):
            if not isinstance(container, Mapping):
                continue
            nested = container.get("paper_portability_quarantine")
            if isinstance(nested, Mapping):
                policy.update(nested)
        for key in PAPER_PORTABILITY_QUARANTINE_FLAG_KEYS:
            if key in config:
                policy["enabled"] = config.get(key)
        for key in ("enabled", "min_closed_count", "min_closed_trades", "min_expectancy_net_bps", "min_expectancy_bps", "neutral_score"):
            if key in config:
                policy[key] = config.get(key)
    elif isinstance(config, bool):
        policy["enabled"] = config
    return policy


def _portability_mode_is_paper(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None,
) -> bool:
    containers: list[Mapping[str, Any]] = [candidate]
    if isinstance(config, Mapping):
        containers.append(config)
        for scope in PAPER_PORTABILITY_QUARANTINE_SCOPES:
            scoped = config.get(scope)
            if isinstance(scoped, Mapping):
                containers.append(scoped)
    for container in containers:
        for key in PAPER_MODE_CONFIG_KEYS:
            mode = _family_identity_token(container.get(key))
            if mode in LIVE_MODE_VALUES:
                return False
    return True


def _portability_family(value: Any) -> str | None:
    token = _family_identity_token(value)
    if not token:
        return None
    parts = set(token.split("_"))
    if "crypto" in parts or parts.intersection({"perp", "perpetual", "swap"}):
        return "crypto"
    if "proxy" in parts or "yahoo" in parts:
        return "proxy"
    if parts.intersection({"equity", "equities", "stock", "stocks", "etf"}):
        return "equities"
    if parts.intersection({"prediction", "event", "kalshi", "polymarket"}):
        return "prediction_markets"
    if parts.intersection({"energy", "power", "electricity"}):
        return "energy"
    return token


def _first_portability_family(
    containers: list[tuple[str, Mapping[str, Any]]],
    fields: tuple[str, ...],
) -> tuple[str | None, str | None, str | None]:
    for container_name, container in containers:
        for field in fields:
            raw = container.get(field)
            family = _portability_family(raw)
            if family:
                return family, str(raw), f"{container_name}.{field}"
    return None, None, None


def _portability_families(candidate: Mapping[str, Any]) -> dict[str, Any]:
    source_containers: list[tuple[str, Mapping[str, Any]]] = []
    destination_containers: list[tuple[str, Mapping[str, Any]]] = []
    for field in ("source_context", "origin_context", "recommendation_context", "lineage_source_context"):
        nested = candidate.get(field)
        if isinstance(nested, Mapping):
            source_containers.append((field, nested))
    source_containers.append(("candidate", candidate))
    for field in ("destination_context", "target_context", "execution_context", "route_context", "candidate"):
        nested = candidate.get(field) if field != "candidate" else candidate
        if isinstance(nested, Mapping):
            destination_containers.append((field, nested))

    source = _first_portability_family(
        source_containers,
        (
            "source_market_family",
            "origin_market_family",
            "source_execution_family",
            "source_family",
            "data_source_family",
            "observation_source_family",
            "market_family",
            "execution_family",
            "instrument_family",
            "asset_class",
        ),
    )
    destination = _first_portability_family(
        destination_containers,
        (
            "destination_execution_family",
            "destination_market_family",
            "target_market_family",
            "target_execution_family",
            "execution_family",
            "market_family",
            "instrument_family",
            "asset_class",
            "market_surface",
        ),
    )
    return {
        "source_family": source[0],
        "source_family_raw": source[1],
        "source_family_field": source[2],
        "destination_family": destination[0],
        "destination_family_raw": destination[1],
        "destination_family_field": destination[2],
    }


def _portability_evidence(candidate: Mapping[str, Any]) -> dict[str, Any]:
    containers: list[tuple[str, Mapping[str, Any]]] = []
    for field in (
        "target_surface_paper_evidence",
        "target_surface_paper_proof",
        "destination_surface_paper_evidence",
        "destination_surface_paper_stats",
        "destination_family_paper_proof",
        "destination_family_paper_stats",
        "destination_paper_stats",
        "translated_variant_paper_stats",
        "portability_evidence",
        "paper_portability_evidence",
        "paper_performance",
    ):
        nested = candidate.get(field)
        if isinstance(nested, Mapping):
            containers.append((field, nested))
    containers.append(("candidate", candidate))

    closed_count = None
    closed_count_field = None
    expectancy_net_bps = None
    expectancy_field = None
    for name, container in containers:
        if closed_count is None:
            for field in (
                "destination_family_closed_count",
                "destination_paper_closed_count",
                "closed_count",
                "closed_trades",
                "sample_size",
                "trade_count",
            ):
                if container.get(field) is not None:
                    closed_count = max(0, _as_int(container.get(field), 0))
                    closed_count_field = f"{name}.{field}"
                    break
        if expectancy_net_bps is None:
            for field in (
                "destination_family_expectancy_net_bps",
                "destination_expectancy_net_bps",
                "expectancy_net_bps",
                "net_expectancy_bps",
                "recent_expectancy_bps",
                "expectancy_bps",
                "avg_pnl_bps",
            ):
                value = _maybe_float(container.get(field))
                if value is not None:
                    expectancy_net_bps = value
                    expectancy_field = f"{name}.{field}"
                    break
    return {
        "closed_count": closed_count,
        "closed_count_field": closed_count_field,
        "expectancy_net_bps": expectancy_net_bps,
        "expectancy_field": expectancy_field,
    }


def paper_portability_quarantine_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    """Require destination-family paper proof for cross-family translations.

    This policy only annotates paper research candidates. Native destination
    strategies never enter the quarantine, and translated variants cannot rank
    above neutral or promote until their destination family has sufficient,
    strictly positive net paper expectancy.
    """
    if not isinstance(candidate, Mapping) or not _portability_mode_is_paper(candidate, config):
        return None
    policy = _portability_policy(config)
    if not _as_bool(policy.get("enabled"), True):
        return None

    families = _portability_families(candidate)
    source_family = families.get("source_family")
    destination_family = families.get("destination_family")
    if not source_family or not destination_family or source_family == destination_family:
        return None

    evidence = _portability_evidence(candidate)
    min_closed_count = max(
        1,
        _as_int(
            policy.get("min_closed_count", policy.get("min_closed_trades")),
            PAPER_PORTABILITY_MIN_CLOSED_COUNT,
        ),
    )
    min_expectancy = _as_float(
        policy.get("min_expectancy_net_bps", policy.get("min_expectancy_bps")),
        PAPER_PORTABILITY_MIN_EXPECTANCY_NET_BPS,
    )
    closed_count = evidence.get("closed_count")
    expectancy = evidence.get("expectancy_net_bps")
    sufficient = closed_count is not None and closed_count >= min_closed_count
    positive = expectancy is not None and expectancy > min_expectancy
    proven = bool(sufficient and positive)

    target_surface_review = paper_only_proxy_frontier_target_evidence_review(
        dict(candidate), dict(config) if isinstance(config, Mapping) else {}
    )
    proxy_momentum_frontier_transfer = bool(
        source_family == "proxy"
        and destination_family == "crypto"
        and target_surface_review.get("applies")
    )
    if proxy_momentum_frontier_transfer:
        transplant_review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            dict(candidate), dict(config) if isinstance(config, Mapping) else {}
        )
        proven = bool(proven and transplant_review.get("eligible"))
    else:
        transplant_review = None

    if proxy_momentum_frontier_transfer and not transplant_review.get("eligible"):
        reason = transplant_review.get("reason") or "proxy_frontier_transplant_quarantined"
        state = "hard_quarantine_pending_source_and_local_confirmation"
    elif not sufficient:
        reason = "insufficient_destination_family_paper_evidence"
        state = "pending_destination_family_proof"
    elif expectancy is None:
        reason = "missing_destination_family_expectancy"
        state = "pending_destination_family_proof"
    elif not positive:
        reason = "non_positive_destination_family_expectancy"
        state = "destination_family_expectancy_failed"
    else:
        reason = "positive_destination_family_paper_expectancy"
        state = "destination_family_proven"

    return {
        "guard": "paper_cross_family_portability_quarantine",
        "paper_only": True,
        "applies": True,
        "eligible": proven,
        "quarantined": not proven,
        "reason": reason,
        "state": state,
        **families,
        **evidence,
        "min_closed_count": min_closed_count,
        "min_expectancy_net_bps": min_expectancy,
        "sufficient_closed_count": sufficient,
        "positive_destination_expectancy": positive,
        "destination_family_proof": proven,
        "rank_above_neutral_allowed": proven,
        "paper_rank_eligible": proven,
        # A proxy-to-frontier transfer without exact-surface proof is capped
        # at sandbox ranking.  The rank remains observational only; promotion
        # and paper fills stay blocked until the target-surface guard passes.
        "sandbox_rank_eligible": bool(
            proxy_momentum_frontier_transfer
            and target_surface_review.get("sandbox_rank_eligible", False)
        ),
        "maximum_stage": (
            "paper_promotion"
            if proven
            else "sandbox_ranking"
            if proxy_momentum_frontier_transfer
            else "quarantined"
        ),
        "promotion_eligible": proven,
        "promotion_blocked": not proven,
        # Missing transfer proof changes ranking and promotion only.  Keep a
        # priceable candidate available to the paper loop as an observation.
        "paper_fill_allowed": True,
        "paper_score_multiplier": 1.0 if proven else PAPER_TRANSLATED_ROUTE_OBSERVATION_MULTIPLIER,
        "paper_allocation_multiplier": 1.0 if proven else PAPER_TRANSLATED_ROUTE_OBSERVATION_MULTIPLIER,
        "observation_only": not proven,
        "neutral_score": _as_float(policy.get("neutral_score"), 0.0),
        "target_surface_paper_evidence_review": (
            target_surface_review if proxy_momentum_frontier_transfer else None
        ),
        "proxy_frontier_transplant_review": transplant_review,
    }


def _flag_override(source: Any, keys: tuple[str, ...], scopes: tuple[str, ...]) -> bool | None:
    if isinstance(source, bool):
        return source
    if not isinstance(source, Mapping):
        return None

    for key in keys:
        if key in source:
            return _as_bool(source.get(key), True)

    for scope in scopes:
        scoped = source.get(scope)
        if not isinstance(scoped, Mapping):
            continue
        for key in keys:
            if key in scoped:
                return _as_bool(scoped.get(key), True)
    return None


def paper_family_quarantine_enabled(
    candidate: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | bool | None = None,
) -> bool:
    override = _flag_override(config, PAPER_FAMILY_QUARANTINE_FLAG_KEYS, PAPER_FAMILY_QUARANTINE_SCOPES)
    if override is not None:
        return override
    override = _flag_override(candidate, PAPER_FAMILY_QUARANTINE_FLAG_KEYS, PAPER_FAMILY_QUARANTINE_SCOPES)
    if override is not None:
        return override
    return True


def _paper_family_quarantine_applies_in_context(
    config: Mapping[str, Any] | bool | None = None,
) -> bool:
    """Keep this policy unreachable from explicitly live runtime contexts."""
    if isinstance(config, bool) or not isinstance(config, Mapping):
        return True

    containers: list[Mapping[str, Any]] = [config]
    for scope in PAPER_FAMILY_QUARANTINE_SCOPES:
        scoped = config.get(scope)
        if isinstance(scoped, Mapping):
            containers.append(scoped)
    for container in containers:
        for key in PAPER_MODE_CONFIG_KEYS:
            mode = str(container.get(key) or "").strip().lower().replace("-", "_").replace(" ", "_")
            if mode in LIVE_MODE_VALUES:
                return False
    return True


def _yahoo_proxy_freshness_shadow_policy(
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any]:
    policy = dict(YAHOO_PROXY_FRESHNESS_SHADOW_DEFAULTS)
    if not isinstance(config, Mapping):
        return policy
    configured = config.get(YAHOO_PROXY_FRESHNESS_SHADOW_POLICY_KEY)
    if isinstance(configured, Mapping):
        policy.update(configured)
    for scope in YAHOO_PROXY_FRESHNESS_SHADOW_SCOPES:
        scoped = config.get(scope)
        if isinstance(scoped, Mapping):
            nested = scoped.get(YAHOO_PROXY_FRESHNESS_SHADOW_POLICY_KEY)
            if isinstance(nested, Mapping):
                policy.update(nested)
    return policy


def _yahoo_proxy_freshness_lookup(
    candidate: Mapping[str, Any],
    *fields: str,
) -> Any:
    containers: list[Mapping[str, Any]] = [candidate]
    for field in ("proxy_reuse_gate", "paper_yahoo_proxy_freshness_gate"):
        nested = candidate.get(field)
        if isinstance(nested, Mapping):
            containers.append(nested)
    for container in containers:
        for field in fields:
            value = container.get(field)
            if value not in (None, "", [], {}, ()):
                return value
    return None


def _yahoo_proxy_cross_tick_consistency(
    candidate: Mapping[str, Any],
    *,
    min_tick_move_bps: float,
    min_tick_observations: int,
    min_alignment_ratio: float,
) -> tuple[list[float], float | None, bool | None]:
    direction = str(candidate.get("direction") or "")
    direction_sign = 1.0 if direction == "long_proxy" else -1.0 if direction == "short_proxy" else 0.0
    if direction_sign == 0.0:
        return [], None, None

    tick_returns = _float_list(
        _yahoo_proxy_freshness_lookup(
            candidate,
            "pre_entry_tick_returns_bps",
            "recent_bar_returns_bps",
            "proxy_tick_returns_bps",
        )
    )
    if not tick_returns:
        short_return_pct = _finite_float(
            _yahoo_proxy_freshness_lookup(candidate, "short_return_pct")
        )
        if short_return_pct is not None:
            tick_returns = [short_return_pct * 100.0]
    if not tick_returns:
        followthrough = _finite_float(
            _yahoo_proxy_freshness_lookup(candidate, "live_session_followthrough_bps")
        )
        if followthrough is not None:
            tick_returns = [followthrough]

    filtered = [value for value in tick_returns if abs(value) >= min_tick_move_bps]
    if len(filtered) < max(1, min_tick_observations):
        return filtered, None, None

    aligned = sum(1 for value in filtered if direction_sign * value > 0.0)
    ratio = aligned / len(filtered) if filtered else None
    consistent = ratio is not None and ratio >= min_alignment_ratio
    return filtered, ratio, consistent


def paper_yahoo_proxy_freshness_shadow_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    if not isinstance(candidate, Mapping):
        return None
    if str(candidate.get("venue") or "").upper() != "YAHOO_PROXY":
        return None
    if str(candidate.get("trade_type") or "") != "global_proxy_momentum":
        return None
    if not _paper_family_quarantine_applies_in_context(config):
        return None

    policy = _yahoo_proxy_freshness_shadow_policy(config)
    if not _as_bool(policy.get("enabled"), True):
        return None

    evaluated_at = (
        _parse_timestamp(_yahoo_proxy_freshness_lookup(candidate, "seen_at", "decision_time_utc"))
        or dt.datetime.now(dt.timezone.utc)
    )
    quote_age = _finite_float(
        _yahoo_proxy_freshness_lookup(
            candidate,
            "source_quote_age_seconds",
            "provider_age_seconds",
            "quote_age_seconds",
            "proxy_quote_age_seconds",
        )
    )
    source_quote_timestamp = _parse_timestamp(
        _yahoo_proxy_freshness_lookup(
            candidate,
            "source_quote_timestamp",
            "source_bar_end_utc",
            "last_bar_utc",
        )
    )
    if quote_age is None and source_quote_timestamp is not None:
        quote_age = max(0.0, (evaluated_at - source_quote_timestamp).total_seconds())

    last_trade_age = _finite_float(
        _yahoo_proxy_freshness_lookup(
            candidate,
            "last_trade_age_seconds",
            "provider_age_seconds",
            "quote_age_seconds",
        )
    )
    last_trade_timestamp = _parse_timestamp(
        _yahoo_proxy_freshness_lookup(
            candidate,
            "last_trade_timestamp",
            "source_bar_end_utc",
            "last_bar_utc",
        )
    )
    if last_trade_age is None and last_trade_timestamp is not None:
        last_trade_age = max(0.0, (evaluated_at - last_trade_timestamp).total_seconds())

    explicit_session_open = _yahoo_proxy_freshness_lookup(candidate, "source_session_open", "proxy_session_open")
    source_session_open = _as_bool(explicit_session_open, False) if explicit_session_open is not None else None
    session_status = str(
        _yahoo_proxy_freshness_lookup(candidate, "source_session_status", "proxy_session_status") or ""
    ).strip().lower()
    if source_session_open is None:
        if session_status in {"open", "regular", "trading", "active"}:
            source_session_open = True
        elif session_status in {"closed", "after_hours", "off_session", "halted", "holiday", "weekend"}:
            source_session_open = False

    tick_returns, alignment_ratio, cross_tick_consistent = _yahoo_proxy_cross_tick_consistency(
        candidate,
        min_tick_move_bps=max(0.0, _as_float(policy.get("min_tick_move_bps"), 3.0)),
        min_tick_observations=max(1, _as_int(policy.get("min_tick_observations"), 2)),
        min_alignment_ratio=max(0.0, min(1.0, _as_float(policy.get("min_alignment_ratio"), 0.5))),
    )
    if quote_age is None or source_session_open is None or cross_tick_consistent is None:
        return None

    max_quote_age = _finite_float(
        _yahoo_proxy_freshness_lookup(candidate, "max_quote_age_seconds", "max_source_quote_age_seconds")
    )
    if max_quote_age is None or max_quote_age <= 0.0:
        max_quote_age = max(1.0, _as_float(policy.get("max_quote_age_seconds"), 20.0 * 60.0))
    max_last_trade_age = _finite_float(
        _yahoo_proxy_freshness_lookup(candidate, "max_last_trade_age_seconds")
    )
    if max_last_trade_age is None or max_last_trade_age <= 0.0:
        max_last_trade_age = max(1.0, _as_float(policy.get("max_last_trade_age_seconds"), max_quote_age))

    reasons: list[str] = []
    if quote_age > max_quote_age:
        reasons.append("proxy_quote_age_exceeded")
    if last_trade_age is not None and last_trade_age > max_last_trade_age:
        reasons.append("proxy_last_trade_age_exceeded")
    if source_session_open is not True:
        reasons.append("source_session_closed" if source_session_open is False else "source_session_unknown")
    if cross_tick_consistent is False:
        reasons.append("cross_tick_direction_inconsistent")

    reuse_reasons = _yahoo_proxy_freshness_lookup(candidate, "reasons")
    if isinstance(reuse_reasons, list) and "opening_gap_without_live_followthrough" in reuse_reasons:
        reasons.append("opening_gap_without_live_followthrough")

    reasons = list(dict.fromkeys(reasons))
    degraded = bool(reasons)
    return {
        "enabled": True,
        "paper_only": True,
        "applies": True,
        "eligible": not degraded,
        "paper_fill_allowed": not degraded,
        "paper_observation_only": degraded,
        "paper_execution_semantics": (
            "synthetic_research_not_live_equivalent" if degraded else "direct_live_equivalent"
        ),
        "signal_stats_scope": "synthetic_research" if degraded else "direct",
        "reason": reasons[0] if reasons else "fresh_proxy_session_confirmed",
        "reasons": reasons,
        "quote_age_seconds": round(quote_age, 3),
        "max_quote_age_seconds": round(max_quote_age, 3),
        "last_trade_age_seconds": round(last_trade_age, 3) if last_trade_age is not None else None,
        "max_last_trade_age_seconds": round(max_last_trade_age, 3),
        "source_quote_timestamp": source_quote_timestamp.isoformat() if source_quote_timestamp else None,
        "last_trade_timestamp": last_trade_timestamp.isoformat() if last_trade_timestamp else None,
        "source_session_open": source_session_open,
        "source_session_status": session_status or ("open" if source_session_open else "closed"),
        "cross_tick_returns_bps": [round(value, 3) for value in tick_returns],
        "cross_tick_alignment_ratio": round(alignment_ratio, 6) if alignment_ratio is not None else None,
        "cross_tick_consistent": cross_tick_consistent,
        "hard_block_promotion_state": "shadow_evaluation_pending",
    }


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


def _family_identity_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "_".join(part for part in "".join(char if char.isalnum() else "_" for char in text).split("_") if part)


def _identity_has_prefix(identity: str, prefix: str) -> bool:
    return identity == prefix or identity.startswith(f"{prefix}_")


def _paper_family_containers(candidate: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Return nested structured metadata without relying on prose inference."""
    containers: list[tuple[str, Mapping[str, Any]]] = []
    pending: list[tuple[str, Mapping[str, Any], int]] = [("candidate", candidate, 0)]
    seen: set[int] = set()
    while pending:
        name, container, depth = pending.pop(0)
        identity = id(container)
        if identity in seen:
            continue
        seen.add(identity)
        containers.append((name, container))
        if depth >= 5:
            continue
        for field, value in container.items():
            if isinstance(value, Mapping):
                pending.append((f"{name}.{field}", value, depth + 1))
            elif isinstance(value, (list, tuple)):
                for index, nested in enumerate(value):
                    if isinstance(nested, Mapping):
                        pending.append((f"{name}.{field}[{index}]", nested, depth + 1))
    return containers


def _family_field_tokens(value: Any) -> set[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return {_family_identity_token(item) for item in values if _family_identity_token(item)}


def _paper_family_quarantine_match(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    """Resolve the family from structured metadata first, then bounded name prefixes."""
    family_key_prefix = _family_identity_token(QUARANTINED_PAPER_FAMILY_KEY)
    source_fields = (
        "source_family",
        "data_source_family",
        "observation_source_family",
        "venue",
        "venues",
        "allowed_venues",
        "source_venue",
        "market_key",
        "source_market_key",
        "family_key",
        "signal_family",
    )
    strategy_fields = (
        "strategy_family",
        "candidate_family",
        "feature_family",
        "feature_set_family",
        "trade_type",
        "trade_types",
        "source_trade_types",
        "strategy",
        "family",
        "signal_family",
    )
    family_key_fields = (
        "family_key",
        "strategy_family_key",
        "paper_family_key",
        "market_key",
        "signal_key",
        "source_market_key",
        "source_signal_key",
    )

    for container_name, container in _paper_family_containers(candidate):
        for field in family_key_fields:
            identity = _family_identity_token(container.get(field))
            if identity and _identity_has_prefix(identity, family_key_prefix):
                return {
                    "type": "family_key_prefix",
                    "field": f"{container_name}.{field}",
                    "value": str(container.get(field)),
                }

        source_matches = [
            field
            for field in source_fields
            if QUARANTINED_PAPER_SOURCE_FAMILY in _family_field_tokens(container.get(field))
        ]
        strategy_matches = [
            field
            for field in strategy_fields
            if QUARANTINED_PAPER_STRATEGY_FAMILY in _family_field_tokens(container.get(field))
        ]
        if source_matches and strategy_matches:
            return {
                "type": "family_metadata",
                "field": container_name,
                "source_fields": source_matches,
                "strategy_fields": strategy_matches,
                "value": QUARANTINED_PAPER_FAMILY_KEY,
            }

    lineage_fields = (
        "strategy_lab_id",
        "parent_strategy_lab_id",
        "market_key",
        "signal_key",
        "strategy_id",
        "variant_id",
        "lineage",
        "lineage_tags",
        "parent_strategy",
        "parent_strategy_id",
        "parent_variant",
        "parent_signal_key",
    )
    normalized_prefixes = tuple(_family_identity_token(value) for value in QUARANTINED_STRATEGY_LAB_PREFIXES)
    for container_name, container in _paper_family_containers(candidate):
        for field in lineage_fields:
            for text in _lineage_texts(container.get(field)):
                identity = _family_identity_token(text)
                for raw_prefix, prefix in zip(QUARANTINED_STRATEGY_LAB_PREFIXES, normalized_prefixes):
                    if _identity_has_prefix(identity, prefix) or _identity_has_prefix(identity, f"strategy_lab_{prefix}"):
                        return {
                            "type": "strategy_lab_name_prefix",
                            "field": f"{container_name}.{field}",
                            "value": text,
                            "prefix": raw_prefix,
                        }

    containers = _paper_family_containers(candidate)
    lineage_tokens = {
        token
        for _container_name, container in containers
        for field in ("lineage", "lineage_tags")
        for text in _lineage_texts(container.get(field))
        for token in _family_identity_token(text).split("_")
        if token
    }
    lineage_text = "_".join(
        text
        for _container_name, container in containers
        for field in ("lineage", "lineage_tags")
        for text in (_family_identity_token(container.get(field)),)
        if text
    )
    if (
        {"yahoo", "proxy"}.issubset(lineage_tokens)
        and QUARANTINED_PAPER_STRATEGY_FAMILY in lineage_text
    ):
        return {
            "type": "lineage_family_metadata",
            "field": "lineage",
            "value": QUARANTINED_PAPER_FAMILY_KEY,
        }
    return None


def _source_veto_policy_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    for container in (
        config,
        config.get("paper_policy"),
        config.get("strategy_lab"),
        config.get("strategy_reliability"),
    ):
        if not isinstance(container, Mapping):
            continue
        for key in (SOURCE_VETO_POLICY_KEY, "paper_source_veto", "source_veto"):
            policy = container.get(key)
            if isinstance(policy, Mapping):
                return policy
    return {}


def _recovery_window_passes(
    window: Any,
    *,
    min_samples: int,
    min_diagnostic_pass_rate: float,
) -> bool:
    if not isinstance(window, Mapping):
        return False
    expectancy = _maybe_float(
        window.get("after_cost_expectancy_bps", window.get("expectancy_bps", window.get("avg_pnl_bps")))
    )
    samples = _as_int(window.get("sample_count", window.get("closed_count", window.get("count"))), 0)
    freshness = window.get("freshness_acceptable")
    if freshness is None:
        freshness = _as_float(window.get("freshness_pass_rate"), -1.0) >= min_diagnostic_pass_rate
    execution_quality = window.get("execution_quality_acceptable")
    if execution_quality is None:
        execution_quality = (
            _as_float(window.get("execution_quality_pass_rate"), -1.0) >= min_diagnostic_pass_rate
        )
    return bool(
        expectancy is not None
        and expectancy >= 0.0
        and samples >= min_samples
        and _as_bool(freshness, False)
        and _as_bool(execution_quality, False)
    )


def paper_source_veto_recovery_status(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the fail-closed recovery contract from trusted runtime settings."""
    policy = _source_veto_policy_config(config)
    evidence = policy.get("recovery_evidence") if isinstance(policy.get("recovery_evidence"), Mapping) else {}
    min_windows = max(1, _as_int(policy.get("min_recovery_windows"), SOURCE_VETO_DEFAULT_MIN_WINDOWS))
    min_samples = max(
        1,
        _as_int(policy.get("min_samples_per_window"), SOURCE_VETO_DEFAULT_MIN_SAMPLES_PER_WINDOW),
    )
    min_diagnostic_pass_rate = max(
        0.0,
        min(
            1.0,
            _as_float(
                policy.get("min_diagnostic_pass_rate"),
                SOURCE_VETO_DEFAULT_MIN_DIAGNOSTIC_PASS_RATE,
            ),
        ),
    )
    scopes: dict[str, Any] = {}
    for scope in ("source_family", "immediate_descendants"):
        scoped = evidence.get(scope) if isinstance(evidence, Mapping) else None
        windows = scoped.get("windows") if isinstance(scoped, Mapping) else None
        windows = windows if isinstance(windows, list) else []
        passing = [
            _recovery_window_passes(
                window,
                min_samples=min_samples,
                min_diagnostic_pass_rate=min_diagnostic_pass_rate,
            )
            for window in windows
        ]
        scopes[scope] = {
            "window_count": len(windows),
            "passing_window_count": sum(passing),
            "recovered": len(windows) >= min_windows and all(passing),
        }
    recovered = all(scope["recovered"] for scope in scopes.values())
    return {
        "recovered": recovered,
        "required_scopes": list(scopes),
        "min_recovery_windows": min_windows,
        "min_samples_per_window": min_samples,
        "min_diagnostic_pass_rate": min_diagnostic_pass_rate,
        "scopes": scopes,
    }


def _lineage_source_health_policy_config(
    config: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    containers = (
        config,
        config.get("paper_policy"),
        config.get("strategy_lab"),
        config.get("strategy_reliability"),
    )
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        policy = container.get(LINEAGE_SOURCE_HEALTH_POLICY_KEY)
        if isinstance(policy, Mapping):
            return policy
        source_veto = container.get(SOURCE_VETO_POLICY_KEY)
        if isinstance(source_veto, Mapping):
            nested = source_veto.get(LINEAGE_SOURCE_HEALTH_POLICY_KEY)
            if isinstance(nested, Mapping):
                return nested
    return {}


def _lineage_source_health_enabled(config: Mapping[str, Any] | bool | None) -> bool:
    if not _paper_family_quarantine_applies_in_context(config):
        return False
    if isinstance(config, bool):
        return config
    policy = _lineage_source_health_policy_config(config if isinstance(config, Mapping) else None)
    return _as_bool(policy.get("enabled"), True)


def _lineage_source_keys(candidate: Mapping[str, Any]) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    for container_name, container in _paper_family_containers(candidate):
        for field in LINEAGE_SOURCE_SIGNAL_KEY_FIELDS:
            value = str(container.get(field) or "").strip()
            if value and (f"{container_name}.{field}", value) not in keys:
                keys.append((f"{container_name}.{field}", value))
    return keys


def _lineage_source_health_evidence(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    source_keys = _lineage_source_keys(candidate)
    fallback_source_key = source_keys[0][1] if source_keys else None
    for container_name, container in _paper_family_containers(candidate):
        for field in LINEAGE_SOURCE_HEALTH_FIELDS:
            raw = container.get(field)
            if not isinstance(raw, Mapping):
                continue
            expectancy = _maybe_float(
                raw.get(
                    "after_cost_expectancy_bps",
                    raw.get(
                        "expectancy_net_bps",
                        raw.get(
                            "realized_edge_bps",
                            raw.get("net_edge_bps", raw.get("avg_pnl_bps")),
                        ),
                    ),
                )
            )
            closed_count = _as_int(
                raw.get("closed_count", raw.get("sample_count", raw.get("count"))),
                0,
            )
            status = str(raw.get("status") or raw.get("health_status") or "").strip().lower()
            persistent_negative = _as_bool(raw.get("persistent_negative"), False) or status in {
                "persistent_negative",
                "negative_edge",
                "degraded_negative_edge",
                "quarantined_negative_edge",
            }
            if expectancy is None and not persistent_negative:
                continue
            evidence.append(
                {
                    "source_field": f"{container_name}.{field}",
                    "source_signal_key": str(raw.get("source_signal_key") or fallback_source_key or ""),
                    "closed_count": closed_count,
                    "after_cost_expectancy_bps": expectancy,
                    "win_rate": _maybe_float(raw.get("win_rate")),
                    "updated_at": raw.get("updated_at") or raw.get("observed_at"),
                    "evidence_source": raw.get("evidence_source") or "candidate_lineage_metadata",
                    "cost_basis": raw.get("cost_basis") or "realized_paper_after_cost",
                    "persistent_negative": persistent_negative,
                }
            )
    return evidence


def paper_lineage_source_health_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    """Penalize or quarantine paper descendants of negative-edge source signals."""
    if not isinstance(candidate, Mapping) or not _lineage_source_health_enabled(config):
        return None
    evidence = _lineage_source_health_evidence(candidate)
    if not evidence:
        return None

    policy = _lineage_source_health_policy_config(config if isinstance(config, Mapping) else None)
    quarantine_min_count = max(
        1,
        _as_int(
            policy.get("min_closed_count"),
            LINEAGE_SOURCE_HEALTH_DEFAULT_MIN_CLOSED_COUNT,
        ),
    )
    penalty_min_count = max(
        1,
        min(
            quarantine_min_count,
            _as_int(
                policy.get("penalty_min_closed_count"),
                LINEAGE_SOURCE_HEALTH_DEFAULT_PENALTY_MIN_CLOSED_COUNT,
            ),
        ),
    )
    negative_edge_floor = _as_float(policy.get("negative_edge_floor_bps"), 0.0)
    penalty_multiplier = max(
        0.0,
        min(
            1.0,
            _as_float(
                policy.get("penalty_score_multiplier"),
                LINEAGE_SOURCE_HEALTH_DEFAULT_PENALTY_MULTIPLIER,
            ),
        ),
    )
    negative = [
        item
        for item in evidence
        if item["persistent_negative"]
        or (
            item["after_cost_expectancy_bps"] is not None
            and item["after_cost_expectancy_bps"] < negative_edge_floor
        )
    ]
    if not negative:
        return None
    selected = sorted(
        negative,
        key=lambda item: (
            bool(item["persistent_negative"]),
            int(item["closed_count"]),
            -_as_float(item["after_cost_expectancy_bps"], negative_edge_floor),
        ),
        reverse=True,
    )[0]
    persistent = bool(
        selected["persistent_negative"]
        or int(selected["closed_count"]) >= quarantine_min_count
    )
    if not persistent and int(selected["closed_count"]) < penalty_min_count:
        return None
    action = "quarantine" if persistent else "penalize"
    multiplier = 0.0 if persistent else penalty_multiplier
    return {
        "reason": (
            "paper_lineage_source_negative_edge_quarantine"
            if persistent
            else "paper_lineage_source_negative_edge_penalty"
        ),
        "guard": "paper_lineage_source_health",
        "policy_key": LINEAGE_SOURCE_HEALTH_POLICY_KEY,
        "paper_only": True,
        "action": action,
        "eligible": not persistent,
        "paper_score_eligible": not persistent,
        "paper_rank_eligible": not persistent,
        "paper_fill_allowed": not persistent,
        "paper_score_multiplier": multiplier,
        "paper_allocation_multiplier": multiplier,
        "promotion_eligible": False,
        "source_health": selected,
        "thresholds": {
            "negative_edge_floor_bps": negative_edge_floor,
            "penalty_min_closed_count": penalty_min_count,
            "quarantine_min_closed_count": quarantine_min_count,
        },
        "release_condition": QUARANTINE_RELEASE_CONDITION,
    }


def hydrate_paper_lineage_source_health(
    candidates: list[dict],
    conn: Any | None,
) -> None:
    """Attach read-only persisted source statistics to explicit paper lineages."""
    if conn is None:
        return
    requested: dict[str, list[dict]] = collections.defaultdict(list)
    for candidate in candidates:
        for _field, source_key in _lineage_source_keys(candidate):
            requested[source_key].append(candidate)
            break
    if not requested:
        return
    placeholders = ",".join("?" for _ in requested)
    try:
        rows = conn.execute(
            f"""
            select signal_key, closed_count, avg_pnl_bps, win_rate, updated_at
            from signal_stats
            where signal_key in ({placeholders})
            """,
            tuple(requested),
        ).fetchall()
    except Exception:  # noqa: BLE001 - optional read-only runtime evidence
        return
    for raw in rows:
        try:
            row = dict(raw)
        except (TypeError, ValueError):
            row = {
                "signal_key": raw[0],
                "closed_count": raw[1],
                "avg_pnl_bps": raw[2],
                "win_rate": raw[3],
                "updated_at": raw[4],
            }
        source_key = str(row.get("signal_key") or "")
        for candidate in requested.get(source_key, []):
            candidate["lineage_source_health"] = {
                "source_signal_key": source_key,
                "closed_count": row.get("closed_count"),
                "after_cost_expectancy_bps": row.get("avg_pnl_bps"),
                "win_rate": row.get("win_rate"),
                "updated_at": row.get("updated_at"),
                "evidence_source": "persisted_paper_signal_stats",
                "cost_basis": "realized_paper_pnl_bps",
            }


def paper_source_veto_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Block the decayed Yahoo momentum source and descendants in paper R&D."""
    if not isinstance(candidate, Mapping) or not _paper_family_quarantine_applies_in_context(config):
        return None
    matched_on = _paper_family_quarantine_match(candidate)
    if matched_on is None:
        return None
    recovery = paper_source_veto_recovery_status(config)
    if recovery["recovered"]:
        return None
    return {
        "reason": "paper_source_family_veto",
        "guard": "paper_only_source_veto",
        "policy_key": SOURCE_VETO_POLICY_KEY,
        "paper_only": True,
        "eligible": False,
        "creation_allowed": False,
        "paper_score_eligible": False,
        "paper_rank_eligible": False,
        "paper_fill_allowed": False,
        "paper_score_multiplier": 0.0,
        "paper_allocation_multiplier": 0.0,
        "family_key": QUARANTINED_PAPER_FAMILY_KEY,
        "matched_on": matched_on,
        "release_condition": QUARANTINE_RELEASE_CONDITION,
        "recovery": recovery,
    }


def paper_family_quarantine_record(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    if (
        not isinstance(candidate, Mapping)
        or not paper_family_quarantine_enabled(candidate, config)
        or not _paper_family_quarantine_applies_in_context(config)
    ):
        return None
    matched_on = _paper_family_quarantine_match(candidate)
    if matched_on is None:
        return None
    recovery = paper_source_veto_recovery_status(config if isinstance(config, Mapping) else None)
    if recovery["recovered"]:
        return None
    return {
        "reason": "quarantined_family_decay",
        "guard": "paper_strategy_family_quarantine",
        "paper_only": True,
        "eligible": False,
        "paper_score_eligible": False,
        "paper_rank_eligible": False,
        "paper_fill_allowed": False,
        "paper_score_multiplier": 0.0,
        "paper_allocation_multiplier": 0.0,
        "quarantine_action": "shadow_monitor_only",
        "family_key": QUARANTINED_PAPER_FAMILY_KEY,
        "quarantine_family": QUARANTINED_PAPER_FAMILY_KEY,
        "release_condition": QUARANTINE_RELEASE_CONDITION,
        "recovery": recovery,
        "matched_on": matched_on,
        "matched_fields": [matched_on["field"]],
        "matched_descendants": [str(matched_on.get("value") or "").lower()]
        if matched_on.get("type") == "strategy_lab_name_prefix"
        else [],
        "evidence": {
            "source": "paper_closed_trade_labels_current_cycle",
            "finding": "family_level_degradation",
            "long_proxy_standard": {"closed_count": 176, "avg_pnl_bps": -16.225, "win_rate": 0.318},
            "short_proxy_conditional": {"closed_count": 171, "avg_pnl_bps": -24.614, "win_rate": 0.322},
        },
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


def _paper_context_prior_policy(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the paper-only context-prior policy without enabling live use."""
    policy = dict(PAPER_CONTEXT_PRIOR_DEFAULTS)
    policy["venue_direction_feasibility_priors"] = dict(
        PAPER_CONTEXT_PRIOR_DEFAULTS["venue_direction_feasibility_priors"]
    )
    if not isinstance(config, Mapping):
        return policy
    configured_blocks: list[Mapping[str, Any]] = []
    configured = config.get(PAPER_CONTEXT_PRIOR_POLICY_KEY)
    if isinstance(configured, Mapping):
        configured_blocks.append(configured)
    for scope in PAPER_CONTEXT_PRIOR_SCOPES:
        scoped = config.get(scope)
        if not isinstance(scoped, Mapping):
            continue
        nested = scoped.get(PAPER_CONTEXT_PRIOR_POLICY_KEY)
        if isinstance(nested, Mapping):
            configured_blocks.append(nested)
    for block in configured_blocks:
        for key, value in block.items():
            if key == "venue_direction_feasibility_priors" and isinstance(value, Mapping):
                policy[key].update(value)
            elif key == "venue_direction_priors" and isinstance(value, Mapping):
                for slice_key, prior in value.items():
                    normalized_key = str(slice_key or "").strip()
                    if not normalized_key:
                        continue
                    policy["venue_direction_feasibility_priors"][f"{normalized_key}|standard"] = prior
                    policy["venue_direction_feasibility_priors"][f"{normalized_key}|conditional"] = prior
            else:
                policy[key] = value
    return policy


def _paper_context_prior_active(candidate: Mapping[str, Any], config: Mapping[str, Any] | None) -> bool:
    """True only for the paper/research scoring path.

    Context evidence is deliberately a ranking input.  It must never leak into
    a live route, even if this helper is called by a reused candidate pipeline.
    """
    policy = _paper_context_prior_policy(config)
    if not _as_bool(policy.get("enabled"), True):
        return False
    if not _as_bool(policy.get("paper_only"), True):
        return False
    combined: list[Mapping[str, Any]] = [candidate]
    if isinstance(config, Mapping):
        combined.insert(0, config)
        for scope in PAPER_CONTEXT_PRIOR_SCOPES:
            scoped = config.get(scope)
            if isinstance(scoped, Mapping):
                combined.insert(1, scoped)
    paper_mode_confirmed = False
    for scope in combined:
        if _as_bool(scope.get("allow_live_trading"), False):
            return False
        mode = str(
            scope.get("mode")
            or scope.get("runtime_mode")
            or scope.get("execution_mode")
            or scope.get("trading_mode")
            or ""
        ).strip().lower()
        if mode in LIVE_MODE_VALUES:
            return False
        if mode in PAPER_MODE_VALUES:
            paper_mode_confirmed = True
    return paper_mode_confirmed


def _paper_context_direction(candidate: Mapping[str, Any]) -> str:
    explicit = str(candidate.get("position_side") or candidate.get("side") or "").strip().lower()
    if explicit in {"long", "short"}:
        return explicit
    direction = str(candidate.get("direction") or "").strip().lower().replace("-", "_")
    tokens = set(direction.split("_"))
    if "long" in tokens and "short" not in tokens:
        return "long"
    if "short" in tokens and "long" not in tokens:
        return "short"
    return "unknown"


def _paper_context_venue(candidate: Mapping[str, Any]) -> str:
    venue = _venue(dict(candidate))
    surface = str(candidate.get("asset_surface") or candidate.get("market_type") or "").strip().lower()
    trade_type = str(candidate.get("trade_type") or "").strip().lower()
    direction = str(candidate.get("direction") or "").strip().lower()
    if venue == "OKX" and (surface == "spot" or ("perp" not in trade_type and "spot" in direction)):
        return "OKX_SPOT"
    if venue == "BYBIT" and (surface == "spot" or ("perp" not in trade_type and "spot" in direction)):
        return "BYBIT_SPOT"
    return venue


def _paper_context_strategy_family(candidate: Mapping[str, Any]) -> str | None:
    family = " ".join(
        str(candidate.get(field) or "").lower()
        for field in ("strategy_family", "signal_family", "trade_type", "direction", "signal_key")
    )
    if any(token in family for token in ("mean_reversion", "convergence", "long_perp_short_spot")):
        return "convergence_or_mean_reversion"
    if any(token in family for token in ("funding_capture", "short_perp_long_spot", "carry")):
        return "carry_or_funding_capture"
    return None


def _paper_context_feasibility_status(candidate: Mapping[str, Any]) -> str:
    feasibility = candidate.get("execution_feasibility") or {}
    route = candidate.get("execution_route") or {}
    return str(
        feasibility.get("route_status")
        or feasibility.get("status")
        or candidate.get("feasibility_status")
        or route.get("route_status")
        or candidate.get("route_status")
        or "unknown"
    ).strip().lower()


def _paper_context_realized_key(
    candidate: Mapping[str, Any],
    *,
    feasibility_status: str | None = None,
) -> str:
    status = str(feasibility_status or _paper_context_feasibility_status(candidate) or "unknown").strip().lower()
    return f"{_paper_context_venue(candidate)}|{_paper_context_direction(candidate)}|{status or 'unknown'}"


def _paper_context_slice_prior(
    candidate: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    feasibility_status: str,
) -> tuple[float, str]:
    key = _paper_context_realized_key(candidate, feasibility_status=feasibility_status)
    priors = policy.get("venue_direction_feasibility_priors")
    if not isinstance(priors, Mapping):
        return 0.0, key
    prior = _as_float(priors.get(key), 0.0)
    return round(prior, 3), key


def _paper_context_realized_stats(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    *,
    feasibility_status: str,
) -> dict[str, Any] | None:
    key = _paper_context_realized_key(candidate, feasibility_status=feasibility_status)
    for field in (
        "paper_context_realized_stats",
        "paper_context_prior_realized_stats",
        "paper_context_loss_stats",
        "paper_context_loss_statistics",
    ):
        value = candidate.get(field)
        if not isinstance(value, Mapping):
            continue
        if any(name in value for name in ("closed_count", "closed_trades", "sample_size")):
            return dict(value)
        scoped = value.get(key)
        if isinstance(scoped, Mapping):
            return dict(scoped)
    policy_stats = _paper_context_prior_policy(config).get("realized_context_stats")
    if isinstance(policy_stats, Mapping):
        scoped = policy_stats.get(key)
        if isinstance(scoped, Mapping):
            return dict(scoped)
    return None


def _paper_context_realized_prior(
    candidate: Mapping[str, Any],
    policy: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    *,
    feasibility_status: str,
) -> tuple[float, dict[str, Any]]:
    key = _paper_context_realized_key(candidate, feasibility_status=feasibility_status)
    stats = _paper_context_realized_stats(candidate, config, feasibility_status=feasibility_status)
    detail = {
        "key": key,
        "closed_count": 0,
        "avg_pnl_bps": None,
        "win_rate": None,
        "tail_average_bps": None,
        "prior": 0.0,
        "applied": False,
        "persistent_negative": False,
    }
    if not isinstance(stats, Mapping):
        return 0.0, detail

    closed_count = _as_int(stats.get("closed_count", stats.get("closed_trades", stats.get("sample_size"))), 0)
    avg_pnl_bps = _paper_context_loss_metric(stats, "avg_pnl_bps", "expectancy_bps", "recent_expectancy_bps")
    win_rate = _paper_context_loss_metric(stats, "win_rate", "recent_win_rate")
    tail_average_bps = _paper_context_loss_metric(stats, "tail_average_bps", "tail_avg_bps", "average_tail_loss_bps")
    persistent_negative = bool(
        _as_bool(stats.get("persistent_negative"), False)
        or (
            closed_count >= max(1, _as_int(policy.get("realized_context_persistent_negative_closed_trades"), 8))
            and avg_pnl_bps is not None
            and avg_pnl_bps < 0.0
        )
    )
    detail.update(
        {
            "closed_count": closed_count,
            "avg_pnl_bps": round(avg_pnl_bps, 3) if avg_pnl_bps is not None else None,
            "win_rate": round(win_rate, 6) if win_rate is not None else None,
            "tail_average_bps": round(tail_average_bps, 3) if tail_average_bps is not None else None,
            "persistent_negative": persistent_negative,
        }
    )
    if closed_count < max(1, _as_int(policy.get("realized_context_min_closed_trades"), 6)) or avg_pnl_bps is None:
        return 0.0, detail

    prior = 0.0
    if avg_pnl_bps > 0.0:
        prior = min(
            _as_float(policy.get("realized_context_max_positive_prior"), 12.0),
            avg_pnl_bps * _as_float(policy.get("realized_context_positive_scale"), 0.2),
        )
    elif avg_pnl_bps < 0.0:
        multiplier = 1.0
        if feasibility_status == "conditional":
            multiplier *= _as_float(policy.get("realized_context_conditional_penalty_multiplier"), 1.5)
        if persistent_negative:
            multiplier *= _as_float(policy.get("realized_context_persistent_negative_multiplier"), 1.25)
        prior = max(
            _as_float(policy.get("realized_context_max_negative_prior"), -18.0),
            avg_pnl_bps * _as_float(policy.get("realized_context_negative_scale"), 0.3) * multiplier,
        )
    detail["prior"] = round(prior, 3)
    detail["applied"] = bool(prior)
    return round(prior, 3), detail


def _paper_context_rank_gate_applies(
    candidate: Mapping[str, Any],
    *,
    strategy_family: str | None,
) -> bool:
    trade_type = str(candidate.get("trade_type") or "").strip().lower()
    if trade_type in PAPER_CONTEXT_RANK_GATE_TRADE_TYPES:
        return True
    return strategy_family in PAPER_CONTEXT_RANK_GATE_FAMILIES


def _paper_context_rank_gate(
    candidate: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    feasibility_status: str,
    strategy_family: str | None,
    realized_context: Mapping[str, Any],
    pre_gate_score: float,
) -> dict[str, Any]:
    applies = _paper_context_rank_gate_applies(candidate, strategy_family=strategy_family)
    min_closed_trades = max(1, _as_int(policy.get("top_rank_min_closed_trades"), 25))
    min_avg_pnl_bps = _as_float(policy.get("top_rank_min_avg_pnl_bps"), 0.0)
    top_rank_score_cap = round(_as_float(policy.get("top_rank_score_cap"), 75.0), 3)
    conditional_rank_score_cap = round(
        min(
            top_rank_score_cap,
            _as_float(policy.get("conditional_rank_score_cap"), 35.0),
        ),
        3,
    )
    realized_closed_count = _as_int(realized_context.get("closed_count"), 0)
    realized_avg_pnl_bps = _maybe_float(realized_context.get("avg_pnl_bps"))
    sufficient_sample = realized_closed_count >= min_closed_trades
    positive_expectancy = realized_avg_pnl_bps is not None and realized_avg_pnl_bps > min_avg_pnl_bps
    top_rank_eligible = bool(
        applies and feasibility_status == "standard" and sufficient_sample and positive_expectancy
    )
    reason = "not_applicable"
    score_cap = None
    if applies and feasibility_status == "conditional":
        reason = "conditional_context_rank_gated"
        score_cap = conditional_rank_score_cap
    elif applies and not top_rank_eligible:
        if not sufficient_sample:
            reason = "insufficient_venue_direction_closed_trades"
        elif not positive_expectancy:
            reason = "non_positive_venue_direction_expectancy"
        else:
            reason = "top_rank_evidence_not_confirmed"
        score_cap = top_rank_score_cap

    gated_score = round(min(pre_gate_score, score_cap), 3) if score_cap is not None else round(pre_gate_score, 3)
    return {
        "enabled": applies,
        "applied": applies and score_cap is not None and gated_score < round(pre_gate_score, 3),
        "reason": reason,
        "feasibility_status": feasibility_status,
        "min_closed_trades": min_closed_trades,
        "min_avg_pnl_bps": round(min_avg_pnl_bps, 3),
        "realized_closed_count": realized_closed_count,
        "realized_avg_pnl_bps": round(realized_avg_pnl_bps, 3) if realized_avg_pnl_bps is not None else None,
        "sufficient_sample": sufficient_sample,
        "positive_expectancy": positive_expectancy,
        "top_rank_eligible": top_rank_eligible,
        "score_cap": score_cap,
        "pre_gate_score": round(pre_gate_score, 3),
        "final_score": gated_score,
        "promotion_eligible": top_rank_eligible,
    }


def apply_paper_context_priors(
    candidate: dict[str, Any],
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Apply measured route, venue, liquidity, and family priors to paper rank.

    This is intentionally additive and non-blocking: every priceable candidate
    remains available to the exploration layer.  Exceptional base signals keep
    their negative context terms as diagnostics but do not receive a negative
    context adjustment.
    """
    existing = candidate.get("paper_context_prior")
    if isinstance(existing, Mapping) and existing.get("applied"):
        return dict(existing)
    if not _paper_context_prior_active(candidate, config):
        return None

    policy = _paper_context_prior_policy(config)
    base_score = round(_as_float(candidate.get("score"), 0.0), 3)
    feasibility_status = _paper_context_feasibility_status(candidate)
    feasibility_prior = (
        _as_float(policy.get("feasibility_standard_prior"), 6.0)
        if feasibility_status == "standard"
        else _as_float(policy.get("feasibility_conditional_prior"), -8.0)
        if feasibility_status == "conditional"
        else 0.0
    )
    context_slice_prior, context_slice_key = _paper_context_slice_prior(
        candidate,
        policy,
        feasibility_status=feasibility_status,
    )
    liquidity_raw = _maybe_float(candidate.get("liquidity_score"))
    liquidity_score = _normalize_fraction(liquidity_raw) if liquidity_raw is not None else None
    liquidity_prior = 0.0
    liquidity_bucket = "unknown"
    if liquidity_score is not None:
        if liquidity_score >= _normalize_fraction(_as_float(policy.get("strong_liquidity_score"), 0.70)):
            liquidity_prior = _as_float(policy.get("strong_liquidity_prior"), 4.0)
            liquidity_bucket = "strong"
        elif liquidity_score < _normalize_fraction(_as_float(policy.get("weak_liquidity_score"), 0.45)):
            liquidity_prior = _as_float(policy.get("weak_liquidity_prior"), -8.0)
            liquidity_bucket = "weak"
        else:
            liquidity_bucket = "normal"
    strategy_family = _paper_context_strategy_family(candidate)
    realized_context_prior, realized_context = _paper_context_realized_prior(
        candidate,
        policy,
        config,
        feasibility_status=feasibility_status,
    )
    terms = {
        "feasibility_prior": round(feasibility_prior, 3),
        "context_slice_prior": round(context_slice_prior, 3),
        "realized_context_prior": round(realized_context_prior, 3),
        "liquidity_prior": round(liquidity_prior, 3),
    }
    raw_total_prior = round(sum(terms.values()), 3)
    exceptional = base_score >= _as_float(policy.get("exceptional_base_signal_score"), 85.0)
    existing_safety_state = bool(candidate.get("paper_entry_blocked")) or candidate.get("paper_score_eligible") is False
    # A context prior can reorder priceable exploration candidates, but must
    # not revive a score that an earlier immutable/safety guard intentionally
    # set aside.
    total_prior = 0.0 if existing_safety_state else max(0.0, raw_total_prior) if exceptional else raw_total_prior
    final_score = round(max(0.0, min(100.0, base_score + total_prior)), 3)
    detail = {
        "paper_only": True,
        "applied": True,
        "base_signal_score": base_score,
        "final_paper_score": final_score,
        **terms,
        "raw_total_prior": raw_total_prior,
        "total_prior": round(total_prior, 3),
        "exceptional_signal_override": exceptional and raw_total_prior < 0.0,
        "existing_safety_state_preserved": existing_safety_state,
        "feasibility_status": feasibility_status,
        "context_slice_key": context_slice_key,
        "realized_context_key": realized_context["key"],
        "realized_context_closed_count": realized_context["closed_count"],
        "realized_context_avg_pnl_bps": realized_context["avg_pnl_bps"],
        "realized_context_win_rate": realized_context["win_rate"],
        "realized_context_tail_average_bps": realized_context["tail_average_bps"],
        "realized_context_persistent_negative": realized_context["persistent_negative"],
        "liquidity_score": round(liquidity_score, 6) if liquidity_score is not None else None,
        "liquidity_bucket": liquidity_bucket,
        "strategy_family": strategy_family,
    }
    rank_gate = _paper_context_rank_gate(
        candidate,
        policy,
        feasibility_status=feasibility_status,
        strategy_family=strategy_family,
        realized_context=realized_context,
        pre_gate_score=final_score,
    )
    if rank_gate["enabled"]:
        detail["rank_gate"] = rank_gate
        detail["top_rank_eligible"] = bool(rank_gate["top_rank_eligible"])
        detail["promotion_eligible"] = bool(rank_gate["promotion_eligible"])
        if rank_gate["applied"]:
            final_score = _as_float(rank_gate["final_score"], final_score)
            detail["final_paper_score"] = round(final_score, 3)
    else:
        detail["rank_gate"] = None
        detail["top_rank_eligible"] = True
        detail["promotion_eligible"] = bool(candidate.get("promotion_eligible", True))
    candidate["score"] = final_score
    candidate["final_paper_score"] = final_score
    if rank_gate["enabled"]:
        candidate["paper_context_rank_gate"] = dict(rank_gate)
        candidate["paper_context_top_rank_eligible"] = bool(rank_gate["top_rank_eligible"])
        if not rank_gate["promotion_eligible"]:
            candidate["promotion_eligible"] = False
    candidate["paper_context_prior"] = detail
    if rank_gate["applied"]:
        candidate["paper_context_prior_status"] = "ranked_hard_gated"
    elif rank_gate["enabled"] and not rank_gate["promotion_eligible"]:
        candidate["paper_context_prior_status"] = "ranked_promotion_gated"
    else:
        candidate["paper_context_prior_status"] = "ranked_not_blocked"
    if raw_total_prior:
        _append_note(candidate, "paper_context_prior:exceptional_signal_override" if detail["exceptional_signal_override"] else "paper_context_prior:applied")
    if rank_gate["applied"]:
        _append_note(candidate, f"paper_context_rank_gate:{rank_gate['reason']}")
    elif rank_gate["enabled"] and not rank_gate["promotion_eligible"]:
        _append_note(candidate, f"paper_context_promotion_gate:{rank_gate['reason']}")
    reliability = candidate.get("strategy_reliability")
    if isinstance(reliability, dict):
        reliability["paper_context_prior"] = detail
    return detail


def _remove_invalid_proxy_confirmation(candidate: dict) -> dict[str, Any] | None:
    """Remove invalid Yahoo proxy influence while retaining the candidate."""

    if candidate.get("proxy_valid_for_reuse") is not False:
        return None
    original_score = _as_float(candidate.get("score"), 0.0)
    score_before = _maybe_float(candidate.get("score_before_proxy_confirmation"))
    contribution_fields = (
        "proxy_confirmation_score_boost",
        "proxy_confirmation_boost",
        "yahoo_proxy_score_contribution",
        "proxy_momentum_score_contribution",
        "proxy_score_contribution",
    )
    removed_contribution = sum(
        max(0.0, numeric)
        for field in contribution_fields
        for numeric in [_maybe_float(candidate.get(field))]
        if numeric is not None
    )
    adjusted_score = score_before if score_before is not None else original_score - removed_contribution
    if "score" in candidate:
        candidate["score"] = round(max(0.0, min(original_score, adjusted_score)), 3)
    raw_proxy_confirmation = {
        field: candidate.get(field)
        for field in (
            "proxy_confirmed",
            "proxy_confirmation_score",
            "yahoo_proxy_confirmed",
            "yahoo_proxy_confirmation_score",
            "proxy_momentum_confirmed",
            "proxy_momentum_confirmation_score",
            *contribution_fields,
        )
        if field in candidate
    }
    for field in ("proxy_confirmed", "yahoo_proxy_confirmed", "proxy_momentum_confirmed"):
        if field in candidate:
            candidate[field] = False
    for field in (
        "proxy_confirmation_score",
        "yahoo_proxy_confirmation_score",
        "proxy_momentum_confirmation_score",
        *contribution_fields,
    ):
        if field in candidate:
            candidate[field] = 0.0
    candidate["effective_proxy_confirmation_weight"] = 0.0
    candidate["propagated_momentum_contribution"] = 0.0
    candidate["proxy_confirmation_used"] = False
    detail = {
        "paper_only": True,
        "applied": True,
        "reason": "proxy_invalid_for_reuse",
        "original_score": original_score,
        "final_score": candidate.get("score", original_score),
        "removed_score_contribution": round(
            max(0.0, original_score - _as_float(candidate.get("score"), original_score)), 3
        ),
        "effective_proxy_confirmation_weight": 0.0,
        "raw_proxy_confirmation": raw_proxy_confirmation,
    }
    candidate["paper_proxy_reuse_scoring_gate"] = detail
    _append_note(candidate, "paper_proxy_reuse_gate:confirmation_removed")
    return detail


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
        "quality_failure_reason": candidate.get("proxy_short_quality_failure_reason") or candidate.get("quality_failure_reason"),
        "quality_failure_reasons": list(
            candidate.get("proxy_short_quality_failure_reasons")
            or candidate.get("quality_failure_reasons")
            or []
        ),
        "proxy_short_quality_review": candidate.get("proxy_short_quality_review"),
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
    direction = str(candidate.get("direction") or "").lower()
    if direction.startswith("long_frontier"):
        liquidity = _as_float(candidate.get("liquidity_score"), -1.0)
        freshness = candidate.get("freshness_age_seconds")
        if freshness is None and candidate.get("stale_minutes") is not None:
            freshness = _as_float(candidate.get("stale_minutes")) * 60.0
        if liquidity < 0.45:
            reasons.append("liquidity_score<0.45")
        if freshness is None:
            reasons.append("missing_freshness_age")
        elif _as_float(freshness) > 90.0:
            reasons.append("freshness_age>90s")
        context_gate = candidate.get("paper_context_cost_gate") or {}
        if context_gate.get("applicable") and context_gate.get("enabled") and not context_gate.get("eligible"):
            reasons.append("paper_context_cost_floor_not_cleared")
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
        if route_status not in {"standard", "paper_testable_proxy"}:
            reasons.append(f"borrow_or_route_status={route_status}")
        if funding < 8.0 and basis < 75.0:
            reasons.append("reverse_basis_not_extreme")
        if candidate.get("paper_proxy_activated") and candidate.get("paper_proxy_not_live_equivalent"):
            diagnostic_reasons = reasons or ["reverse_basis_proxy_quality_measured"]
            return _annotate(
                candidate,
                profile="okx_reverse_basis",
                action="reverse_basis_proxy_counterfactual",
                reasons=[
                    *diagnostic_reasons,
                    "paper_proxy_quality_and_outcomes_are_counterfactual_not_live_route_evidence",
                ],
                score_delta=0.0,
                allocation_multiplier=0.25,
            )
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


def _record_proxy_short_quality(
    candidate: dict,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    review = proxy_short_quality_review(candidate, config)
    if not review["applies"]:
        return review
    candidate["proxy_short_quality_review"] = review
    proxy_reasons = list(review.get("quality_failure_reasons") or [])
    candidate["proxy_short_quality_failure_reason"] = review.get("quality_failure_reason")
    candidate["proxy_short_quality_failure_reasons"] = proxy_reasons
    if proxy_reasons:
        existing_reasons = candidate.get("quality_failure_reasons") or []
        if isinstance(existing_reasons, str):
            existing_reasons = [existing_reasons]
        candidate["quality_failure_reason"] = review["quality_failure_reason"]
        candidate["quality_failure_reasons"] = list(dict.fromkeys([*existing_reasons, *proxy_reasons]))
    for reason in proxy_reasons:
        _append_note(candidate, f"proxy_short_quality:{reason}")
    return review


def _repair_proxy_candidate(
    candidate: dict,
    config: Mapping[str, Any] | None = None,
) -> dict | None:
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
        quality_review = _record_proxy_short_quality(candidate, config)
        reasons.extend(quality_review.get("quality_failure_reasons") or [])
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


def _repair_proxy_shock_reversal_candidate(
    candidate: dict,
    config: Mapping[str, Any] | None = None,
) -> dict | None:
    if candidate.get("trade_type") != "global_proxy_shock_reversal":
        return None
    reasons = []
    if str(candidate.get("direction") or "") == "short_proxy":
        quality_review = _record_proxy_short_quality(candidate, config)
        reasons.extend(quality_review.get("quality_failure_reasons") or [])
    if _as_float(candidate.get("edge_bps_estimate"), 0.0) < 3.0:
        reasons.append("shock_reversal_edge_below_confirmation")
    if _as_float(candidate.get("spread_bps"), 999.0) > 8.0:
        reasons.append("shock_reversal_cost_too_high")
    if _as_float(candidate.get("liquidity_score"), 0.0) < 0.65:
        reasons.append("shock_reversal_liquidity_weak")
    if _as_float(candidate.get("stale_minutes"), 999.0) > 5.0:
        reasons.append("shock_reversal_data_stale")
    if reasons:
        return _annotate(
            candidate,
            profile="yahoo_proxy_shock_reversal",
            action="shock_reversal_shadow_confirmation",
            reasons=list(dict.fromkeys(reasons)),
            score_delta=-12.0,
            allocation_multiplier=0.0,
            shadow_only=True,
        )
    return _annotate(
        candidate,
        profile="yahoo_proxy_shock_reversal",
        action="shock_reversal_confirmation_probe",
        reasons=["distinct shock-reversal candidate passed edge, cost, liquidity, freshness, and proxy quality gates"],
        score_delta=-3.0,
        allocation_multiplier=0.25,
    )


def hydrate_paper_context_loss_statistics(
    candidates: list[dict],
    conn: Any | None,
    config: Mapping[str, Any] | bool | None = None,
) -> None:
    """Attach rolling closed-paper evidence and persisted quarantine state."""
    if conn is None or not _paper_context_loss_enabled(config):
        return
    policy = _paper_context_loss_policy(config)
    keys = {paper_context_loss_key(candidate) for candidate in candidates}
    if not keys:
        return
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    try:
        rows = conn.execute(
            """
            select venue, direction, trade_type, pnl_bps, candidate_json
            from paper_trades
            where status = 'closed' and pnl_bps is not null
            order by closed_at desc, id desc
            """
        ).fetchall()
    except Exception:  # noqa: BLE001 - optional paper evidence is read-only
        return
    for row in rows:
        try:
            raw = dict(row)
        except (TypeError, ValueError):
            continue
        try:
            trade_candidate = json.loads(raw.get("candidate_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            trade_candidate = {}
        if not isinstance(trade_candidate, Mapping):
            trade_candidate = {}
        if str(trade_candidate.get("signal_stats_scope") or "").lower() == "synthetic_research":
            continue
        context_candidate = {
            **trade_candidate,
            "venue": trade_candidate.get("venue") or raw.get("venue"),
            "direction": trade_candidate.get("direction") or raw.get("direction"),
            "trade_type": trade_candidate.get("trade_type") or raw.get("trade_type"),
        }
        key = paper_context_loss_key(context_candidate)
        if key in keys:
            grouped[key].append(_as_float(raw.get("pnl_bps")))

    window = max(1, _as_int(policy.get("rolling_window_closed_trades"), 30))
    stats_by_key: dict[str, dict[str, Any]] = {}
    for key, values in grouped.items():
        values = values[:window]
        if not values:
            continue
        tail_count = max(1, math.ceil(len(values) * 0.25))
        worst_values = sorted(values)[:tail_count]
        stats_by_key[key] = {
            "closed_count": len(values),
            "wins": sum(value > 0.0 for value in values),
            "win_rate": round(sum(value > 0.0 for value in values) / len(values), 6),
            "expectancy_bps": round(sum(values) / len(values), 6),
            "tail_average_bps": round(sum(worst_values) / len(worst_values), 6),
            "worst_bps": round(min(values), 6),
            "rolling_window_closed_trades": window,
        }
    states: dict[str, dict[str, Any]] = {}
    placeholders = ",".join("?" for _ in keys)
    try:
        state_rows = conn.execute(
            f"""
            select context_key, status, quarantined_at, cooldown_until,
                   baseline_closed_count, last_closed_count, evidence_json, updated_at
            from paper_context_quarantines
            where context_key in ({placeholders})
            """,
            tuple(keys),
        ).fetchall()
    except Exception:  # noqa: BLE001 - compatible with un-migrated read-only stores
        state_rows = []
    for row in state_rows:
        item = dict(row)
        try:
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["evidence"] = {}
        states[str(item["context_key"])] = item
    for candidate in candidates:
        key = paper_context_loss_key(candidate)
        if key in stats_by_key and not isinstance(candidate.get("paper_context_loss_stats"), Mapping):
            candidate["paper_context_loss_stats"] = stats_by_key[key]
        if key in states:
            candidate["paper_context_loss_quarantine_state"] = states[key]


def hydrate_paper_context_prior_statistics(
    candidates: list[dict],
    conn: Any | None,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Attach venue/direction/feasibility realized paper stats for ranking only."""
    if conn is None:
        return
    eligible = [candidate for candidate in candidates if _paper_context_prior_active(candidate, config)]
    if not eligible:
        return
    policy = _paper_context_prior_policy(config)
    keys = {
        _paper_context_realized_key(candidate, feasibility_status=_paper_context_feasibility_status(candidate))
        for candidate in eligible
    }
    if not keys:
        return
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    try:
        rows = conn.execute(
            """
            select venue, direction, trade_type, pnl_bps, candidate_json, review_json, context_json
            from paper_trades
            where status = 'closed' and pnl_bps is not null
            order by closed_at desc, id desc
            """
        ).fetchall()
    except Exception:  # noqa: BLE001 - optional paper evidence is read-only
        return
    for row in rows:
        try:
            raw = dict(row)
        except (TypeError, ValueError):
            continue
        try:
            trade_candidate = json.loads(raw.get("candidate_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            trade_candidate = {}
        if not isinstance(trade_candidate, Mapping):
            trade_candidate = {}
        try:
            review = json.loads(raw.get("review_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            review = {}
        if not isinstance(review, Mapping):
            review = {}
        try:
            context = json.loads(raw.get("context_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            context = {}
        if not isinstance(context, Mapping):
            context = {}
        if str(trade_candidate.get("signal_stats_scope") or "").lower() == "synthetic_research":
            continue
        review_feasibility_status = review.get("feasibility_status") or context.get("feasibility_status")
        review_route_status = review.get("route_status") or context.get("route_status")
        context_candidate = {
            **trade_candidate,
            "venue": trade_candidate.get("venue") or raw.get("venue"),
            "direction": trade_candidate.get("direction") or raw.get("direction"),
            "trade_type": trade_candidate.get("trade_type") or raw.get("trade_type"),
            "feasibility_status": trade_candidate.get("feasibility_status") or review_feasibility_status,
            "route_status": trade_candidate.get("route_status") or review_route_status,
        }
        if not isinstance(context_candidate.get("execution_feasibility"), Mapping):
            fallback_status = review_feasibility_status or review_route_status
            if fallback_status:
                context_candidate["execution_feasibility"] = {
                    "status": fallback_status,
                    "route_status": review_route_status or fallback_status,
                }
        key = _paper_context_realized_key(
            context_candidate,
            feasibility_status=_paper_context_feasibility_status(context_candidate),
        )
        if key in keys:
            grouped[key].append(_as_float(raw.get("pnl_bps")))

    window = max(1, _as_int(policy.get("realized_context_window_closed_trades"), 30))
    min_persistent_count = max(1, _as_int(policy.get("realized_context_persistent_negative_closed_trades"), 8))
    stats_by_key: dict[str, dict[str, Any]] = {}
    for key, values in grouped.items():
        values = values[:window]
        if not values:
            continue
        tail_count = max(1, math.ceil(len(values) * 0.25))
        worst_values = sorted(values)[:tail_count]
        avg_pnl_bps = sum(values) / len(values)
        stats_by_key[key] = {
            "closed_count": len(values),
            "win_rate": round(sum(value > 0.0 for value in values) / len(values), 6),
            "avg_pnl_bps": round(avg_pnl_bps, 6),
            "expectancy_bps": round(avg_pnl_bps, 6),
            "tail_average_bps": round(sum(worst_values) / len(worst_values), 6),
            "persistent_negative": bool(len(values) >= min_persistent_count and avg_pnl_bps < 0.0),
            "rolling_window_closed_trades": window,
        }
    for candidate in eligible:
        key = _paper_context_realized_key(candidate, feasibility_status=_paper_context_feasibility_status(candidate))
        if key in stats_by_key and not isinstance(candidate.get("paper_context_realized_stats"), Mapping):
            candidate["paper_context_realized_stats"] = stats_by_key[key]


def _persist_paper_context_loss_quarantine(
    conn: Any | None,
    record: Mapping[str, Any] | None,
) -> None:
    if conn is None or not isinstance(record, Mapping) or not record.get("state_transition"):
        return
    context = record.get("context") or {}
    stats = record.get("stats") or {}
    state = record.get("state") or {}
    now = dt.datetime.now(dt.timezone.utc)
    transition = record["state_transition"]
    if transition == "released":
        try:
            conn.execute(
                """
                update paper_context_quarantines
                set status = 'released', last_closed_count = ?, evidence_json = ?, updated_at = ?
                where context_key = ?
                """,
                (
                    _as_int(stats.get("closed_count"), 0),
                    json.dumps(dict(record), sort_keys=True),
                    now.isoformat(),
                    record["context_key"],
                ),
            )
            conn.commit()
        except Exception:  # noqa: BLE001 - failure must not weaken the in-memory gate
            return
        return
    cooldown_hours = max(0.0, _as_float((record.get("thresholds") or {}).get("cooldown_hours"), 24.0))
    cooldown_until = now + dt.timedelta(hours=cooldown_hours)
    try:
        conn.execute(
            """
            insert into paper_context_quarantines (
                context_key, venue, asset_surface, trade_type, direction, status,
                quarantined_at, cooldown_until, baseline_closed_count, last_closed_count,
                evidence_json, updated_at
            ) values (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
            on conflict(context_key) do update set
                status = 'active', cooldown_until = excluded.cooldown_until,
                last_closed_count = excluded.last_closed_count,
                evidence_json = excluded.evidence_json, updated_at = excluded.updated_at
            """,
            (
                record["context_key"],
                context.get("venue", "unknown"),
                context.get("asset_surface", "unknown"),
                context.get("trade_type", "unknown"),
                context.get("direction", "unknown"),
                state.get("quarantined_at") or now.isoformat(),
                cooldown_until.isoformat(),
                _as_int(state.get("baseline_closed_count"), _as_int(stats.get("closed_count"), 0)),
                _as_int(stats.get("closed_count"), 0),
                json.dumps(dict(record), sort_keys=True),
                now.isoformat(),
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 - failure must not weaken the in-memory gate
        return


def _apply_context_loss_quarantine(
    candidate: dict,
    config: Mapping[str, Any] | bool | None = None,
    conn: Any | None = None,
) -> dict | None:
    record = paper_context_loss_quarantine_record(candidate, config=config)
    if record is None:
        return None
    candidate["paper_context_loss_quarantine"] = dict(record)
    _persist_paper_context_loss_quarantine(conn, record)
    if not record["quarantined"]:
        return None
    pre_quarantine_score = _as_float(candidate.get("score"), 0.0)
    reliability = _annotate(
        candidate,
        profile="paper_context_loss_quarantine",
        action="context_loss_quarantine_shadow_only",
        reasons=[record["reason"]],
        allocation_multiplier=0.0,
        shadow_only=True,
    )
    candidate["pre_context_loss_quarantine_score"] = pre_quarantine_score
    candidate["score"] = 0.0
    candidate["paper_score_multiplier"] = 0.0
    candidate["paper_score_eligible"] = False
    candidate["paper_rank_eligible"] = False
    candidate["paper_fill_allowed"] = False
    candidate["promotion_eligible"] = False
    candidate["candidate_reject_reason"] = record["reason"]
    reliability["paper_context_loss_quarantine"] = dict(record)
    return reliability


def _apply_family_quarantine(
    candidate: dict,
    config: Mapping[str, Any] | bool | None = None,
) -> dict | None:
    quarantine = paper_family_quarantine_record(candidate, config=config)
    if quarantine is None:
        return None

    pre_quarantine_score = _as_float(candidate.get("score"), 0.0)
    reliability = _annotate(
        candidate,
        profile="yahoo_proxy_family_quarantine",
        action="family_quarantine_shadow_only",
        reasons=["family_level_negative_after_cost_expectancy_and_hit_rate"],
        allocation_multiplier=0.0,
        shadow_only=True,
    )
    reliability["paper_strategy_quarantine"] = dict(quarantine)
    reliability["pre_quarantine_score"] = pre_quarantine_score
    reliability["paper_score_multiplier"] = 0.0
    reliability["paper_rank_eligible"] = False
    candidate["pre_quarantine_score"] = pre_quarantine_score
    candidate["score"] = 0.0
    candidate["paper_score_multiplier"] = 0.0
    candidate["paper_score_eligible"] = False
    candidate["paper_rank_eligible"] = False
    candidate["paper_fill_allowed"] = False
    candidate["paper_strategy_quarantine"] = dict(quarantine)
    candidate["strategy_reliability"] = reliability
    _append_note(candidate, "paper_strategy_family_quarantine:yahoo_proxy_family_decay")
    return reliability


def _apply_yahoo_proxy_freshness_shadow(
    candidate: dict,
    freshness_gate: Mapping[str, Any],
) -> dict:
    reliability = _annotate(
        candidate,
        profile="yahoo_proxy_freshness_shadow_gate",
        action="yahoo_proxy_freshness_shadow_only",
        reasons=list(freshness_gate.get("reasons") or [freshness_gate.get("reason") or "proxy_freshness_degraded"]),
        allocation_multiplier=0.0,
        shadow_only=True,
    )
    candidate["paper_yahoo_proxy_freshness_gate"] = dict(freshness_gate)
    candidate["paper_fill_allowed"] = False
    candidate["paper_observation_only"] = True
    candidate["paper_observation_reason"] = freshness_gate.get("reason")
    candidate["paper_execution_mode"] = "observe_only"
    candidate["paper_execution_semantics"] = str(
        freshness_gate.get("paper_execution_semantics") or "synthetic_research_not_live_equivalent"
    )
    candidate["signal_stats_scope"] = str(freshness_gate.get("signal_stats_scope") or "synthetic_research")
    candidate["candidate_status"] = "shadow_only"
    candidate["paper_action"] = "shadow_only"
    candidate["paper_status"] = "shadow_only"
    candidate["paper_fill_status"] = "shadow_only"
    candidate["paper_order_status"] = "shadow_only"
    candidate["shadow_reason"] = str(freshness_gate.get("reason") or "proxy_freshness_degraded")
    candidate["candidate_reject_reason"] = candidate["shadow_reason"]
    candidate["candidate_reject_detail"] = dict(freshness_gate)
    candidate["paper_score_eligible"] = True
    candidate["paper_rank_eligible"] = True
    candidate["_hunter_bucket"] = "diagnose"
    reliability["paper_yahoo_proxy_freshness_gate"] = dict(freshness_gate)
    return reliability


def _apply_okx_basis_decay_quarantine(
    candidate: dict,
    config: Mapping[str, Any] | bool | None = None,
    conn: Any | None = None,
) -> dict | None:
    """Record exact-family decay; only hard-quarantine outside exploration mode."""
    record = okx_basis_decay_quarantine_record(candidate, settings=config, conn=conn)
    if record is None:
        return None
    candidate["paper_okx_basis_decay_quarantine"] = dict(record)
    if not record["active"]:
        return None
    reliability = _annotate(
        candidate,
        profile="okx_basis_decay_quarantine",
        action="decay_quarantine_shadow_trial",
        reasons=[record["reason"]],
        allocation_multiplier=0.0 if not record.get("diagnostic_only") else 1.0,
        # Keep exploration-mode candidates priceable while still surfacing the
        # exact-family quarantine and score penalty in the ranking pipeline.
        shadow_only=False,
    )
    if record.get("diagnostic_only"):
        score_policy = apply_okx_basis_decay_score_policy(candidate, config, zero_score=False)
        reasons = list(candidate.get("paper_exploration_would_block_reasons") or [])
        reasons.append(record["reason"])
        candidate["paper_exploration_would_block_reasons"] = list(dict.fromkeys(reasons))
        candidate["paper_guard_would_block"] = {
            "reason": record["reason"],
            "guard": record.get("guard"),
            "record": dict(record),
        }
        candidate["candidate_status"] = "shadow_quarantined"
        candidate["paper_quarantine_status"] = "shadow_quarantined"
        candidate["paper_fill_allowed"] = True
        candidate["paper_eligible"] = True
        candidate["promotion_eligible"] = False
        candidate["_hunter_bucket"] = "diagnose"
        if not candidate.get("paper_exploration_immutable_rejections") and not candidate.get(
            "paper_experiment_capacity_deferred"
        ):
            candidate["shadow_filtered"] = False
            candidate["paper_entry_blocked"] = False
            candidate.pop("candidate_reject_reason", None)
            candidate.pop("candidate_reject_detail", None)
        reliability["paper_okx_basis_decay_quarantine"] = dict(record)
        reliability["okx_basis_decay_quarantine_score_policy"] = dict(score_policy)
        _append_note(candidate, "paper_guard_would_block:okx_basis_decay_quarantine")
        return reliability
    score_policy = apply_okx_basis_decay_score_policy(candidate, config, zero_score=True)
    candidate["candidate_status"] = "shadow_quarantined"
    candidate["paper_fill_allowed"] = False
    candidate["paper_eligible"] = False
    candidate["paper_action"] = "shadow_trial"
    candidate["paper_execution_mode"] = "observe_only"
    candidate["paper_observation_only"] = True
    candidate["paper_observation_reason"] = record["reason"]
    candidate["paper_quarantine_status"] = "shadow_quarantined"
    candidate["paper_score_multiplier"] = 0.0
    candidate["paper_score_eligible"] = False
    candidate["paper_rank_eligible"] = False
    candidate["promotion_eligible"] = False
    candidate["candidate_reject_reason"] = record["reason"]
    candidate["candidate_reject_detail"] = dict(record)
    reliability["paper_okx_basis_decay_quarantine"] = dict(record)
    reliability["okx_basis_decay_quarantine_score_policy"] = dict(score_policy)
    return reliability


def _apply_lineage_source_health(
    candidate: dict,
    config: Mapping[str, Any] | bool | None = None,
) -> dict | None:
    if not _lineage_source_health_enabled(config):
        return None
    already_rank_applied = _as_bool(
        candidate.get("paper_lineage_source_health_rank_applied"),
        False,
    )
    existing_review = candidate.get("paper_lineage_source_health")
    review = (
        dict(existing_review)
        if already_rank_applied and isinstance(existing_review, Mapping)
        else paper_lineage_source_health_record(candidate, config=config)
    )
    if review is None:
        return None

    pre_guard_score = _as_float(
        candidate.get("pre_lineage_source_health_score", candidate.get("score")),
        0.0,
    )
    multiplier = _as_float(review.get("paper_score_multiplier"), 0.0)
    quarantined = review.get("action") == "quarantine"
    action = "lineage_source_negative_edge_shadow_only" if quarantined else "lineage_source_negative_edge_penalty"
    reliability = _annotate(
        candidate,
        profile="lineage_source_health_guard",
        action=action,
        reasons=[review["reason"]],
        allocation_multiplier=multiplier,
        shadow_only=quarantined,
    )
    candidate["paper_lineage_source_health"] = dict(review)
    candidate["pre_lineage_source_health_score"] = pre_guard_score
    if not already_rank_applied:
        candidate["score"] = round(max(0.0, pre_guard_score * multiplier), 3)
    candidate["paper_score_multiplier"] = multiplier
    candidate["paper_score_eligible"] = bool(review["paper_score_eligible"])
    candidate["paper_rank_eligible"] = bool(review["paper_rank_eligible"])
    candidate["promotion_eligible"] = False
    candidate["paper_allocation_multiplier"] = min(
        _as_float(candidate.get("paper_allocation_multiplier"), 1.0),
        multiplier,
    )
    reliability["paper_lineage_source_health"] = dict(review)
    reliability["pre_guard_score"] = pre_guard_score
    reliability["paper_score_multiplier"] = multiplier
    reliability["paper_rank_eligible"] = bool(review["paper_rank_eligible"])
    if quarantined:
        candidate["paper_fill_allowed"] = False
        candidate["candidate_reject_reason"] = review["reason"]
    _append_note(candidate, f"paper_lineage_source_health:{review['action']}")
    return reliability


def _apply_portability_quarantine(
    candidate: dict,
    config: Mapping[str, Any] | bool | None = None,
) -> dict | None:
    quarantine = paper_portability_quarantine_record(candidate, config=config)
    if quarantine is None:
        return None
    candidate["paper_portability_quarantine"] = dict(quarantine)
    if quarantine["eligible"]:
        return None

    pre_quarantine_score = _as_float(candidate.get("score"), 0.0)
    reliability = _annotate(
        candidate,
        profile="cross_family_portability_quarantine",
        action="destination_family_proof_observation_only",
        reasons=[quarantine["reason"]],
        allocation_multiplier=_as_float(quarantine.get("paper_allocation_multiplier"), 1.0),
        shadow_only=False,
    )
    multiplier = _as_float(
        quarantine.get("paper_score_multiplier"), PAPER_TRANSLATED_ROUTE_OBSERVATION_MULTIPLIER
    )
    score_multiplier = multiplier
    if isinstance(candidate.get("paper_route_lineage"), Mapping) and candidate["paper_route_lineage"].get(
        "observation_only"
    ):
        # The route-lineage overlay has already applied the translated-route
        # haircut.  Portability proof remains diagnostic and promotion-gating
        # evidence, rather than compounding a second penalty.
        multiplier = 1.0
        score_multiplier = _as_float(
            candidate.get("paper_score_multiplier"), PAPER_TRANSLATED_ROUTE_OBSERVATION_MULTIPLIER
        )
    candidate["pre_portability_quarantine_score"] = pre_quarantine_score
    candidate["score"] = round(max(0.0, pre_quarantine_score * multiplier), 3)
    candidate["paper_score_multiplier"] = score_multiplier
    candidate["paper_score_eligible"] = True
    candidate["paper_rank_eligible"] = True
    candidate["sandbox_rank_eligible"] = True
    candidate["promotion_eligible"] = False
    candidate["paper_observation_only"] = True
    candidate["paper_observation_reason"] = quarantine["reason"]
    candidate["paper_allocation_multiplier"] = min(
        _as_float(candidate.get("paper_allocation_multiplier"), 1.0), score_multiplier
    )
    reliability["paper_portability_quarantine"] = dict(quarantine)
    reliability["pre_quarantine_score"] = pre_quarantine_score
    reliability["paper_score_multiplier"] = score_multiplier
    reliability["paper_rank_eligible"] = True
    reliability["sandbox_rank_eligible"] = True
    reliability["maximum_stage"] = quarantine.get("maximum_stage")
    candidate["strategy_reliability"] = reliability
    _append_note(candidate, f"paper_portability_quarantine:{quarantine['reason']}")
    return reliability


def apply_paper_route_lineage_confirmation(
    candidate: dict[str, Any],
    config: Mapping[str, Any] | bool | None = None,
) -> dict[str, Any] | None:
    """Apply a severe but non-blocking paper score treatment to translations."""
    lineage = paper_route_lineage_record(candidate, config=config)
    candidate["paper_route_lineage"] = lineage
    candidate["paper_lineage_tags"] = {
        "lineage_type": lineage["lineage_type"],
        "confirmation_status": lineage["confirmation_status"],
    }
    promotion = lineage.get("promotion_guard")
    if not isinstance(promotion, Mapping):
        return None
    candidate["paper_context_promotion_guard"] = dict(promotion)
    candidate["paper_context_promotion_guard_key"] = promotion.get("guard")
    candidate["paper_context_promotion_eligible"] = promotion.get("eligible")
    candidate["paper_context_promotion_blocked"] = promotion.get("promotion_blocked")
    candidate["paper_context_promotion_reason"] = promotion.get("reason")
    if not promotion.get("observation_only"):
        return None

    pre_score = _as_float(candidate.get("score"), 0.0)
    multiplier = _as_float(
        promotion.get("paper_score_multiplier"), PAPER_TRANSLATED_ROUTE_OBSERVATION_MULTIPLIER
    )
    reliability = _annotate(
        candidate,
        profile="paper_route_lineage_confirmation",
        action="translated_route_observation_only",
        reasons=[str(promotion.get("reason") or "route_local_confirmation_missing")],
        allocation_multiplier=multiplier,
        shadow_only=False,
    )
    candidate["pre_route_lineage_score"] = pre_score
    candidate["score"] = round(max(0.0, pre_score * multiplier), 3)
    candidate["paper_score_multiplier"] = multiplier
    candidate["paper_score_eligible"] = True
    candidate["paper_rank_eligible"] = True
    candidate["paper_normal_scoring_eligible"] = False
    candidate["promotion_eligible"] = False
    candidate["paper_observation_only"] = True
    candidate["paper_observation_reason"] = promotion["reason"]
    candidate["paper_allocation_multiplier"] = min(
        _as_float(candidate.get("paper_allocation_multiplier"), 1.0), multiplier
    )
    # Do not overwrite a route safety verdict.  In particular, missing local
    # confirmation is not itself a reason to block paper execution.
    reliability["paper_route_lineage"] = dict(lineage)
    reliability["pre_route_lineage_score"] = pre_score
    reliability["paper_score_multiplier"] = multiplier
    return reliability


def _apply_one(
    candidate: dict,
    config: Mapping[str, Any] | bool | None = None,
    conn: Any | None = None,
) -> dict | None:
    _record_proxy_short_quality(candidate, config)
    yahoo_proxy_freshness_gate = paper_yahoo_proxy_freshness_shadow_record(candidate, config=config)
    skip_family_quarantine = False
    if yahoo_proxy_freshness_gate is not None:
        candidate["paper_yahoo_proxy_freshness_gate"] = dict(yahoo_proxy_freshness_gate)
        skip_family_quarantine = True
    route_lineage = apply_paper_route_lineage_confirmation(candidate, config=config)
    context_loss_quarantine = _apply_context_loss_quarantine(candidate, config=config, conn=conn)
    if context_loss_quarantine is not None:
        return context_loss_quarantine
    portability_quarantine = _apply_portability_quarantine(candidate, config=config)
    if portability_quarantine is not None:
        return portability_quarantine
    okx_basis_decay_quarantine = _apply_okx_basis_decay_quarantine(candidate, config=config, conn=conn)
    if okx_basis_decay_quarantine is not None:
        return okx_basis_decay_quarantine
    if yahoo_proxy_freshness_gate is not None and not _as_bool(
        yahoo_proxy_freshness_gate.get("paper_fill_allowed"),
        True,
    ):
        return _apply_yahoo_proxy_freshness_shadow(candidate, yahoo_proxy_freshness_gate)
    quarantined = None if skip_family_quarantine else _apply_family_quarantine(candidate, config=config)
    if quarantined is not None:
        return quarantined
    lineage_source_health = _apply_lineage_source_health(candidate, config=config)
    if lineage_source_health is not None:
        return lineage_source_health
    trade_type = candidate.get("trade_type")
    if trade_type == "frontier_crypto_venue_map":
        return _repair_frontier_candidate(candidate)
    if trade_type == "perp_funding_basis":
        return _repair_okx_candidate(candidate)
    if trade_type == "global_proxy_shock_reversal":
        return _repair_proxy_shock_reversal_candidate(candidate, config=config)
    if trade_type in PROXY_TRADE_TYPES:
        return _repair_proxy_candidate(candidate, config=config)
    return route_lineage


def _summarize(items: list[dict], candidates: list[dict]) -> dict:
    by_action = collections.Counter(item["action"] for item in items)
    by_profile = collections.Counter(item["profile"] for item in items)
    by_signal = collections.Counter(item["signal_key"] for item in items)
    by_direction = collections.Counter(item["direction"] for item in items)
    by_venue = collections.Counter(item["venue"] for item in items if item.get("venue"))
    blocked = sum(1 for item in items if item["action"].startswith("shadow") or "shadow" in item["action"])
    protected = sum(1 for item in items if item.get("protect_working_slice"))
    quarantined = sum(1 for item in items if item.get("profile") == "yahoo_proxy_family_quarantine")
    portability_quarantined = sum(
        1 for item in items if item.get("profile") == "cross_family_portability_quarantine"
    )
    lineage_source_health_guarded = sum(
        1 for item in items if item.get("profile") == "lineage_source_health_guard"
    )
    context_loss_quarantined = sum(
        1 for item in items if item.get("profile") == "paper_context_loss_quarantine"
    )
    okx_basis_decay_quarantined = sum(
        1
        for candidate in candidates
        if isinstance(candidate.get("paper_okx_basis_decay_quarantine"), Mapping)
        and candidate["paper_okx_basis_decay_quarantine"].get("active")
    )
    by_quality_failure = collections.Counter(
        reason
        for candidate in candidates
        for reason in candidate.get("proxy_short_quality_failure_reasons") or []
    )
    return {
        "candidate_count": len(candidates),
        "annotated_count": len(items),
        "shadow_or_blocked_count": blocked,
        "family_quarantine_count": quarantined,
        "portability_quarantine_count": portability_quarantined,
        "lineage_source_health_guard_count": lineage_source_health_guarded,
        "context_loss_quarantine_count": context_loss_quarantined,
        "okx_basis_decay_quarantine_count": okx_basis_decay_quarantined,
        "by_quality_failure": dict(by_quality_failure),
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
    yahoo_proxy_transfer = report.get("yahoo_proxy_transfer_friction_diagnostic", {})
    native_surface = yahoo_proxy_transfer.get("native_surface", {})
    transferred_routes = yahoo_proxy_transfer.get("transferred_routes", {})
    lines = [
        "# Strategy Reliability Report",
        "",
        "Paper-only venue/direction reliability layer for the recurring manual self-improvement queue.",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Candidates reviewed: `{summary.get('candidate_count', 0)}`",
        f"- Annotated candidates: `{summary.get('annotated_count', 0)}`",
        f"- Shadow/blocked by reliability layer: `{summary.get('shadow_or_blocked_count', 0)}`",
        f"- Lineage source-health guards: `{summary.get('lineage_source_health_guard_count', 0)}`",
        f"- Cross-family portability quarantines: `{summary.get('portability_quarantine_count', 0)}`",
        f"- Protected working slices: `{summary.get('protected_working_slice_count', 0)}`",
        f"- Actions: `{summary.get('by_action', {})}`",
        f"- Profiles: `{summary.get('by_profile', {})}`",
        f"- Proxy-short quality failures: `{summary.get('by_quality_failure', {})}`",
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
        "## Yahoo Proxy Transfer Friction",
        "",
        f"- Current-cycle candidates tagged: `{yahoo_proxy_transfer.get('current_cycle_candidate_count', 0)}`",
        f"- Current-cycle mapping status: `{yahoo_proxy_transfer.get('current_cycle_mapping_status', {})}`",
        f"- Closed trade count: `{yahoo_proxy_transfer.get('closed_trade_count', 0)}`",
        f"- Native proxy avg PnL / win rate: `{native_surface.get('avg_pnl_bps')}` bps / `{native_surface.get('win_rate')}`",
        f"- OKX route deltas vs native: `{yahoo_proxy_transfer.get('route_vs_native_pnl_delta_bps', {})}`",
        f"- Transferred route stats: `{transferred_routes}`",
        f"- Delay segments: `{(yahoo_proxy_transfer.get('segments') or {}).get('delay_bucket', {})}`",
        f"- Liquidity segments: `{(yahoo_proxy_transfer.get('segments') or {}).get('liquidity_tier', {})}`",
        f"- Spread segments: `{(yahoo_proxy_transfer.get('segments') or {}).get('spread_regime', {})}`",
        "",
        "## Hard Limits",
        "",
        "- Paper-only candidate annotation.",
        "- No live trading, credentials, broker writes, dependency installation, or startup changes.",
        "- Promotions still require the existing reliable-label gates.",
        ]
    )
    return "\n".join(lines) + "\n"


def _hydrate_portability_paper_evidence(candidates: list[dict], conn: Any | None) -> None:
    """Attach prior-loop realized destination evidence without changing storage."""
    if conn is None:
        return
    for candidate in candidates:
        families = _portability_families(candidate)
        if (
            not families.get("source_family")
            or not families.get("destination_family")
            or families["source_family"] == families["destination_family"]
        ):
            continue
        evidence = _portability_evidence(candidate)
        if evidence.get("closed_count") is not None or evidence.get("expectancy_net_bps") is not None:
            continue
        candidate_signal_key = signal_key(candidate)
        try:
            row = conn.execute(
                """
                select closed_count, avg_pnl_bps, win_rate, updated_at
                from signal_stats
                where signal_key = ?
                """,
                (candidate_signal_key,),
            ).fetchone()
        except Exception:  # noqa: BLE001 - optional read-only runtime evidence
            continue
        if row is None:
            continue
        try:
            stats = dict(row)
        except (TypeError, ValueError):
            stats = {
                "closed_count": row[0],
                "avg_pnl_bps": row[1],
                "win_rate": row[2],
                "updated_at": row[3],
            }
        candidate["destination_family_paper_stats"] = {
            "signal_key": candidate_signal_key,
            "closed_count": stats.get("closed_count"),
            "expectancy_net_bps": stats.get("avg_pnl_bps"),
            "win_rate": stats.get("win_rate"),
            "updated_at": stats.get("updated_at"),
            "evidence_source": "persisted_paper_signal_stats",
            "cost_basis": "realized_paper_pnl_bps",
        }
        target_review = paper_only_proxy_frontier_target_evidence_review(candidate)
        if target_review.get("applies"):
            target_surface = target_review.get("target_surface")
            candidate["target_surface_paper_evidence"] = {
                **candidate["destination_family_paper_stats"],
                "paper_only": True,
                "target_surface": target_surface,
                "target_venue": str(candidate.get("venue") or "").strip(),
                "quality_pass_rate": stats.get("win_rate"),
            }


def _yahoo_proxy_transfer_source_signal_key(candidate: Mapping[str, Any]) -> str | None:
    for field in (
        "source_signal_key",
        "strategy_lab_source_signal_key",
        "parent_signal_key",
        "origin_signal_key",
        "lineage_source_signal_key",
        "market_key",
        "signal_key",
    ):
        value = str(candidate.get(field) or "").strip()
        if value.startswith(YAHOO_PROXY_TRANSFER_SOURCE_PREFIX):
            return value
    if (
        str(candidate.get("venue") or "").strip().upper() == "YAHOO_PROXY"
        and str(candidate.get("trade_type") or "").strip() == "global_proxy_momentum"
    ):
        fallback = str(candidate.get("signal_key") or candidate.get("market_key") or "").strip()
        if fallback:
            return fallback
        try:
            return signal_key(dict(candidate))
        except Exception:  # noqa: BLE001 - diagnostic-only fallback
            return None
    return None


def _yahoo_proxy_transfer_target_surface(candidate: Mapping[str, Any]) -> str | None:
    venue = str(candidate.get("venue") or "").strip().upper()
    inst_id = str(candidate.get("inst_id") or "").strip().upper()
    for field in ("target_surface", "paper_target_surface", "execution_surface", "market_surface"):
        value = str(candidate.get(field) or "").strip().upper()
        if value in YAHOO_PROXY_TRANSFER_OKX_SURFACES | {"YAHOO_PROXY"}:
            return value
    if venue == "YAHOO_PROXY":
        return "YAHOO_PROXY"
    if venue == "OKX_SPOT":
        return "OKX_SPOT"
    if venue == "OKX":
        return "OKX_PERP" if "SWAP" in inst_id or "PERP" in inst_id else "OKX_SPOT"
    return None


def _yahoo_proxy_transfer_target_route_key(
    candidate: Mapping[str, Any],
    target_surface: str | None,
) -> str | None:
    if target_surface not in YAHOO_PROXY_TRANSFER_OKX_SURFACES:
        return None
    for field in ("inst_id", "route_id", "instrument_id", "symbol"):
        value = str(candidate.get(field) or "").strip()
        if value:
            return value
    return target_surface


def _yahoo_proxy_transfer_delay_seconds(
    candidate: Mapping[str, Any],
    *,
    opened_at: Any = None,
) -> float | None:
    for field in ("source_quote_age_seconds", "proxy_quote_age_seconds", "source_age_seconds"):
        delay = _maybe_float(candidate.get(field))
        if delay is not None:
            return max(0.0, delay)
    source_at = _parse_timestamp(
        candidate.get("source_quote_timestamp")
        or candidate.get("last_trade_timestamp")
        or candidate.get("source_observed_at")
    )
    destination_at = _parse_timestamp(opened_at or candidate.get("opened_at") or candidate.get("seen_at"))
    if source_at is None or destination_at is None:
        return None
    return max(0.0, (destination_at - source_at).total_seconds())


def _yahoo_proxy_transfer_delay_bucket(delay_seconds: float | None) -> str:
    if delay_seconds is None:
        return "unknown"
    for limit, label in YAHOO_PROXY_TRANSFER_DELAY_BUCKETS:
        if delay_seconds <= limit:
            return label
    return "unknown"


def _yahoo_proxy_liquidity_tier(liquidity_score: float | None) -> str:
    if liquidity_score is None:
        return "unknown"
    if liquidity_score <= 0.35:
        return "low"
    if liquidity_score <= 0.65:
        return "mid"
    if liquidity_score <= 0.85:
        return "high"
    return "elite"


def _yahoo_proxy_spread_regime(spread_bps: float | None) -> str:
    if spread_bps is None:
        return "unknown"
    if spread_bps <= 3.0:
        return "tight"
    if spread_bps <= 8.0:
        return "normal"
    if spread_bps <= 20.0:
        return "wide"
    return "extreme"


def _paper_expectancy_snapshot(evidence: Mapping[str, Any] | None) -> tuple[int | None, float | None]:
    if not isinstance(evidence, Mapping):
        return None, None
    closed_count = _as_int(
        evidence.get(
            "closed_count",
            evidence.get("closed_trades", evidence.get("paper_observation_count")),
        ),
        0,
    )
    expectancy = _maybe_float(
        evidence.get(
            "expectancy_net_bps",
            evidence.get(
                "after_cost_expectancy_bps",
                evidence.get(
                    "avg_pnl_bps",
                    evidence.get("destination_expectancy_net_bps"),
                ),
            ),
        )
    )
    return (closed_count if closed_count > 0 else None), expectancy


def _aggregate_yahoo_proxy_transfer_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(item["pnl_bps"]) for item in rows if item.get("pnl_bps") is not None]
    delays = [float(item["transfer_delay_seconds"]) for item in rows if item.get("transfer_delay_seconds") is not None]
    scores = [float(item["native_proxy_score"]) for item in rows if item.get("native_proxy_score") is not None]
    basis = [float(item["basis_snapshot_bps"]) for item in rows if item.get("basis_snapshot_bps") is not None]
    spreads = [float(item["spread_snapshot_bps"]) for item in rows if item.get("spread_snapshot_bps") is not None]
    liquidity = [float(item["liquidity_score"]) for item in rows if item.get("liquidity_score") is not None]
    return {
        "closed_count": len(rows),
        "avg_pnl_bps": round(sum(pnls) / len(pnls), 3) if pnls else None,
        "win_rate": round(sum(1 for value in pnls if value > 0.0) / len(pnls), 3) if pnls else None,
        "avg_transfer_delay_seconds": round(sum(delays) / len(delays), 3) if delays else None,
        "avg_native_proxy_score": round(sum(scores) / len(scores), 3) if scores else None,
        "avg_basis_snapshot_bps": round(sum(basis) / len(basis), 3) if basis else None,
        "avg_spread_bps": round(sum(spreads) / len(spreads), 3) if spreads else None,
        "avg_liquidity_score": round(sum(liquidity) / len(liquidity), 3) if liquidity else None,
    }


def _segment_yahoo_proxy_transfer_rows(
    rows: list[dict[str, Any]],
    field: str,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        grouped[str(row.get(field) or "unknown")].append(row)
    return {
        key: {
            **_aggregate_yahoo_proxy_transfer_rows(items),
            "routes": dict(collections.Counter(str(item.get("route") or "unknown") for item in items)),
        }
        for key, items in sorted(grouped.items())
    }


def _hydrate_yahoo_proxy_transfer_diagnostics(
    candidates: list[dict[str, Any]],
    conn: Any | None,
) -> None:
    requested_signal_stats: dict[str, list[tuple[str, dict[str, Any]]]] = collections.defaultdict(list)
    for candidate in candidates:
        source_signal_key = _yahoo_proxy_transfer_source_signal_key(candidate)
        if source_signal_key and not isinstance(candidate.get("native_yahoo_proxy_paper_evidence"), Mapping):
            requested_signal_stats[source_signal_key].append(("native", candidate))
        target_surface = _yahoo_proxy_transfer_target_surface(candidate)
        if (
            target_surface in YAHOO_PROXY_TRANSFER_OKX_SURFACES
            and not isinstance(candidate.get("target_surface_paper_evidence"), Mapping)
        ):
            requested_signal_stats[signal_key(candidate)].append(("target", candidate))
        if (
            source_signal_key
            and not isinstance(candidate.get("native_yahoo_proxy_paper_evidence"), Mapping)
            and isinstance(candidate.get("lineage_source_health"), Mapping)
        ):
            candidate["native_yahoo_proxy_paper_evidence"] = dict(candidate["lineage_source_health"])

    if conn is not None and requested_signal_stats:
        placeholders = ",".join("?" for _ in requested_signal_stats)
        try:
            rows = conn.execute(
                f"""
                select signal_key, closed_count, avg_pnl_bps, win_rate, updated_at
                from signal_stats
                where signal_key in ({placeholders})
                """,
                tuple(requested_signal_stats),
            ).fetchall()
        except Exception:  # noqa: BLE001 - optional read-only runtime evidence
            rows = []
        for raw in rows:
            try:
                stats = dict(raw)
            except (TypeError, ValueError):
                stats = {
                    "signal_key": raw[0],
                    "closed_count": raw[1],
                    "avg_pnl_bps": raw[2],
                    "win_rate": raw[3],
                    "updated_at": raw[4],
                }
            signal_key_value = str(stats.get("signal_key") or "")
            for kind, candidate in requested_signal_stats.get(signal_key_value, []):
                if kind == "native":
                    candidate["native_yahoo_proxy_paper_evidence"] = {
                        "source_signal_key": signal_key_value,
                        "closed_count": stats.get("closed_count"),
                        "expectancy_net_bps": stats.get("avg_pnl_bps"),
                        "win_rate": stats.get("win_rate"),
                        "updated_at": stats.get("updated_at"),
                        "evidence_source": "persisted_paper_signal_stats",
                        "cost_basis": "realized_paper_pnl_bps",
                    }
                    continue
                if kind == "target" and not isinstance(candidate.get("target_surface_paper_evidence"), Mapping):
                    target_surface = _yahoo_proxy_transfer_target_surface(candidate)
                    target_route_key = _yahoo_proxy_transfer_target_route_key(candidate, target_surface)
                    source_signal_key = _yahoo_proxy_transfer_source_signal_key(candidate)
                    candidate["target_surface_paper_evidence"] = {
                        "paper_only": True,
                        "target_surface": target_surface,
                        "target_venue": str(candidate.get("venue") or "").strip(),
                        "target_route_key": target_route_key,
                        "source_signal_key": source_signal_key,
                        "closed_count": stats.get("closed_count"),
                        "expectancy_net_bps": stats.get("avg_pnl_bps"),
                        "win_rate": stats.get("win_rate"),
                        "quality_pass_rate": stats.get("win_rate"),
                        "updated_at": stats.get("updated_at"),
                        "evidence_source": "persisted_paper_signal_stats",
                        "cost_basis": "realized_paper_pnl_bps",
                        "transfer_mapping_key": (
                            f"{source_signal_key}->{target_route_key}"
                            if source_signal_key and target_route_key
                            else None
                        ),
                    }

    for candidate in candidates:
        source_signal_key = _yahoo_proxy_transfer_source_signal_key(candidate)
        target_surface = _yahoo_proxy_transfer_target_surface(candidate)
        if not source_signal_key and target_surface != "YAHOO_PROXY":
            continue
        target_route_key = _yahoo_proxy_transfer_target_route_key(candidate, target_surface)
        expected_mapping_key = (
            f"{source_signal_key}->{target_route_key}"
            if source_signal_key and target_route_key
            else None
        )
        target_evidence = candidate.get("target_surface_paper_evidence")
        evidence_mapping_key = None
        if isinstance(target_evidence, Mapping):
            evidence_mapping_key = str(
                target_evidence.get("transfer_mapping_key")
                or target_evidence.get("source_target_mapping_key")
                or target_evidence.get("mapping_key")
                or ""
            ).strip() or None
        if target_surface == "YAHOO_PROXY":
            mapping_status = "native_surface"
        elif expected_mapping_key and evidence_mapping_key:
            mapping_status = "matched" if evidence_mapping_key == expected_mapping_key else "mismatch"
        elif expected_mapping_key:
            mapping_status = "expected_unverified"
        else:
            mapping_status = "unknown"
        liquidity_score = _maybe_float(candidate.get("liquidity_score"))
        spread_bps = _maybe_float(candidate.get("spread_bps"))
        basis_snapshot_bps = _maybe_float(candidate.get("basis_bps"))
        native_closed_count, native_expectancy = _paper_expectancy_snapshot(
            candidate.get("native_yahoo_proxy_paper_evidence")
        )
        route_closed_count, route_expectancy = _paper_expectancy_snapshot(target_evidence)
        transfer_delay_seconds = _yahoo_proxy_transfer_delay_seconds(candidate)
        candidate["yahoo_proxy_transfer_diagnostic"] = {
            "paper_only": True,
            "applies": True,
            "source_signal_key": source_signal_key,
            "native_surface": "YAHOO_PROXY",
            "mapped_okx_route": target_surface if target_surface in YAHOO_PROXY_TRANSFER_OKX_SURFACES else None,
            "target_route_key": target_route_key,
            "expected_transfer_mapping_key": expected_mapping_key,
            "evidence_transfer_mapping_key": evidence_mapping_key,
            "mapping_status": mapping_status,
            "native_proxy_score": (
                _maybe_float(candidate.get("score_before_proxy_momentum_context"))
                if _maybe_float(candidate.get("score_before_proxy_momentum_context")) is not None
                else _maybe_float(candidate.get("score"))
            ),
            "source_quote_timestamp": (
                candidate.get("source_quote_timestamp")
                or candidate.get("last_trade_timestamp")
            ),
            "transfer_delay_seconds": round(transfer_delay_seconds, 3) if transfer_delay_seconds is not None else None,
            "delay_bucket": _yahoo_proxy_transfer_delay_bucket(transfer_delay_seconds),
            "basis_snapshot_bps": basis_snapshot_bps,
            "spread_snapshot_bps": spread_bps,
            "spread_regime": _yahoo_proxy_spread_regime(spread_bps),
            "liquidity_score": liquidity_score,
            "liquidity_tier": _yahoo_proxy_liquidity_tier(liquidity_score),
            "estimated_round_trip_cost_bps": _maybe_float(candidate.get("estimated_round_trip_cost_bps")),
            "native_surface_closed_count": native_closed_count,
            "native_surface_paper_pnl_bps": native_expectancy,
            "mapped_route_closed_count": route_closed_count,
            "mapped_route_paper_pnl_bps": route_expectancy,
            "route_vs_native_pnl_delta_bps": (
                round(route_expectancy - native_expectancy, 3)
                if route_expectancy is not None and native_expectancy is not None
                else None
            ),
        }


def _build_yahoo_proxy_transfer_friction_diagnostic(
    candidates: list[dict[str, Any]],
    conn: Any | None,
) -> dict[str, Any]:
    current_cycle = [
        dict(candidate["yahoo_proxy_transfer_diagnostic"], inst_id=candidate.get("inst_id"))
        for candidate in candidates
        if isinstance(candidate.get("yahoo_proxy_transfer_diagnostic"), Mapping)
    ]
    historical_rows: list[dict[str, Any]] = []
    if conn is not None:
        try:
            rows = conn.execute(
                """
                select opened_at, venue, inst_id, signal_key, pnl_bps, candidate_json
                from paper_trades
                where status = 'closed'
                  and pnl_bps is not null
                """
            ).fetchall()
        except Exception:  # noqa: BLE001 - diagnostic-only report
            rows = []
        for row in rows:
            try:
                candidate = json.loads(row["candidate_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                candidate = {}
            if not isinstance(candidate, dict):
                candidate = {}
            candidate.setdefault("venue", row["venue"])
            candidate.setdefault("inst_id", row["inst_id"])
            candidate.setdefault("signal_key", row["signal_key"])
            source_signal_key = _yahoo_proxy_transfer_source_signal_key(candidate)
            target_surface = _yahoo_proxy_transfer_target_surface(candidate)
            if target_surface not in YAHOO_PROXY_TRANSFER_OKX_SURFACES | {"YAHOO_PROXY"}:
                continue
            if not source_signal_key and target_surface != "YAHOO_PROXY":
                continue
            transfer_delay_seconds = _yahoo_proxy_transfer_delay_seconds(candidate, opened_at=row["opened_at"])
            liquidity_score = _maybe_float(candidate.get("liquidity_score"))
            spread_bps = _maybe_float(candidate.get("spread_bps"))
            historical_rows.append(
                {
                    "route": "native_proxy" if target_surface == "YAHOO_PROXY" else target_surface,
                    "pnl_bps": _maybe_float(row["pnl_bps"]),
                    "native_proxy_score": (
                        _maybe_float(candidate.get("score_before_proxy_momentum_context"))
                        if _maybe_float(candidate.get("score_before_proxy_momentum_context")) is not None
                        else _maybe_float(candidate.get("score"))
                    ),
                    "transfer_delay_seconds": transfer_delay_seconds,
                    "delay_bucket": _yahoo_proxy_transfer_delay_bucket(transfer_delay_seconds),
                    "basis_snapshot_bps": _maybe_float(candidate.get("basis_bps")),
                    "spread_snapshot_bps": spread_bps,
                    "spread_regime": _yahoo_proxy_spread_regime(spread_bps),
                    "liquidity_score": liquidity_score,
                    "liquidity_tier": _yahoo_proxy_liquidity_tier(liquidity_score),
                }
            )

    routes: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in historical_rows:
        routes[str(row.get("route") or "unknown")].append(row)
    native_summary = _aggregate_yahoo_proxy_transfer_rows(routes.get("native_proxy", []))
    route_summaries = {
        route: _aggregate_yahoo_proxy_transfer_rows(items)
        for route, items in sorted(routes.items())
        if route != "native_proxy"
    }
    native_avg = native_summary.get("avg_pnl_bps")
    route_deltas = {
        route: (
            round(summary["avg_pnl_bps"] - native_avg, 3)
            if summary.get("avg_pnl_bps") is not None and native_avg is not None
            else None
        )
        for route, summary in route_summaries.items()
    }
    return {
        "current_cycle_candidate_count": len(current_cycle),
        "current_cycle_mapping_status": dict(
            collections.Counter(str(item.get("mapping_status") or "unknown") for item in current_cycle)
        ),
        "current_cycle_routes": dict(
            collections.Counter(
                str(item.get("mapped_okx_route") or item.get("native_surface") or "unknown")
                for item in current_cycle
            )
        ),
        "closed_trade_count": len(historical_rows),
        "native_surface": native_summary,
        "transferred_routes": route_summaries,
        "route_vs_native_pnl_delta_bps": route_deltas,
        "segments": {
            "delay_bucket": _segment_yahoo_proxy_transfer_rows(historical_rows, "delay_bucket"),
            "liquidity_tier": _segment_yahoo_proxy_transfer_rows(historical_rows, "liquidity_tier"),
            "spread_regime": _segment_yahoo_proxy_transfer_rows(historical_rows, "spread_regime"),
        },
    }


def apply_strategy_reliability(
    candidates: list[dict],
    settings: dict | None = None,
    conn: Any | None = None,
) -> tuple[list[dict], dict]:
    """Annotate candidates with bounded paper-only reliability controls."""

    hydrate_paper_lineage_source_health(candidates, conn)
    _hydrate_portability_paper_evidence(candidates, conn)
    hydrate_paper_context_loss_statistics(candidates, conn, settings)
    hydrate_paper_context_prior_statistics(candidates, conn, settings)
    _hydrate_yahoo_proxy_transfer_diagnostics(candidates, conn)
    for candidate in candidates:
        _remove_invalid_proxy_confirmation(candidate)
        _record_proxy_short_quality(candidate, settings)
    if settings is not None and not settings.get("strategy_reliability", {}).get("enabled", True):
        quarantined = []
        for candidate in candidates:
            yahoo_proxy_freshness_gate = paper_yahoo_proxy_freshness_shadow_record(candidate, config=settings)
            skip_family_quarantine = False
            if yahoo_proxy_freshness_gate is not None:
                candidate["paper_yahoo_proxy_freshness_gate"] = dict(yahoo_proxy_freshness_gate)
                skip_family_quarantine = True
            record = (
                _apply_portability_quarantine(candidate, config=settings)
                or _apply_context_loss_quarantine(candidate, config=settings, conn=conn)
                or _apply_okx_basis_decay_quarantine(candidate, config=settings, conn=conn)
                or (
                    _apply_yahoo_proxy_freshness_shadow(candidate, yahoo_proxy_freshness_gate)
                    if yahoo_proxy_freshness_gate is not None
                    and not _as_bool(yahoo_proxy_freshness_gate.get("paper_fill_allowed"), True)
                    else None
                )
                or (None if skip_family_quarantine else _apply_family_quarantine(candidate, config=settings))
                or _apply_lineage_source_health(candidate, config=settings)
            )
            if record is not None:
                quarantined.append(record)
        candidates.sort(key=lambda row: row.get("score", 0), reverse=True)
        yahoo_proxy_transfer_friction_diagnostic = _build_yahoo_proxy_transfer_friction_diagnostic(
            candidates,
            conn,
        )
        return candidates, {
            "enabled": False,
            "paper_family_quarantine_enabled": paper_family_quarantine_enabled(config=settings),
            "generated_at": _utc_now(),
            "summary": _summarize(quarantined, candidates),
            "yahoo_proxy_transfer_friction_diagnostic": yahoo_proxy_transfer_friction_diagnostic,
        }

    adjusted = []
    context_prior_adjustments = []
    for candidate in candidates:
        reliability = _apply_one(candidate, config=settings, conn=conn)
        if reliability:
            adjusted.append(reliability)
        context_prior = apply_paper_context_priors(candidate, settings)
        if context_prior is not None:
            context_prior_adjustments.append(context_prior)
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
        "paper_context_prior_adjustments": sorted(
            context_prior_adjustments,
            key=lambda row: abs(_as_float(row.get("total_prior"))),
            reverse=True,
        )[:50],
        "covered_improvement_task_ids": COVERED_IMPROVEMENT_TASK_IDS,
        "covered_growth_experiment_ids": COVERED_GROWTH_EXPERIMENT_IDS,
        "task_cleanup": _cleanup_manual_tasks(conn),
        "yahoo_proxy_transfer_friction_diagnostic": _build_yahoo_proxy_transfer_friction_diagnostic(
            candidates,
            conn,
        ),
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
