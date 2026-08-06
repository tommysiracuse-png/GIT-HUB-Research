#!/usr/bin/env python3
"""Frontier crypto venue adapter.

Public market-data only. This module expands the radar's venue map and creates
paper-only exploratory candidates from reachable public endpoints. It never
uses credentials, private/account APIs, or order endpoints.
"""

from __future__ import annotations
import datetime as dt
import re
from typing import Any

try:
    from src.frontier_data_quality import (
        _paper_only_context_evidence_review,
        _paper_only_strategy_scope_review,
        _paper_only_cross_surface_seed_guard_review,
        _paper_only_yahoo_proxy_crypto_freshness_review,
        _paper_only_family_decay_guard_review,
        _paper_only_parse_timestamp,
        paper_only_yahoo_proxy_okx_target_review,
        paper_only_yahoo_proxy_cross_surface_alignment_guard,
        paper_only_shadow_direction_inversion_review,
        paper_only_route_requirement_profile,
        paper_only_route_quality_record,
    )
except ImportError:  # pragma: no cover - fallback for direct module execution
    try:
        from frontier_data_quality import (
            _paper_only_context_evidence_review,
            _paper_only_strategy_scope_review,
            _paper_only_cross_surface_seed_guard_review,
            _paper_only_yahoo_proxy_crypto_freshness_review,
            _paper_only_parse_timestamp,
            paper_only_yahoo_proxy_okx_target_review,
            paper_only_yahoo_proxy_cross_surface_alignment_guard,
            paper_only_shadow_direction_inversion_review,
            paper_only_route_requirement_profile,
            _paper_only_family_decay_guard_review,
            paper_only_route_quality_record,
        )
    except ImportError:  # pragma: no cover - route-quality enrichment becomes optional
        def _paper_only_parse_timestamp(value):
            if value is None:
                return None
            if isinstance(value, dt.datetime):
                return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = None
            if numeric is not None:
                try:
                    if abs(numeric) >= 1_000_000_000_000:
                        numeric /= 1000.0
                    parsed = dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc)
                except (OverflowError, OSError, ValueError):
                    return None
                return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)
            text = str(value).strip()
            if not text:
                return None
            try:
                parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)

        def _paper_only_context_evidence_review(record, config=None):
            return {
                "enabled": False,
                "context_signature": None,
                "context_signature_key": None,
                "matched": False,
                "eligible": True,
                "sample_size": None,
                "min_sample_size": 20.0,
                "expectancy_bps": None,
                "min_expectancy_bps": 0.0,
                "inherited_confidence": None,
                "variant_state": "guard_disabled",
                "activation_mode": "guard_disabled",
                "reason": "guard_disabled",
            }

        def _paper_only_strategy_scope_review(record, runtime_context=None):
            return {
                "flag": "paper_only_strategy_lab_scope_guard_v1",
                "enabled": False,
                "applies": False,
                "eligible": True,
                "blocked": False,
                "rejected_by_scope": False,
                "reason": "guard_disabled",
                "missing_scope_fields": [],
                "missing_runtime_fields": [],
                "mismatches": [],
            }

        def _paper_only_cross_surface_seed_guard_review(record, profile=None):
            return {
                "enabled": False,
                "applies": False,
                "blocked": False,
                "eligible": True,
                "reason": "guard_disabled",
                "source_family": None,
                "feature_family": None,
                "candidate_surface": None,
                "native_proxy_variant": False,
                "cross_surface_target": False,
                "policy": "guard_disabled",
            }

        def _paper_only_yahoo_proxy_crypto_freshness_review(record, profile=None, *, now=None):
            contribution = record.get("momentum_contribution") if isinstance(record, dict) else None
            return {
                "enabled": False,
                "paper_only": True,
                "applies": False,
                "eligible": True,
                "blocked": False,
                "reason": "guard_disabled",
                "gate_reason": None,
                "gate_reasons": [],
                "input_momentum_contribution": contribution,
                "propagated_momentum_contribution": contribution,
            }

        def paper_only_yahoo_proxy_cross_surface_alignment_guard(record, profile=None):
            return {
                "enabled": False,
                "paper_only": True,
                "applies": False,
                "eligible": True,
                "blocked": False,
                "entry_allowed": True,
                "reason": "guard_disabled",
                "force_paper_exit": False,
            }

        def paper_only_yahoo_proxy_okx_target_review(record, profile=None):
            return {
                "destination_venue": None,
                "target_surface": None,
                "quarantined": False,
                "quarantined_target_surfaces": ["OKX_SPOT", "OKX_PERP"],
                "allow_native_proxy_monitoring": True,
                "reenable_condition": (
                    "stable_positive_realized_paper_outcomes_for_same_source_target_mapping_and_"
                    "native_proxy_regime_and_local_frontier_confirmation"
                ),
            }

        def _paper_only_family_decay_guard_review(record, config=None):
            return {
                "flag": "paper_only_family_decay_suppression_v1",
                "enabled": False,
                "applies": False,
                "blocked": False,
                "eligible": True,
                "reason": "guard_disabled",
                "event": None,
                "family": None,
                "market_key": None,
                "strategy_family": None,
                "attempted_direction": None,
                "raw_score": None,
                "freshness_state": "unknown",
                "paper_score_multiplier": 1.0,
                "latest_family_paper": None,
            }

        def paper_only_shadow_direction_inversion_review(record, config=None):
            return {
                "enabled": False,
                "applies": False,
                "scope_matched": False,
                "market_key": None,
                "family": None,
                "baseline_direction": None,
                "shadow_direction": None,
                "variant": None,
                "reason": "guard_disabled",
                "activation_mode": "guard_disabled",
            }

        def paper_only_route_requirement_profile(record):
            return {}

        paper_only_route_quality_record = None

try:
    from src.paper_route_registry import assess_paper_route_registry
    from src.route_intelligence import build_paper_route_requirement_report
except ImportError:  # pragma: no cover - fallback for direct module execution
    from paper_route_registry import assess_paper_route_registry
    from route_intelligence import build_paper_route_requirement_report

try:
    from src.signals.frontier_crypto_venue_map import native_spot_surface_fields
except ImportError:  # pragma: no cover - fallback for direct module execution
    from signals.frontier_crypto_venue_map import native_spot_surface_fields

_VALR_PAPER_PUBLIC_BASE_URL = "https://api.valr.com"
_VALR_PAPER_SUPPORTED_SYMBOLS = {
    "BTCZAR": {"base": "BTC", "quote": "ZAR"},
    "ETHZAR": {"base": "ETH", "quote": "ZAR"},
    "USDTZAR": {"base": "USDT", "quote": "ZAR"},
}
_MERCADO_BITCOIN_PAPER_PUBLIC_BASE_URL = "https://www.mercadobitcoin.net/api"
_MERCADO_BITCOIN_PAPER_SUPPORTED_SYMBOLS = {
    "BTCBRL": {"base": "BTC", "quote": "BRL", "api_symbol": "BTC"},
    "ETHBRL": {"base": "ETH", "quote": "BRL", "api_symbol": "ETH"},
}

_PAPER_ONLY_PREMARKET_LIQUIDITY_DEFAULTS = {
    "min_premarket_dollar_volume_usd": 2500000.0,
    "max_spread_pct": 0.5,
    "min_recent_prints_window_minutes": 5,
    "min_recent_trade_count": 20,
}

_PAPER_ONLY_ROUTE_GUARD_MIN_LIQUIDITY_FLAG = "paper_route_guard_min_liquidity_v1"
_PAPER_ROUTE_GUARD_SHORT_FRONTIER_SPOT_FLAG = "paper_route_guard_short_frontier_spot_v1"
_PAPER_ONLY_ENFORCED_ROUTE_RESOLUTION_FLAG = "paper_only_enforced_route_resolution_v1"
_PAPER_ONLY_STRATEGY_LAB_EXACT_CONTEXT_PROMOTION_FLAG = "paper_only_strategy_lab_exact_context_promotion_v1"
_PAPER_ONLY_SPREAD_VOLATILITY_GATE_FLAG = "paper_only_spread_volatility_gate_v1"
_PAPER_ONLY_SHADOW_DIRECTION_INVERSION_FLAG = "paper_only_shadow_direction_inversion_v1"
_PAPER_ONLY_OKX_CARRY_ALIGNMENT_GATE_FLAG = "paper_only_okx_carry_alignment_gate_v1"
_PAPER_ONLY_FRONTIER_ROUTE_QUALITY_PROMOTION_FLAG = "paper_only_frontier_route_quality_promotion_v1"
_PAPER_ONLY_STRATEGY_LAB_SURFACE_LINEAGE_FLAG = "paper_only_strategy_lab_surface_lineage_v1"
_PAPER_ONLY_CONTEXT_SCOPING_FLAG = "paper_only_context_scoping_v1"


def _paper_only_route_review_text(value):
    text = str(value or "").strip()
    return text or None


def _paper_only_route_review_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled", "allow", "allowed", "supported"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "deny", "denied", "blocked", "unsupported"}:
        return False
    return None


def _paper_only_route_review_float(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _paper_only_route_signal_token(value):
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text or None


def _paper_only_route_lookup(route_status, profile, *keys):
    containers = []
    if isinstance(route_status, dict):
        containers.append(route_status)
        existing_requirements = route_status.get("route_requirements")
        if isinstance(existing_requirements, dict):
            containers.append(existing_requirements)
    if isinstance(profile, dict):
        containers.append(profile)
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value not in (None, "", [], {}, ()):
                return value
    return None


def _paper_only_route_support_bool(value):
    token = _paper_only_route_signal_token(value)
    if token in {
        "supported",
        "available",
        "enabled",
        "allow",
        "allowed",
        "reachable",
        "confirmed",
        "explicit",
        "present",
        "public_market_data",
    }:
        return True
    if token in {
        "unsupported",
        "unavailable",
        "blocked",
        "forbidden",
        "restricted",
        "denied",
        "paper_shadow_only",
        "shadow_only",
    }:
        return False
    return _paper_only_route_review_bool(value)


def _paper_only_route_quality_lookup(value, *keys):
    if not isinstance(value, dict):
        return None
    containers = [value]
    for nested_key in (
        "route_quality",
        "route_metrics",
        "execution_quality",
        "market_quality",
        "quality",
        "route_requirements",
    ):
        nested = value.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        for key in keys:
            candidate = container.get(key)
            if candidate not in (None, "", [], {}, ()):
                return candidate
    return None


def _paper_only_surface_token(value):
    token = _paper_only_route_signal_token(value)
    return token or None


def _paper_only_transfer_policy_explicit(value):
    if value is True:
        return True
    token = _paper_only_route_signal_token(value)
    return token in {
        "explicit",
        "allow",
        "allowed",
        "validated",
        "approved",
        "permitted",
        "cross_surface_allowed",
        "cross_surface_validated",
        "native_or_validated",
        "same_or_validated",
    }


def _paper_only_context_scoping_enabled(value):
    explicit = _paper_only_route_review_bool(
        _paper_only_route_quality_lookup(
            value,
            "paper_only_context_scoping_enabled",
            "context_scoping_enabled",
            "execution_context_scoping_enabled",
        )
    )
    return True if explicit is None else explicit


def _paper_only_context_scope_tokens(value):
    tokens = []
    for key in (
        "strategy_family",
        "signal_family",
        "feature_family",
        "source_family",
        "seed_family",
        "prior_family",
        "variant",
        "strategy_key",
        "source_market_key",
        "market_key",
    ):
        token = _paper_only_surface_token(_paper_only_route_quality_lookup(value, key))
        if token:
            tokens.append(token)
    return tokens


def _paper_only_context_contains_token(tokens, *needles):
    for token in tokens:
        if token and any(needle in token for needle in needles):
            return True
    return False


def _paper_only_context_validation_passed(value):
    explicit = _paper_only_route_review_bool(
        _paper_only_route_quality_lookup(
            value,
            "independent_validation_passed",
            "independent_validation",
            "basis_context_validated",
            "cross_surface_validated",
            "paper_validation_passed",
        )
    )
    if explicit is not None:
        return explicit
    score = _paper_only_route_review_float(
        _paper_only_route_quality_lookup(
            value,
            "independent_validation_score",
            "basis_validation_score",
            "cross_surface_validation_score",
        )
    )
    if score is None:
        return False
    if 1.0 < score <= 100.0:
        score /= 100.0
    return score >= 0.6


def _paper_only_surface_fingerprint_component(value, component):
    token = _paper_only_surface_token(value)
    if token is None:
        return None
    if component == "venue_family":
        parts = [part for part in token.split("_") if part]
        while parts and parts[-1] in {"spot", "perp", "basis", "proxy", "frontier", "cash"}:
            parts.pop()
        return "_".join(parts) or token
    if component == "instrument_class":
        if "basis" in token:
            return "basis"
        if "proxy" in token:
            return "proxy"
        if any(marker in token for marker in ("perp", "perpetual", "swap", "future", "futures")):
            return "perp"
        if "frontier" in token:
            return "frontier"
        if token == "cash" or "spot" in token:
            return "spot"
        return token
    if component == "direction_family":
        if token in {"buy", "bullish", "up"} or "long" in token:
            return "long"
        if token in {"sell", "bearish", "down"} or "short" in token:
            return "short"
        if token in {"neutral", "market_neutral", "hedged"} or "neutral" in token:
            return "market_neutral"
        return token
    if component == "execution_style":
        if token in {"maker", "post_only"} or "passive" in token:
            return "passive"
        if token in {"market", "ioc", "taker"} or "aggress" in token:
            return "aggressive"
        if "condition" in token or token in {"stop", "trigger"}:
            return "conditional"
        return token
    if component == "horizon_bucket":
        if token in {"intraday", "day", "scalp", "scalping"} or "intraday" in token:
            return "intraday"
        if token in {"swing", "position"} or "swing" in token:
            return "swing"
        if token in {"carry", "funding"} or "carry" in token or "funding" in token:
            return "carry"
        return token
    if component == "cost_regime":
        if token in {"low", "cheap"} or "low" in token:
            return "low"
        if token in {"medium", "mid"} or "medium" in token:
            return "medium"
        if token in {"high", "expensive"} or "high" in token:
            return "high"
        return token
    return token


def _paper_only_surface_fingerprint(value, *, namespace=None, fallback_surface=None):
    if not isinstance(value, dict):
        return {}

    if namespace == "origin":
        prefixes = ("origin", "source", "home", "seed", "prior")
    elif namespace == "target":
        prefixes = ("target", "candidate", "execution")
    else:
        prefixes = ()

    def _lookup(*field_names):
        keys = []
        for field_name in field_names:
            for prefix in prefixes:
                keys.append(f"{prefix}_{field_name}")
            keys.append(field_name)
        return _paper_only_route_quality_lookup(value, *keys)

    surface_token = _paper_only_surface_token(fallback_surface)
    venue_family = _paper_only_surface_fingerprint_component(
        _lookup("venue_family", "venue", "exchange", "broker", "execution_venue"),
        "venue_family",
    )
    instrument_class = _paper_only_surface_fingerprint_component(
        _lookup("instrument_class", "instrument", "asset_class", "market_type"),
        "instrument_class",
    )
    direction_family = _paper_only_surface_fingerprint_component(
        _lookup("direction_family", "direction", "side", "bias"),
        "direction_family",
    )
    execution_style = _paper_only_surface_fingerprint_component(
        _lookup("execution_style", "execution", "order_style", "order_type"),
        "execution_style",
    )
    horizon_bucket = _paper_only_surface_fingerprint_component(
        _lookup("horizon_bucket", "horizon", "holding_period", "term"),
        "horizon_bucket",
    )
    cost_regime = _paper_only_surface_fingerprint_component(
        _lookup("cost_regime", "cost", "fee_regime", "fee", "slippage"),
        "cost_regime",
    )

    if venue_family is None and surface_token is not None:
        venue_family = _paper_only_surface_fingerprint_component(surface_token, "venue_family")

    return {
        "venue_family": venue_family,
        "instrument_class": instrument_class,
        "direction_family": direction_family,
        "execution_style": execution_style,
        "horizon_bucket": horizon_bucket,
        "cost_regime": cost_regime,
    }


def _paper_only_surface_fingerprint_matches(source, target):
    source_fp = _paper_only_surface_fingerprint(source, namespace="origin", fallback_surface=source)
    target_fp = _paper_only_surface_fingerprint(target, namespace="target", fallback_surface=target)
    if not source_fp or not target_fp:
        return False

    matched = 0
    checked = 0
    for key in ("venue_family", "instrument_class", "direction_family", "execution_style"):
        left = source_fp.get(key)
        right = target_fp.get(key)
        if left is None or right is None:
            continue
        checked += 1
        if left == right:
            matched += 1
    if checked == 0:
        return False
    return matched == checked


def _paper_only_surface_boost_allowed(source, target):
    if not _paper_only_context_validation_passed(source):
        return False
    if not _paper_only_context_validation_passed(target):
        return False
    return _paper_only_surface_fingerprint_matches(source, target)


def _paper_only_context_scoping_review(value, *, origin_surface=None, target_surface=None, same_surface=False):
    if not isinstance(value, dict):
        return None
    enabled = _paper_only_context_scoping_enabled(value)
    runtime_context = {
        "venue": _paper_only_surface_token(
            _paper_only_route_quality_lookup(value, "venue", "exchange", "broker", "execution_venue")
        ),
        "instrument_family": _paper_only_surface_token(
            _paper_only_route_quality_lookup(value, "instrument_family", "family", "market_family", "asset_class")
        ),
        "direction": _paper_only_surface_token(
            _paper_only_route_quality_lookup(value, "direction", "signal_direction", "position_direction", "side")
        ),
        "surface": target_surface
        or _paper_only_surface_token(
            _paper_only_route_quality_lookup(value, "target_surface", "candidate_surface", "execution_surface")
        ),
        "quote_ccy": _paper_only_surface_token(
            _paper_only_route_quality_lookup(value, "quote_ccy", "quote_currency", "quote", "quote_asset")
        ),
    }
    scope_review = _paper_only_strategy_scope_review(value, runtime_context=runtime_context)
    if scope_review.get("applies") and scope_review.get("blocked"):
        return False
    market_key = _paper_only_surface_token(
        _paper_only_route_quality_lookup(value, "market_key", "paper_market_key", "context_signature_key")
    )
    family_tokens = _paper_only_context_scope_tokens(value)
    scope_tokens = [token for token in (origin_surface, target_surface, market_key) if token]
    all_tokens = family_tokens + scope_tokens
    target_tokens = [token for token in (target_surface, market_key) if token]

    frontier_execution = _paper_only_context_contains_token(target_tokens, "frontier")
    funding_context = _paper_only_context_contains_token(scope_tokens, "funding")
    proxy_like_source = _paper_only_context_contains_token(all_tokens, "yahoo", "proxy")
    momentum_family = _paper_only_context_contains_token(all_tokens, "momentum")
    basis_family = _paper_only_context_contains_token(all_tokens, "basis")
    mean_reversion_family = _paper_only_context_contains_token(
        all_tokens, "mean_reversion", "reversion", "convergence"
    )
    carry_family = _paper_only_context_contains_token(all_tokens, "carry", "funding")

    if frontier_execution and proxy_like_source and momentum_family:
        return False
    if frontier_execution and basis_family and not _paper_only_context_validation_passed(value):
        return False
    if frontier_execution and carry_family and funding_context:
        return True
    if same_surface and mean_reversion_family:
        return _paper_only_context_validation_passed(value)
    if not enabled:
        return None
    return None

def _paper_only_strategy_lab_surface_lineage_review(value, metrics, *, explicit_confidence=None):
    if not isinstance(value, dict):
        return None

    strategy_lab_marker = _paper_only_route_quality_lookup(
        value,
        "lab_id",
        "strategy_lab_id",
        "strategy_lab_signal_id",
        "strategy_lab_candidate",
        "strategy_lab_signal",
    )
    origin_surface = _paper_only_surface_token(
        _paper_only_route_quality_lookup(value, "origin_surface", "source_surface", "home_surface")
    )
    target_surface = _paper_only_surface_token(
        _paper_only_route_quality_lookup(value, "target_surface", "candidate_surface", "execution_surface")
    )
    transfer_policy_value = _paper_only_route_quality_lookup(
        value, "transfer_policy", "surface_transfer_policy", "cross_surface_policy"
    )
    transfer_policy = _paper_only_route_signal_token(transfer_policy_value)

    if strategy_lab_marker in (None, "", [], {}, ()) and not any(
        item is not None for item in (origin_surface, target_surface, transfer_policy)
    ):
        return None

    quote_age_seconds = metrics.get("quote_age_seconds")
    liquidity_usd = metrics.get("liquidity_usd")
    market_quality_score = metrics.get("market_quality_score")

    max_quote_age_seconds = (
        _paper_only_route_review_float(
            _paper_only_route_quality_lookup(
                value,
                "max_quote_age_seconds",
                "max_staleness_seconds",
                "freshness_limit_seconds",
            )
        )
        or 60.0
    )
    min_liquidity_usd = (
        _paper_only_route_review_float(
            _paper_only_route_quality_lookup(
                value,
                "min_liquidity_usd",
                "min_depth_usd",
                "min_book_depth_usd",
            )
        )
        or 25000.0
    )
    min_behavior_consistency = (
        _paper_only_route_review_float(
            _paper_only_route_quality_lookup(
                value,
                "min_behavior_consistency",
                "min_behavior_consistency_score",
                "behavior_consistency_floor",
            )
        )
        or 0.6
    )

    explicit_behavior_bool = _paper_only_route_review_bool(
        _paper_only_route_quality_lookup(
            value,
            "behavior_consistency_passed",
            "behavior_match",
            "surface_behavior_match",
        )
    )
    explicit_behavior_score = _paper_only_route_review_float(
        _paper_only_route_quality_lookup(
            value,
            "behavior_consistency",
            "behavior_consistency_score",
            "surface_behavior_consistency",
            "behavior_match_score",
        )
    )
    if explicit_behavior_score is not None and 1.0 < explicit_behavior_score <= 100.0:
        explicit_behavior_score /= 100.0

    if explicit_behavior_bool is not None:
        behavior_consistency_passed = explicit_behavior_bool
    elif explicit_behavior_score is not None:
        behavior_consistency_passed = explicit_behavior_score >= min_behavior_consistency
    else:
        behavior_consistency_passed = (
            market_quality_score is not None and market_quality_score >= min_behavior_consistency
        )

    freshness_passed = quote_age_seconds is not None and quote_age_seconds <= max_quote_age_seconds
    liquidity_passed = liquidity_usd is not None and liquidity_usd >= min_liquidity_usd
    same_surface = bool(origin_surface and target_surface and origin_surface == target_surface)
    explicit_cross_surface = _paper_only_transfer_policy_explicit(transfer_policy_value)

    review = {
        "flag": _PAPER_ONLY_STRATEGY_LAB_SURFACE_LINEAGE_FLAG,
        "applies": True,
        "origin_surface": origin_surface,
        "target_surface": target_surface,
        "transfer_policy": transfer_policy,
        "same_surface": same_surface,
        "explicit_cross_surface": explicit_cross_surface,
        "freshness_passed": freshness_passed,
        "liquidity_passed": liquidity_passed,
        "behavior_consistency_passed": behavior_consistency_passed,
        "confidence": None if explicit_confidence is None else max(0.0, min(1.0, explicit_confidence)),
    }
    if not origin_surface or not target_surface:
        review.update(
            {
                "eligible": False,
                "blocked": True,
                "reason": "missing_lineage_metadata",
                "confidence": 0.0,
            }
        )
        return review
    if same_surface:
        review.update(
            {
                "eligible": True,
                "blocked": False,
                "reason": "native_surface_match",
            }
        )
        return review
    if not explicit_cross_surface:
        review.update(
            {
                "eligible": False,
                "blocked": True,
                "reason": "cross_surface_requires_explicit_transfer_policy",
                "confidence": 0.0,
            }
        )
        return review
    checks_passed = freshness_passed and liquidity_passed and behavior_consistency_passed
    review.update(
        {
            "eligible": checks_passed,
            "blocked": not checks_passed,
            "reason": "cross_surface_transfer_validated"
            if checks_passed
            else "cross_surface_transfer_checks_failed",
            "confidence": 1.0 if checks_passed else 0.0,
        }
    )
    return review


def _paper_only_route_requirement_profile_dict(value):
    if not isinstance(value, dict):
        return {}
    try:
        profile = paper_only_route_requirement_profile(value)
    except Exception:
        profile = {}
    return profile if isinstance(profile, dict) else {}


def _paper_only_route_classification(value, profile=None):
    profile = profile if isinstance(profile, dict) else {}
    explicit = _paper_only_route_signal_token(
        _paper_only_route_lookup(
            value,
            profile,
            "route_classification",
            "route_type",
            "route_requirement",
            "route_profile",
            "route_kind",
            "route_side",
        )
    )
    if explicit in {"spot_only", "spot", "long_spot"}:
        return "spot_only"
    if explicit in {"perp_only", "perp", "perpetual", "swap", "future", "futures", "linear_perp"}:
        return "perp_only"
    if explicit in {
        "spot_plus_perp",
        "basis",
        "cash_and_carry",
        "funding_capture",
        "basis_trade",
    }:
        return "spot_plus_perp"
    if explicit in {
        "spot_short",
        "short_spot",
        "spot_short_with_borrow",
        "borrow_dependent_spot_short",
    }:
        return "spot_short_with_borrow"
    if explicit in {"cross_venue_conditional", "conditional_cross_venue"}:
        return "cross_venue_conditional"

    side_pattern = _paper_only_route_signal_token(
        _paper_only_route_lookup(
            value,
            profile,
            "side_pattern",
            "candidate_side_pattern",
            "side",
            "signal",
            "direction",
        )
    )
    instrument_type = _paper_only_route_signal_token(
        _paper_only_route_lookup(
            value,
            profile,
            "instrument_type",
            "market_type",
            "product_type",
            "instrument",
            "instrument_kind",
        )
    )
    leg_count = _paper_only_route_review_float(
        _paper_only_route_lookup(value, profile, "leg_count", "legs", "route_leg_count", "route_count")
    )
    conditional = _paper_only_route_review_bool(
        _paper_only_route_lookup(
            value,
            profile,
            "conditional",
            "is_conditional",
            "cross_venue_conditional",
            "requires_conditionals",
        )
    )
    route_tokens = " ".join(token for token in (explicit, side_pattern, instrument_type) if token)

    if "spot_short" in route_tokens or ("short" in route_tokens and "spot" in route_tokens):
        return "spot_short_with_borrow"
    if leg_count is not None and leg_count >= 2.0 and (
        "perp" in route_tokens or "swap" in route_tokens or "future" in route_tokens
    ):
        return "spot_plus_perp"
    if conditional and leg_count is not None and leg_count >= 2.0:
        return "cross_venue_conditional"
    if "perp" in route_tokens or "swap" in route_tokens or "future" in route_tokens:
        return "perp_only"
    return "spot_only"


def _paper_only_frontier_route_feasibility_gate_review(value):
    if not isinstance(value, dict):
        return None

    profile = _paper_only_route_requirement_profile_dict(value)
    route_classification = _paper_only_route_classification(value, profile)
    if route_classification not in {
        "spot_short_with_borrow",
        "spot_plus_perp",
        "cross_venue_conditional",
    }:
        return None

    venue = _paper_only_surface_token(
        _paper_only_route_lookup(
            value,
            profile,
            "venue",
            "exchange",
            "broker",
            "broker_surface",
            "execution_venue",
        )
    )
    review = {
        "flag": _PAPER_ROUTE_GUARD_SHORT_FRONTIER_SPOT_FLAG,
        "applies": True,
        "eligible": True,
        "blocked": False,
        "route_classification": route_classification,
        "venue": venue,
        "reason": "route_supported",
    }

    if route_classification == "spot_short_with_borrow":
        short_support = _paper_only_route_support_bool(
            _paper_only_route_lookup(
                value,
                profile,
                "spot_short_support",
                "short_support",
                "margin_short_support",
            )
        )
        margin_support = _paper_only_route_support_bool(
            _paper_only_route_lookup(
                value,
                profile,
                "margin_support",
                "spot_margin_support",
                "margin_enabled",
            )
        )
        borrow_reference = _paper_only_route_review_text(
            _paper_only_route_lookup(
                value,
                profile,
                "borrow_reference",
                "borrow_proxy",
                "borrow_source",
                "borrow_cost_reference",
            )
        )
        review.update(
            {
                "spot_short_support": short_support,
                "margin_support": margin_support,
                "borrow_reference": borrow_reference,
            }
        )
        if short_support is False or margin_support is False:
            review.update(
                {
                    "eligible": False,
                    "blocked": True,
                    "reason": "spot_short_route_unsupported",
                    "confidence": 0.0,
                }
            )
            return review
        if short_support is None and margin_support is None:
            review.update(
                {
                    "eligible": False,
                    "blocked": True,
                    "reason": "spot_short_route_support_unknown",
                    "confidence": 0.0,
                }
            )
            return review
        if not borrow_reference:
            review.update(
                {
                    "reason": "borrow_proxy_missing",
                    "confidence_cap": 0.55,
                }
            )
        return review

    if route_classification == "spot_plus_perp":
        basis_support = _paper_only_route_support_bool(
            _paper_only_route_lookup(value, profile, "basis_support", "spot_plus_perp_support")
        )
        perp_support = _paper_only_route_support_bool(
            _paper_only_route_lookup(value, profile, "perp_support", "swap_support", "futures_support")
        )
        fee_reference = _paper_only_route_review_text(
            _paper_only_route_lookup(
                value,
                profile,
                "fee_reference",
                "fee_schedule_reference",
                "trading_fee_reference",
            )
        )
        review.update(
            {
                "basis_support": basis_support,
                "perp_support": perp_support,
                "fee_reference": fee_reference,
            }
        )
        if basis_support is False or perp_support is False:
            review.update(
                {
                    "eligible": False,
                    "blocked": True,
                    "reason": "spot_plus_perp_route_unsupported",
                    "confidence": 0.0,
                }
            )
            return review
        if not fee_reference:
            review.update(
                {
                    "reason": "fee_reference_missing",
                    "confidence_cap": 0.65,
                }
            )
        return review

    fee_reference = _paper_only_route_review_text(
        _paper_only_route_lookup(
            value,
            profile,
            "fee_reference",
            "fee_schedule_reference",
            "trading_fee_reference",
        )
    )
    if not venue:
        review.update(
            {
                "eligible": False,
                "blocked": True,
                "reason": "missing_venue_metadata",
                "confidence": 0.0,
            }
        )
        return review
    if not fee_reference:
        review.update(
            {
                "reason": "fee_reference_missing",
                "confidence_cap": 0.6,
            }
        )
    return review


def _paper_only_frontier_route_quality_gate_review(value):
    if not isinstance(value, dict):
        return None

    route_feasibility_review = _paper_only_frontier_route_feasibility_gate_review(value)
    if isinstance(route_feasibility_review, dict) and route_feasibility_review.get("blocked"):
        return route_feasibility_review

    route_count = _paper_only_route_review_float(
        _paper_only_route_quality_lookup(value, "route_count", "route_richness", "venue_count", "path_count", "routes")
    )
    liquidity_usd = _paper_only_route_review_float(
        _paper_only_route_quality_lookup(
            value,
            "liquidity_usd",
            "depth_usd",
            "book_depth_usd",
            "top_of_book_depth_usd",
            "notional_liquidity_usd",
            "dollar_volume_usd",
        )
    )
    spread_pct = _paper_only_route_review_float(
        _paper_only_route_quality_lookup(value, "spread_pct", "effective_spread_pct", "quoted_spread_pct")
    )
    if spread_pct is None:
        spread_bps = _paper_only_route_review_float(
            _paper_only_route_quality_lookup(value, "spread_bps", "effective_spread_bps", "quoted_spread_bps")
        )
        if spread_bps is not None:
            spread_pct = spread_bps / 100.0
    quote_age_seconds = _paper_only_route_review_float(
        _paper_only_route_quality_lookup(
            value,
            "quote_age_seconds",
            "quote_staleness_seconds",
            "staleness_seconds",
            "data_age_seconds",
            "freshness_seconds",
        )
    )
    if quote_age_seconds is None:
        quote_age_ms = _paper_only_route_review_float(
            _paper_only_route_quality_lookup(value, "quote_age_ms", "quote_staleness_ms", "staleness_ms")
        )
        if quote_age_ms is not None:
            quote_age_seconds = quote_age_ms / 1000.0
    market_quality_score = _paper_only_route_review_float(
        _paper_only_route_quality_lookup(
            value,
            "market_quality_score",
            "quality_score",
            "data_quality_score",
            "market_score",
        )
    )
    if market_quality_score is not None and 1.0 < market_quality_score <= 100.0:
        market_quality_score /= 100.0

    metrics = {
        "route_count": route_count,
        "liquidity_usd": liquidity_usd,
        "spread_pct": spread_pct,
        "quote_age_seconds": quote_age_seconds,
        "market_quality_score": market_quality_score,
    }
    if not any(metric is not None for metric in metrics.values()):
        return None

    explicit_confidence = _paper_only_route_review_float(
        _paper_only_route_quality_lookup(value, "route_confidence", "confidence", "route_quality_confidence")
    )
    lineage_review = _paper_only_strategy_lab_surface_lineage_review(
        value,
        metrics,
        explicit_confidence=explicit_confidence,
    )
    complete = all(metric is not None for metric in metrics.values())
    if not complete:
        if isinstance(lineage_review, dict) and lineage_review.get("blocked"):
            review = dict(lineage_review)
            review.setdefault("complete", False)
            return review
        neutral_confidence = 0.5 if explicit_confidence is None else min(0.5, max(0.0, min(1.0, explicit_confidence)))
        review = {
            "flag": _PAPER_ONLY_FRONTIER_ROUTE_QUALITY_PROMOTION_FLAG,
            "applies": True,
            "complete": False,
            "eligible": True,
            "blocked": False,
            "reason": "incomplete_quality_metrics",
            "confidence": neutral_confidence,
        }
        if isinstance(lineage_review, dict):
            review["surface_lineage_review"] = lineage_review
        return review

    checks = {
        "route_count": route_count >= (_paper_only_route_review_float(_paper_only_route_quality_lookup(value, "min_route_count", "min_routes", "min_route_richness")) or 2.0),
        "liquidity_usd": liquidity_usd >= (_paper_only_route_review_float(_paper_only_route_quality_lookup(value, "min_liquidity_usd", "min_depth_usd", "min_book_depth_usd")) or 25000.0),
        "spread_pct": spread_pct <= (_paper_only_route_review_float(_paper_only_route_quality_lookup(value, "max_spread_pct", "max_effective_spread_pct", "spread_limit_pct")) or 1.0),
        "quote_age_seconds": quote_age_seconds <= (_paper_only_route_review_float(_paper_only_route_quality_lookup(value, "max_quote_age_seconds", "max_staleness_seconds", "freshness_limit_seconds")) or 60.0),
        "market_quality_score": market_quality_score >= (_paper_only_route_review_float(_paper_only_route_quality_lookup(value, "min_market_quality_score", "min_quality_score", "quality_floor")) or 0.5),
    }
    passed = all(checks.values())
    review = {
        "flag": _PAPER_ONLY_FRONTIER_ROUTE_QUALITY_PROMOTION_FLAG,
        "applies": True,
        "complete": True,
        "eligible": passed,
        "blocked": not passed,
        "reason": "quality_thresholds_satisfied" if passed else "quality_threshold_failed",
        "confidence": max(0.0, min(1.0, explicit_confidence if explicit_confidence is not None and passed else (1.0 if passed else 0.0))),
    }
    if isinstance(lineage_review, dict):
        review["surface_lineage_review"] = lineage_review
        if lineage_review.get("blocked"):
            review.update(
                {
                    "flag": lineage_review.get("flag", review["flag"]),
                    "eligible": False,
                    "blocked": True,
                    "reason": lineage_review.get("reason") or review["reason"],
                    "confidence": 0.0,
                }
            )
        elif passed and lineage_review.get("reason") == "cross_surface_transfer_validated":
            review["reason"] = "cross_surface_transfer_validated"
    return review


def _paper_only_route_confidence(value, *, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, dict):
        gate_review = _paper_only_frontier_route_quality_gate_review(value)
        if isinstance(gate_review, dict):
            confidence = _paper_only_route_review_float(gate_review.get("confidence"))
            if confidence is not None:
                return max(0.0, min(1.0, confidence))
        explicit_confidence = _paper_only_route_review_float(
            _paper_only_route_quality_lookup(value, "route_confidence", "confidence", "route_quality_confidence")
        )
        if explicit_confidence is not None:
            return max(0.0, min(1.0, explicit_confidence))
        return default
    token = _paper_only_route_signal_token(value)
    if not token:
        return default
    if token in {
        "supported",
        "available",
        "enabled",
        "allow",
        "allowed",
        "reachable",
        "confirmed",
        "explicit",
        "present",
        "public_market_data",
        "public_taker_fee_schedule",
    }:
        return 1.0
    if token in {"conditional", "partial", "fallback", "secondary", "estimated", "derived", "hinted", "hint"}:
        return 0.75
    if token in {"paper_shadow_only", "shadow_only"}:
        return 0.15
    if token in {"unsupported", "unavailable", "blocked", "forbidden", "restricted", "denied"}:
        return 0.0
    if token in {"unknown", "unclear", "unverified", "missing", "absent"}:
        return 0.25 if default is None else default
    if "supported" in token or "available" in token or "reachable" in token:
        return 1.0
    if "shadow" in token:
        return 0.15
    if "blocked" in token or "restricted" in token or "unsupported" in token or "forbidden" in token:
        return 0.0
    if "unknown" in token or "missing" in token or "unverified" in token:
        return 0.25 if default is None else default
    return default


def _paper_only_route_permissions(route_status, profile):
    permissions = []
    for container in (route_status, profile):
        if not isinstance(container, dict):
            continue
        values = container.get("required_permissions")
        if values in (None, "", [], {}, ()):
            continue
        if isinstance(values, (list, tuple, set)):
            candidates = values
        else:
            candidates = [values]
        for candidate in candidates:
            token = _paper_only_route_signal_token(candidate)
            if token and token not in permissions:
                permissions.append(token)
    return permissions


def _paper_only_required_side(route_status, profile):
    explicit = _paper_only_route_review_text(
        _paper_only_route_lookup(route_status, profile, "required_side", "required_route_side", "side_requirement")
    )
    if explicit:
        return explicit
    summary = " ".join(
        value
        for value in (
            _paper_only_route_signal_token(_paper_only_route_lookup(route_status, profile, "summary")),
            _paper_only_route_signal_token(
                _paper_only_route_lookup(route_status, profile, "route_requirement_summary")
            ),
            _paper_only_route_signal_token(_paper_only_route_lookup(route_status, profile, "strategy")),
        )
        if value
    )
    permissions = set(_paper_only_route_permissions(route_status, profile))
    basis_support = _paper_only_route_lookup(route_status, profile, "basis_support")
    spot_short_support = _paper_only_route_lookup(
        route_status, profile, "spot_short_support", "shortability", "spot_shortability", "short_support"
    )
    perp_support = _paper_only_route_lookup(route_status, profile, "perp_support", "perp_available", "perp_enabled")
    if basis_support not in (None, "", [], {}, ()) or "basis" in summary or "funding" in summary:
        return "spot_plus_perp"
    if (
        spot_short_support not in (None, "", [], {}, ())
        or "spot_short" in permissions
        or "short" in permissions
        or "borrow" in permissions
        or "margin" in permissions
        or "spot_short" in summary
        or "borrow" in summary
    ):
        return "spot_short"
    if perp_support not in (None, "", [], {}, ()) or "perp" in summary or "swap" in summary or "futures" in summary:
        return "perp"
    return None


def _paper_only_alignment_direction_from_token(value):
    token = _paper_only_route_signal_token(value)
    if token in {
        "short",
        "sell",
        "short_perp",
        "short_perp_long_spot",
        "receive_funding",
        "positive_carry",
        "perp_rich",
    }:
        return "short"
    if token in {
        "long",
        "buy",
        "long_perp",
        "long_perp_short_spot",
        "pay_funding",
        "negative_carry",
        "perp_cheap",
    }:
        return "long"
    return None


def _paper_only_alignment_direction_from_numeric(value):
    numeric = _paper_only_route_review_float(value)
    if numeric is None or numeric == 0.0:
        return None
    return "short" if numeric > 0.0 else "long"


def _paper_only_alignment_lookup(route_status, profile, direct_keys, numeric_keys=()):
    direct = _paper_only_alignment_direction_from_token(
        _paper_only_route_lookup(route_status, profile, *tuple(direct_keys))
    )
    if direct:
        return direct
    for key in tuple(numeric_keys):
        direction = _paper_only_alignment_direction_from_numeric(_paper_only_route_lookup(route_status, profile, key))
        if direction:
            return direction
    return None


def _paper_only_okx_basis_variant_alignment_review(route_status, profile):
    scope_tokens = " ".join(
        token
        for token in (
            _paper_only_route_signal_token(
                _paper_only_route_lookup(
                    route_status,
                    profile,
                    "venue",
                    "execution_venue",
                    "exchange",
                    "broker",
                    "venue_key",
                )
            ),
            _paper_only_route_signal_token(
                _paper_only_route_lookup(
                    route_status,
                    profile,
                    "market_key",
                    "signal_key",
                    "strategy",
                    "strategy_family",
                    "candidate_family",
                )
            ),
            _paper_only_route_signal_token(
                _paper_only_route_lookup(
                    route_status,
                    profile,
                    "strategy_family",
                    "candidate_family",
                    "directional_template",
                    "variant",
                    "signal_variant",
                )
            ),
        )
        if token
    )
    applies = "okx" in scope_tokens and "perp_funding_basis" in scope_tokens
    candidate_direction = _paper_only_alignment_lookup(
        route_status,
        profile,
        (
            "direction",
            "side",
            "signal_side",
            "candidate_direction",
            "position_side",
            "directional_template",
            "variant",
            "signal_variant",
        ),
    )
    basis_direction = _paper_only_alignment_lookup(
        route_status,
        profile,
        (
            "basis_direction",
            "basis_side",
            "basis_trade_direction",
            "basis_variant_direction",
        ),
        (
            "basis_bps",
            "basis_edge_bps",
            "basis_alignment_edge_bps",
            "spot_perp_basis_bps",
            "perp_spot_basis_bps",
            "perp_premium_bps",
        ),
    )
    funding_direction = _paper_only_alignment_lookup(
        route_status,
        profile,
        (
            "funding_direction",
            "funding_capture_direction",
            "carry_capture_direction",
            "expected_funding_direction",
        ),
        (
            "expected_funding_capture_bps",
            "perp_funding_edge_bps",
            "funding_rate_bps",
            "predicted_funding_bps",
            "next_funding_rate_bps",
            "funding_rate",
        ),
    )
    expected_direction = basis_direction if basis_direction and basis_direction == funding_direction else None
    blocked = False
    eligible = True
    reason = "not_applicable"
    if applies:
        reason = "insufficient_alignment_evidence"
        if basis_direction and funding_direction and basis_direction != funding_direction:
            blocked = True
            eligible = False
            reason = "carry_misaligned"
        elif expected_direction and candidate_direction and candidate_direction != expected_direction:
            blocked = True
            eligible = False
            reason = "direction_fights_carry"
        elif expected_direction:
            reason = "carry_aligned"
    return {
        "flag": _PAPER_ONLY_OKX_CARRY_ALIGNMENT_GATE_FLAG,
        "applies": applies,
        "eligible": eligible,
        "blocked": blocked,
        "reason": reason,
        "candidate_direction": candidate_direction,
        "basis_direction": basis_direction,
        "funding_direction": funding_direction,
        "expected_direction": expected_direction,
    }


def _paper_only_scope_contract_lookup(containers, *keys):
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if value not in (None, "", [], {}, ()):
                return value
    return None


def _paper_only_scope_flag_enabled(route_status, profile):
    values = _paper_only_route_lookup(
        route_status,
        profile,
        "paper_policy_flags",
        "policy_flags",
        "feature_flags",
        "activation_flags",
        "flags",
    )
    if values in (None, "", [], {}, ()):
        return False
    if isinstance(values, (list, tuple, set)):
        candidates = values
    else:
        candidates = str(values).replace(",", " ").split()
    for candidate in candidates:
        token = _paper_only_route_signal_token(candidate)
        if token == _PAPER_ONLY_STRATEGY_LAB_EXACT_CONTEXT_PROMOTION_FLAG:
            return True
    return False


def _paper_only_surface_scope_value(scope_name, value):
    token = _paper_only_route_signal_token(value)
    if not token:
        return None
    if scope_name == "direction_scope":
        return _paper_only_alignment_direction_from_token(token) or token
    if scope_name == "instrument_scope":
        if token in {"spot", "cash"} or "spot" in token:
            return "spot"
        if token in {"perpetual", "perpetuals", "perp", "perps", "swap", "swaps"} or "perp" in token or "swap" in token:
            return "perp"
        if token in {"future", "futures", "dated_future", "dated_futures"} or "future" in token:
            return "dated_futures"
        return token
    if scope_name == "trade_family_scope":
        if "route" in token and "arb" in token:
            return "route_arb"
        if "basis" in token:
            return "basis"
        if "carry" in token or "funding" in token:
            return "carry"
        if "momentum" in token:
            return "momentum"
        if "reversion" in token:
            return "mean_reversion"
        return token
    if scope_name == "execution_scope":
        if token == "spot_plus_perp" or ("spot" in token and ("perp" in token or "swap" in token)):
            return "spot_plus_perp"
        if token == "spot_short" or ("spot" in token and "short" in token):
            return "spot_short"
        if token == "perp" or "perp" in token or "swap" in token:
            return "perp"
        if token == "spot" or "spot" in token:
            return "spot"
        if "maker" in token and "taker" not in token:
            return "maker"
        if "taker" in token and "maker" not in token:
            return "taker"
        return token
    return token


def _paper_only_surface_scope_contract(route_status, profile, *, source):
    route_requirements = route_status.get("route_requirements") if isinstance(route_status, dict) else None
    source_containers = [route_status, route_requirements]
    target_containers = [profile, route_status, route_requirements]
    if source:
        field_keys = {
            "venue_scope": (
                "source_venue_scope",
                "paper_venue_scope",
                "training_venue_scope",
                "evidence_venue",
                "seed_venue",
                "venue_scope",
                "declared_venue",
                "declared_venue_scope",
            ),
            "instrument_scope": (
                "source_instrument_scope",
                "paper_instrument_scope",
                "training_instrument_scope",
                "instrument_scope",
                "declared_instrument_scope",
                "source_market_type",
            ),
            "trade_family_scope": (
                "source_trade_family_scope",
                "paper_trade_family_scope",
                "trade_family_scope",
                "declared_trade_family",
                "source_strategy_family",
            ),
            "direction_scope": (
                "source_direction_scope",
                "paper_direction_scope",
                "direction_scope",
                "declared_direction_scope",
                "source_direction",
                "seed_direction",
            ),
            "execution_scope": (
                "source_execution_scope",
                "paper_execution_scope",
                "execution_scope",
                "declared_execution_scope",
                "source_route_style",
                "seed_execution_scope",
            ),
        }
        containers = source_containers
    else:
        field_keys = {
            "venue_scope": ("target_venue_scope", "activation_venue", "venue", "execution_venue", "exchange", "broker", "venue_key"),
            "instrument_scope": ("target_instrument_scope", "instrument_scope", "market_type", "market_kind", "instrument_type"),
            "trade_family_scope": ("target_trade_family_scope", "trade_family_scope", "strategy_family", "candidate_family", "signal_key", "market_key"),
            "direction_scope": ("target_direction_scope", "direction_scope", "direction", "side", "signal_side", "candidate_direction", "position_side", "directional_template", "variant", "signal_variant"),
            "execution_scope": ("target_execution_scope", "execution_scope", "execution_style", "route_style", "required_side", "side_requirement"),
        }
        containers = target_containers

    contract = {}
    for scope_name, keys in field_keys.items():
        raw_value = _paper_only_scope_contract_lookup(containers, *keys)
        if scope_name == "execution_scope" and raw_value in (None, "", [], {}, ()) and not source:
            raw_value = _paper_only_required_side(route_status, profile)
        contract[scope_name] = _paper_only_surface_scope_value(scope_name, raw_value)
    return contract


def _paper_only_exact_surface_scope_review(route_status, profile):
    source_scope_contract = _paper_only_surface_scope_contract(route_status, profile, source=True)
    target_scope_contract = _paper_only_surface_scope_contract(route_status, profile, source=False)
    strategy_tokens = " ".join(
        token
        for token in (
            _paper_only_route_signal_token(
                _paper_only_route_lookup(
                    route_status,
                    profile,
                    "market_key",
                    "signal_key",
                    "strategy",
                    "strategy_family",
                    "candidate_family",
                    "source_agent",
                    "candidate_source",
                    "variant",
                    "signal_variant",
                )
            ),
            _paper_only_route_signal_token(_paper_only_route_lookup(route_status, profile, "promotion_source", "idea_source", "generator")),
        )
        if token
    )
    applies = _paper_only_scope_flag_enabled(route_status, profile) or "strategy_lab" in strategy_tokens or any(source_scope_contract.values())
    if not applies:
        return {
            "flag": _PAPER_ONLY_STRATEGY_LAB_EXACT_CONTEXT_PROMOTION_FLAG,
            "enabled": True,
            "applies": False,
            "eligible": True,
            "blocked": False,
            "reason": "not_applicable",
            "missing_fields": [],
            "mismatched_fields": [],
            "source_scope_contract": source_scope_contract,
            "target_scope_contract": target_scope_contract,
        }

    missing_fields = []
    for scope_name, value in source_scope_contract.items():
        if not value:
            missing_fields.append(f"source_{scope_name}")
    for scope_name, value in target_scope_contract.items():
        if not value:
            missing_fields.append(f"target_{scope_name}")

    mismatched_fields = []
    for scope_name, source_value in source_scope_contract.items():
        target_value = target_scope_contract.get(scope_name)
        if source_value and target_value and source_value != target_value:
            mismatched_fields.append(scope_name)

    blocked = bool(missing_fields or mismatched_fields)
    if missing_fields and mismatched_fields:
        reason = "missing_scope_fields_and_surface_mismatch"
    elif missing_fields:
        reason = "missing_scope_fields"
    elif mismatched_fields:
        reason = "surface_scope_mismatch"
    else:
        reason = "scope_matched"
    return {
        "flag": _PAPER_ONLY_STRATEGY_LAB_EXACT_CONTEXT_PROMOTION_FLAG,
        "enabled": True,
        "applies": True,
        "eligible": not blocked,
        "blocked": blocked,
        "reason": reason,
        "missing_fields": missing_fields,
        "mismatched_fields": mismatched_fields,
        "source_scope_contract": source_scope_contract,
        "target_scope_contract": target_scope_contract,
    }


def _paper_only_route_requirements_packet(route_status, profile):
    permissions = _paper_only_route_permissions(route_status, profile)
    permissions_set = set(permissions)
    required_side = _paper_only_required_side(route_status, profile)
    cross_surface_seed_guard_review = _paper_only_cross_surface_seed_guard_review(
        route_status,
        profile,
    )
    carry_alignment_review = _paper_only_okx_basis_variant_alignment_review(route_status, profile)
    family_decay_guard_review = _paper_only_family_decay_guard_review(route_status, profile)
    exact_surface_scope_review = _paper_only_exact_surface_scope_review(route_status, profile)
    if family_decay_guard_review.get("blocked"):
        carry_alignment_review = dict(
            carry_alignment_review,
            eligible=False,
            blocked=True,
            reason="family_decay_suppressed",
        )
    if exact_surface_scope_review.get("blocked"):
        carry_alignment_review = dict(
            carry_alignment_review,
            eligible=False,
            blocked=True,
            reason=exact_surface_scope_review.get("reason") or "surface_scope_mismatch",
        )
    carry_alignment_review = dict(
        carry_alignment_review,
        cross_surface_seed_guard_review=cross_surface_seed_guard_review,
        family_decay_guard_review=family_decay_guard_review,
        exact_surface_scope_review=exact_surface_scope_review,
    )

    margin_available = _paper_only_route_support_bool(
        _paper_only_route_lookup(route_status, profile, "margin_available", "margin_enabled", "margin_support")
    )
    if margin_available is None and permissions_set.intersection({"margin", "cross_margin", "isolated_margin"}):
        margin_available = True

    shortability = _paper_only_route_support_bool(
        _paper_only_route_lookup(
            route_status, profile, "spot_short_support", "shortability", "spot_shortability", "short_support"
        )
    )
    perp_available = _paper_only_route_support_bool(
        _paper_only_route_lookup(route_status, profile, "perp_support", "perp_available", "perp_enabled")
    )
    if perp_available is None:
        basis_support = _paper_only_route_support_bool(_paper_only_route_lookup(route_status, profile, "basis_support"))
        if basis_support is True:
            perp_available = True

    hedge_mode_support = _paper_only_route_support_bool(
        _paper_only_route_lookup(route_status, profile, "hedge_mode_support", "hedge_mode", "hedge_support")
    )

    borrow_hint_value = _paper_only_route_lookup(
        route_status,
        profile,
        "borrow_hint",
        "borrow_available",
        "borrow_support",
        "borrow_indication",
        "borrow_required",
    )
    borrow_hint_present = _paper_only_route_support_bool(borrow_hint_value)
    borrow_hint_token = _paper_only_route_signal_token(borrow_hint_value)
    if borrow_hint_present is None and borrow_hint_token in {"hinted", "hint", "present", "required"}:
        borrow_hint_present = True
    if borrow_hint_present is None and permissions_set.intersection({"borrow", "margin", "short"}):
        borrow_hint_present = True

    route_confidence = _paper_only_route_confidence(
        _paper_only_route_lookup(
            route_status,
            profile,
            "route_confidence",
            "route_requirement_status",
            "route_status",
            "route_access_status",
            "status",
        ),
        default=0.25,
    )
    fee_confidence = _paper_only_route_confidence(
        _paper_only_route_lookup(route_status, profile, "fee_confidence", "fee_reference", "fee_source"),
        default=0.35,
    )
    api_surface_confidence = _paper_only_route_confidence(
        _paper_only_route_lookup(route_status, profile, "api_surface_confidence", "api_surface"),
        default=0.35,
    )

    borrow_required = required_side == "spot_short"
    borrow_confidence = _paper_only_route_confidence(
        _paper_only_route_lookup(
            route_status,
            profile,
            "borrow_confidence",
            "borrow_support",
            "borrow_hint",
            "borrow_available",
            "borrow_indication",
            "spot_short_support",
        ),
        default=0.25 if borrow_required else 1.0,
    )

    score_inputs = [route_confidence, fee_confidence, api_surface_confidence]
    route_complete = True
    critical_missing_fields = []
    if cross_surface_seed_guard_review.get("blocked"):
        route_confidence = 0.0
        score_inputs[0] = 0.0
        route_complete = False
        critical_missing_fields.append("cross_surface_seeding_blocked")

    if borrow_required:
        score_inputs.append(borrow_confidence)
        if shortability is not True:
            route_complete = False
            critical_missing_fields.append("shortability")
        if borrow_confidence < 0.75:
            route_complete = False
            critical_missing_fields.append("borrow")

    if required_side in {"perp", "spot_plus_perp"}:
        perp_confidence = 1.0 if perp_available is True else 0.0 if perp_available is False else 0.25
        score_inputs.append(perp_confidence)
        if perp_available is not True:
            route_complete = False
            critical_missing_fields.append("perp")

    if api_surface_confidence < 0.5:
        route_complete = False
        critical_missing_fields.append("api_surface")

    route_viability_score = sum(score_inputs) / float(len(score_inputs) or 1)
    if required_side == "spot_short" and (shortability is not True or borrow_confidence < 0.75):
        route_viability_score = min(route_viability_score, 0.34)
    if required_side == "spot_plus_perp":
        if perp_available is not True:
            route_viability_score = min(route_viability_score, 0.34)
        if route_confidence < 0.75:
            route_viability_score = min(route_viability_score, 0.49)
    if fee_confidence < 0.5:
        route_viability_score = max(0.0, route_viability_score - 0.1)
    paper_only_route_blocked = bool(
        carry_alignment_review.get("blocked") or cross_surface_seed_guard_review.get("blocked")
    )
    paper_only_block_reason = None
    if cross_surface_seed_guard_review.get("blocked"):
        paper_only_block_reason = cross_surface_seed_guard_review.get("gate_reason") or cross_surface_seed_guard_review.get("reason")
    elif carry_alignment_review.get("blocked"):
        paper_only_block_reason = carry_alignment_review.get("reason")
    if paper_only_route_blocked:
        route_complete = False
        if "carry_alignment" not in critical_missing_fields:
            critical_missing_fields.append("carry_alignment")
        route_viability_score = min(route_viability_score, 0.19)
    route_viability_score = round(max(0.0, min(1.0, route_viability_score)), 4)

    if paper_only_route_blocked or (not route_complete) or route_viability_score < 0.35:
        route_actionability = "low_priority_research"
        route_priority_cap = "low"
    elif route_viability_score < 0.75:
        route_actionability = "guarded_paper_review"
        route_priority_cap = "medium"
    else:
        route_actionability = "actionable_paper"
        route_priority_cap = "high"

    venue_capabilities = {
        "margin_available": margin_available,
        "shortability_indication": shortability,
        "borrow_hint_present": borrow_hint_present,
        "perp_available": perp_available,
        "hedge_mode_support": hedge_mode_support,
        "fee_source_confidence": fee_confidence,
    }
    return {
        "required_side": required_side,
        "required_permissions": permissions,
        "venue_capabilities": venue_capabilities,
        "route_confidence": route_confidence,
        "borrow_confidence": borrow_confidence,
        "fee_confidence": fee_confidence,
        "api_surface_confidence": api_surface_confidence,
        "route_viability_score": route_viability_score,
        "route_complete": route_complete,
        "route_priority_cap": route_priority_cap,
        "route_actionability": route_actionability,
        "critical_missing_fields": critical_missing_fields,
        "carry_alignment_review": carry_alignment_review,
        "yahoo_proxy_crypto_freshness_gate": cross_surface_seed_guard_review.get("freshness_gate"),
        "yahoo_proxy_cross_surface_alignment_guard": cross_surface_seed_guard_review.get(
            "alignment_guard"
        ),
        "propagated_momentum_contribution": cross_surface_seed_guard_review.get(
            "propagated_momentum_contribution"
        ),
        "paper_only_route_blocked": paper_only_route_blocked,
        "paper_only_block_reason": paper_only_block_reason,
        "emit_recommendation": not bool(cross_surface_seed_guard_review.get("blocked")),
        "emit_route": not bool(cross_surface_seed_guard_review.get("blocked")),
    }


def _paper_only_annotate_route_intelligence(route_status):
    if not isinstance(route_status, dict):
        return route_status
    profile = paper_only_route_requirement_profile(route_status)
    if not isinstance(profile, dict) or not profile:
        return route_status
    existing = route_status.get("route_requirements")
    if isinstance(existing, dict):
        merged = dict(profile)
        merged.update(existing)
        route_status["route_requirements"] = merged
    else:
        route_status["route_requirements"] = profile
    summary = _paper_only_route_review_text(
        route_status.get("route_requirement_summary") or profile.get("summary")
    )
    if summary:
        route_status["route_requirement_summary"] = summary
    requirement_status = _paper_only_route_review_text(
        route_status.get("route_requirement_status") or profile.get("route_requirement_status")
    )
    if requirement_status:
        route_status["route_requirement_status"] = requirement_status
    broker_surface = _paper_only_route_review_text(route_status.get("route_broker_surface") or profile.get("broker_surface"))
    if broker_surface:
        route_status["route_broker_surface"] = broker_surface
    api_surface = _paper_only_route_review_text(route_status.get("api_surface") or profile.get("api_surface"))
    if api_surface:
        route_status["api_surface"] = api_surface
    if route_status.get("required_permissions") in (None, "", [], {}, ()):
        route_status["required_permissions"] = list(profile.get("required_permissions") or [])
    route_packet = _paper_only_route_requirements_packet(route_status, profile)
    existing_packet = route_status.get("route_requirements_packet")
    if isinstance(existing_packet, dict):
        merged_packet = dict(existing_packet)
        merged_packet.update(route_packet)
        route_packet = merged_packet
    route_status["route_requirements_packet"] = route_packet
    yahoo_freshness_gate = route_packet.get("yahoo_proxy_crypto_freshness_gate")
    if isinstance(yahoo_freshness_gate, dict) and yahoo_freshness_gate.get("applies"):
        route_status["yahoo_proxy_crypto_freshness_gate"] = yahoo_freshness_gate
        route_status["propagated_momentum_contribution"] = yahoo_freshness_gate.get(
            "propagated_momentum_contribution"
        )
        route_status["proxy_momentum_gate_reason"] = yahoo_freshness_gate.get("gate_reason")
    yahoo_alignment_guard = route_packet.get("yahoo_proxy_cross_surface_alignment_guard")
    if isinstance(yahoo_alignment_guard, dict) and yahoo_alignment_guard.get("applies"):
        route_status["yahoo_proxy_cross_surface_alignment_guard"] = yahoo_alignment_guard
        route_status["local_cross_surface_confirmation"] = yahoo_alignment_guard.get(
            "local_direction_confirmed"
        )
    if route_packet.get("paper_only_route_blocked"):
        route_status["route_requirement_status"] = "blocked"
        route_status["route_complete"] = False
        route_status["route_priority_cap"] = route_packet.get("route_priority_cap") or "low"
        route_status["route_actionability"] = route_packet.get("route_actionability") or "low_priority_research"
        if route_status.get("route_requirement_summary") in (None, "", [], {}, ()):
            route_status["route_requirement_summary"] = (
                f"paper_only_blocked:{route_packet.get('paper_only_block_reason') or 'carry_alignment'}"
            )
    cross_surface_seed_review = (
        (route_packet.get("carry_alignment_review") or {}).get("cross_surface_seed_guard_review")
        if isinstance(route_packet.get("carry_alignment_review"), dict)
        else None
    )
    if isinstance(cross_surface_seed_review, dict) and cross_surface_seed_review.get("blocked"):
        route_status["emit_recommendation"] = False
        route_status["emit_route"] = False
        route_status["paper_entry_blocked"] = True
        route_status["promotion_eligible"] = False
        route_status["paper_allocation_multiplier"] = 0.0
    existing_capabilities = route_status.get("venue_capabilities")
    if isinstance(existing_capabilities, dict):
        merged_capabilities = dict(existing_capabilities)
        merged_capabilities.update(route_packet.get("venue_capabilities") or {})
        route_status["venue_capabilities"] = merged_capabilities
    else:
        route_status["venue_capabilities"] = dict(route_packet.get("venue_capabilities") or {})
    for field_name in (
        "required_side",
        "route_confidence",
        "borrow_confidence",
        "fee_confidence",
        "api_surface_confidence",
        "route_viability_score",
        "route_complete",
        "route_priority_cap",
        "route_actionability",
        "critical_missing_fields",
        "carry_alignment_review",
        "yahoo_proxy_crypto_freshness_gate",
        "yahoo_proxy_cross_surface_alignment_guard",
        "propagated_momentum_contribution",
        "paper_only_route_blocked",
        "paper_only_block_reason",
    ):
        if route_status.get(field_name) in (None, "", [], {}, ()):
            route_status[field_name] = route_packet.get(field_name)
    return route_status


def _paper_only_spread_volatility_gate(config, spread_bps=None, volatility_pct=None):
    """Paper-only gate that requires both spread and volatility to be acceptable."""
    if not isinstance(config, dict):
        return {"enabled": False, "spread_pass": True, "volatility_pass": True, "pass": True}
    enabled = _paper_only_route_guard_short_frontier_spot_enabled(config)
    direct_flag = _paper_only_route_review_bool(config.get(_PAPER_ONLY_SPREAD_VOLATILITY_GATE_FLAG))
    if direct_flag is not None:
        enabled = direct_flag
    feature_flags = config.get("feature_flags")
    if isinstance(feature_flags, dict):
        nested_flag = _paper_only_route_review_bool(feature_flags.get(_PAPER_ONLY_SPREAD_VOLATILITY_GATE_FLAG))
        if nested_flag is not None:
            enabled = nested_flag
    spread_cap = _paper_only_route_review_float(config.get("spread_bps_max"))
    if spread_cap is None and isinstance(feature_flags, dict):
        spread_cap = _paper_only_route_review_float(feature_flags.get("spread_bps_max"))
    volatility_cap = _paper_only_route_review_float(config.get("volatility_pct_max"))
    if volatility_cap is None and isinstance(feature_flags, dict):
        volatility_cap = _paper_only_route_review_float(feature_flags.get("volatility_pct_max"))
    spread_pass = True if spread_bps is None or spread_cap is None else float(spread_bps) <= float(spread_cap)
    volatility_pass = True if volatility_pct is None or volatility_cap is None else float(volatility_pct) <= float(volatility_cap)
    return {
        "enabled": bool(enabled),
        "spread_pass": bool(spread_pass),
        "volatility_pass": bool(volatility_pass),
        "pass": bool((not enabled) or (spread_pass and volatility_pass)),
        "spread_bps_max": spread_cap,
        "volatility_pct_max": volatility_cap,
    }


def _paper_only_route_guard_short_frontier_spot_enabled(config):
    if not isinstance(config, dict):
        return False
    direct_value = config.get(_PAPER_ROUTE_GUARD_SHORT_FRONTIER_SPOT_FLAG)
    direct_flag = _paper_only_route_review_bool(direct_value)
    if direct_flag is not None:
        return direct_flag
    feature_flags = config.get("feature_flags")
    if isinstance(feature_flags, dict):
        nested_flag = _paper_only_route_review_bool(feature_flags.get(_PAPER_ROUTE_GUARD_SHORT_FRONTIER_SPOT_FLAG))
        if nested_flag is not None:
            return nested_flag
    return False


def _paper_only_strategy_lab_exact_context_guard_enabled(config, *, context_review=None):
    direct_flag = None
    if isinstance(config, dict):
        direct_flag = _paper_only_route_review_bool(config.get(_PAPER_ONLY_STRATEGY_LAB_EXACT_CONTEXT_PROMOTION_FLAG))
        if direct_flag is None:
            feature_flags = config.get("feature_flags")
            if isinstance(feature_flags, dict):
                direct_flag = _paper_only_route_review_bool(
                    feature_flags.get(_PAPER_ONLY_STRATEGY_LAB_EXACT_CONTEXT_PROMOTION_FLAG)
                )
    if direct_flag is not None:
        return bool(direct_flag)
    return bool(isinstance(context_review, dict) and context_review.get("enabled"))


def _paper_only_route_context_value(route_status, direct_keys, *, nested_keys=()):
    if not isinstance(route_status, dict):
        return None
    _paper_only_annotate_route_intelligence(route_status)
    for key in direct_keys:
        value = route_status.get(key)
        if value not in (None, "", [], {}, ()):
            return value
    for nested_key in nested_keys:
        nested = route_status.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for key in direct_keys:
            value = nested.get(key)
            if value not in (None, "", [], {}, ()):
                return value
    return None


def _paper_only_strategy_lab_context_transfer_review(route_status, *, context_review=None, config=None):
    review = {
        "enabled": False,
        "applies": False,
        "exact_context_required": False,
        "match": True,
        "block_promotion": False,
        "reason": None,
        "mismatch_fields": [],
        "source_context": {},
        "target_context": {},
        "required_fields": ["venue_class", "market_surface", "direction", "data_source_class"],
        "missing_fields": [],
        "evidence_eligible": True,
        "sample_size": None,
        "min_sample_size": None,
        "sample_guard_passed": True,
        "promotion_delta_scale": 1.0,
        "cross_context_tag": False,
        "cross_context_validated": False,
    }
    if not isinstance(route_status, dict):
        return review

    required_fields = set(review["required_fields"])
    review["enabled"] = _paper_only_strategy_lab_exact_context_guard_enabled(config, context_review=context_review)
    origin_tokens = []
    for value in (
        _paper_only_route_context_value(
            route_status,
            ("recommendation_source", "promotion_source", "source_agent", "origin", "strategy_source"),
            nested_keys=("recommendation_metadata", "strategy_metadata", "promotion_context", "strategy_lab_context"),
        ),
        route_status.get("experiment_id"),
        route_status.get("variant_id"),
        route_status.get("strategy_lab_variant"),
    ):
        text = _paper_only_safe_route_tag(value)
        if text:
            origin_tokens.append(text)

    review["applies"] = any("strategy_lab" in token for token in origin_tokens)
    review["exact_context_required"] = bool(review["enabled"] and review["applies"])
    if not review["exact_context_required"]:
        return review
    if not isinstance(context_review, dict) or not context_review.get("enabled"):
        cross_context_tag_value = _paper_only_route_context_value(
            route_status,
            ("cross_context_tag", "allow_cross_context", "allow_cross_context_inheritance", "paper_cross_context_tag", "paper_only_cross_context_tag"),
            nested_keys=("promotion_context", "recommendation_context", "recommendation_metadata", "strategy_lab_context", "strategy_metadata"),
        )
        review["cross_context_tag"] = bool(_paper_only_route_review_bool(cross_context_tag_value))
        review["match"] = False
        review["block_promotion"] = True
        review["reason"] = "strategy_lab_context_evidence_missing"
        review["promotion_delta_scale"] = 0.0
        return review

    review["evidence_eligible"] = bool(context_review.get("eligible", True))
    review["sample_size"] = _paper_only_route_review_float(context_review.get("sample_size"))
    review["min_sample_size"] = _paper_only_route_review_float(context_review.get("min_sample_size"))
    if review["sample_size"] is not None and review["min_sample_size"] is not None:
        cross_context_tag_value = _paper_only_route_context_value(
            route_status,
            ("cross_context_tag", "allow_cross_context", "allow_cross_context_inheritance", "paper_cross_context_tag", "paper_only_cross_context_tag"),
            nested_keys=("promotion_context", "recommendation_context", "recommendation_metadata", "strategy_lab_context", "strategy_metadata"),
        )
        review["sample_guard_passed"] = review["sample_size"] >= review["min_sample_size"]

    field_aliases = {
        "venue_class": {
            "source": ("source_venue_class", "recommendation_venue_class", "promotion_venue_class", "venue_class"),
            "target": ("target_venue_class", "venue_class", "execution_venue_class"),
        },
        "venue_family": {
            "source": ("source_venue_family", "recommendation_venue_family", "promotion_venue_family", "venue_family"),
            "target": ("target_venue_family", "venue_family", "execution_venue_family", "venue", "execution_venue"),
        },
        "market_surface": {
            "source": ("source_market_surface", "recommendation_market_surface", "promotion_market_surface", "source_execution_surface", "recommendation_execution_surface", "promotion_execution_surface"),
            "target": ("target_market_surface", "market_surface", "execution_surface", "surface", "market_surface", "market_type"),
        },
        "execution_surface": {
            "source": ("source_execution_surface", "recommendation_execution_surface", "promotion_execution_surface"),
            "target": ("target_execution_surface", "execution_surface", "surface", "market_surface", "market_type"),
        },
        "directional_template": {
            "source": (
                "source_directional_template",
                "recommendation_directional_template",
                "promotion_directional_template",
                "source_strategy_family",
            ),
            "target": ("target_directional_template", "directional_template", "strategy_family", "candidate_family", "strategy"),
        },
        "direction": {
            "source": ("source_direction", "recommendation_direction", "promotion_direction", "source_side", "recommendation_side", "promotion_side"),
            "target": ("target_direction", "direction", "side", "signal_side", "candidate_direction", "recommended_side", "position_side", "position_direction"),
        },
        "data_source_class": {
            "source": ("source_data_source_class", "recommendation_data_source_class", "promotion_data_source_class", "data_source_class", "source_data_source", "data_source"),
            "target": ("target_data_source_class", "data_source_class", "market_data_source_class", "market_data_source", "quote_source", "data_source"),
        },
        "market_regime_tag": {
            "source": ("source_market_regime_tag", "recommendation_market_regime_tag", "promotion_market_regime_tag"),
            "target": ("target_market_regime_tag", "market_regime_tag", "regime_tag", "market_regime"),
        },
    }
    nested_source_keys = ("strategy_lab_context", "source_context", "promotion_context", "recommendation_context", "recommendation_metadata")
    review["cross_context_tag"] = bool(_paper_only_route_review_bool(cross_context_tag_value))
    nested_target_keys = ("target_context", "recommendation_context", "recommendation_metadata", "strategy_metadata")

    for field_name, aliases in field_aliases.items():
        source_value = _paper_only_route_context_value(route_status, aliases["source"], nested_keys=nested_source_keys)
        target_value = _paper_only_route_context_value(route_status, aliases["target"], nested_keys=nested_target_keys)
        source_text = _paper_only_safe_route_tag(source_value)
        target_text = _paper_only_safe_route_tag(target_value)
        review["source_context"][field_name] = source_text
        review["target_context"][field_name] = target_text
        if field_name in required_fields:
            if not source_text:
                review["missing_fields"].append(f"source:{field_name}")
            if not target_text:
                review["missing_fields"].append(f"target:{field_name}")
        if source_text and target_text and source_text != target_text:
            review["match"] = False
            review["mismatch_fields"].append(field_name)

    if review["missing_fields"]:
        review["block_promotion"] = True
        review["reason"] = "strategy_lab_exact_context_incomplete"
        review["promotion_delta_scale"] = 0.0
        return review

    if not review["match"]:
        if not review["cross_context_tag"]:
            review["block_promotion"] = True
            review["reason"] = "strategy_lab_exact_context_mismatch"
            review["promotion_delta_scale"] = 0.0
            return review
        if context_review.get("matched") is False or not review["evidence_eligible"]:
            review["block_promotion"] = True
            review["reason"] = "strategy_lab_cross_context_evidence_ineligible"
            review["promotion_delta_scale"] = 0.0
            return review
        if not review["sample_guard_passed"]:
            review["block_promotion"] = True
            review["reason"] = "strategy_lab_cross_context_sample_guard"
            review["promotion_delta_scale"] = 0.0
            return review
        review["cross_context_validated"] = True
        review["reason"] = "strategy_lab_cross_context_validated"
        return review

    if context_review.get("matched") is False or not review["evidence_eligible"]:
        review["block_promotion"] = True
        review["reason"] = "strategy_lab_context_evidence_ineligible"
        review["promotion_delta_scale"] = 0.0
        return review

    if not review["sample_guard_passed"]:
        review["block_promotion"] = True
        review["reason"] = "strategy_lab_context_evidence_sample_guard"
        review["promotion_delta_scale"] = 0.0
    return review


def _paper_only_route_guard_min_liquidity_enabled(config):
    if not isinstance(config, dict):
        return False
    direct_value = config.get(_PAPER_ONLY_ROUTE_GUARD_MIN_LIQUIDITY_FLAG)
    direct_flag = _paper_only_route_review_bool(direct_value)
    if direct_flag is not None:
        return direct_flag
    feature_flags = config.get("feature_flags")
    if isinstance(feature_flags, dict):
        nested_flag = _paper_only_route_review_bool(feature_flags.get(_PAPER_ONLY_ROUTE_GUARD_MIN_LIQUIDITY_FLAG))
        if nested_flag is not None:
            return nested_flag
    return False


def _paper_only_enforced_route_resolution_enabled(config):
    if not isinstance(config, dict):
        return False
    direct_value = config.get(_PAPER_ONLY_ENFORCED_ROUTE_RESOLUTION_FLAG)
    direct_flag = _paper_only_route_review_bool(direct_value)
    if direct_flag is not None:
        return direct_flag
    feature_flags = config.get("feature_flags")
    if isinstance(feature_flags, dict):
        nested_flag = _paper_only_route_review_bool(feature_flags.get(_PAPER_ONLY_ENFORCED_ROUTE_RESOLUTION_FLAG))
        if nested_flag is not None:
            return nested_flag
    return False


def _paper_only_route_review_lookup(route_status, *keys):
    if not isinstance(route_status, dict):
        return None
    _paper_only_annotate_route_intelligence(route_status)
    for key in keys:
        value = route_status.get(key)
        if value not in (None, "", [], {}, ()):
            return value
    return None


def _paper_only_route_mode_enabled(value):
    flag = _paper_only_route_review_bool(value)
    if flag is not None:
        return flag
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if text in {"paper", "paper_only", "sim", "simulation", "simulated", "enforced"}:
        return True
    if text in {"live", "real", "production", "prod"}:
        return False
    return None


def _paper_only_route_has_explicit_paper_mode(route_status, *, config=None):
    for value in (
        _paper_only_route_review_lookup(route_status, "paper_mode", "paper_only", "execution_mode", "mode"),
        config.get("paper_mode") if isinstance(config, dict) else None,
    ):
        flag = _paper_only_route_mode_enabled(value)
        if flag is not None:
            return flag
    return False


def _paper_only_safe_route_tag(text):
    normalized = str(text or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized else None


def _paper_only_route_status_text(route_status):
    if isinstance(route_status, dict):
        _paper_only_annotate_route_intelligence(route_status)
        return _paper_only_route_review_text(route_status.get("route_status") or route_status.get("status"))
    return _paper_only_route_review_text(route_status)


def _paper_only_shadow_direction_inversion_enabled(config):
    if not isinstance(config, dict):
        return False
    direct_flag = _paper_only_route_review_bool(config.get(_PAPER_ONLY_SHADOW_DIRECTION_INVERSION_FLAG))
    if direct_flag is not None:
        return direct_flag
    feature_flags = config.get("feature_flags")
    if isinstance(feature_flags, dict):
        nested_flag = _paper_only_route_review_bool(feature_flags.get(_PAPER_ONLY_SHADOW_DIRECTION_INVERSION_FLAG))
        if nested_flag is not None:
            return nested_flag
    return False


def _paper_only_shadow_direction_inversion_scope(route_status, *, context_review=None):
    tokens = []

    def _append_token(value):
        text = _paper_only_safe_route_tag(value)
        if text:
            tokens.append(text)

    if isinstance(context_review, dict):
        for key in ("context_signature_key", "context_signature", "reason"):
            _append_token(context_review.get(key))
    if isinstance(route_status, dict):
        candidate_keys = (
            "market_key",
            "signal_key",
            "family",
            "strategy",
            "strategy_family",
            "candidate_family",
            "directional_template",
            "variant_id",
            "experiment_id",
            "signal_family",
        )
        nested_keys = (
            "recommendation_metadata",
            "strategy_metadata",
            "promotion_context",
            "strategy_lab_context",
            "recommendation_context",
        )
        for key in candidate_keys:
            _append_token(route_status.get(key))
        for nested_key in nested_keys:
            nested = route_status.get(nested_key)
            if not isinstance(nested, dict):
                continue
            for key in candidate_keys:
                _append_token(nested.get(key))

    joined = " ".join(tokens)
    in_scope = "yahoo_proxy" in joined and "global_proxy_momentum" in joined
    return {
        "in_scope": bool(in_scope),
        "market_key": "YAHOO_PROXY" if "yahoo_proxy" in joined else None,
        "signal_family": "global_proxy_momentum" if "global_proxy_momentum" in joined else None,
    }


def _paper_only_shadow_direction_inversion_baseline(route_status, *, context_review=None):
    candidates = []
    if isinstance(route_status, dict):
        direct_keys = (
            "direction",
            "side",
            "signal_side",
            "candidate_direction",
            "recommended_side",
            "position_side",
            "position_direction",
            "signal_key",
            "variant_id",
            "strategy_family",
            "candidate_family",
        )
        nested_keys = (
            "recommendation_metadata",
            "strategy_metadata",
            "promotion_context",
            "strategy_lab_context",
            "recommendation_context",
        )
        for key in direct_keys:
            candidates.append(route_status.get(key))
        for nested_key in nested_keys:
            nested = route_status.get(nested_key)
            if not isinstance(nested, dict):
                continue
            for key in direct_keys:
                candidates.append(nested.get(key))
    if isinstance(context_review, dict):
        candidates.extend(
            [
                context_review.get("context_signature_key"),
                context_review.get("context_signature"),
            ]
        )

    for candidate in candidates:
        text = _paper_only_safe_route_tag(candidate)
        if not text:
            continue
        if (
            text in {"buy", "long", "long_only"}
            or "long_proxy" in text
            or text.startswith("long_")
            or text.endswith("_long")
        ):
            return "long"
        if (
            text in {"sell", "short", "short_only"}
            or "short_proxy" in text
            or text.startswith("short_")
            or text.endswith("_short")
        ):
            return "short"
    return None


def _paper_only_shadow_direction_inversion_review(route_status, *, context_review=None, config=None):
    review = {
        "enabled": False,
        "eligible": False,
        "paper_only": True,
        "preserve_baseline": True,
        "activation_mode": "paper_shadow_parallel",
        "variant": "shadow_direction_inversion",
        "market_key": None,
        "signal_family": None,
        "baseline_direction": None,
        "shadow_direction": None,
        "sample_size": None,
        "min_sample_size": 20.0,
        "expectancy_bps": None,
        "publish_shadow_candidate": False,
        "reason": "guard_disabled",
    }
    review["enabled"] = _paper_only_shadow_direction_inversion_enabled(config)
    if not review["enabled"]:
        return review

    scope = _paper_only_shadow_direction_inversion_scope(route_status, context_review=context_review)
    review["market_key"] = scope.get("market_key")
    review["signal_family"] = scope.get("signal_family")
    if not scope.get("in_scope"):
        review["reason"] = "out_of_scope_family"
        return review

    if isinstance(context_review, dict):
        review["sample_size"] = _paper_only_route_review_float(context_review.get("sample_size"))
        min_sample_size = _paper_only_route_review_float(context_review.get("min_sample_size"))
        if min_sample_size is not None:
            review["min_sample_size"] = float(min_sample_size)
        review["expectancy_bps"] = _paper_only_route_review_float(context_review.get("expectancy_bps"))
    if review["sample_size"] is None:
        review["reason"] = "missing_sample_size"
        return review
    if float(review["sample_size"]) < float(review["min_sample_size"]):
        review["reason"] = "insufficient_sample_size"
        return review
    if review["expectancy_bps"] is None:
        review["reason"] = "missing_expectancy"
        return review
    if float(review["expectancy_bps"]) >= 0.0:
        review["reason"] = "non_negative_expectancy"
        return review

    review["baseline_direction"] = _paper_only_shadow_direction_inversion_baseline(
        route_status,
        context_review=context_review,
    )
    if review["baseline_direction"] == "long":
        review["shadow_direction"] = "short"
    elif review["baseline_direction"] == "short":
        review["shadow_direction"] = "long"
    else:
        review["reason"] = "direction_unresolved"
        return review

    review["eligible"] = True
    review["publish_shadow_candidate"] = True
    review["reason"] = "negative_expectancy_shadow_inverse"
    return review


def _paper_only_apply_context_inheritance_review(review, route_status, *, config=None):
    if not isinstance(review, dict):
        return review
    context_review = _paper_only_context_evidence_review(route_status, config=config)
    if not isinstance(context_review, dict):
        return review
    if context_review.get("enabled") or context_review.get("context_signature_key"):
        review["context_signature"] = context_review.get("context_signature")
        review["context_signature_key"] = context_review.get("context_signature_key")
        review["context_match_found"] = bool(context_review.get("matched"))
        review["context_sample_size"] = context_review.get("sample_size")
        review["context_expectancy_bps"] = context_review.get("expectancy_bps")
        review["inherited_confidence"] = context_review.get("inherited_confidence")
        review["variant_state"] = context_review.get("variant_state")
        review["recommendation_eligible"] = bool(context_review.get("eligible"))
        review["context_guard_reason"] = context_review.get("reason")
    shadow_review = paper_only_shadow_direction_inversion_review(route_status, config=config)
    if isinstance(shadow_review, dict) and shadow_review.get("enabled"):
        review["shadow_direction_inversion_enabled"] = True
        review["shadow_direction_inversion_applies"] = bool(shadow_review.get("applies"))
        review["shadow_direction_inversion_reason"] = shadow_review.get("reason")
        review["shadow_direction_activation_mode"] = shadow_review.get("activation_mode")
        review["shadow_direction_market_key"] = shadow_review.get("market_key")
        review["shadow_direction_family"] = shadow_review.get("family")
        review["shadow_direction_baseline"] = shadow_review.get("baseline_direction")
        review["shadow_direction_candidate"] = shadow_review.get("shadow_direction")
        review["shadow_direction_variant"] = shadow_review.get("variant")
        review["shadow_direction_scope_matched"] = bool(shadow_review.get("scope_matched"))
        if shadow_review.get("signal_key"):
            review["shadow_direction_signal_key"] = shadow_review.get("signal_key")
    if context_review.get("enabled") and not context_review.get("eligible"):
        if review.get("route_status") in (None, "", "eligible"):
            review["route_status"] = "paper_shadow_only"
        review["paper_eligible"] = False
        review["trade_effect"] = review.get("trade_effect") or "none"
    shadow_review = _paper_only_shadow_direction_inversion_review(
        route_status,
        context_review=context_review,
        config=config,
    )
    review["shadow_direction_inversion"] = shadow_review
    if shadow_review.get("enabled"):
        review["shadow_activation_mode"] = shadow_review.get("activation_mode")
        review["shadow_publish"] = bool(shadow_review.get("publish_shadow_candidate"))
        review["shadow_reason"] = shadow_review.get("reason")
        review["shadow_variant"] = shadow_review.get("variant")
        review["shadow_direction"] = shadow_review.get("shadow_direction")
    return review


def _paper_only_short_route_review(route_status, *, config=None):
    def _fallback_review(reason, base_review=None):
        review = dict(base_review or {})
        review.update(
            {
                "route_status": "eligible",
                "short_support_source": review.get("short_support_source") or "paper_policy_fallback",
                "route_blocked": False,
                "paper_mode": True,
                "execution_mode": "paper",
                "transport": "no_send",
                "simulated_venue_tag": "paper_sim_only",
                "fill_model": "synthetic_best_effort",
                "deny_live_transmit": True,
                "route_resolution_policy": "simulate_on_ambiguity",
                "paper_eligible": True,
                "route_supported": True,
                "fallback_applied": True,
                "fallback_reason": reason,
                "trade_effect": "none",
            }
        )
        return review

    def _has_simulated_venue_tag(value):
        if not isinstance(value, dict):
            return False
        governor = value.get("build_governor_fields")
        candidates = [
            _paper_only_route_review_lookup(
                value,
                "simulated_venue_tag",
                "venue_tag",
                "route_transport",
                "transport",
                "execution_mode",
                "fill_model",
                "venue_allowlist",
                "audit_tag",
                "venue",
                "execution_venue",
                "route_destination",
            )
        ]
        if isinstance(governor, dict):
            candidates.extend(
                [
                    governor.get("paper_mode"),
                    governor.get("transport"),
                    governor.get("fill_model"),
                    governor.get("venue_allowlist"),
                    governor.get("audit_tag"),
                ]
            )
        for candidate in candidates:
            text = _paper_only_safe_route_tag(candidate)
            if not text:
                continue
            if any(token in text for token in ("paper", "sim", "sandbox", "synthetic", "no_send")):
                return True
        return False

    review = {
        "route_status": _paper_only_route_status_text(route_status),
        "short_support_source": None,
        "borrow_support_flag": None,
        "margin_mode_flag": None,
        "block_reason": None,
        "paper_mode": None,
        "execution_mode": None,
        "transport": None,
        "simulated_venue_tag": None,
        "fill_model": None,
        "deny_live_transmit": None,
        "route_resolution_policy": None,
        "paper_eligible": None,
        "route_supported": None,
        "route_blocked": False,
        "fallback_applied": False,
        "fallback_reason": None,
        "trade_effect": None,
    }
    if not isinstance(route_status, dict):
        if _paper_only_enforced_route_resolution_enabled(config):
            return _fallback_review("missing_route_metadata", review)
        return review

    review["short_support_source"] = _paper_only_route_review_text(
        route_status.get("short_support_source")
        or route_status.get("route_source")
        or route_status.get("capability_source")
    )
    review["borrow_support_flag"] = _paper_only_route_review_bool(
        route_status.get("borrow_support_flag")
        or route_status.get("borrow_supported")
        or route_status.get("short_supported")
        or route_status.get("short_route_supported")
    )
    review["margin_mode_flag"] = _paper_only_route_review_text(
        route_status.get("margin_mode_flag") or route_status.get("margin_mode")
    )
    review["block_reason"] = _paper_only_route_review_text(
        route_status.get("block_reason") or route_status.get("blocking_reason")
    )
    review["paper_mode"] = _paper_only_route_mode_enabled(
        _paper_only_route_review_lookup(route_status, "paper_mode", "paper_only", "execution_mode", "mode")
    )
    review["execution_mode"] = _paper_only_route_review_text(route_status.get("execution_mode"))
    review["transport"] = _paper_only_route_review_text(
        _paper_only_route_review_lookup(route_status, "route_transport", "transport")
    )
    review["simulated_venue_tag"] = _paper_only_route_review_text(
        _paper_only_route_review_lookup(route_status, "simulated_venue_tag", "venue_tag")
    )
    review["fill_model"] = _paper_only_route_review_text(route_status.get("fill_model"))
    review["deny_live_transmit"] = _paper_only_route_review_bool(
        route_status.get("deny_live_transmit") or route_status.get("transport_guard")
    )
    review["route_resolution_policy"] = _paper_only_route_review_text(route_status.get("route_resolution_policy"))
    review["paper_eligible"] = _paper_only_route_review_bool(route_status.get("paper_eligible"))
    review["route_supported"] = _paper_only_route_review_bool(route_status.get("route_supported"))
    review["trade_effect"] = _paper_only_route_review_text(route_status.get("trade_effect"))

    if not _paper_only_route_guard_short_frontier_spot_enabled(config):
        if _paper_only_enforced_route_resolution_enabled(config):
            if not _paper_only_route_has_explicit_paper_mode(route_status, config=config):
                return _fallback_review("paper_mode_required", review)
            if not _has_simulated_venue_tag(route_status):
                return _fallback_review("missing_simulated_venue_tag", review)
            if review["route_status"] is None:
                return _fallback_review("unresolved_route_status", review)
        return _paper_only_apply_context_inheritance_review(review, route_status, config=config)

    if _paper_only_enforced_route_resolution_enabled(config):
        if not _paper_only_route_has_explicit_paper_mode(route_status, config=config):
            return _fallback_review("paper_mode_required", review)
        if not _has_simulated_venue_tag(route_status):
            return _fallback_review("missing_simulated_venue_tag", review)

    strategy_family = _paper_only_route_review_text(
        route_status.get("strategy_family") or route_status.get("candidate_family") or route_status.get("strategy")
    )
    if str(strategy_family or "").strip().lower() != "short_frontier_spot":
        return _paper_only_apply_context_inheritance_review(review, route_status, config=config)

    explicit_supported = _paper_only_route_review_bool(route_status.get("route_supported"))
    if explicit_supported is None:
        explicit_supported = _paper_only_route_review_bool(route_status.get("paper_eligible"))

    margin_mode_key = str(review["margin_mode_flag"] or "").strip().lower()
    margin_confirms_short = margin_mode_key in {"cross", "isolated", "margin", "spot_margin", "portfolio_margin"}
    short_confirmed = explicit_supported is True or review["borrow_support_flag"] is True or margin_confirms_short
    short_explicitly_blocked = (
        explicit_supported is False
        or review["borrow_support_flag"] is False
        or margin_mode_key in {"none", "cash_only", "spot_only", "unsupported", "disabled"}
    )
    if short_confirmed:
        review["route_status"] = review["route_status"] or "eligible"
        return _paper_only_apply_context_inheritance_review(review, route_status, config=config)

    review["route_blocked"] = True
    if review["route_status"] is None:
        review["route_status"] = "blocked_route"

    if review["block_reason"] is None:
        review["block_reason"] = "short_route_unsupported" if short_explicitly_blocked else "short_route_support_unconfirmed"
    return _paper_only_apply_context_inheritance_review(review, route_status, config=config)


def _paper_only_build_governor_fields(*, source="frontier_crypto_adapter", paper_only=True):
    """Return lightweight governance metadata for paper-only packets."""

    return {
        "source_module": source,
        "paper_only": bool(paper_only),
        "execution_mode": "paper",
        "paper_mode": "enforced" if paper_only else "disabled",
        "transport": "no_send" if paper_only else "none",
        "fill_model": "synthetic_best_effort" if paper_only else "none",
        "route_resolution_policy": "simulate_on_ambiguity" if paper_only else "none",
        "venue_allowlist": "paper_sim_only" if paper_only else "none",
        "deny_live_transmit": bool(paper_only),
        "audit_tag": "execution_route_hunter_paper_only" if paper_only else "non_paper_context",
        "trade_effect": "none",
    }




def paper_only_premarket_liquidity_gate(
    *,
    dollar_volume_usd=None,
    spread_pct=None,
    recent_trade_count=None,
    recent_print_window_minutes=None,
    config=None,
):
    """Return whether a paper-only premarket gap candidate is liquid enough."""

    thresholds = dict(_PAPER_ONLY_PREMARKET_LIQUIDITY_DEFAULTS)
    if isinstance(config, dict):
        for key in thresholds:
            value = config.get(key)
            if value is not None:
                thresholds[key] = value

    def _as_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    dollar_volume = _as_float(dollar_volume_usd)
    spread = _as_float(spread_pct)
    trade_count = _as_float(recent_trade_count)
    print_window = _as_float(recent_print_window_minutes)

    if dollar_volume is None or dollar_volume < float(thresholds["min_premarket_dollar_volume_usd"]):
        return False
    if spread is None or spread > float(thresholds["max_spread_pct"]):
        return False
    if trade_count is None or trade_count < float(thresholds["min_recent_trade_count"]):
        return False
    if print_window is None or print_window < float(thresholds["min_recent_prints_window_minutes"]):
        return False
    return True


def _paper_only_valr_float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _paper_only_valr_pick_first(payload, *keys):
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}, ()):
            return value
    return None


def _paper_only_valr_order_book_top(levels, *, reverse=False):
    if not isinstance(levels, (list, tuple)):
        return (None, None)

    normalized = []
    for level in levels:
        if isinstance(level, dict):
            price = _paper_only_valr_float_or_none(
                level.get("price") or level.get("rate") or level.get("bidPrice") or level.get("askPrice")
            )
            quantity = _paper_only_valr_float_or_none(
                level.get("quantity") or level.get("qty") or level.get("volume") or level.get("size")
            )
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price = _paper_only_valr_float_or_none(level[0])
            quantity = _paper_only_valr_float_or_none(level[1])
        else:
            continue
        if price is None or quantity is None or price <= 0.0 or quantity <= 0.0:
            continue
        normalized.append((price, quantity))

    if not normalized:
        return (None, None)

    normalized.sort(key=lambda item: item[0], reverse=bool(reverse))
    return normalized[0]


def _paper_only_valr_shallow_order_book(levels, *, reverse=False, max_levels=5):
    """Normalize a bounded public VALR book without retaining full depth."""

    if not isinstance(levels, (list, tuple)):
        return []
    normalized = []
    for level in levels:
        if isinstance(level, dict):
            price = _paper_only_valr_float_or_none(
                level.get("price") or level.get("rate") or level.get("bidPrice") or level.get("askPrice")
            )
            quantity = _paper_only_valr_float_or_none(
                level.get("quantity") or level.get("qty") or level.get("volume") or level.get("size")
            )
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price = _paper_only_valr_float_or_none(level[0])
            quantity = _paper_only_valr_float_or_none(level[1])
        else:
            continue
        if price is not None and quantity is not None and price > 0.0 and quantity > 0.0:
            normalized.append([price, quantity])
    normalized.sort(key=lambda item: item[0], reverse=bool(reverse))
    return normalized[: max(1, int(max_levels))]


def paper_only_valr_normalize_symbol(symbol):
    """Normalize supported VALR spot symbols into the venue's compact form."""

    text = str(symbol or "").strip().upper()
    if not text:
        return None
    for separator in ("/", "-", "_", " "):
        text = text.replace(separator, "")
    return text if text in _VALR_PAPER_SUPPORTED_SYMBOLS else None


def paper_only_valr_market_catalog(symbols=None):
    """Return a read-only paper market catalog for VALR spot coverage."""

    if symbols is None:
        requested_symbols = tuple(_VALR_PAPER_SUPPORTED_SYMBOLS)
    elif isinstance(symbols, (list, tuple, set, frozenset)):
        requested_symbols = tuple(symbols)
    else:
        requested_symbols = (symbols,)

    catalog = []
    seen = set()
    for symbol in requested_symbols:
        venue_symbol = paper_only_valr_normalize_symbol(symbol)
        if not venue_symbol or venue_symbol in seen:
            continue
        seen.add(venue_symbol)
        spec = _VALR_PAPER_SUPPORTED_SYMBOLS[venue_symbol]
        display_symbol = f"{spec['base']}/{spec['quote']}"
        base_path = f"{_VALR_PAPER_PUBLIC_BASE_URL}/v1/public/{venue_symbol}"
        catalog.append(
            {
                "venue": "VALR",
                "market": f"VALR:{display_symbol}",
                "symbol": display_symbol,
                "venue_symbol": venue_symbol,
                "instrument_id": f"VALR:{venue_symbol}",
                "base_asset": spec["base"],
                "quote_asset": spec["quote"],
                "market_type": "spot",
                "route_id": "valr_spot_public",
                "instrument_metadata": {
                    "venue": "VALR",
                    "venue_symbol": venue_symbol,
                    "base_asset": spec["base"],
                    "quote_asset": spec["quote"],
                    "market_type": "spot",
                    "public_read_only": True,
                },
                "paper_only": True,
                "build_governor_fields": _paper_only_build_governor_fields(),
                "frontier_tags": ["frontier_crypto_venue_map", "zar_fiat_quote", "public_read_only"],
                "endpoints": {
                    "ticker": f"{base_path}/marketsummary",
                    "top_of_book": f"{base_path}/orderbook",
                    "recent_trades": f"{base_path}/trades?limit=50",
                },
            }
        )
    return catalog


def _paper_only_depth_liquidity_score(
    *,
    best_bid=None,
    best_ask=None,
    bid_size=None,
    ask_size=None,
    spread_bps=None,
    intended_paper_notional_usd=None,
    route_quality=None,
):
    visible_depth_candidates = []
    bid_price = _paper_only_valr_float_or_none(best_bid)
    ask_price = _paper_only_valr_float_or_none(best_ask)
    bid_quantity = _paper_only_valr_float_or_none(bid_size)
    ask_quantity = _paper_only_valr_float_or_none(ask_size)

    if bid_price is not None and bid_quantity is not None and bid_price > 0.0 and bid_quantity > 0.0:
        visible_depth_candidates.append(bid_price * bid_quantity)
    if ask_price is not None and ask_quantity is not None and ask_price > 0.0 and ask_quantity > 0.0:
        visible_depth_candidates.append(ask_price * ask_quantity)

    visible_top_of_book_notional = min(visible_depth_candidates) if visible_depth_candidates else None
    target_notional = _paper_only_valr_float_or_none(intended_paper_notional_usd)
    if target_notional is not None:
        target_notional = abs(target_notional)
        if target_notional == 0.0:
            target_notional = None

    if visible_top_of_book_notional is None:
        return None

    if target_notional is not None:
        score = min(1.0, visible_top_of_book_notional / target_notional)
    else:
        score = min(1.0, visible_top_of_book_notional / 5000.0)

    spread_value = _paper_only_valr_float_or_none(spread_bps)
    if spread_value is not None and spread_value > 0.0:
        score *= max(0.0, min(1.0, 40.0 / max(40.0, spread_value)))

    if isinstance(route_quality, dict):
        quote_age_ms = _paper_only_valr_float_or_none(route_quality.get("quote_age_ms"))
        if quote_age_ms is not None and quote_age_ms > 0.0:
            score *= max(0.0, min(1.0, 15000.0 / max(15000.0, quote_age_ms)))
        if route_quality.get("paper_ineligible"):
            score *= 0.5

    return round(max(0.0, min(1.0, score)), 6)


def paper_only_mercado_bitcoin_normalize_symbol(symbol):
    """Normalize supported Mercado Bitcoin spot symbols into compact BRL form."""

    text = str(symbol or "").strip().upper()
    if not text:
        return None
    for separator in ("/", "-", "_", " "):
        text = text.replace(separator, "")
    if text in _MERCADO_BITCOIN_PAPER_SUPPORTED_SYMBOLS:
        return text
    if text in {"BTC", "ETH"}:
        return f"{text}BRL"
    return None


def paper_only_mercado_bitcoin_market_catalog(symbols=None):
    """Return a read-only paper market catalog for Mercado Bitcoin spot coverage."""

    if symbols is None:
        requested_symbols = tuple(_MERCADO_BITCOIN_PAPER_SUPPORTED_SYMBOLS)
    elif isinstance(symbols, (list, tuple, set, frozenset)):
        requested_symbols = tuple(symbols)
    else:
        requested_symbols = (symbols,)

    catalog = []
    seen = set()
    for symbol in requested_symbols:
        venue_symbol = paper_only_mercado_bitcoin_normalize_symbol(symbol)
        if not venue_symbol or venue_symbol in seen:
            continue
        seen.add(venue_symbol)
        spec = _MERCADO_BITCOIN_PAPER_SUPPORTED_SYMBOLS[venue_symbol]
        display_symbol = f"{spec['base']}/{spec['quote']}"
        base_path = f"{_MERCADO_BITCOIN_PAPER_PUBLIC_BASE_URL}/{spec['api_symbol']}"
        catalog.append(
            {
                "venue": "MERCADO_BITCOIN",
                "market": f"MERCADO_BITCOIN:{display_symbol}",
                "symbol": display_symbol,
                "venue_symbol": venue_symbol,
                "base_asset": spec["base"],
                "quote_asset": spec["quote"],
                "paper_only": True,
                "build_governor_fields": _paper_only_build_governor_fields(),
                "frontier_tags": ["frontier_crypto_venue_map", "latam_fiat_quote", "brl_fiat_quote", "public_read_only"],
                "endpoints": {
                    "ticker": f"{base_path}/ticker/",
                    "top_of_book": f"{base_path}/orderbook/",
                },
            }
        )
    return catalog


def paper_only_mercado_bitcoin_observation_from_public_payloads(
    symbol,
    *,
    ticker_payload=None,
    orderbook_payload=None,
    quote_timestamp=None,
    evaluation_timestamp=None,
    route_status=None,
    intended_paper_notional_usd=None,
    venue_spread_baseline_bps=None,
    route_quality_config=None,
    as_of=None,
):
    """Build a paper-only normalized spot observation from Mercado Bitcoin payloads."""

    venue_symbol = paper_only_mercado_bitcoin_normalize_symbol(symbol)
    if not venue_symbol:
        return None

    spec = _MERCADO_BITCOIN_PAPER_SUPPORTED_SYMBOLS[venue_symbol]
    display_symbol = f"{spec['base']}/{spec['quote']}"
    raw_ticker = ticker_payload if isinstance(ticker_payload, dict) else {}
    ticker = raw_ticker.get("ticker") if isinstance(raw_ticker.get("ticker"), dict) else raw_ticker
    orderbook = orderbook_payload if isinstance(orderbook_payload, dict) else {}

    best_bid = _paper_only_valr_float_or_none(
        _paper_only_valr_pick_first(ticker, "buy", "bid", "bestBid", "best_bid")
    )
    best_ask = _paper_only_valr_float_or_none(
        _paper_only_valr_pick_first(ticker, "sell", "ask", "bestAsk", "best_ask")
    )

    bid_book, bid_size = _paper_only_valr_order_book_top(orderbook.get("bids") or orderbook.get("Bids"), reverse=True)
    ask_book, ask_size = _paper_only_valr_order_book_top(orderbook.get("asks") or orderbook.get("Asks"), reverse=False)

    if best_bid is None:
        best_bid = bid_book
    if best_ask is None:
        best_ask = ask_book

    last_price = _paper_only_valr_float_or_none(_paper_only_valr_pick_first(ticker, "last", "lastPrice", "price"))
    base_volume = _paper_only_valr_float_or_none(_paper_only_valr_pick_first(ticker, "vol", "volume", "baseVolume"))
    quote_volume = _paper_only_valr_float_or_none(
        _paper_only_valr_pick_first(ticker, "quoteVolume", "quote_volume", "volumeQuote")
    )
    if quote_volume is None and last_price is not None and base_volume is not None:
        quote_volume = last_price * base_volume

    mid_price = None
    spread_bps = None
    if best_bid is not None and best_ask is not None and best_bid > 0.0 and best_ask > 0.0:
        mid_price = (best_bid + best_ask) / 2.0
        if mid_price > 0.0 and best_ask >= best_bid:
            spread_bps = ((best_ask - best_bid) / mid_price) * 10_000.0

    observed_at = _paper_only_parse_timestamp(
        quote_timestamp or _paper_only_valr_pick_first(ticker, "date", "timestamp", "time") or as_of
    )
    evaluation_at = _paper_only_parse_timestamp(evaluation_timestamp or as_of)
    route_review = _paper_only_short_route_review(route_status, config=route_quality_config)
    if route_review.get("route_status") is not None:
        route_status = route_review["route_status"]

    route_quality = None
    if callable(paper_only_route_quality_record):
        route_quality = paper_only_route_quality_record(
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            mid_price=mid_price,
            spread_bps=spread_bps,
            observed_at=observed_at,
            as_of=evaluation_at,
            intended_paper_notional_usd=intended_paper_notional_usd,
            venue_spread_baseline_bps=venue_spread_baseline_bps,
            route_status=route_status,
            config=route_quality_config,
        )
    if isinstance(route_quality, dict) or route_review.get("short_support_source") is not None or route_review.get(
        "borrow_support_flag"
    ) is not None or route_review.get("margin_mode_flag") is not None or route_review.get("block_reason") is not None or route_review.get("route_blocked"):
        route_quality = dict(route_quality or {})
        route_quality["route_status"] = route_status
        route_quality["short_support_source"] = route_review.get("short_support_source")
        route_quality["borrow_support_flag"] = route_review.get("borrow_support_flag")
        route_quality["margin_mode_flag"] = route_review.get("margin_mode_flag")
        route_quality["block_reason"] = route_review.get("block_reason")
        if route_review.get("route_blocked"):
            route_quality["paper_ineligible"] = True
            route_quality["blocking_reason"] = route_review.get("block_reason") or route_quality.get("blocking_reason")
            route_quality["simulated_slippage_tier"] = "blocked"

    paper_ineligible = bool(route_quality.get("paper_ineligible")) if isinstance(route_quality, dict) else bool(
        route_review.get("route_blocked")
    )
    paper_ineligible_reason = route_quality.get("blocking_reason") if isinstance(route_quality, dict) else None
    if paper_ineligible_reason is None:
        paper_ineligible_reason = route_review.get("block_reason")
    simulated_slippage_tier = route_quality.get("simulated_slippage_tier") if isinstance(route_quality, dict) else None
    if simulated_slippage_tier is None and route_review.get("route_blocked"):
        simulated_slippage_tier = "blocked"

    depth_liquidity_score = _paper_only_depth_liquidity_score(
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size=bid_size,
        ask_size=ask_size,
        spread_bps=spread_bps,
        intended_paper_notional_usd=intended_paper_notional_usd,
        route_quality=route_quality,
    )

    observation = {
        "venue": "MERCADO_BITCOIN",
        "market": f"MERCADO_BITCOIN:{display_symbol}",
        "symbol": display_symbol,
        "venue_symbol": venue_symbol,
        "instrument_id": f"MERCADO_BITCOIN:{venue_symbol}",
        "base_asset": spec["base"],
        "quote_asset": spec["quote"],
        "instrument_metadata": {
            "venue": "MERCADO_BITCOIN",
            "venue_symbol": venue_symbol,
            "base_asset": spec["base"],
            "quote_asset": spec["quote"],
            "market_type": "spot",
            "public_read_only": True,
        },
        "paper_only": True,
        "observation_type": "spot",
        "last_price": last_price,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
        "spread_bps": spread_bps,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "base_volume_24h": base_volume,
        "quote_volume_24h": quote_volume,
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "freshness_timestamp": observed_at.isoformat() if observed_at is not None else None,
        "route_status": route_status,
        "short_support_source": route_review.get("short_support_source"),
        "borrow_support_flag": route_review.get("borrow_support_flag"),
        "margin_mode_flag": route_review.get("margin_mode_flag"),
        "block_reason": paper_ineligible_reason,
        "route_quality": route_quality,
        "depth_liquidity_score": depth_liquidity_score,
        "liquidity_score": depth_liquidity_score,
        "venue_quality": {
            "route_status": route_status,
            "paper_ineligible": paper_ineligible,
            "blocking_reason": paper_ineligible_reason,
            "simulated_slippage_tier": simulated_slippage_tier,
            "quote_age_ms": route_quality.get("quote_age_ms") if isinstance(route_quality, dict) else None,
            "depth_to_size_ratio": route_quality.get("depth_to_size_ratio") if isinstance(route_quality, dict) else None,
            "short_support_source": route_review.get("short_support_source"),
            "borrow_support_flag": route_review.get("borrow_support_flag"),
            "margin_mode_flag": route_review.get("margin_mode_flag"),
            "block_reason": paper_ineligible_reason,
            "spread_to_baseline_ratio": route_quality.get("spread_to_baseline_ratio") if isinstance(route_quality, dict) else None,
        },
        "paper_ineligible": paper_ineligible,
        "paper_ineligible_reason": paper_ineligible_reason,
        "simulated_slippage_tier": simulated_slippage_tier,
    }
    observation.update(native_spot_surface_fields(observation))
    return observation


def paper_only_valr_observation_from_public_payloads(
    symbol,
    *,
    ticker_payload=None,
    orderbook_payload=None,
    trades_payload=None,
    quote_timestamp=None,
    evaluation_timestamp=None,
    route_status=None,
    intended_paper_notional_usd=None,
    venue_spread_baseline_bps=None,
    route_quality_config=None,
    as_of=None,
):
    """Build a paper-only normalized spot observation from public VALR payloads."""

    venue_symbol = paper_only_valr_normalize_symbol(symbol)
    if not venue_symbol:
        return None

    spec = _VALR_PAPER_SUPPORTED_SYMBOLS[venue_symbol]
    display_symbol = f"{spec['base']}/{spec['quote']}"
    ticker = ticker_payload if isinstance(ticker_payload, dict) else {}
    orderbook = orderbook_payload if isinstance(orderbook_payload, dict) else {}

    trade_items = []
    if isinstance(trades_payload, list):
        trade_items = list(trades_payload)
    elif isinstance(trades_payload, dict):
        nested_trades = trades_payload.get("trades") or trades_payload.get("data") or trades_payload.get("items")
        if isinstance(nested_trades, list):
            trade_items = list(nested_trades)

    best_bid = _paper_only_valr_float_or_none(
        _paper_only_valr_pick_first(ticker, "bidPrice", "bestBidPrice", "bid", "bid_price")
    )
    best_ask = _paper_only_valr_float_or_none(
        _paper_only_valr_pick_first(ticker, "askPrice", "bestAskPrice", "ask", "ask_price")
    )

    bid_book, bid_size = _paper_only_valr_order_book_top(orderbook.get("bids") or orderbook.get("Bids"), reverse=True)
    ask_book, ask_size = _paper_only_valr_order_book_top(orderbook.get("asks") or orderbook.get("Asks"), reverse=False)
    shallow_order_book = {
        "bids": _paper_only_valr_shallow_order_book(orderbook.get("bids") or orderbook.get("Bids"), reverse=True),
        "asks": _paper_only_valr_shallow_order_book(orderbook.get("asks") or orderbook.get("Asks"), reverse=False),
    }

    if best_bid is None:
        best_bid = bid_book
    if best_ask is None:
        best_ask = ask_book

    last_price = _paper_only_valr_float_or_none(
        _paper_only_valr_pick_first(ticker, "lastTradedPrice", "lastPrice", "price", "last_trade_price")
    )
    quote_volume = _paper_only_valr_float_or_none(
        _paper_only_valr_pick_first(ticker, "quoteVolume", "quote_volume", "volumeQuote", "rolling24HourQuoteVolume")
    )
    base_volume = _paper_only_valr_float_or_none(
        _paper_only_valr_pick_first(ticker, "baseVolume", "base_volume", "volumeBase", "rolling24HourVolume")
    )

    recent_trade = next((item for item in trade_items if isinstance(item, dict)), None)
    recent_trade_price = _paper_only_valr_float_or_none(
        _paper_only_valr_pick_first(recent_trade or {}, "price", "tradedPrice", "lastTradedPrice")
    )
    recent_trade_quantity = _paper_only_valr_float_or_none(
        _paper_only_valr_pick_first(recent_trade or {}, "quantity", "qty", "volume", "size")
    )
    recent_trade_timestamp = _paper_only_parse_timestamp(
        _paper_only_valr_pick_first(recent_trade or {}, "tradedAt", "timestamp", "createdAt", "time")
    )

    if last_price is None:
        last_price = recent_trade_price

    mid_price = None
    spread_bps = None
    if best_bid is not None and best_ask is not None and best_bid > 0.0 and best_ask > 0.0:
        mid_price = (best_bid + best_ask) / 2.0
        if mid_price > 0.0 and best_ask >= best_bid:
            spread_bps = ((best_ask - best_bid) / mid_price) * 10_000.0

    ticker_trade_timestamp = _paper_only_parse_timestamp(
        _paper_only_valr_pick_first(
            ticker,
            "lastTradedTimestamp",
            "lastTradedAt",
            "lastTradeTimestamp",
            "lastTradeAt",
            "timestamp",
        )
    )
    observed_at = (
        _paper_only_parse_timestamp(quote_timestamp or as_of)
        or ticker_trade_timestamp
        or recent_trade_timestamp
    )
    evaluation_at = _paper_only_parse_timestamp(evaluation_timestamp or as_of)

    route_quality = None
    if callable(paper_only_route_quality_record):
        route_quality = paper_only_route_quality_record(
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            mid_price=mid_price,
            spread_bps=spread_bps,
            observed_at=observed_at,
            as_of=evaluation_at,
            intended_paper_notional_usd=intended_paper_notional_usd,
            venue_spread_baseline_bps=venue_spread_baseline_bps,
            route_status=route_status,
            config=route_quality_config,
        )
    paper_ineligible = bool(route_quality.get("paper_ineligible")) if isinstance(route_quality, dict) else False
    paper_ineligible_reason = route_quality.get("blocking_reason") if isinstance(route_quality, dict) else None
    simulated_slippage_tier = route_quality.get("simulated_slippage_tier") if isinstance(route_quality, dict) else None

    observation = {
        "venue": "VALR",
        "market": f"VALR:{display_symbol}",
        "symbol": display_symbol,
        "venue_symbol": venue_symbol,
        "instrument_id": f"VALR:{venue_symbol}",
        "base_asset": spec["base"],
        "quote_asset": spec["quote"],
        "market_type": "spot",
        "route_id": "valr_spot_public",
        "instrument_metadata": {
            "venue": "VALR",
            "venue_symbol": venue_symbol,
            "base_asset": spec["base"],
            "quote_asset": spec["quote"],
            "market_type": "spot",
            "public_read_only": True,
        },
        "paper_only": True,
        "observation_type": "spot",
        "last_price": last_price,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
        "spread_bps": spread_bps,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "base_volume_24h": base_volume,
        "quote_volume_24h": quote_volume,
        "recent_trade_count": len(trade_items),
        "recent_trade_price": recent_trade_price,
        "recent_trade_quantity": recent_trade_quantity,
        "recent_trade_timestamp": recent_trade_timestamp.isoformat() if recent_trade_timestamp is not None else None,
        "last_trade_timestamp": (
            ticker_trade_timestamp or recent_trade_timestamp
        ).isoformat() if (ticker_trade_timestamp or recent_trade_timestamp) is not None else None,
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "shallow_order_book": shallow_order_book,
        "route_quality": route_quality,
        "paper_ineligible": paper_ineligible,
        "paper_ineligible_reason": paper_ineligible_reason,
        "simulated_slippage_tier": simulated_slippage_tier,
    }
    observation.update(native_spot_surface_fields(observation))
    return observation


def paper_only_entry_confirmation_gate(
    *,
    entry_confidence=None,
    trend_confirmation=None,
    liquidity_confirmation=None,
    entry_confidence_min=0.72,
    require_trend_confirmation=True,
    require_liquidity_confirmation=True,
):
    """Paper-only entry confirmation gate.

    This is diagnostic-only and fail-closed: missing or invalid inputs reject
    the signal for paper selection.
    """

    try:
        confidence_value = float(entry_confidence)
    except (TypeError, ValueError):
        return {"eligible": False, "reason": "incomplete_confirmation_inputs"}

    trend_ok = bool(trend_confirmation)
    liquidity_ok = bool(liquidity_confirmation)

    if confidence_value < float(entry_confidence_min):
        return {"eligible": False, "reason": "confidence_below_threshold"}
    if require_trend_confirmation and not trend_ok:
        return {"eligible": False, "reason": "trend_confirmation_missing"}
    if require_liquidity_confirmation and not liquidity_ok:
        return {"eligible": False, "reason": "liquidity_confirmation_missing"}

    return {
        "eligible": True,
        "reason": "eligible",
        "entry_confidence": confidence_value,
        "entry_confidence_min": float(entry_confidence_min),
        "trend_confirmation_required": bool(require_trend_confirmation),
        "liquidity_confirmation_required": bool(require_liquidity_confirmation),
        "trend_confirmation": trend_ok,
        "liquidity_confirmation": liquidity_ok,
    }


def paper_only_strategy_decay_guard(
    *,
    strategy_family,
    long_expectancy_recent=None,
    short_expectancy_recent=None,
    sample_size_recent=None,
    min_sample_size=20,
    negative_margin=0.0,
    recovery_expectancy=0.0,
):
    """Paper-only decay diagnostic for strategy selection metadata.

    Returns a small dict suitable for LLM packet/report enrichment. Missing or
    non-numeric inputs fail closed into a neutral state.
    """

    family = str(strategy_family or "").strip().lower()
    if not family:
        return {"strategy_decay_state": "unknown", "recovery_gate": False}

    try:
        long_value = float(long_expectancy_recent)
        short_value = float(short_expectancy_recent)
        sample_value = int(sample_size_recent)
    except (TypeError, ValueError):
        return {
            "strategy_decay_state": "unknown",
            "recovery_gate": False,
            "strategy_family": family,
        }

    decay_block = (
        sample_value >= int(min_sample_size)
        and long_value <= -abs(float(negative_margin))
        and short_value <= -abs(float(negative_margin))
    )
    recovery_gate = (
        sample_value >= int(min_sample_size)
        and (long_value >= float(recovery_expectancy) or short_value >= float(recovery_expectancy))
    )
    failed_legs = []
    if long_value <= -abs(float(negative_margin)):
        failed_legs.append("long")
    if short_value <= -abs(float(negative_margin)):
        failed_legs.append("short")
    rolling_expectancy_recent = round((long_value + short_value) / 2.0, 6)

    return {
        "strategy_family": family,
        "sample_size_recent": sample_value,
        "long_expectancy_recent": long_value,
        "short_expectancy_recent": short_value,
        "strategy_decay_state": "blocked_for_paper_selection" if decay_block else "active",
        "guard_reason": (
            "bilateral_negative_expectancy"
            if decay_block
            else "recovery_threshold_met"
            if recovery_gate
            else "bilateral_decay_not_confirmed"
        ),
        "decay_score": round(max(0.0, -long_value) + max(0.0, -short_value), 6),
        "rolling_expectancy_recent": rolling_expectancy_recent,
        "failed_legs": failed_legs,
        "bilateral_failure": len(failed_legs) == 2 and sample_value >= int(min_sample_size),
        "recovery_gate": bool(recovery_gate),
    }


def paper_only_liquidity_volatility_entry_gate(
    *,
    spread_bps=None,
    realized_volatility_zscore=None,
    volume_ratio=None,
    max_spread_bps=35.0,
    max_volatility_zscore=2.0,
    min_volume_ratio=1.25,
):
    """Paper-only entry gate for market quality.

    Fails closed when required inputs are missing or non-numeric.
    """

    try:
        spread_bps_value = float(spread_bps)
    except (TypeError, ValueError):
        spread_bps_value = None

    try:
        volatility_zscore_value = float(realized_volatility_zscore)
    except (TypeError, ValueError):
        volatility_zscore_value = None

    try:
        volume_ratio_value = float(volume_ratio)
    except (TypeError, ValueError):
        volume_ratio_value = None

    if spread_bps_value is None or volatility_zscore_value is None or volume_ratio_value is None:
        return {"eligible": False, "reason": "incomplete_market_quality_inputs"}
    if spread_bps_value > float(max_spread_bps):
        return {"eligible": False, "reason": "spread_above_threshold"}
    if volatility_zscore_value > float(max_volatility_zscore):
        return {"eligible": False, "reason": "volatility_above_threshold"}
    if volume_ratio_value < float(min_volume_ratio):
        return {"eligible": False, "reason": "volume_below_threshold"}

    return {
        "eligible": True,
        "reason": "eligible",
        "spread_bps": spread_bps_value,
        "realized_volatility_zscore": volatility_zscore_value,
        "volume_ratio": volume_ratio_value,
        "max_spread_bps": float(max_spread_bps),
        "max_volatility_zscore": float(max_volatility_zscore),
        "min_volume_ratio": float(min_volume_ratio),
    }


def paper_only_crypto_conditional_spot_short_feasibility_gate(
    route_context=None,
    *,
    enabled=None,
    required_lifecycle="open_hold_cover",
):
    """Paper-only feasibility gate for conditional crypto spot shorts."""

    context = route_context if isinstance(route_context, dict) else {}

    def _lower_text(value):
        return str(value or "").strip().lower()

    def _truthy(value):
        if isinstance(value, bool):
            return value
        text = _lower_text(value)
        if text in {"1", "true", "yes", "y", "on", "supported", "available"}:
            return True
        if text in {"0", "false", "no", "n", "off", "unsupported", "unavailable"}:
            return False
        return bool(value)

    def _numeric_present(value):
        try:
            return float(value) == float(value)
        except (TypeError, ValueError):
            return False

    def _lifecycle_supported(value):
        target = _lower_text(required_lifecycle)
        if isinstance(value, str):
            return _lower_text(value) == target
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(_lower_text(item) == target for item in value)
        if isinstance(value, dict):
            if target in value:
                return _truthy(value.get(target))
            return any(_lower_text(item) == target for item in value.values())
        return False

    asset_class = _lower_text(context.get("asset_class") or context.get("instrument_type"))
    market_type = _lower_text(context.get("market_type") or context.get("observation_type"))
    side = _lower_text(context.get("side") or context.get("direction"))
    trigger_type = _lower_text(context.get("trigger_type") or context.get("entry_trigger_type"))
    paper_only = _truthy(context.get("paper_only"))

    applies = bool(
        paper_only
        and asset_class == "crypto"
        and market_type == "spot"
        and side == "short"
        and trigger_type == "conditional"
    )

    if not applies:
        return {
            "eligible": True,
            "reason": "not_applicable",
            "applies": False,
            "feasible": True,
            "asset_class": asset_class,
            "market_type": market_type,
            "side": side,
            "trigger_type": trigger_type,
        }

    venue_shorting_supported = _truthy(context.get("venue_shorting_supported"))
    instrument_margin_shortable = _truthy(context.get("instrument_margin_shortable"))
    borrow_model_available = _truthy(context.get("borrow_model_available"))
    lifecycle_supported = _lifecycle_supported(
        context.get("route_lifecycle")
        or context.get("lifecycle")
        or context.get("supported_lifecycles")
        or context.get("route_lifecycle_state")
    )
    fee_assumptions_present = any(
        _numeric_present(context.get(key))
        for key in ("estimated_fee_bps", "estimated_fees_bps", "taker_fee_bps", "maker_fee_bps", "estimated_fee_rate")
    )
    borrow_assumptions_present = borrow_model_available or any(
        _numeric_present(context.get(key))
        for key in ("estimated_borrow_bps", "estimated_borrow_fee_bps", "assumed_borrow_bps", "borrow_rate", "borrow_apr")
    )

    blockers = []
    if not venue_shorting_supported:
        blockers.append("venue_shorting_unsupported")
    if not (instrument_margin_shortable or borrow_model_available):
        blockers.append("short_mechanism_unavailable")
    if not lifecycle_supported:
        blockers.append("route_lifecycle_missing_open_hold_cover")
    if not fee_assumptions_present:
        blockers.append("fee_assumptions_missing")
    if not borrow_assumptions_present:
        blockers.append("borrow_assumptions_missing")

    return {
        "eligible": not blockers,
        "reason": "paper_short_route_infeasible" if blockers else "paper_short_route_feasible",
        "applies": True,
        "feasible": not blockers,
        "blockers": blockers,
        "enabled": enabled,
        "asset_class": asset_class,
        "market_type": market_type,
        "side": side,
        "trigger_type": trigger_type,
    }


def paper_only_volatility_liquidity_entry_gate(
    *,
    spread_bps,
    realized_volatility_zscore,
    recent_volume_ratio,
    max_spread_bps=12.0,
    max_volatility_zscore=2.0,
    min_volume_ratio=1.1,
):
    """Paper-only entry gate for execution-quality filtering.

    Rejects new paper entries when the spread is wide, volatility is elevated,
    or recent volume is thin. This is diagnostic-only and does not affect live
    routing or broker behavior.
    """

    def _to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    spread_value = _to_float(spread_bps)
    volatility_value = _to_float(realized_volatility_zscore)
    volume_ratio_value = _to_float(recent_volume_ratio)

    meets_spread = spread_value is not None and spread_value <= float(max_spread_bps)
    meets_volatility = volatility_value is not None and volatility_value <= float(max_volatility_zscore)
    meets_volume = volume_ratio_value is not None and volume_ratio_value >= float(min_volume_ratio)

    eligible = bool(meets_spread and meets_volatility and meets_volume)

    if spread_value is None or volatility_value is None or volume_ratio_value is None:
        reason = "input_incomplete"
    elif not meets_spread:
        reason = "spread_above_threshold"
    elif not meets_volatility:
        reason = "volatility_above_threshold"
    elif not meets_volume:
        reason = "volume_below_baseline"
    else:
        reason = "eligible"

    return {
        "eligible": eligible,
        "reason": reason,
        "spread_bps": spread_value,
        "realized_volatility_zscore": volatility_value,
        "recent_volume_ratio": volume_ratio_value,
        "max_spread_bps": float(max_spread_bps),
        "max_volatility_zscore": float(max_volatility_zscore),
        "min_volume_ratio": float(min_volume_ratio),
    }


def _paper_only_route_profile_value(profile, *keys):
    if not isinstance(profile, dict):
        return None

    for key in keys:
        if isinstance(key, (list, tuple)):
            value = profile
            for part in key:
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(part)
        else:
            value = profile.get(key)
        if value not in (None, "", [], {}, ()):
            return value
    return None


def _paper_only_route_truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {
        "1",
        "true",
        "t",
        "yes",
        "y",
        "on",
        "ok",
        "available",
        "supported",
        "enabled",
        "present",
        "modeled",
        "modelled",
    }


def _paper_only_route_has_assumption(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return _paper_only_route_truthy(value)
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return bool(value)
    return value is not None


def _paper_only_route_supports_open_hold_cover(value):
    if isinstance(value, dict):
        if all(_paper_only_route_truthy(value.get(part)) for part in ("open", "hold", "cover")):
            return True
        nested = _paper_only_route_profile_value(value, "mode", "lifecycle", "state", "name")
        if nested is not None:
            return _paper_only_route_supports_open_hold_cover(nested)
        return _paper_only_route_truthy(value.get("open_hold_cover"))

    if isinstance(value, (list, tuple, set, frozenset)):
        normalized = {
            str(item or "").strip().lower().replace("-", "_").replace(" ", "_")
            for item in value
        }
        if "open_hold_cover" in normalized:
            return True
        return {"open", "hold", "cover"}.issubset(normalized)

    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text in {"open_hold_cover", "open_hold_cover_close", "openholdcover"}


def paper_only_crypto_conditional_spot_short_feasibility_gate(route_profile=None, *, enabled=True):
    """Paper-only feasibility gate for crypto conditional spot shorts."""

    profile = route_profile if isinstance(route_profile, dict) else {}
    gate_flag = _paper_only_route_profile_value(
        profile,
        "paper_conditional_spot_short_feasibility_gate",
        ("paper_policy", "conditional_spot_short_feasibility_gate"),
    )
    if gate_flag is not None:
        enabled = _paper_only_route_truthy(gate_flag)

    asset_class = str(_paper_only_route_profile_value(profile, "asset_class", ("instrument", "asset_class")) or "").strip().lower()
    market_type = str(_paper_only_route_profile_value(profile, "market_type", ("instrument", "market_type")) or "").strip().lower()
    side = str(_paper_only_route_profile_value(profile, "side", "direction") or "").strip().lower()
    trigger_type = str(_paper_only_route_profile_value(profile, "trigger_type", ("trigger", "type")) or "").strip().lower()
    applies = bool(enabled and asset_class == "crypto" and market_type == "spot" and side == "short" and trigger_type == "conditional")

    if not applies:
        return {
            "enabled": bool(enabled),
            "applies": False,
            "eligible": True,
            "reason": "disabled" if not enabled else "not_applicable",
            "asset_class": asset_class,
            "market_type": market_type,
            "side": side,
            "trigger_type": trigger_type,
        }

    venue_shorting_supported = _paper_only_route_truthy(
        _paper_only_route_profile_value(profile, "venue_shorting_supported", ("venue", "shorting_supported"))
    )
    margin_shortable = _paper_only_route_truthy(
        _paper_only_route_profile_value(profile, "instrument_margin_shortable", "margin_shortable", ("instrument", "margin_shortable"))
    )
    borrow_model_available = _paper_only_route_truthy(
        _paper_only_route_profile_value(profile, "borrow_model_available", ("borrow", "model_available"))
    )
    lifecycle_supported = _paper_only_route_supports_open_hold_cover(
        _paper_only_route_profile_value(
            profile,
            "route_lifecycle",
            "supported_lifecycle",
            ("route", "lifecycle"),
            ("route", "supported_lifecycle"),
        )
    )
    fee_assumptions_present = _paper_only_route_has_assumption(
        _paper_only_route_profile_value(
            profile,
            "estimated_fee_bps",
            "estimated_fees",
            "fee_model_available",
            ("fees", "estimated_bps"),
            ("fees", "assumptions_present"),
        )
    )
    borrow_assumptions_present = _paper_only_route_has_assumption(
        _paper_only_route_profile_value(
            profile,
            "estimated_borrow_bps",
            "borrow_rate_bps",
            "borrow_assumption_present",
            ("borrow", "estimated_bps"),
            ("borrow", "assumptions_present"),
        )
    )
    instrument_shortable = bool(margin_shortable or borrow_model_available)

    failed_checks = []
    if not venue_shorting_supported:
        failed_checks.append("venue_shorting_supported")
    if not instrument_shortable:
        failed_checks.append("instrument_shortable_or_borrow_model")
    if not lifecycle_supported:
        failed_checks.append("open_hold_cover_lifecycle")
    if not fee_assumptions_present:
        failed_checks.append("estimated_fees_present")
    if not borrow_assumptions_present:
        failed_checks.append("borrow_assumptions_present")

    return {
        "enabled": bool(enabled),
        "applies": True,
        "eligible": not failed_checks,
        "reason": "eligible" if not failed_checks else "paper_short_route_infeasible",
        "asset_class": asset_class,
        "market_type": market_type,
        "side": side,
        "trigger_type": trigger_type,
        "venue_shorting_supported": venue_shorting_supported,
        "instrument_margin_shortable": margin_shortable,
        "borrow_model_available": borrow_model_available,
        "route_lifecycle_supports_open_hold_cover": lifecycle_supported,
        "fee_assumptions_present": fee_assumptions_present,
        "borrow_assumptions_present": borrow_assumptions_present,
        "failed_checks": failed_checks,
    }


def paper_only_frontier_short_route_profile_gate(
    route_profile=None,
    *,
    minimum_fill_quality=0.70,
    maximum_spread_bps=25.0,
    enforce_conditional_spot_short_feasibility=True,
):
    """Paper-only gate for frontier short recommendations.

    The gate fails closed when required route-profile evidence is missing,
    stale, or below threshold. It is diagnostic-only and does not affect live
    routing or broker behavior.
    """

    profile = route_profile if isinstance(route_profile, dict) else {}
    fresh_profile = bool(profile.get("fresh_profile"))
    fill_quality = profile.get("simulated_fill_quality")
    spread_bps = profile.get("simulated_spread_bps")
    borrow_state = profile.get("simulated_borrow_state")

    feasibility = paper_only_crypto_conditional_spot_short_feasibility_gate(
        profile,
        enabled=enforce_conditional_spot_short_feasibility,
    )
    try:
        fill_quality_value = float(fill_quality)
    except (TypeError, ValueError):
        fill_quality_value = None

    try:
        spread_bps_value = float(spread_bps)
    except (TypeError, ValueError):
        spread_bps_value = None

    has_required_profile = fresh_profile and fill_quality_value is not None and spread_bps_value is not None
    meets_fill_quality = fill_quality_value is not None and fill_quality_value >= float(minimum_fill_quality)
    meets_spread = spread_bps_value is not None and spread_bps_value <= float(maximum_spread_bps)
    has_borrow = str(borrow_state).lower() == "available"

    eligible = bool(
        has_required_profile and meets_fill_quality and meets_spread and has_borrow and feasibility["eligible"]
    )

    if feasibility["applies"] and not feasibility["eligible"]:
        reason = feasibility["reason"]
    elif not has_required_profile:
        reason = "route_profile_incomplete"
    elif not fresh_profile:
        reason = "route_profile_stale"
    elif not meets_fill_quality:
        reason = "simulated_fill_quality_below_threshold"
    elif not meets_spread:
        reason = "simulated_spread_bps_above_threshold"
    elif not has_borrow:
        reason = "simulated_borrow_state_unavailable"
    else:
        reason = "eligible"

    return {
        "eligible": eligible,
        "reason": reason,
        "fresh_profile": fresh_profile,
        "simulated_fill_quality": fill_quality_value,
        "simulated_spread_bps": spread_bps_value,
        "simulated_borrow_state": borrow_state,
        "minimum_fill_quality": float(minimum_fill_quality),
        "maximum_spread_bps": float(maximum_spread_bps),
        "conditional_spot_short_feasibility": feasibility,
        "feasibility_gate_applied": bool(feasibility["applies"]),
    }


def paper_only_cross_market_confirmation_gate(
    confirmation_score,
    confirmation_age_seconds=None,
    minimum_score=0.70,
    maximum_age_seconds=120,
    allow_neutral_alignment=False,
):
    """Paper-only confirmation gate for directional cross-market signals.

    This is diagnostic-only and never changes execution routing.
    """

    try:
        score = float(confirmation_score)
    except (TypeError, ValueError):
        score = 0.0

    try:
        age_seconds = None if confirmation_age_seconds is None else float(confirmation_age_seconds)
    except (TypeError, ValueError):
        age_seconds = None

    minimum_score = float(minimum_score)
    maximum_age_seconds = float(maximum_age_seconds)
    neutral_alignment = score == 0.0
    meets_score = score >= minimum_score
    meets_age = age_seconds is None or age_seconds <= maximum_age_seconds
    eligible = bool(meets_score and meets_age and (allow_neutral_alignment or not neutral_alignment))

    if neutral_alignment and not allow_neutral_alignment:
        reason = "neutral_alignment_disallowed"
    elif not meets_score:
        reason = "below_minimum_score"
    elif not meets_age:
        reason = "stale_confirmation"
    else:
        reason = "eligible"

    return {
        "eligible": eligible,
        "reason": reason,
        "score": score,
        "age_seconds": age_seconds,
        "minimum_score": minimum_score,
        "maximum_age_seconds": maximum_age_seconds,
        "allow_neutral_alignment": bool(allow_neutral_alignment),
    }


def paper_only_multi_factor_entry_gate(
    *,
    momentum_ok,
    liquidity_ok,
    spread_ok,
    confirmation_score,
    confirmation_age_seconds=None,
    minimum_confirmation_score=0.70,
    maximum_confirmation_age_seconds=120,
    require_positive_confirmation=True,
):
    """Paper-only entry gate that combines signal quality checks.

    This is diagnostic-only and never changes live execution routing.
    """

    confirmation = paper_only_cross_market_confirmation_gate(
        confirmation_score=confirmation_score,
        confirmation_age_seconds=confirmation_age_seconds,
        minimum_score=minimum_confirmation_score,
        maximum_age_seconds=maximum_confirmation_age_seconds,
        allow_neutral_alignment=not require_positive_confirmation,
    )

    momentum_ok = bool(momentum_ok)
    liquidity_ok = bool(liquidity_ok)
    spread_ok = bool(spread_ok)
    eligible = bool(momentum_ok and liquidity_ok and spread_ok and confirmation["eligible"])

    if not momentum_ok:
        reason = "momentum_rejected"
    elif not liquidity_ok:
        reason = "liquidity_rejected"
    elif not spread_ok:
        reason = "spread_rejected"
    elif not confirmation["eligible"]:
        reason = confirmation["reason"]
    else:
        reason = "eligible"

    return {
        "eligible": eligible,
        "reason": reason,
        "momentum_ok": momentum_ok,
        "liquidity_ok": liquidity_ok,
        "spread_ok": spread_ok,
        "confirmation": confirmation,
    }


def _paper_only_parse_timestamp(value):
    if value in (None, "", [], {}, ()):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                value = float(text)
            except (TypeError, ValueError):
                return None
        else:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if abs(numeric) > 10_000_000_000_000:
        numeric /= 1_000_000.0
    elif abs(numeric) > 10_000_000_000:
        numeric /= 1000.0
    try:
        return dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _paper_only_source_alignment_audit(payload, *, max_source_skew_seconds=2.0):
    result = {
        "eligible": False,
        "reason": "missing_source_timestamps",
        "source_skew_seconds": None,
        "source_alignment_status": "unknown",
        "max_source_skew_seconds": float(max_source_skew_seconds),
    }
    if not isinstance(payload, dict):
        result["reason"] = "invalid_payload"
        return result

    try:
        skew_seconds = float(payload.get("spot_perp_skew_seconds"))
    except (TypeError, ValueError):
        skew_seconds = None

    if skew_seconds is None:
        spot_timestamp = None
        perp_timestamp = None
        for key in ("spot_timestamp", "spot_quote_timestamp", "spot_data_timestamp"):
            spot_timestamp = _paper_only_parse_timestamp(payload.get(key))
            if spot_timestamp is not None:
                break
        for key in ("perp_timestamp", "perp_quote_timestamp", "perp_data_timestamp"):
            perp_timestamp = _paper_only_parse_timestamp(payload.get(key))
            if perp_timestamp is not None:
                break
        if spot_timestamp is not None and perp_timestamp is not None:
            skew_seconds = abs((spot_timestamp - perp_timestamp).total_seconds())
        elif any(key in payload for key in ("spot_timestamp", "spot_quote_timestamp", "spot_data_timestamp")) or any(
            key in payload for key in ("perp_timestamp", "perp_quote_timestamp", "perp_data_timestamp")
        ):
            result["reason"] = "invalid_source_timestamps"
            return result
        else:
            return result

    result["source_skew_seconds"] = skew_seconds
    result["eligible"] = skew_seconds <= float(max_source_skew_seconds)
    result["reason"] = "eligible" if result["eligible"] else "skew_above_threshold"
    result["source_alignment_status"] = "aligned" if result["eligible"] else "misaligned"
    return result


def paper_only_signal_freshness_audit(
    payload,
    *,
    max_age_seconds=300,
    freshness_buckets=(30, 120, 300),
    max_source_skew_seconds=2.0,
    horizon_label=None,
):
    """Paper-only diagnostic helper for stale-data and horizon mismatch checks.

    Returns audit fields only; it never changes routing, position sizing, or
    capital allocation.
    """

    if not isinstance(payload, dict):
        return {
            "eligible": False,
            "reason": "invalid_payload",
            "data_age_seconds": None,
            "freshness_bucket": "unknown",
            "horizon_label": horizon_label,
            "horizon_alignment": "unknown",
            "source_skew_seconds": None,
            "source_alignment_status": "unknown",
            "source_alignment_eligible": False,
            "max_source_skew_seconds": float(max_source_skew_seconds),
        }

    try:
        data_age_seconds = float(payload.get("data_age_seconds"))
    except (TypeError, ValueError):
        data_age_seconds = None

    freshness_buckets = tuple(sorted(float(value) for value in freshness_buckets))
    max_age_seconds = float(max_age_seconds)

    if data_age_seconds is None:
        freshness_bucket = "unknown"
        eligible = False
        reason = "missing_data_age"
    else:
        if data_age_seconds <= freshness_buckets[0]:
            freshness_bucket = f"<= {int(freshness_buckets[0])}s"
        elif data_age_seconds <= freshness_buckets[1]:
            freshness_bucket = f"{int(freshness_buckets[0]) + 1}-{int(freshness_buckets[1])}s"
        elif data_age_seconds <= freshness_buckets[2]:
            freshness_bucket = f"{int(freshness_buckets[1]) + 1}-{int(freshness_buckets[2])}s"
        else:
            freshness_bucket = f"> {int(freshness_buckets[2])}s"

        eligible = data_age_seconds <= max_age_seconds
        reason = "eligible" if eligible else "stale_data"

    horizon_seconds = None
    horizon_alignment = "unknown"
    horizon_seconds_value = payload.get("forecast_horizon_seconds", payload.get("holding_period_seconds"))
    try:
        horizon_seconds = None if horizon_seconds_value is None else float(horizon_seconds_value)
    except (TypeError, ValueError):
        horizon_seconds = None
    if horizon_seconds is not None and data_age_seconds is not None:
        horizon_alignment = "aligned" if horizon_seconds >= data_age_seconds else "lagging"

    source_alignment = _paper_only_source_alignment_audit(payload, max_source_skew_seconds=max_source_skew_seconds)

    return {
        "eligible": eligible,
        "reason": reason,
        "data_age_seconds": data_age_seconds,
        "freshness_bucket": freshness_bucket,
        "max_age_seconds": max_age_seconds,
        "horizon_label": horizon_label,
        "forecast_horizon_seconds": horizon_seconds,
        "horizon_alignment": horizon_alignment,
        "source_skew_seconds": source_alignment["source_skew_seconds"],
        "source_alignment_status": source_alignment["source_alignment_status"],
        "source_alignment_eligible": source_alignment["eligible"],
        "max_source_skew_seconds": source_alignment["max_source_skew_seconds"],
    }


def _paper_only_route_review_context(payload):
    if not isinstance(payload, dict):
        return {}

    context = {}
    for field in (
        "symbol",
        "market",
        "route",
        "route_key",
        "buy_venue",
        "sell_venue",
        "venue",
        "side",
        "order_type",
        "signal_id",
        "strategy",
    ):
        value = payload.get(field)
        if isinstance(value, str):
            value = value.strip()
        if value not in (None, "", [], {}, ()):
            context[field] = value
    return context


def paper_only_execution_mode_guard(payload, *, required_mode="paper", allow_route_evaluation=True):
    """Paper-only guard that blocks non-paper dispatch attempts.

    Route evaluation and analytics may still proceed, but any live-capable
    submission path must be considered blocked unless execution_mode resolves
    to the required paper mode.
    """

    if not isinstance(payload, dict):
        return {
            "eligible": False,
            "status": "blocked",
            "reason": "invalid_payload",
            "execution_mode": None,
            "required_execution_mode": str(required_mode or "paper").strip().lower() or "paper",
            "route_evaluation_allowed": bool(allow_route_evaluation),
            "submission_allowed": False,
            "review_context": {},
        }

    normalized_required_mode = str(required_mode or "paper").strip().lower() or "paper"
    explicit_execution_mode = payload.get("execution_mode")
    normalized_execution_mode = str(explicit_execution_mode or "").strip().lower() or None
    review_context = _paper_only_route_review_context(payload)

    if normalized_execution_mode is None and "paper_mode" in payload:
        normalized_execution_mode = "paper" if bool(payload.get("paper_mode")) else "disabled"

    eligible = normalized_execution_mode == normalized_required_mode

    if eligible:
        reason = "eligible"
        status = "eligible"
    elif explicit_execution_mode not in (None, ""):
        reason = "execution_mode_blocked"
        status = "blocked"
    elif "paper_mode" in payload and not bool(payload.get("paper_mode")):
        reason = "paper_mode_disabled"
        status = "blocked"
    else:
        reason = "execution_mode_missing"
        status = "blocked"

    return {
        "eligible": eligible,
        "status": status,
        "reason": reason,
        "execution_mode": normalized_execution_mode,
        "required_execution_mode": normalized_required_mode,
        "route_evaluation_allowed": bool(allow_route_evaluation),
        "submission_allowed": bool(eligible),
        "review_context": review_context,
    }


def paper_only_radar_alert_gate(payload, minimum_confidence=0.68, require_execution_mode_paper=True):
    """Paper-only alert gate for radar recommendation payloads.

    Rejects incomplete payloads and suppresses alerts unless paper_mode is true
    and confidence meets the configured minimum.
    """

    if not isinstance(payload, dict):
        return {"eligible": False, "reason": "invalid_payload", "missing_required_fields": ["payload"]}

    execution_guard = paper_only_execution_mode_guard(payload) if require_execution_mode_paper else None
    if execution_guard is not None and not execution_guard["eligible"]:
        return {
            "eligible": False,
            "status": execution_guard["status"],
            "reason": execution_guard["reason"],
            "execution_guard": execution_guard,
            "paper_mode": False,
        }

    required_fields = ("confidence",)
    missing_required_fields = [field for field in required_fields if field not in payload]
    if missing_required_fields:
        return {
            "eligible": False,
            "reason": "missing_required_fields",
            "missing_required_fields": missing_required_fields,
        }

    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        return {"eligible": False, "reason": "invalid_confidence"}

    minimum_confidence = float(minimum_confidence)
    if confidence < minimum_confidence:
        return {
            "eligible": False,
            "reason": "below_minimum_confidence",
            "confidence": confidence,
            "minimum_confidence": minimum_confidence,
        }

    return {
        "eligible": True,
        "reason": "eligible",
        "confidence": confidence,
        "minimum_confidence": minimum_confidence,
        "paper_mode": True if execution_guard is not None else bool(payload.get("paper_mode", False)),
        "execution_guard": execution_guard,
    }


def paper_only_route_toxicity_key(buy_venue, sell_venue):
    buy = (buy_venue or "").strip().upper()
    sell = (sell_venue or "").strip().upper()
    if not buy or not sell:
        return None
    return f"{buy}->{sell}"


class RouteToxicityRegistry:
    """Paper-only directional route quality tracker.

    Tracks realized route outcomes for paper analytics only. It is intentionally
    conservative: routes only become suppressed after enough samples and
    recover automatically when recent EWMA edge improves.
    """

    def __init__(
        self,
        min_samples=25,
        ewma_alpha=0.18,
        toxic_cutoff_bps=-12.0,
        recovery_cutoff_bps=-3.0,
        cooldown_hours=12.0,
        stale_quote_penalty_bps=4.0,
    ):
        self.min_samples = int(min_samples)
        self.ewma_alpha = float(ewma_alpha)
        self.toxic_cutoff_bps = float(toxic_cutoff_bps)
        self.recovery_cutoff_bps = float(recovery_cutoff_bps)
        self.cooldown_hours = float(cooldown_hours)
        self.stale_quote_penalty_bps = float(stale_quote_penalty_bps)
        self._routes = {}

    def _state(self, key):
        return self._routes.setdefault(
            key,
            {
                "samples": 0,
                "ewma_net_bps": 0.0,
                "win_count": 0,
                "toxic": False,
            },
        )

    def score_route(self, buy_venue, sell_venue):
        key = paper_only_route_toxicity_key(buy_venue, sell_venue)
        if key is None:
            return {"eligible": True, "reason": "missing_route_key", "route_key": None}
        state = self._routes.get(key)
        if not state:
            return {"eligible": True, "reason": "unseen_route", "route_key": key}
        eligible = not state.get("toxic", False)
        return {
            "eligible": eligible,
            "reason": "toxic_route" if not eligible else "eligible",
            "route_key": key,
            "samples": state.get("samples", 0),
            "ewma_net_bps": state.get("ewma_net_bps", 0.0),
            "win_rate": (state.get("win_count", 0) / state["samples"]) if state.get("samples", 0) else 0.0,
        }

    def update_from_fill(self, buy_venue, sell_venue, realized_net_bps, stale_quote=False):
        key = paper_only_route_toxicity_key(buy_venue, sell_venue)
        if key is None:
            return None
        state = self._state(key)
        net_bps = float(realized_net_bps)
        if stale_quote:
            net_bps -= self.stale_quote_penalty_bps
        state["samples"] += 1
        state["win_count"] += int(net_bps > 0.0)
        if state["samples"] == 1:
            state["ewma_net_bps"] = net_bps
        else:
            a = self.ewma_alpha
            state["ewma_net_bps"] = (a * net_bps) + ((1.0 - a) * state["ewma_net_bps"])
        if state["samples"] >= self.min_samples and state["ewma_net_bps"] <= self.toxic_cutoff_bps:
            state["toxic"] = True
        elif state["toxic"] and state["ewma_net_bps"] >= self.recovery_cutoff_bps:
            state["toxic"] = False
        return dict(state, route_key=key)


_LATAM_FIAT_QUOTE_ALIASES = {
    "CLP": "CLP",
    "CLP$": "CLP",
    "$CLP": "CLP",
    "XCLP": "CLP",
    "MXN": "MXN",
    "MXN$": "MXN",
    "$MXN": "MXN",
    "MX$": "MXN",
    "MEX$": "MXN",
}
_LATAM_FIAT_QUOTE_REGIONS = {
    "CLP": "cl",
    "MXN": "mx",
}
_LATAM_FIAT_VENUE_REGIONS = {
    "BUDA": "cl",
    "BITSO": "mx",
}


def _normalize_latam_fiat_quote(quote_asset, venue_name=None, venue_notes=None):
    raw_quote = (quote_asset or "").strip().upper()
    venue = (venue_name or "").strip().upper()
    _ = (venue_notes or "").strip().upper()
    compact_quote = raw_quote.replace(" ", "").replace(".", "")
    normalized_quote = (
        _LATAM_FIAT_QUOTE_ALIASES.get(raw_quote)
        or _LATAM_FIAT_QUOTE_ALIASES.get(compact_quote)
        or _LATAM_FIAT_QUOTE_ALIASES.get(compact_quote.replace("$", ""))
        or raw_quote
    )
    quote_region = _LATAM_FIAT_QUOTE_REGIONS.get(normalized_quote)
    venue_region = _LATAM_FIAT_VENUE_REGIONS.get(venue)
    return {
        "raw_quote": raw_quote,
        "normalized_quote": normalized_quote,
        "alias_applied": bool(raw_quote and normalized_quote != raw_quote),
        "quote_region": quote_region,
        "venue_region": venue_region,
        "venue_quote_region_match": bool(quote_region and venue_region and quote_region == venue_region),
    }


def classify_fiat_corridor(base_asset, quote_asset, venue_name=None, venue_notes=None):
    """
    Classify a public paper-only market into a broad liquidity corridor.

    This is metadata only: no routing, no execution, no trade decisions.
    """
    regional_quote = _normalize_latam_fiat_quote(quote_asset, venue_name=venue_name, venue_notes=venue_notes)
    base = (base_asset or "").strip().upper()
    quote = regional_quote["normalized_quote"]
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
        if quote == "IDR" and "INDODAX" in venue:
            corridor_confidence = 0.8
        elif regional_quote["quote_region"]:
            corridor_type = "latam_local_fiat"
            corridor_confidence = 0.78
            if regional_quote["venue_quote_region_match"]:
                corridor_confidence = 0.82
            if regional_quote["alias_applied"]:
                corridor_confidence = min(0.99, corridor_confidence + 0.01)

    if any(token in venue for token in ("BINANCE", "COINBASE", "KRAKEN", "GATE", "KUCOIN", "MEXC", "BITGET")):
        corridor_confidence = min(0.99, corridor_confidence + 0.03)
    if "LOCAL" in notes or "FIAT" in notes:
        corridor_confidence = min(0.99, corridor_confidence + 0.02)

    return {
        "corridor_base": base,
        "corridor_quote": quote,
        "corridor_type": corridor_type,
        "corridor_confidence": round(corridor_confidence, 3),
        "corridor_region": regional_quote["quote_region"],
        "regional_quote_raw": regional_quote["raw_quote"],
        "regional_quote_alias_applied": regional_quote["alias_applied"],
    }


def _paper_only_is_truthy(value):
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "y", "on", "enabled"}


def _paper_only_route_token(value):
    return str(value or "").strip().upper().replace("-", "_").replace("/", "_").replace(" ", "_")


def _paper_only_route_value_missing(value):
    normalized = str(value or "").strip().lower()
    return normalized in {"", "unknown", "unspecified", "n/a", "na", "none", "null"}


def _paper_only_route_liquidity_gate(
    *,
    spread_bps=None,
    top_of_book_notional_usd=None,
    max_spread_bps=12,
    min_top_of_book_notional_usd=25000,
):
    """
    Paper-only pre-execution gate for route quality.

    Returns a small review packet that can be consumed by paper simulations and
    route-ranking reports without touching live execution.
    """
    try:
        spread_bps_value = float(spread_bps) if spread_bps is not None else None
    except (TypeError, ValueError):
        spread_bps_value = None
    try:
        depth_value = float(top_of_book_notional_usd) if top_of_book_notional_usd is not None else None
    except (TypeError, ValueError):
        depth_value = None

    spread_limit = float(max_spread_bps or 0)
    depth_limit = float(min_top_of_book_notional_usd or 0)

    spread_pass = spread_bps_value is not None and spread_bps_value <= spread_limit
    depth_pass = depth_value is not None and depth_value >= depth_limit
    passed = bool(spread_pass and depth_pass)

    reasons = []
    if spread_bps_value is None:
        reasons.append("missing_spread")
    elif not spread_pass:
        reasons.append("wide_spread")
    if depth_value is None:
        reasons.append("missing_depth")
    elif not depth_pass:
        reasons.append("shallow_book")

    return {
        "paper_only": True,
        "passed": passed,
        "spread_bps": spread_bps_value,
        "top_of_book_notional_usd": depth_value,
        "max_spread_bps": spread_limit,
        "min_top_of_book_notional_usd": depth_limit,
        "reasons": reasons,
    }


def _paper_only_route_instrument_family(value):
    normalized = _paper_only_route_token(value)
    if any(token in normalized for token in ("PERP", "PERPETUAL", "SWAP", "FUTURE", "FUTURES")):
        return "perp"
    if "SPOT" in normalized:
        return "spot"
    return normalized.lower() or None


def _paper_only_route_side_family(value):
    normalized = _paper_only_route_token(value)
    if normalized in {"BUY", "LONG", "BID"}:
        return "long"
    if normalized in {"SELL", "SHORT", "ASK"}:
        return "short"
    return None


def _paper_only_route_direction_pattern(recommendation=None):
    recommendation = recommendation or {}
    joined = " ".join(
        _paper_only_route_token(recommendation.get(key))
        for key in (
            "signal_direction",
            "signal",
            "strategy",
            "route_pattern",
            "market_key",
            "title",
            "condition",
            "condition_type",
        )
        if recommendation.get(key) not in (None, "")
    )
    if "LONG_PERP_SHORT_SPOT" in joined:
        return "long_perp_short_spot"
    if "SHORT_PERP_LONG_SPOT" in joined:
        return "short_perp_long_spot"
    return None


def route_feasible_for_paper_conditional_short(recommendation=None):
    recommendation = dict(recommendation or {})
    market_key = _paper_only_route_token(recommendation.get("market_key"))
    venue_name = _paper_only_route_token(recommendation.get("venue"))
    asset_class = _paper_only_route_token(recommendation.get("asset_class"))
    signal_text = " ".join(
        _paper_only_route_token(recommendation.get(key))
        for key in (
            "signal",
            "signal_direction",
            "strategy",
            "title",
            "condition",
            "condition_type",
        )
        if recommendation.get(key) not in (None, "")
    )
    is_conditional = bool(
        _paper_only_is_truthy(recommendation.get("conditional"))
        or _paper_only_route_token(recommendation.get("recommendation_type")) == "CONDITIONAL"
        or _paper_only_route_token(recommendation.get("signal_context")) == "CONDITIONAL"
        or "CONDITIONAL" in market_key
        or "CONDITIONAL" in signal_text
    )
    is_crypto = bool(
        asset_class == "CRYPTO"
        or "CRYPTO" in market_key
        or any(
            marker in " ".join((market_key, venue_name, signal_text))
            for marker in (
                "OKX",
                "BINANCE",
                "BYBIT",
                "BITGET",
                "MEXC",
                "KUCOIN",
                "KRAKEN",
                "COINBASE",
                "GATE",
                "INDODAX",
            )
        )
    )
    direction_pattern = _paper_only_route_direction_pattern(recommendation)
    short_context = bool(
        _paper_only_route_side_family(recommendation.get("route_primary_side")) == "short"
        or _paper_only_route_side_family(recommendation.get("signal_direction")) == "short"
        or direction_pattern == "short_perp_long_spot"
        or "SHORT" in signal_text
    )
    applicable = bool(is_conditional and is_crypto and short_context)

    required_route_fields = (
        "route_primary_venue",
        "route_primary_symbol",
        "route_primary_instrument_type",
        "route_primary_side",
        "route_hedge_venue",
        "route_hedge_symbol",
        "route_hedge_instrument_type",
        "route_hedge_side",
        "route_inventory_mode",
    )
    missing_route_fields = [
        field for field in required_route_fields if _paper_only_route_value_missing(recommendation.get(field))
    ]
    route_metadata_complete = not missing_route_fields
    if recommendation.get("route_metadata_complete") not in (None, ""):
        route_metadata_complete = route_metadata_complete and _paper_only_is_truthy(
            recommendation.get("route_metadata_complete")
        )

    gating = {
        "is_shortable": _paper_only_is_truthy(recommendation.get("is_shortable")),
        "supports_conditional_orders": _paper_only_is_truthy(recommendation.get("supports_conditional_orders")),
        "simulated_margin_ok": _paper_only_is_truthy(recommendation.get("simulated_margin_ok")),
        "simulated_borrow_available": _paper_only_is_truthy(recommendation.get("simulated_borrow_available")),
        "supports_paper_trading": _paper_only_is_truthy(recommendation.get("supports_paper_trading")),
        "route_metadata_complete": route_metadata_complete,
    }
    failed_reasons = []
    if applicable and not gating["route_metadata_complete"]:
        failed_reasons.append("route_metadata_incomplete_or_inconsistent")
    if applicable and not gating["is_shortable"]:
        failed_reasons.append("shorting_unavailable_or_unknown")
    if applicable and not gating["supports_conditional_orders"]:
        failed_reasons.append("conditional_orders_unsupported_or_unknown")
    if applicable and not gating["simulated_margin_ok"]:
        failed_reasons.append("margin_unavailable_or_unknown")
    if applicable and not gating["simulated_borrow_available"]:
        failed_reasons.append("borrow_unavailable_or_unknown")
    if applicable and not gating["supports_paper_trading"]:
        failed_reasons.append("paper_trading_unsupported_or_unknown")

    feasible = not failed_reasons if applicable else True
    asset_context = _paper_only_extract_okx_asset_context(recommendation)
    return {
        "applicable": applicable,
        "destination_valid": feasible,
        "paper_only_warning": failed_reasons[0] if failed_reasons else None,
        "failed_reasons": failed_reasons,
        **asset_context,
    }


_PAPER_ONLY_OKX_QUOTE_ASSET_SUFFIXES = (
    "USDT",
    "USDC",
    "FDUSD",
    "DAI",
    "USD",
    "EUR",
    "BTC",
    "ETH",
    "TRY",
    "BRL",
    "GBP",
    "AUD",
    "JPY",
    "MXN",
    "CLP",
    "IDR",
)


def _paper_only_okx_asset_context_status(value, source_field):
    normalized = _paper_only_route_token(value)
    segments = [segment for segment in normalized.replace("/", "-").split("-") if segment]
    has_instrument_suffix = len(segments) >= 3 or normalized.endswith(("_SWAP", "_PERP", "_SPOT"))
    if "INSTRUMENT_ID" in _paper_only_route_token(source_field) or has_instrument_suffix:
        return "parsed_from_instrument_id"
    return "parsed_from_symbol"


def _paper_only_okx_assets_from_value(value):
    normalized = str(value or "").strip().upper()
    if _paper_only_route_value_missing(normalized):
        return None

    tokenized = normalized.replace("/", "-").replace("_", "-")
    segments = [segment for segment in tokenized.split("-") if segment]
    if len(segments) >= 2:
        base_asset = segments[0]
        quote_asset = segments[1]
        if base_asset.isalnum() and quote_asset.isalnum():
            return {"base_asset": base_asset, "quote_asset": quote_asset, "parsed_value": normalized}

    compact = "".join(ch for ch in normalized if ch.isalnum())
    for quote_asset in _PAPER_ONLY_OKX_QUOTE_ASSET_SUFFIXES:
        if compact.endswith(quote_asset) and len(compact) > len(quote_asset):
            base_asset = compact[: -len(quote_asset)]
            if base_asset.isalnum() and 2 <= len(base_asset) <= 12:
                return {"base_asset": base_asset, "quote_asset": quote_asset, "parsed_value": normalized}
    return None


def _paper_only_extract_okx_asset_context(recommendation=None):
    recommendation = recommendation or {}
    venue_tokens = " ".join(
        _paper_only_route_token(recommendation.get(key))
        for key in ("venue", "route_primary_venue", "route_hedge_venue", "market_key", "strategy", "title")
        if recommendation.get(key) not in (None, "")
    )
    if "OKX" not in venue_tokens:
        return {
            "base_asset": "unknown",
            "quote_asset": "unknown",
            "asset_context_source": None,
            "asset_context_status": "not_okx_market",
        }

    for source_field in (
        "route_primary_symbol",
        "route_primary_instrument_id",
        "route_hedge_symbol",
        "route_hedge_instrument_id",
        "instrument_id",
        "symbol",
        "instId",
    ):
        parsed = _paper_only_okx_assets_from_value(recommendation.get(source_field))
        if parsed:
            return {
                "base_asset": parsed["base_asset"],
                "quote_asset": parsed["quote_asset"],
                "asset_context_source": source_field,
                "asset_context_status": _paper_only_okx_asset_context_status(parsed["parsed_value"], source_field),
            }
    return {
        "base_asset": "unknown",
        "quote_asset": "unknown",
        "asset_context_source": None,
        "asset_context_status": "unknown",
    }


def _paper_only_is_okx_basis_market(recommendation=None):
    recommendation = recommendation or {}
    joined = " ".join(
        _paper_only_route_token(recommendation.get(key))
        for key in ("market_key", "venue", "exchange", "strategy", "variant", "signal", "title")
        if recommendation.get(key) not in (None, "")
    )
    return "OKX" in joined and "BASIS" in joined


def _paper_only_okx_parse_status_valid(parse_status):
    return str(parse_status or "").strip().lower() in {"parsed_delimited", "parsed_compact_suffix"}


def _paper_only_parse_okx_instrument_id(instrument_id=None):
    raw = str(instrument_id or "").strip().upper()
    if not raw:
        return {
            "base_asset": None,
            "quote_asset": None,
            "normalized_instrument_id": None,
            "instrument_family": None,
            "parse_status": "missing_instrument_id",
        }

    normalized = raw.replace("/", "-").replace("_", "-").replace(":", "-")
    parts = [part for part in normalized.split("-") if part]
    derivative_suffixes = {
        "SWAP",
        "PERP",
        "PERPETUAL",
        "FUTURE",
        "FUTURES",
        "THISWEEK",
        "NEXTWEEK",
        "QUARTER",
        "BIQUARTER",
    }
    if len(parts) >= 2 and parts[1] not in derivative_suffixes and parts[0] != parts[1]:
        base_asset = parts[0]
        quote_asset = parts[1]
        return {
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "normalized_instrument_id": f"{base_asset}-{quote_asset}",
            "instrument_family": _paper_only_route_instrument_family(raw),
            "parse_status": "parsed_delimited",
        }

    compact_candidate = parts[0] if parts else normalized
    for quote_asset in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if compact_candidate.endswith(quote_asset) and len(compact_candidate) > len(quote_asset):
            base_asset = compact_candidate[:-len(quote_asset)]
            if base_asset and base_asset != quote_asset:
                return {
                    "base_asset": base_asset,
                    "quote_asset": quote_asset,
                    "normalized_instrument_id": f"{base_asset}-{quote_asset}",
                    "instrument_family": _paper_only_route_instrument_family(raw),
                    "parse_status": "parsed_compact_suffix",
                }

    return {
        "base_asset": None,
        "quote_asset": None,
        "normalized_instrument_id": normalized,
        "instrument_family": _paper_only_route_instrument_family(raw),
        "parse_status": "unparsed_instrument_id",
    }


def _paper_only_enrich_okx_basis_context(recommendation=None):
    recommendation = dict(recommendation or {})
    if not _paper_only_is_okx_basis_market(recommendation):
        return recommendation

    instrument_source = (
        recommendation.get("instrument_id")
        or recommendation.get("symbol")
        or recommendation.get("route_primary_symbol")
        or recommendation.get("route_hedge_symbol")
    )
    parsed = _paper_only_parse_okx_instrument_id(instrument_source)
    recommendation["parse_status"] = parsed.get("parse_status")
    recommendation["instrument_parse_status"] = parsed.get("parse_status")
    recommendation["normalized_instrument_id"] = parsed.get("normalized_instrument_id")
    if _paper_only_route_value_missing(recommendation.get("instrument_id")) and instrument_source:
        recommendation["instrument_id"] = instrument_source
    if _paper_only_route_value_missing(recommendation.get("base_asset")) and parsed.get("base_asset"):
        recommendation["base_asset"] = parsed.get("base_asset")
    if _paper_only_route_value_missing(recommendation.get("quote_asset")) and parsed.get("quote_asset"):
        recommendation["quote_asset"] = parsed.get("quote_asset")
    if _paper_only_route_value_missing(recommendation.get("instrument_family")) and parsed.get("instrument_family"):
        recommendation["instrument_family"] = parsed.get("instrument_family")
    return recommendation


def _paper_only_okx_basis_variant_match(recommendation=None):
    recommendation = dict(recommendation or {})
    blocked_variants = {
        "basis_mean_reversion_long_perp",
        "basis_mean_reversion_short_perp",
    }
    for key in (
        "signal_key",
        "market_key",
        "route_id",
        "route_registry_id",
        "paper_context_key",
    ):
        raw = str(recommendation.get(key) or "").strip()
        if not raw:
            continue
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) < 3:
            continue
        if parts[0].upper() != "OKX" or parts[1] != "perp_funding_basis":
            continue
        direction = parts[2]
        return {
            "matched": True,
            "blocked_variant": direction if direction in blocked_variants else None,
            "source": key,
        }

    joined = " ".join(
        _paper_only_route_token(recommendation.get(key))
        for key in ("strategy_variant", "variant", "direction", "strategy", "signal", "title")
        if recommendation.get(key) not in (None, "")
    )
    blocked_variant = None
    if "BASIS_MEAN_REVERSION_LONG_PERP" in joined:
        blocked_variant = "basis_mean_reversion_long_perp"
    elif "BASIS_MEAN_REVERSION_SHORT_PERP" in joined:
        blocked_variant = "basis_mean_reversion_short_perp"
    return {
        "matched": bool(blocked_variant),
        "blocked_variant": blocked_variant,
        "source": "descriptive_fields" if blocked_variant else None,
    }


def _paper_only_apply_okx_basis_variant_gate(recommendation=None):
    recommendation = _paper_only_enrich_okx_basis_context(recommendation)
    if not _paper_only_is_okx_basis_market(recommendation):
        return recommendation

    quarantine_reason = "decayed_basis_mean_reversion_quarantine"
    variant_match = _paper_only_okx_basis_variant_match(recommendation)
    blocked_variant = variant_match.get("blocked_variant")

    parse_status = recommendation.get("parse_status")
    variant_blocked = bool(blocked_variant and _paper_only_okx_parse_status_valid(parse_status))
    if (
        _paper_only_route_value_missing(recommendation.get("base_asset"))
        or _paper_only_route_value_missing(recommendation.get("quote_asset"))
        or not _paper_only_okx_parse_status_valid(parse_status)
    ):
        recommendation["route_confidence"] = 0.0
        recommendation["route_rejected"] = True
        recommendation["route_rejection_reason"] = "missing_or_unparsed_okx_basis_asset_context"
        recommendation["paper_only_variant_gate"] = {
            "paper_only": True,
            "market_key": recommendation.get("market_key"),
            "variant": blocked_variant or recommendation.get("variant"),
            "parse_status": parse_status,
            "blocked": True,
            "reason": "missing_or_unparsed_okx_basis_asset_context",
        }
        return recommendation

    recommendation["paper_only_variant_gate"] = {
        "paper_only": True,
        "market_key": recommendation.get("market_key"),
        "variant": blocked_variant or recommendation.get("variant"),
        "match_source": variant_match.get("source"),
        "parse_status": parse_status,
        "blocked": variant_blocked,
        "shadow_only": variant_blocked,
        "reason": quarantine_reason if variant_blocked else "allowed_okx_basis_variant",
    }
    if variant_blocked:
        recommendation["route_shadow_only"] = True
        recommendation["paper_only_shadow_reason"] = quarantine_reason
        recommendation["route_rejected"] = False
        recommendation.pop("route_rejection_reason", None)
        if recommendation.get("route_status") in (None, "", "eligible"):
            recommendation["route_status"] = "paper_shadow_only"
    return recommendation

def _paper_only_conditional_crypto_route_review(recommendation=None):
    recommendation = dict(recommendation or {})
    market_key = _paper_only_route_token(recommendation.get("market_key"))
    venue_name = _paper_only_route_token(recommendation.get("venue"))
    asset_class = _paper_only_route_token(recommendation.get("asset_class"))
    signal_text = " ".join(
        _paper_only_route_token(recommendation.get(key))
        for key in ("signal", "signal_direction", "strategy", "title")
        if recommendation.get(key) not in (None, "")
    )
    is_conditional = bool(
        _paper_only_is_truthy(recommendation.get("conditional"))
        or _paper_only_route_token(recommendation.get("recommendation_type")) == "CONDITIONAL"
        or _paper_only_route_token(recommendation.get("signal_context")) == "CONDITIONAL"
        or "CONDITIONAL" in market_key
        or "CONDITIONAL" in signal_text
    )
    is_crypto = bool(
        asset_class == "CRYPTO"
        or "CRYPTO" in market_key
        or any(
            marker in " ".join((market_key, venue_name, signal_text))
            for marker in ("OKX", "BINANCE", "BYBIT", "BITGET", "MEXC", "KUCOIN", "KRAKEN", "COINBASE", "GATE", "INDODAX")
        )
    )
    applicable = bool(is_conditional and is_crypto)
    gate_flag = recommendation.get("paper_route_gate_enabled")
    enabled = applicable if gate_flag is None else _paper_only_is_truthy(gate_flag)
    required_fields = (
        "route_primary_venue",
        "route_primary_symbol",
        "route_primary_instrument_type",
        "route_primary_side",
        "route_hedge_venue",
        "route_hedge_symbol",
        "route_hedge_instrument_type",
        "route_hedge_side",
        "route_inventory_mode",
        "route_confidence",
    )
    if not applicable or not enabled:
        return {
            "applicable": applicable,
            "enabled": enabled,
            "approved": True,
            "required_route_fields": list(required_fields),
            "missing_route_fields": [],
            "inconsistent_route_fields": [],
            "direction_pattern": _paper_only_route_direction_pattern(recommendation),
            "route_confidence": recommendation.get("route_confidence"),
            "requires_zero_confidence": False,
            "paper_only": True,
        }

    missing_fields = []
    for field in required_fields:
        if field == "route_confidence":
            continue
        if _paper_only_route_value_missing(recommendation.get(field)):
            missing_fields.append(field)

    raw_confidence = recommendation.get("route_confidence")
    try:
        route_confidence = float(raw_confidence)
    except (TypeError, ValueError):
        route_confidence = None
        missing_fields.append("route_confidence")

    inconsistent_fields = []
    base_asset = _paper_only_route_token(recommendation.get("base_asset"))
    quote_asset = _paper_only_route_token(recommendation.get("quote_asset"))
    requires_zero_confidence = not base_asset or not quote_asset
    if requires_zero_confidence:
        inconsistent_fields.append("unknown_base_or_quote_asset")
        route_confidence = 0.0
    elif route_confidence is not None and route_confidence <= 0.0:
        inconsistent_fields.append("route_confidence_not_positive")
    elif route_confidence is not None and route_confidence > 1.0:
        inconsistent_fields.append("route_confidence_above_one")

    direction_pattern = _paper_only_route_direction_pattern(recommendation)
    primary_type = _paper_only_route_instrument_family(recommendation.get("route_primary_instrument_type"))
    hedge_type = _paper_only_route_instrument_family(recommendation.get("route_hedge_instrument_type"))
    primary_side = _paper_only_route_side_family(recommendation.get("route_primary_side"))
    hedge_side = _paper_only_route_side_family(recommendation.get("route_hedge_side"))
    if direction_pattern == "long_perp_short_spot":
        if primary_type != "perp":
            inconsistent_fields.append("route_primary_instrument_type")
        if primary_side != "long":
            inconsistent_fields.append("route_primary_side")
        if hedge_type != "spot":
            inconsistent_fields.append("route_hedge_instrument_type")
        if hedge_side != "short":
            inconsistent_fields.append("route_hedge_side")
    elif direction_pattern == "short_perp_long_spot":
        if primary_type != "perp":
            inconsistent_fields.append("route_primary_instrument_type")
        if primary_side != "short":
            inconsistent_fields.append("route_primary_side")
        if hedge_type != "spot":
            inconsistent_fields.append("route_hedge_instrument_type")
        if hedge_side != "long":
            inconsistent_fields.append("route_hedge_side")

    return {
        "applicable": True,
        "enabled": True,
        "approved": not missing_fields and not inconsistent_fields,
        "required_route_fields": list(required_fields),
        "missing_route_fields": missing_fields,
        "inconsistent_route_fields": sorted(set(inconsistent_fields)),
        "direction_pattern": direction_pattern,
        "route_confidence": route_confidence,
        "requires_zero_confidence": requires_zero_confidence,
        "paper_only": True,
    }


def paper_only_validate_recommendation_destination(recommendation=None, execution_destination=None,
                                                   allow_simulation_only=True):
    """
    Mandatory paper-mode destination validation.

    Ensures downstream recommendation packets remain simulation-only and never
    target a live execution destination. Uncertain or missing destinations are
    converted to a no-op with a warning marker.
    """
    recommendation = dict(recommendation or {})
    destination = (execution_destination or recommendation.get("execution_destination") or "").strip().lower()
    route_mode = (recommendation.get("mode") or "paper").strip().lower()
    route_policy = (recommendation.get("execution_gate") or "simulate_only").strip().lower()

    live_markers = ("live", "real", "production", "prod", "broker", "exchange", "order", "submit")
    is_live_destination = any(marker in destination for marker in live_markers)
    is_uncertain = not destination or destination in {"unknown", "unspecified", "n/a", "na", "none"}

    recommendation["mode"] = "paper"
    recommendation["execution_gate"] = "simulate_only"
    recommendation["simulation_only"] = True

    route_review = _paper_only_conditional_crypto_route_review(recommendation)
    if route_review.get("applicable"):
        recommendation["paper_route_review"] = route_review
        if route_review.get("requires_zero_confidence"):
            recommendation["route_confidence"] = 0.0
    if is_live_destination:
        recommendation["action"] = "no_op"
        recommendation["paper_only_warning"] = "rejected_live_execution_destination"
        recommendation["destination_valid"] = False
    elif is_uncertain:
        recommendation["action"] = "no_op"
        recommendation["paper_only_warning"] = "uncertain_destination_defaulted_to_no_op"
        recommendation["destination_valid"] = False
    elif route_review.get("applicable") and route_review.get("enabled") and not route_review.get("approved"):
        recommendation["action"] = "no_op"
        recommendation["paper_only_warning"] = "conditional_route_incomplete_or_inconsistent"
        recommendation["destination_valid"] = False
        recommendation["route_publication_blocked"] = True
        recommendation["simulation_only"] = True
    else:
        recommendation["destination_valid"] = True

    if not allow_simulation_only or route_mode not in {"paper", "simulation", "sim"} or route_policy != "simulate_only":
        recommendation["action"] = "no_op"
        recommendation["paper_only_warning"] = "non_paper_route_normalized"

    return {
        "recommendation": recommendation,
        "destination": destination or None,
        "simulation_only": True,
        "destination_valid": bool(recommendation.get("destination_valid", False)),
        "paper_only_warning": recommendation.get("paper_only_warning"),
        "paper_route_review": recommendation.get("paper_route_review"),
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


def paper_only_adjusted_route_score(raw_edge_bps=None, estimated_slippage_bps=None,
                                    quote_age_ms=None, stale_quote_threshold_ms=750,
                                    top_of_book_notional_usd=None, min_top_of_book_notional_usd=25000,
                                    max_projected_slippage_bps=6):
    """
    Paper-only route scoring helper.

    Returns a score packet that can be used by existing paper runners to prefer
    executable routes over nominal edge.
    """
    raw_edge = float(raw_edge_bps or 0.0)
    slippage = float(estimated_slippage_bps or 0.0)
    quote_age = None if quote_age_ms is None else float(quote_age_ms)
    stale_threshold = float(stale_quote_threshold_ms or 0.0)
    tob_notional = float(top_of_book_notional_usd or 0.0)
    min_tob = float(min_top_of_book_notional_usd or 0.0)
    max_slip = float(max_projected_slippage_bps or 0.0)

    freshness_ok = quote_age is None or quote_age <= stale_threshold
    depth_ok = tob_notional >= min_tob
    slippage_ok = slippage <= max_slip

    stale_penalty = 0.0
    if quote_age is not None and quote_age > stale_threshold:
        stale_penalty = min(3.0, (quote_age - stale_threshold) / max(stale_threshold, 1.0) * 1.5)

    thin_depth_penalty = 0.0
    if tob_notional < min_tob:
        thin_depth_penalty = min(4.0, (min_tob - tob_notional) / max(min_tob, 1.0) * 2.0)

    adjusted_edge_bps = raw_edge - slippage - stale_penalty - thin_depth_penalty
    return {
        "raw_edge_bps": round(raw_edge, 6),
        "estimated_slippage_bps": round(slippage, 6),
        "quote_age_ms": None if quote_age is None else round(quote_age, 6),
        "top_of_book_notional_usd": round(tob_notional, 6),
        "freshness_ok": freshness_ok,
        "depth_ok": depth_ok,
        "slippage_ok": slippage_ok,
        "stale_quote_penalty_bps": round(stale_penalty, 6),
        "thin_depth_penalty_bps": round(thin_depth_penalty, 6),
        "adjusted_edge_bps": round(adjusted_edge_bps, 6),
        "passes_route_guard": bool(freshness_ok and depth_ok and slippage_ok),
        "paper_only": True,
    }


def paper_only_validate_recommendation(recommendation=None, execution_destination=None,
                                       fallback_action="no_op"):
    """
    Mandatory paper-mode recommendation validator.

    Tags all outputs as simulation-only, rejects live destinations, and defaults
    uncertain cases to a no-op.
    """
    recommendation = dict(recommendation or {})
    destination = (execution_destination or recommendation.get("execution_destination") or "").strip().lower()
    live_route_markers = (
        recommendation.get("live_route"),
        recommendation.get("live_execution"),
        recommendation.get("broker_destination"),
        recommendation.get("order_endpoint"),
    )

    has_live_marker = any(bool(marker) for marker in live_route_markers)
    destination_is_live = destination in {"live", "production", "prod", "real", "broker", "exchange"}
    uncertain = not recommendation or not recommendation.get("signal") or recommendation.get("confidence") is None
    route_review = _paper_only_conditional_crypto_route_review(recommendation)
    route_rejected = bool(
        route_review.get("applicable")
        and route_review.get("enabled")
        and not route_review.get("approved")
    )

    rejected = bool(has_live_marker or destination_is_live or route_rejected)
    approved = not rejected and not uncertain
    action = "simulate_only" if approved else fallback_action

    return {
        "paper_only": True,
        "simulation_only": True,
        "route_publication_blocked": route_rejected,
        "approved": approved,
        "rejected": rejected,
        "action": action,
        "execution_destination": None if rejected else destination or "paper",
        "route_review": route_review,
        "warning": (
            "live destination rejected" if rejected else
            "conditional route incomplete or inconsistent" if route_rejected else
            "uncertain recommendation defaulted to no-op" if uncertain else
            None
        ),
    }


def paper_only_confirmation_gate(primary_signal=None, confirming_signals=None, liquidity_snapshot=None,
                                 min_confirming_markets=1):
    """
    Paper-only confirmation gate for recommendation emission.

    This keeps the system in observation mode unless the primary signal agrees
    with at least one confirming market signal and the instrument passes a
    basic liquidity screen.
    """
    primary_signal = (primary_signal or "").strip().lower()
    confirming_signals = tuple((signal or "").strip().lower() for signal in (confirming_signals or ()))
    liquidity_snapshot = liquidity_snapshot or {}

    spread = liquidity_snapshot.get("spread_bps")
    recent_trades = liquidity_snapshot.get("recent_trade_count")
    spread_ok = spread is None or float(spread) <= 25.0
    activity_ok = recent_trades is None or float(recent_trades) >= 3.0

    confirming_count = sum(1 for signal in confirming_signals if signal and signal == primary_signal)
    confirmed = confirming_count >= int(min_confirming_markets or 1)
    passes_liquidity = bool(spread_ok and activity_ok)

    return {
        "confirmation_required": True,
        "primary_signal": primary_signal or "inconclusive",
        "confirming_signals": [signal for signal in confirming_signals if signal],
        "confirming_count": confirming_count,
        "min_confirming_markets": int(min_confirming_markets or 1),
        "passes_liquidity": passes_liquidity,
        "emit_recommendation": bool(primary_signal and confirmed and passes_liquidity),
        "paper_only": True,
    }


def paper_only_cross_market_review_state(evidence=None, signal_state=None, required_fields=None):
    """
    Paper-only review gate for cross-market recommendations.

    When the evidence package is incomplete, the safe default is observe-only
    with no simulated allocation change.
    """
    evidence = evidence or {}
    required_fields = tuple(required_fields or ("data_quality", "execution_scope", "risk_view", "signal_state"))
    missing_fields = [field for field in required_fields if not str(evidence.get(field, "")).strip()]

    normalized_signal_state = (signal_state or evidence.get("signal_state") or "").strip().lower()
    if missing_fields:
        return {
            "paper_review_state": "observe_only",
            "portfolio_action": "no position change",
            "sizing": "0 simulated allocation change",
            "missing_evidence_fields": missing_fields,
            "signal_state": normalized_signal_state or "inconclusive",
        }

    if normalized_signal_state in {"inconclusive", "uncertain", "observe_only", "defer"}:
        return {
            "paper_review_state": "observe_only",
            "portfolio_action": "no position change",
            "sizing": "0 simulated allocation change",
            "missing_evidence_fields": [],
            "signal_state": normalized_signal_state,
        }

    return {
        "paper_review_state": "review_ok",
        "portfolio_action": "paper candidate eligible",
        "sizing": "paper-sized allocation permitted",
        "missing_evidence_fields": [],
        "signal_state": normalized_signal_state or "validated",
    }


PAPER_RECOMMENDATION_ROUTE_REQUIREMENT_FIELDS = (
    "broker_permissions",
    "borrow_availability",
    "fees",
    "margin",
    "api_coverage",
)


def _paper_recommendation_route_requirement_gaps(payload):
    """Return missing route checklist fields without contacting any route API."""

    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    checklist = payload.get("route_requirement_checklist")
    if not isinstance(checklist, dict):
        checklist = evidence.get("route_requirement_checklist")
    if not isinstance(checklist, dict):
        return list(PAPER_RECOMMENDATION_ROUTE_REQUIREMENT_FIELDS)

    return [
        field
        for field in PAPER_RECOMMENDATION_ROUTE_REQUIREMENT_FIELDS
        if checklist.get(field) in (None, "", [], {}, ())
    ]


def validate_paper_recommendation_payload(payload=None, confidence_threshold=0.65):
    """
    Paper-only safety gate for machine-actionable recommendations.

    If the payload, its read-only route requirement checklist, or confidence
    is incomplete, return a complete hold recommendation instead of a
    directional action.  Unknown route values are allowed when explicitly
    represented in the checklist; absent categories are not.
    """
    payload = payload or {}
    required_fields = (
        "action",
        "evidence",
        "market_key",
        "priority",
        "proposed_change",
        "rationale",
        "title",
    )
    missing_fields = [field for field in required_fields if payload.get(field) in (None, "", [], {}, ())]
    missing_route_requirement_fields = _paper_recommendation_route_requirement_gaps(payload)

    raw_confidence = payload.get("confidence", payload.get("confidence_score", 0.0))
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    if missing_fields or missing_route_requirement_fields or confidence < float(confidence_threshold or 0.0):
        return {
            "action": "hold",
            "title": "Insufficient evidence for directional paper recommendation",
            "market_key": payload.get("market_key") or "paper.market_radar.signal_quality",
            "priority": payload.get("priority", 0),
            "evidence": {
                "issue_type": "insufficient_recommendation_evidence",
                "missing_fields": missing_fields,
                "missing_route_requirement_fields": missing_route_requirement_fields,
                "confidence": round(confidence, 3),
                "confidence_threshold": float(confidence_threshold or 0.0),
                "paper_scope": "Paper-trading only; no live orders.",
            },
            "proposed_change": {
                "default_fallback": "hold",
                "objective": "Emit only fully-formed paper recommendations with stronger gating.",
                "rule": "Fallback to hold when confidence, required fields, or route checklist categories are missing.",
            },
            "rationale": "Recommendation was incomplete or below confidence threshold; safe fallback is hold.",
        }

    proxy_freshness_gate = _paper_only_proxy_signal_freshness_gate_from_payload(payload)
    if proxy_freshness_gate and proxy_freshness_gate.get("applicable") and not proxy_freshness_gate.get("eligible", True):
        return {
            "action": "hold",
            "title": "Proxy freshness gate suppressed paper recommendation",
            "market_key": payload.get("market_key") or "paper.market_radar.signal_quality",
            "priority": payload.get("priority", 0),
            "evidence": {
                "issue_type": "proxy_signal_freshness_failed",
                "fail_closed_reason": proxy_freshness_gate.get("fail_closed_reason"),
                "fail_closed_reasons": list(proxy_freshness_gate.get("fail_closed_reasons") or ()),
                "proxy_bar_age_ms": proxy_freshness_gate.get("proxy_bar_age_ms"),
                "source_timestamp_lag_ms": proxy_freshness_gate.get("source_timestamp_lag_ms"),
                "basis_deviation_bps": proxy_freshness_gate.get("basis_deviation_bps"),
                "mapping_confidence": proxy_freshness_gate.get("mapping_confidence"),
                "suppressed_signal_count": int(proxy_freshness_gate.get("suppressed_signal_count", 1) or 1),
                "paper_scope": "Paper-trading only; no live orders.",
            },
            "proposed_change": {
                "default_fallback": "hold",
                "objective": "Fail closed for stale or low-integrity proxy momentum contexts.",
                "rule": "Emit no paper recommendation when proxy freshness, lag, basis, or mapping checks fail.",
            },
            "rationale": "Proxy-backed context failed paper-only freshness validation; safe fallback is hold.",
        }

    return payload


DEFAULT_PAPER_ONLY_INDODAX_DISCOVERY_POLICY = {
    "enabled": True,
    "venue": "INDODAX_SPOT",
    "quote_assets": ("IDR",),
    "max_markets": 25,
    "max_spread_bps_for_health": 120.0,
}


def _indodax_symbol_components(symbol=None):
    normalized = str(symbol or "").strip().upper().replace("-", "_").replace("/", "_")
    if not normalized:
        return None, None
    if "_" in normalized:
        base, quote = normalized.split("_", 1)
        return (base or None), (quote or None)
    for quote_asset in DEFAULT_PAPER_ONLY_INDODAX_DISCOVERY_POLICY["quote_assets"]:
        if normalized.endswith(quote_asset):
            base_asset = normalized[:-len(quote_asset)]
            return (base_asset or None), quote_asset
    return normalized, None


def _indodax_quote_turnover(snapshot=None, base_asset=None, quote_asset=None, last_price=None):
    snapshot = snapshot or {}
    quote_key = f"vol_{str(quote_asset or '').lower()}"
    turnover_quote = _paper_only_float_or_none(snapshot.get(quote_key))
    if turnover_quote is not None and turnover_quote > 0.0:
        return turnover_quote
    base_key = f"vol_{str(base_asset or '').lower()}"
    turnover_base = _paper_only_float_or_none(snapshot.get(base_key))
    if turnover_base is not None and turnover_base > 0.0 and last_price is not None and last_price > 0.0:
        return turnover_base * last_price
    fallback_volume = _paper_only_float_or_none(snapshot.get("vol") or snapshot.get("volume"))
    if fallback_volume is not None and fallback_volume > 0.0 and last_price is not None and last_price > 0.0:
        return fallback_volume * last_price
    return None


def paper_only_indodax_symbol_discovery(payload=None, max_markets=None, allowed_quotes=None):
    """
    Read-only INDODAX spot snapshot normalization for paper-only discovery.

    Accepts a public ticker summary payload and returns normalized best-bid/ask
    snapshots for liquid IDR spot pairs only. No trading or account state.
    """
    policy = DEFAULT_PAPER_ONLY_INDODAX_DISCOVERY_POLICY
    allowed_quotes = tuple(
        str(quote or "").strip().upper()
        for quote in (allowed_quotes or policy["quote_assets"])
        if str(quote or "").strip()
    )
    market_limit = int(max_markets or policy["max_markets"] or 0)
    raw_snapshots = {}
    if isinstance(payload, dict):
        if isinstance(payload.get("tickers"), dict):
            raw_snapshots = payload.get("tickers") or {}
        elif all(isinstance(value, dict) for value in payload.values()):
            raw_snapshots = payload

    observations = []
    for native_symbol, snapshot in raw_snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        base_asset, quote_asset = _indodax_symbol_components(native_symbol)
        if not base_asset or not quote_asset:
            continue
        if allowed_quotes and quote_asset not in allowed_quotes:
            continue
        bid = _paper_only_float_or_none(snapshot.get("buy") or snapshot.get("bid"))
        ask = _paper_only_float_or_none(snapshot.get("sell") or snapshot.get("ask"))
        last = _paper_only_float_or_none(snapshot.get("last") or snapshot.get("close"))
        if last is None or last <= 0.0:
            continue
        if bid is None:
            bid = last
        if ask is None:
            ask = last
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            continue
        mid = (bid + ask) / 2.0
        spread_bps = 0.0 if mid <= 0.0 else ((ask - bid) / mid) * 10000.0
        turnover_quote = _indodax_quote_turnover(snapshot, base_asset, quote_asset, last) or 0.0
        observations.append(
            {
                "venue": policy["venue"],
                "venue_name": "INDODAX",
                "market_type": "spot",
                "symbol": f"{base_asset}_{quote_asset}",
                "native_symbol": str(native_symbol),
                "base_asset": base_asset,
                "quote_asset": quote_asset,
                "best_bid": round(bid, 12),
                "best_ask": round(ask, 12),
                "last_price": round(last, 12),
                "mid_price": round(mid, 12),
                "spread_bps": round(spread_bps, 6),
                "quote_turnover": round(turnover_quote, 6),
                "paper_only": True,
                "read_only": True,
                "discovery_source": "indodax_public_tickers",
            }
        )
    observations.sort(key=lambda item: (-float(item.get("quote_turnover") or 0.0), item.get("symbol") or ""))
    if market_limit > 0:
        observations = observations[:market_limit]
    return observations


def paper_only_indodax_market_health(payload=None, max_markets=None):
    """Compact paper-only INDODAX venue health summary derived from public tickers."""
    policy = DEFAULT_PAPER_ONLY_INDODAX_DISCOVERY_POLICY
    observations = paper_only_indodax_symbol_discovery(payload=payload, max_markets=max_markets)
    max_spread_bps = float(policy["max_spread_bps_for_health"])
    healthy_count = sum(1 for item in observations if float(item.get("spread_bps") or 0.0) <= max_spread_bps)
    return {
        "venue": policy["venue"],
        "market_count": len(observations),
        "healthy_market_count": healthy_count,
        "quotes": sorted({item.get("quote_asset") for item in observations if item.get("quote_asset")}),
        "paper_only": True,
        "read_only": True,
    }

import argparse
import collections
import concurrent.futures
import copy
import datetime as dt
import json
import math
import pathlib
import statistics
import time
import urllib.error
import urllib.request

try:
    from src.regional_fx_reference import get_regional_fx_references
    from src.scan_batch import ScanBatch, normalize_observation
    from src.paper_context_cost import annotate_paper_context_cost
except ImportError:  # pragma: no cover - fallback for direct module execution
    from regional_fx_reference import get_regional_fx_references
    from scan_batch import ScanBatch, normalize_observation
    from paper_context_cost import annotate_paper_context_cost

DEFAULT_PAPER_ONLY_EXECUTABLE_QUALITY_POLICY = {
    "fee_buffer_bps": 4.0,
    "slippage_buffer_bps": 6.0,
    "min_net_edge_bps": 12.0,
    "max_quote_age_ms": 1500.0,
    "max_spread_as_pct_of_edge": 0.35,
    "min_depth_multiple_of_paper_size": 3.0,
}


INTRADAY_CONFIRMATION_FEATURES = frozenset(
    {
        "microstructure_history_ready",
        "return_1m_bps",
        "relative_volume_1m_60m",
    }
)


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

DEFAULT_PAPER_ONLY_PROXY_SIGNAL_FRESHNESS_POLICY = {
    "enabled": True,
    "market_key_tokens": ("YAHOO_PROXY",),
    "max_bar_age_ms": 180000.0,
    "max_source_lag_ms": 90000.0,
    "max_basis_deviation_bps": 35.0,
    "min_mapping_confidence": 0.70,
    "require_monotonic_updates": True,
    "fail_closed_state": "observe_only",
    "max_destination_proxy_age_ms": 90000.0,
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
    "unverified_route_multiplier": 0.15,
    "exact_unsupported_multiplier": 0.15,
    "generic_unsupported_multiplier": 0.15,
    "metadata_missing_behavior": "suppress",
    "metadata_negative_behavior": "suppress",
    "metadata_missing_multiplier": 0.0,
    "metadata_negative_multiplier": 0.0,
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
    "BYBIT": {
        "supports_spot_short": True,
        "requires_margin_permission": True,
        "requires_borrow_check": True,
        "fee_bps_hint": 9.0,
        "margin_mode_hint": "unified_margin",
        "api_route_hint": "spot_margin",
    },
    "BYBIT_SPOT": {
        "supports_spot_short": True,
        "requires_margin_permission": True,
        "requires_borrow_check": True,
        "fee_bps_hint": 9.0,
        "margin_mode_hint": "unified_margin",
        "api_route_hint": "spot_margin",
    },
    "BYBIT_LINEAR": {},
    "BINANCE_US": {
        "supports_spot_short": False,
        "requires_margin_permission": None,
        "requires_borrow_check": None,
        "fee_bps_hint": None,
        "margin_mode_hint": "unsupported",
        "api_route_hint": "unsupported",
    },
    "VALR": {
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


def _paper_only_is_proxy_market_key(market_key: str | None) -> bool:
    """Return True when the market key references a proxy-backed paper context."""

    normalized = str(market_key or "").strip().upper()
    if not normalized:
        return False
    return any(token in normalized for token in DEFAULT_PAPER_ONLY_PROXY_SIGNAL_FRESHNESS_POLICY["market_key_tokens"])


def _paper_only_is_crypto_destination(destination: object) -> bool:
    normalized = str(destination or "").strip().lower().replace("-", "_")
    return any(
        token in normalized
        for token in (
            "crypto",
            "frontier",
            "spot",
            "perp",
            "swap",
            "okx",
            "bitget",
            "binance",
            "valr",
            "gate",
            "bybit",
            "indodax",
            "bitso",
            "mercado_bitcoin",
        )
    )


def _paper_only_float_or_none(value: object) -> float | None:
    try:
        if value in (None, ""):
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _paper_only_timestamp_ms(value: object) -> float | None:
    """Best-effort timestamp normalization for paper-only freshness checks."""

    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
        return normalized.timestamp() * 1000.0
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            numeric = float(stripped)
        except ValueError:
            try:
                parsed = dt.datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                return None
            normalized = parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
            return normalized.timestamp() * 1000.0
    else:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
    if not math.isfinite(numeric):
        return None
    abs_numeric = abs(numeric)
    if abs_numeric > 10_000_000_000_000_000:
        numeric /= 1_000_000.0
    elif abs_numeric > 10_000_000_000_000:
        numeric /= 1000.0
    elif abs_numeric < 10_000_000_000:
        numeric *= 1000.0
    return numeric


def paper_only_yahoo_proxy_crypto_momentum_gate(
    *,
    momentum_contribution: float | None = None,
    source_quote_timestamp: object = None,
    evaluated_at: object = None,
    source_session_status: str | None = None,
    source_session_open: bool | None = None,
    allow_delayed_mode: bool = False,
    destination_proxy_age_seconds: float | None = None,
    max_source_quote_age_seconds: float = 90.0,
    max_destination_proxy_age_seconds: float = 90.0,
    execution_mode: str = "paper",
    destination_surface: str = "perp",
    destination_venue: str = "OKX",
    source_family: str = "yahoo_proxy",
    feature_family: str = "global_proxy_momentum",
    source_signal_key: str | None = None,
    target_route_key: str | None = None,
    proxy_valid_for_reuse: bool | None = None,
    target_surface_paper_evidence: dict | None = None,
    native_proxy_momentum_bps: float | None = None,
    native_proxy_regime_stable: bool | None = None,
    native_proxy_regime_state: str | None = None,
    destination_direction: str | None = None,
    local_short_horizon_trend_bps: float | None = None,
    destination_spread_bps: float | None = None,
    destination_liquidity_score: float | None = None,
) -> dict:
    """Return the quarantined Yahoo momentum contribution for a crypto route.

    This is a paper-policy boundary only.  Live/non-paper inputs are left
    untouched so this diagnostic helper can never make live execution more
    reachable.
    """

    return _paper_only_yahoo_proxy_crypto_freshness_review(
        {
            "execution_mode": execution_mode,
            "source_family": source_family,
            "feature_family": feature_family,
            "source_signal_key": source_signal_key,
            "target_route_key": target_route_key,
            "target_surface": destination_surface,
            "target_venue": destination_venue,
            "source_quote_timestamp": source_quote_timestamp,
            "evaluated_at": evaluated_at,
            "source_session_status": source_session_status,
            "source_session_open": source_session_open,
            "allow_delayed_mode": allow_delayed_mode,
            "destination_proxy_age_seconds": destination_proxy_age_seconds,
            "max_source_quote_age_seconds": max_source_quote_age_seconds,
            "max_destination_proxy_age_seconds": max_destination_proxy_age_seconds,
            "momentum_contribution": momentum_contribution,
            "proxy_valid_for_reuse": proxy_valid_for_reuse,
            "target_surface_paper_evidence": target_surface_paper_evidence,
            "native_proxy_momentum_bps": native_proxy_momentum_bps,
            "native_proxy_regime_stable": native_proxy_regime_stable,
            "native_proxy_regime_state": native_proxy_regime_state,
            "destination_direction": destination_direction,
            "local_short_horizon_trend_bps": local_short_horizon_trend_bps,
            "destination_spread_bps": destination_spread_bps,
            "destination_liquidity_score": destination_liquidity_score,
        },
        now=evaluated_at,
    )


def paper_only_proxy_signal_freshness_gate(
    *,
    market_key: str | None = None,
    latest_bar_timestamp: object = None,
    scheduler_timestamp: object = None,
    source_timestamp: object = None,
    previous_bar_timestamp: object = None,
    basis_deviation_bps: float | None = None,
    mapping_confidence: float | None = None,
    max_bar_age_ms: float | None = None,
    max_source_lag_ms: float | None = None,
    max_basis_deviation_bps: float | None = None,
    min_mapping_confidence: float | None = None,
    enabled: bool | None = None,
    require_monotonic_updates: bool | None = None,
    execution_mode: str = "paper",
    destination_market: object = None,
    source_session_status: str | None = None,
    source_session_open: bool | None = None,
    allow_delayed_mode: bool = False,
    destination_proxy_age_ms: float | None = None,
    max_destination_proxy_age_ms: float | None = None,
    momentum_contribution: float | None = None,
    proxy_valid_for_reuse: bool | None = None,
    target_surface_paper_evidence: dict | None = None,
) -> dict:
    """Fail-closed paper-only freshness gate for proxy-backed signal contexts."""

    policy = DEFAULT_PAPER_ONLY_PROXY_SIGNAL_FRESHNESS_POLICY
    enabled = policy["enabled"] if enabled is None else bool(enabled)
    normalized_mode = str(execution_mode or "paper").strip().lower()
    paper_mode = normalized_mode in {"paper", "paper_only", "simulation", "sim", "review"}
    applicable = enabled and paper_mode and _paper_only_is_proxy_market_key(market_key)
    crypto_destination = _paper_only_is_crypto_destination(destination_market)
    threshold_bar_age_ms = float(
        policy["max_bar_age_ms"] if max_bar_age_ms is None else max_bar_age_ms
    )
    threshold_source_lag_ms = float(
        policy["max_source_lag_ms"] if max_source_lag_ms is None else max_source_lag_ms
    )
    threshold_basis_bps = float(
        policy["max_basis_deviation_bps"] if max_basis_deviation_bps is None else max_basis_deviation_bps
    )
    threshold_mapping_confidence = float(
        policy["min_mapping_confidence"] if min_mapping_confidence is None else min_mapping_confidence
    )
    threshold_destination_proxy_age_ms = float(
        policy["max_destination_proxy_age_ms"]
        if max_destination_proxy_age_ms is None
        else max_destination_proxy_age_ms
    )
    require_monotonic = bool(
        policy["require_monotonic_updates"] if require_monotonic_updates is None else require_monotonic_updates
    )
    scheduler_timestamp_ms = _paper_only_timestamp_ms(scheduler_timestamp)
    if scheduler_timestamp_ms is None:
        scheduler_timestamp_ms = dt.datetime.now(dt.timezone.utc).timestamp() * 1000.0

    latest_bar_timestamp_ms = _paper_only_timestamp_ms(latest_bar_timestamp)
    previous_bar_timestamp_ms = _paper_only_timestamp_ms(previous_bar_timestamp)
    source_timestamp_ms = _paper_only_timestamp_ms(source_timestamp)
    basis_bps = _paper_only_float_or_none(basis_deviation_bps)
    mapping_score = _paper_only_float_or_none(mapping_confidence)

    if not applicable:
        return {
            "enabled": enabled,
            "applicable": False,
            "eligible": True,
            "emit_recommendation": True,
            "fail_closed_reason": None,
            "fail_closed_reasons": [],
            "proxy_bar_age_ms": None,
            "source_timestamp_lag_ms": None,
            "basis_deviation_bps": basis_bps,
            "mapping_confidence": mapping_score,
            "suppressed_signal_count": 0,
            "paper_only": True,
            "execution_mode": normalized_mode,
            "crypto_destination": crypto_destination,
            "gate_reason": None,
            "gate_reasons": [],
            "input_momentum_contribution": _paper_only_float_or_none(momentum_contribution),
            "propagated_momentum_contribution": _paper_only_float_or_none(momentum_contribution),
            "momentum_state": "unchanged",
            "state": "eligible",
        }

    fail_closed_reasons = []
    if proxy_valid_for_reuse is False:
        fail_closed_reasons.append("proxy_invalid_for_reuse")
    proxy_bar_age_ms = None
    if latest_bar_timestamp_ms is None:
        fail_closed_reasons.append("missing_latest_bar_timestamp")
    else:
        proxy_bar_age_ms = max(0.0, scheduler_timestamp_ms - latest_bar_timestamp_ms)
        if proxy_bar_age_ms > threshold_bar_age_ms:
            fail_closed_reasons.append("stale_proxy_bar")

    source_timestamp_lag_ms = None
    if source_timestamp_ms is None:
        fail_closed_reasons.append("missing_source_timestamp")
    else:
        source_timestamp_lag_ms = max(0.0, scheduler_timestamp_ms - source_timestamp_ms)
        if source_timestamp_lag_ms > threshold_source_lag_ms:
            fail_closed_reasons.append("source_timestamp_lag_exceeded")

    monotonic_updates = True
    if require_monotonic and latest_bar_timestamp_ms is not None and previous_bar_timestamp_ms is not None:
        monotonic_updates = latest_bar_timestamp_ms > previous_bar_timestamp_ms
        if not monotonic_updates:
            fail_closed_reasons.append("non_monotonic_proxy_update")

    if basis_bps is None:
        fail_closed_reasons.append("missing_basis_deviation_bps")
    elif abs(basis_bps) > threshold_basis_bps:
        fail_closed_reasons.append("basis_deviation_exceeded")

    if mapping_score is None:
        fail_closed_reasons.append("missing_mapping_confidence")
    elif mapping_score < threshold_mapping_confidence:
        fail_closed_reasons.append("mapping_confidence_too_low")

    session_open = source_session_open
    normalized_session = str(source_session_status or "").strip().lower().replace("-", "_")
    if session_open is None and normalized_session:
        if normalized_session in {"open", "regular", "regular_hours", "trading", "active"}:
            session_open = True
        elif normalized_session in {"closed", "off_session", "after_hours", "halted", "holiday", "weekend"}:
            session_open = False
    destination_age = _paper_only_float_or_none(destination_proxy_age_ms)
    if crypto_destination:
        if not (session_open is True or bool(allow_delayed_mode)):
            fail_closed_reasons.append(
                "source_session_unknown" if session_open is None else "source_session_closed"
            )
        if destination_age is None:
            destination_age = source_timestamp_lag_ms
        if destination_age is None:
            fail_closed_reasons.append("missing_destination_proxy_age")
        elif destination_age > threshold_destination_proxy_age_ms:
            fail_closed_reasons.append("stale_destination_proxy")

    normalized_market_key = str(market_key or "").strip().upper()
    target_packet = {
        "destination_surface": destination_market,
        "target_venue": destination_market,
        "source_family": "yahoo_proxy" if "YAHOO_PROXY" in normalized_market_key else None,
        "signal_family": "global_proxy_momentum" if "MOMENTUM" in normalized_market_key else None,
        "execution_mode": normalized_mode,
        "target_surface_paper_evidence": target_surface_paper_evidence,
    }
    target_review = paper_only_yahoo_proxy_okx_target_review(target_packet)
    target_alignment = paper_only_yahoo_proxy_cross_surface_alignment_guard(target_packet)
    quarantined_cross_surface = bool(
        crypto_destination
        and "YAHOO_PROXY" in normalized_market_key
        and "MOMENTUM" in normalized_market_key
        and target_alignment.get("blocked")
    )
    if quarantined_cross_surface:
        fail_closed_reasons.append("yahoo_proxy_cross_surface_quarantined")

    eligible = not fail_closed_reasons
    raw_contribution = _paper_only_float_or_none(momentum_contribution)
    return {
        "enabled": enabled,
        "applicable": True,
        "market_key": str(market_key or "").strip(),
        "eligible": eligible,
        "emit_recommendation": eligible,
        "fail_closed_reason": fail_closed_reasons[0] if fail_closed_reasons else None,
        "fail_closed_reasons": fail_closed_reasons,
        "gate_reason": fail_closed_reasons[0] if fail_closed_reasons else None,
        "gate_reasons": fail_closed_reasons,
        "proxy_bar_age_ms": round(proxy_bar_age_ms, 3) if proxy_bar_age_ms is not None else None,
        "source_timestamp_lag_ms": round(source_timestamp_lag_ms, 3) if source_timestamp_lag_ms is not None else None,
        "basis_deviation_bps": basis_bps,
        "mapping_confidence": mapping_score,
        "suppressed_signal_count": 0 if eligible else 1,
        "max_bar_age_ms": threshold_bar_age_ms,
        "max_source_lag_ms": threshold_source_lag_ms,
        "max_basis_deviation_bps": threshold_basis_bps,
        "min_mapping_confidence": threshold_mapping_confidence,
        "paper_only": True,
        "execution_mode": normalized_mode,
        "crypto_destination": crypto_destination,
        "quarantined_cross_surface": quarantined_cross_surface,
        "target_surface": target_alignment.get("target_surface") or target_review.get("target_surface"),
        "quarantined_target_surfaces": target_review.get("quarantined_target_surfaces"),
        "allow_native_proxy_monitoring": target_review.get("allow_native_proxy_monitoring"),
        "reenable_condition": target_review.get("reenable_condition"),
        "target_surface_paper_evidence_review": target_alignment.get(
            "target_surface_paper_evidence_review"
        ),
        "source_session_status": normalized_session or None,
        "source_session_open": session_open,
        "delayed_mode_allowed": bool(allow_delayed_mode),
        "destination_proxy_age_ms": destination_age,
        "max_destination_proxy_age_ms": threshold_destination_proxy_age_ms,
        "input_momentum_contribution": raw_contribution,
        "propagated_momentum_contribution": 0.0 if not eligible else raw_contribution,
        "momentum_state": "neutral" if not eligible else "propagated",
        "proxy_valid_for_reuse": eligible if proxy_valid_for_reuse is None else bool(proxy_valid_for_reuse and eligible),
        "require_monotonic_updates": require_monotonic,
        "monotonic_updates": monotonic_updates,
        "latest_bar_timestamp_ms": latest_bar_timestamp_ms,
        "previous_bar_timestamp_ms": previous_bar_timestamp_ms,
        "source_timestamp_ms": source_timestamp_ms,
        "scheduler_timestamp_ms": scheduler_timestamp_ms,
        "state": "eligible" if eligible else policy["fail_closed_state"],
    }


def _paper_only_proxy_signal_freshness_gate_from_payload(payload: dict | None) -> dict | None:
    """Extract standardized proxy freshness telemetry from a paper payload."""

    payload = payload or {}
    if not _paper_only_is_proxy_market_key(payload.get("market_key")):
        return None
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    gate = (
        evidence.get("paper_only_proxy_signal_freshness_gate")
        or evidence.get("proxy_signal_freshness")
        or payload.get("paper_only_proxy_signal_freshness_gate")
    )
    if isinstance(gate, dict):
        normalized = dict(gate)
        if "eligible" not in normalized:
            normalized["eligible"] = bool(normalized.get("emit_recommendation", True))
        if "emit_recommendation" not in normalized:
            normalized["emit_recommendation"] = bool(normalized.get("eligible", True))
        normalized.setdefault("applicable", True)
        normalized.setdefault("suppressed_signal_count", 0 if normalized.get("eligible", True) else 1)
        return normalized
    return None


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

    freshness_limit = float(
        freshness_limit_ms
        or DEFAULT_PAPER_ONLY_CROSS_MARKET_RISK_GATE_POLICY["freshness_limit_ms"]
    )
    mean_reversion = float(
        mean_reversion_bps
        or DEFAULT_PAPER_ONLY_CROSS_MARKET_RISK_GATE_POLICY["mean_reversion_bps"]
    )
    has_required_inputs = all(
        value is not None
        for value in (
            divergence_bps,
            trigger_bps,
            source_a_freshness_ms,
            source_b_freshness_ms,
        )
    )
    if not enabled or not has_required_inputs:
        return {
            "enabled": bool(enabled),
            "applicable": bool(enabled and has_required_inputs),
            "allow_record": False,
            "close_position": False,
            "fresh": None,
            "exceeds_trigger": None,
            "mean_reverted": None,
            "score_multiplier": 1.0,
            "divergence_bps": None if divergence_bps is None else float(divergence_bps),
            "trigger_bps": None if trigger_bps is None else float(trigger_bps),
            "freshness_limit_ms": freshness_limit,
            "reason": "disabled" if not enabled else "insufficient_inputs",
        }

    divergence = float(divergence_bps)
    trigger = float(trigger_bps)
    freshness_a = float(source_a_freshness_ms)
    freshness_b = float(source_b_freshness_ms)
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
        "applicable": True,
        "allow_record": allow_record,
        "close_position": close_position,
        "fresh": fresh,
        "exceeds_trigger": exceeds_trigger,
        "mean_reverted": mean_reverted,
        "score_multiplier": max(0.0, min(1.0, score_multiplier)),
        "divergence_bps": divergence,
        "trigger_bps": trigger,
        "freshness_limit_ms": freshness_limit,
        "reason": "allow_record" if allow_record else "close_position" if close_position else "observe_only",
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
    market_key: str | None = None,
    proxy_signal_freshness_gate: dict | None = None,
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
    proxy_freshness = dict(proxy_signal_freshness_gate or {})
    proxy_eligible = bool(proxy_freshness.get("eligible", True))
    proxy_applicable = bool(proxy_freshness.get("applicable", False))
    if proxy_applicable and not proxy_eligible:
        promote = False
    observe_only = bool(not promote)
    return {
        "enabled": bool(enabled),
        "promote": promote,
        "market_key": str(market_key or ""),
        "proxy_signal_freshness_gate": proxy_freshness or None,
        "fail_closed_reason": proxy_freshness.get("fail_closed_reason") if proxy_applicable and not proxy_eligible else None,
        "suppressed_signal_count": int(proxy_freshness.get("suppressed_signal_count", 0) or 0) if proxy_applicable and not proxy_eligible else 0,
        "observe_only": observe_only,
        "primary_trigger_present": bool(primary_trigger_present),
        "related_market_confirmed": bool(related_market_confirmed),
        "within_confirmation_window": within_window,
        "confidence": score,
        "min_confidence": threshold,
        "signal_age_ms": age_ms,
        "confirmation_window_ms": window_ms,
        "state": "promoted" if promote else proxy_freshness.get("state", state),
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
    """Build progressively simplified lookup keys for paper-only route metadata."""

    normalized = str(venue or "").strip().upper().replace("-", "_").replace("/", "_")
    if not normalized:
        return ("",)

    keys: list[str] = []
    pending: list[str] = []

    def _append(value: str) -> None:
        if value and value not in keys:
            keys.append(value)
            pending.append(value)

    _append(normalized)

    trim_suffixes = (
        "_SPOT_PUBLIC",
        "_PERP_PUBLIC",
        "_PUBLIC",
        "_SPOT",
        "_PERP",
        "_SWAP",
        "_MARGIN",
    )
    while pending:
        candidate = pending.pop(0)
        for suffix in trim_suffixes:
            if candidate.endswith(suffix) and len(candidate) > len(suffix):
                _append(candidate[: -len(suffix)])

    if normalized in {"GATEIO", "GATE_IO"}:
        _append("GATE")
    if normalized.startswith("OKX"):
        _append("OKX")
    if normalized in {"BINANCEUS", "BINANCE_US"}:
        _append("BINANCE_US")
    if normalized.startswith("VALR"):
        _append("VALR")

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


def _paper_only_is_frontier_short_route_context(direction: str, context_stats: dict | None) -> bool:
    normalized_direction = str(direction or "").strip().lower()
    if normalized_direction == "short_frontier_spot":
        return True

    stats = context_stats or {}
    descriptors = " ".join(
        str(stats.get(field) or "")
        for field in (
            "trade_type",
            "signal_family",
            "market_surface",
            "strategy",
            "strategy_id",
            "context_key",
            "signal_key",
        )
    ).lower()
    return "frontier_crypto_venue_map" in descriptors and "short" in normalized_direction


def _paper_only_route_affirmation(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    if text in {
        "1",
        "true",
        "t",
        "yes",
        "y",
        "on",
        "supported",
        "available",
        "confirmed",
        "configured",
        "ready",
        "eligible",
        "observed",
        "satisfied",
        "mapped",
        "present",
    }:
        return True
    if text in {
        "0",
        "false",
        "f",
        "no",
        "n",
        "off",
        "unsupported",
        "unavailable",
        "blocked",
        "rejected",
        "denied",
        "missing",
        "unknown",
        "unconfirmed",
        "needs_confirmation",
        "needs_route_validation",
        "required_unconfirmed",
        "not_checked",
        "public_data_only",
    }:
        return False
    return None


def _paper_only_route_prerequisite_state(*values):
    saw_unknown = False
    for value in values:
        if value in (None, "", [], {}, ()):
            saw_unknown = True
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            nested = _paper_only_route_prerequisite_state(*list(value))
            if nested == "negative":
                return "negative"
            if nested == "affirmed":
                return "affirmed"
            saw_unknown = True
            continue
        if isinstance(value, dict):
            status = value.get("status")
            if status not in (None, ""):
                nested = _paper_only_route_prerequisite_state(status)
                if nested == "negative":
                    return "negative"
                if nested == "affirmed":
                    return "affirmed"
                saw_unknown = True
                continue
            saw_unknown = True
            continue
        normalized = _paper_only_route_affirmation(value)
        if normalized is True:
            return "affirmed"
        if normalized is False:
            return "negative"
        saw_unknown = True
    return "missing" if saw_unknown else "missing"


def _paper_only_symbol_support_state(profile: dict) -> tuple[str, str]:
    explicit = _paper_only_route_prerequisite_state(
        _paper_only_route_profile_value(
            profile,
            "symbol_supported",
            "instrument_supported",
            "paper_symbol_supported",
            "paper_instrument_supported",
            "route_symbol_supported",
        )
    )
    if explicit != "missing":
        return explicit, "candidate.symbol_supported"

    tokens = {
        str(_paper_only_route_profile_value(profile, "inst_id", "route_primary_symbol") or "").strip().upper(),
        str(_paper_only_route_profile_value(profile, "symbol", "route_hedge_symbol") or "").strip().upper(),
    }
    tokens.discard("")
    for field in (
        "supported_symbols",
        "paper_supported_symbols",
        "route_supported_symbols",
        "supported_instruments",
        "paper_supported_instruments",
    ):
        supported = _paper_only_route_profile_value(profile, field, ("venue_capabilities", field))
        if supported in (None, "", [], {}, ()):
            continue
        supported_tokens: set[str] = set()
        if isinstance(supported, dict):
            supported_tokens.update(str(key).strip().upper() for key in supported)
        elif isinstance(supported, (list, tuple, set, frozenset)):
            supported_tokens.update(str(item).strip().upper() for item in supported if item not in (None, ""))
        else:
            supported_tokens.add(str(supported).strip().upper())
        if not tokens:
            return "affirmed", field
        if tokens & supported_tokens:
            return "affirmed", field
        return "negative", field
    return "missing", "candidate.symbol_supported"


def _paper_only_conditional_order_support_state(profile: dict) -> tuple[str, str]:
    explicit = _paper_only_route_prerequisite_state(
        _paper_only_route_profile_value(
            profile,
            "supports_conditional_orders",
            "conditional_order_support",
            "conditional_orders_supported",
            ("venue_capabilities", "supports_conditional_orders"),
        )
    )
    if explicit != "missing":
        return explicit, "candidate.supports_conditional_orders"

    order_type_support = _paper_only_route_profile_value(
        profile,
        ("route_requirement_summary", "order_type_support"),
        ("paper_route_requirement_report", "route_requirement_summary", "order_type_support"),
        ("paper_route_requirement_report", "route_requirements", "order_type_support"),
    )
    if isinstance(order_type_support, dict):
        status = str(order_type_support.get("status") or "").strip().lower()
        if status in {"supported", "observed"}:
            return "affirmed", "route_requirement_summary.order_type_support"
        if status in {"unsupported", "missing"}:
            return "negative", "route_requirement_summary.order_type_support"
        supported = {
            str(item or "").strip().lower().replace("-", "_")
            for item in (order_type_support.get("supported_order_types") or [])
        }
        if supported & {"conditional", "trigger", "stop", "stop_limit", "stop_market"}:
            return "affirmed", "route_requirement_summary.order_type_support"
    return "missing", "route_requirement_summary.order_type_support"


def _paper_only_conditional_short_prerequisite_review(profile: dict, *, applies: bool) -> dict:
    if not applies:
        return {
            "applies": False,
            "paper_ineligible": False,
            "reason": "not_applicable",
            "failed_prerequisites": [],
            "failure_codes": [],
            "prerequisites": {},
        }

    checklist = _paper_only_route_profile_value(
        profile,
        ("paper_route_requirement_report", "route_requirements", "route_requirement_checklist"),
        ("route_requirements_panel", "route_requirement_checklist"),
    )
    checklist = checklist if isinstance(checklist, dict) else {}

    def _check(name, state, source):
        blocking = state != "affirmed"
        failure_code = None
        if state == "missing":
            failure_code = f"{name}_missing"
        elif state == "negative":
            failure_code = f"{name}_unsupported"
        return {
            "status": state,
            "source": source,
            "blocking": blocking,
            "failure_code": failure_code,
        }

    borrow_inventory_state = _paper_only_route_prerequisite_state(
        _paper_only_route_profile_value(
            profile,
            "borrow_confirmed",
            "borrowable",
            "spot_borrow_confirmed",
            "spot_borrow_available",
        ),
        checklist.get("shortable_inventory_declared"),
    )
    borrow_model_state = _paper_only_route_prerequisite_state(
        _paper_only_route_profile_value(
            profile,
            "borrow_model_available",
            "borrow_cost_model_present",
            "borrow_fee_modeled",
        ),
        checklist.get("borrow_cost_model_present"),
        _paper_only_route_profile_value(
            profile,
            "borrow_cost_bps",
            "borrow_fee_bps",
            "borrow_fee_bps_estimate",
            ("paper_route_requirement_report", "route_requirement_summary", "short_borrow_availability", "borrow_fee_bps_estimate"),
        ),
    )
    if "negative" in {borrow_inventory_state, borrow_model_state}:
        borrow_state = "negative"
    elif borrow_inventory_state == "affirmed" and borrow_model_state == "affirmed":
        borrow_state = "affirmed"
    else:
        borrow_state = "missing"

    margin_state = _paper_only_route_prerequisite_state(
        _paper_only_route_profile_value(
            profile,
            "margin_eligible",
            "margin_available",
            ("venue_capabilities", "supports_margin_spot"),
            ("venue_capabilities", "margin_supported"),
        ),
        checklist.get("venue_supports_margin_or_equivalent"),
    )
    fee_state = _paper_only_route_prerequisite_state(
        _paper_only_route_profile_value(
            profile,
            "fees_modeled",
            "fee_model_available",
            "estimated_fee_bps_per_side",
            "maker_fee_bps",
            "taker_fee_bps",
            ("paper_route_requirement_report", "route_requirement_summary", "fee_estimate", "estimated_round_trip_taker_bps"),
        ),
        checklist.get("fees_modeled"),
    )
    symbol_state, symbol_source = _paper_only_symbol_support_state(profile)
    conditional_order_state, conditional_order_source = _paper_only_conditional_order_support_state(profile)

    prerequisites = {
        "borrow_availability_model": _check(
            "borrow_availability_model",
            borrow_state,
            "route_requirement_checklist.shortable_inventory_declared+borrow_cost_model_present",
        ),
        "margin_eligibility": _check(
            "margin_eligibility",
            margin_state,
            "route_requirement_checklist.venue_supports_margin_or_equivalent",
        ),
        "fee_assumptions": _check(
            "fee_assumptions",
            fee_state,
            "route_requirement_checklist.fees_modeled",
        ),
        "symbol_support": _check(
            "symbol_support",
            symbol_state,
            symbol_source,
        ),
        "conditional_order_route_support": _check(
            "conditional_order_route_support",
            conditional_order_state,
            conditional_order_source,
        ),
    }
    failed_prerequisites = [
        name for name, details in prerequisites.items() if details["blocking"]
    ]
    failure_codes = [
        details["failure_code"]
        for details in prerequisites.values()
        if details["failure_code"] is not None
    ]
    if any(details["status"] == "negative" for details in prerequisites.values()):
        reason = "conditional_short_paper_metadata_unsupported"
    elif failed_prerequisites:
        reason = "conditional_short_paper_metadata_missing"
    else:
        reason = "supported"
    return {
        "applies": True,
        "paper_ineligible": bool(failed_prerequisites),
        "reason": reason,
        "failed_prerequisites": failed_prerequisites,
        "failure_codes": failure_codes,
        "prerequisites": prerequisites,
    }


def _paper_only_frontier_short_borrow_confirmed(profile: dict) -> bool:
    """Accept explicit borrow confirmation from nested route-report packets."""

    direct_state = _paper_only_route_prerequisite_state(
        _paper_only_route_profile_value(
            profile,
            "borrow_confirmed",
            "borrowable",
            "spot_borrow_confirmed",
            "spot_borrow_available",
            "borrow_ok",
        )
    )
    if direct_state == "affirmed":
        return True
    if direct_state == "negative":
        return False

    nested_state = _paper_only_route_prerequisite_state(
        _paper_only_route_profile_value(
            profile,
            "borrow_status",
            "borrow_availability",
            "borrow_availability_status",
            "spot_borrow_status",
            ("frontier_short_spot_route_intelligence", "borrow_availability"),
            ("frontier_short_spot_route_requirements_report", "short_borrow_availability", "status"),
            ("frontier_short_spot_route_requirements_report", "short_borrow_availability", "availability_status"),
            ("paper_route_requirement_report", "route_requirement_summary", "short_borrow_availability", "status"),
            ("paper_route_requirement_report", "route_requirement_summary", "short_borrow_availability", "availability_status"),
            ("paper_route_requirement_report", "route_requirement_summary", "borrow_availability", "status"),
            ("paper_route_requirement_report", "route_requirement_summary", "borrow_availability"),
            ("route_requirement_summary", "short_borrow_availability", "status"),
            ("route_requirement_summary", "short_borrow_availability", "availability_status"),
            ("route_requirement_summary", "borrow_availability", "status"),
            ("route_requirement_summary", "borrow_availability"),
        )
    )
    return nested_state == "affirmed"


def _paper_only_frontier_short_verified_route_support(profile: dict) -> bool:
    """Treat supported nested route reports as verified paper-route exceptions."""

    supported_values = (
        _paper_only_route_profile_value(
            profile,
            "paper_route_registry_status",
            ("paper_route_requirement_report", "route_requirements", "support_status"),
            ("frontier_short_spot_route_requirements_report", "per_venue_status", "status"),
            ("paper_route_requirement_report", "route_requirement_summary", "route_requirement_status"),
            ("route_requirement_summary", "route_requirement_status"),
        ),
    )
    for value in supported_values:
        if isinstance(value, dict):
            value = (
                value.get("support_status")
                or value.get("route_requirement_status")
                or value.get("status")
            )
        text = str(value or "").strip().lower()
        if not text:
            continue
        if text in {"standard", "executable", "feasible", "supported", "available", "configured", "ready"}:
            return True
        if text.startswith("supported") and not text.startswith("unsupported"):
            return True
    return False


def _paper_only_frontier_short_route_feasibility_reason(
    *,
    venue: str,
    direction: str,
    context_stats: dict | None = None,
    registry: dict | None = None,
) -> dict:
    """Classify paper-only frontier short route confidence without blocking emission."""

    stats = context_stats or {}
    normalized_direction = str(direction or "").strip().lower()
    route_status = ""
    for container in (
        stats,
        stats.get("execution_feasibility"),
        stats.get("execution_route"),
    ):
        if not isinstance(container, dict):
            continue
        for field in ("route_status", "status", "paper_route_status"):
            value = str(container.get(field) or "").strip().lower()
            if value:
                route_status = value
                break
        if route_status:
            break
    applies = bool(
        "short" in normalized_direction
        and (
            route_status in {"conditional", "route_unknown", "unsupported_or_unknown"}
            or _paper_only_is_conditional_short_context(direction, stats)
        )
    )
    if not applies:
        return {
            "applies": False,
            "route_feasibility_reason": "not_applicable",
            "shadow_label": False,
            "active_scoring_eligible": True,
            "explicit_borrow_ok": False,
            "verified_standard_route": False,
            "route_registry_status": "not_applicable",
            "route_requirement_support_status": "not_applicable",
        }

    route_registry = stats.get("paper_route_registry")
    if not isinstance(route_registry, dict):
        trade_type = str(stats.get("trade_type") or "frontier_crypto_venue_map").strip().lower()
        route_registry = assess_paper_route_registry(
            {
                "venue": venue,
                "trade_type": trade_type,
                "direction": normalized_direction,
            }
        )
    generic_requirements = paper_only_conditional_short_route_requirements(
        venue=venue,
        registry=registry,
    )
    registry_status = str(route_registry.get("support_status") or "unspecified").strip().lower()
    generic_status = str(generic_requirements.get("support_status") or "unknown").strip().lower()

    explicit_borrow_ok = any(
        _paper_only_frontier_short_borrow_confirmed(container)
        for container in (
            stats,
            stats.get("execution_feasibility"),
            stats.get("execution_route"),
            stats.get("frontier_short_spot_route_intelligence"),
            stats.get("frontier_short_spot_route_requirements_report"),
            stats.get("paper_route_requirement_report"),
            stats.get("route_requirement_summary"),
        )
        if isinstance(container, dict)
    )

    frontier_short_route = _paper_only_is_frontier_short_route_context(direction, stats)
    verified_standard_route = bool(
        route_status in {"standard", "executable", "feasible", "supported"}
        or registry_status == "supported"
        or _paper_only_frontier_short_verified_route_support(stats)
    )
    generic_supported_without_verified_route = bool(
        not explicit_borrow_ok
        and not verified_standard_route
        and registry_status in {"", "unknown", "unspecified"}
        and generic_status == "supported"
    )
    if explicit_borrow_ok:
        reason = "explicit_borrow_ok"
        shadow_label = False
        active_scoring_eligible = True
    elif verified_standard_route:
        reason = "verified_standard_short_route"
        shadow_label = False
        active_scoring_eligible = True
    elif generic_supported_without_verified_route:
        reason = "conditional_short_unverified_route"
        shadow_label = True
        active_scoring_eligible = False
    elif registry_status == "unsupported":
        reason = "conditional_short_exact_route_unsupported"
        shadow_label = True
        active_scoring_eligible = False
    elif generic_status == "unsupported":
        reason = "conditional_short_generic_route_unsupported"
        shadow_label = True
        active_scoring_eligible = False
    else:
        reason = "conditional_short_support_unknown"
        shadow_label = frontier_short_route
        active_scoring_eligible = not frontier_short_route
    return {
        "applies": True,
        "route_feasibility_reason": reason,
        "shadow_label": shadow_label,
        "active_scoring_eligible": active_scoring_eligible,
        "explicit_borrow_ok": explicit_borrow_ok,
        "verified_standard_route": verified_standard_route,
        "route_registry_status": registry_status,
        "route_requirement_support_status": generic_status,
    }


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
    route_review = _paper_only_frontier_short_route_feasibility_reason(
        venue=venue,
        direction=direction,
        context_stats=context_stats,
        registry=registry,
    )

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

    if not route_review.get("applies", False):
        return {
            "enabled": True,
            "applied": False,
            "allow": True,
            "suppressed": False,
            "score_multiplier": 1.0,
            "reason": route_review.get("route_feasibility_reason", "not_applicable"),
            "reasons": [route_review.get("route_feasibility_reason", "not_applicable")],
            "route_requirements": None,
            "route_feasibility_reason": route_review.get("route_feasibility_reason", "not_applicable"),
            "active_scoring_eligible": bool(route_review.get("active_scoring_eligible", True)),
            "shadow_label": bool(route_review.get("shadow_label", False)),
            "route_registry_status": route_review.get("route_registry_status"),
            "verified_standard_route": bool(route_review.get("verified_standard_route", False)),
            "explicit_borrow_ok": bool(route_review.get("explicit_borrow_ok", False)),
        }

    requirements = paper_only_conditional_short_route_requirements(venue=venue, registry=registry)
    support_status = requirements.get("support_status")
    prerequisite_review = _paper_only_conditional_short_prerequisite_review(
        context_stats if isinstance(context_stats, dict) else {},
        applies=True,
    )
    reasons: list[str] = []
    suppressed = False
    allow = True
    score_multiplier = 1.0
    if route_review.get("shadow_label", False):
        reasons.append(str(route_review.get("route_feasibility_reason") or "conditional_short_exact_route_unsupported"))
        route_reason = str(route_review.get("route_feasibility_reason") or "").strip().lower()
        if route_reason == "conditional_short_support_unknown":
            score_multiplier *= max(0.0, min(1.0, float(merged_policy.get("unknown_multiplier", 0.75) or 0.75)))
        elif route_reason == "conditional_short_unverified_route":
            score_multiplier *= max(
                0.0,
                min(1.0, float(merged_policy.get("unverified_route_multiplier", 0.15) or 0.15)),
            )
        elif str(route_review.get("route_registry_status") or "").strip().lower() == "unsupported":
            score_multiplier *= max(
                0.0,
                min(1.0, float(merged_policy.get("exact_unsupported_multiplier", 0.15) or 0.15)),
            )
        else:
            score_multiplier *= max(
                0.0,
                min(1.0, float(merged_policy.get("generic_unsupported_multiplier", 0.15) or 0.15)),
            )

    if route_review.get("active_scoring_eligible", True) and support_status == "unsupported":
        reasons.append("unsupported_spot_short")
        if str(merged_policy.get("unsupported_behavior", "suppress")).strip().lower() == "suppress":
            suppressed = True
            allow = False
            score_multiplier = 0.0
    elif route_review.get("active_scoring_eligible", True) and support_status == "unknown":
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

    if prerequisite_review.get("paper_ineligible", False):
        reasons = list(
            dict.fromkeys(
                [str(prerequisite_review.get("reason") or "conditional_short_paper_metadata_missing")]
                + list(prerequisite_review.get("failure_codes") or [])
                + reasons
            )
        )
        score_multiplier = max(
            0.0,
            min(
                1.0,
                float(
                    merged_policy.get(
                        "metadata_negative_multiplier"
                        if str(prerequisite_review.get("reason")) == "conditional_short_paper_metadata_unsupported"
                        else "metadata_missing_multiplier",
                        0.0,
                    )
                    or 0.0
                ),
            ),
        )
        behavior = str(
            merged_policy.get(
                "metadata_negative_behavior"
                if str(prerequisite_review.get("reason")) == "conditional_short_paper_metadata_unsupported"
                else "metadata_missing_behavior",
                "suppress",
            )
            or "suppress"
        ).strip().lower()
        if behavior == "suppress":
            suppressed = True
            allow = False
        route_reason = str(prerequisite_review.get("reason") or "").strip()
        return {
            "enabled": True,
            "applied": True,
            "allow": allow,
            "suppressed": suppressed,
            "score_multiplier": score_multiplier,
            "reason": route_reason,
            "reasons": reasons,
            "route_requirements": requirements,
            "route_feasibility_reason": route_reason,
            "active_scoring_eligible": False,
            "shadow_label": True,
            "route_registry_status": route_review.get("route_registry_status"),
            "verified_standard_route": bool(route_review.get("verified_standard_route", False)),
            "explicit_borrow_ok": bool(route_review.get("explicit_borrow_ok", False)),
            "paper_ineligible": True,
            "paper_ineligible_reason": route_reason,
            "paper_rationale_fields": prerequisite_review["prerequisites"],
            "paper_rationale_failures": prerequisite_review["failed_prerequisites"],
            "paper_rationale_codes": prerequisite_review["failure_codes"],
        }

    return {
        "enabled": True,
        "applied": True,
        "allow": allow,
        "suppressed": suppressed,
        "score_multiplier": max(0.0, min(1.0, score_multiplier)),
        "reason": reasons[0] if reasons else "supported",
        "reasons": reasons or ["supported"],
        "route_requirements": requirements,
        "route_feasibility_reason": str(
            route_review.get("route_feasibility_reason") or (reasons[0] if reasons else "supported")
        ),
        "active_scoring_eligible": bool(route_review.get("active_scoring_eligible", True)),
        "shadow_label": bool(route_review.get("shadow_label", False)),
        "route_registry_status": route_review.get("route_registry_status"),
        "verified_standard_route": bool(route_review.get("verified_standard_route", False)),
        "explicit_borrow_ok": bool(route_review.get("explicit_borrow_ok", False)),
        "paper_ineligible": False,
        "paper_ineligible_reason": None,
        "paper_rationale_fields": prerequisite_review["prerequisites"],
        "paper_rationale_failures": prerequisite_review["failed_prerequisites"],
        "paper_rationale_codes": prerequisite_review["failure_codes"],
    }


def _annotate_frontier_route_feasibility_shadow_state(candidate: dict, gate: dict) -> None:
    """Project non-blocking route-feasibility ranking state into report packets."""

    if not isinstance(candidate, dict) or not isinstance(gate, dict):
        return
    reason = str(
        gate.get("route_feasibility_reason")
        or gate.get("reason")
        or "unknown"
    )
    active_scoring_eligible = bool(gate.get("active_scoring_eligible", True))
    shadow_label = bool(gate.get("shadow_label", False))
    paper_ineligible = bool(gate.get("paper_ineligible", False))
    paper_ineligible_reason = gate.get("paper_ineligible_reason")
    rationale_fields = dict(gate.get("paper_rationale_fields") or {})
    rationale_failures = list(gate.get("paper_rationale_failures") or [])
    rationale_codes = list(gate.get("paper_rationale_codes") or [])
    candidate["route_feasibility_reason"] = reason
    candidate["paper_active_scoring_eligible"] = active_scoring_eligible
    candidate["paper_route_feasibility_shadow_label"] = shadow_label
    candidate["paper_ineligible"] = paper_ineligible
    candidate["paper_ineligible_reason"] = paper_ineligible_reason
    candidate["paper_route_rationale_fields"] = rationale_fields
    candidate["paper_route_rationale_failures"] = rationale_failures
    candidate["paper_route_rationale_codes"] = rationale_codes
    if shadow_label:
        candidate["paper_route_feasibility_shadow_reason"] = reason

    report_fields = (
        "frontier_short_spot_route_intelligence",
        "frontier_short_spot_route_requirements_report",
        "paper_route_requirement_report",
        "route_requirement_summary",
    )
    for field in report_fields:
        report = candidate.get(field)
        if not isinstance(report, dict):
            continue
        report["route_feasibility_reason"] = reason
        report["paper_active_scoring_eligible"] = active_scoring_eligible
        report["paper_route_feasibility_shadow_label"] = shadow_label
        report["paper_ineligible"] = paper_ineligible
        report["paper_ineligible_reason"] = paper_ineligible_reason
        report["paper_route_rationale_fields"] = rationale_fields
        report["paper_route_rationale_failures"] = rationale_failures
        report["paper_route_rationale_codes"] = rationale_codes
        if field == "paper_route_requirement_report":
            nested = report.get("frontier_short_spot_route_intelligence")
            if isinstance(nested, dict):
                nested["route_feasibility_reason"] = reason
                nested["paper_active_scoring_eligible"] = active_scoring_eligible
                nested["paper_route_feasibility_shadow_label"] = shadow_label
                nested["paper_ineligible"] = paper_ineligible
                nested["paper_ineligible_reason"] = paper_ineligible_reason
                nested["paper_route_rationale_fields"] = rationale_fields
                nested["paper_route_rationale_failures"] = rationale_failures
                nested["paper_route_rationale_codes"] = rationale_codes
            nested = report.get("frontier_short_spot_route_requirements_report")
            if isinstance(nested, dict):
                nested["route_feasibility_reason"] = reason
                nested["paper_active_scoring_eligible"] = active_scoring_eligible
                nested["paper_route_feasibility_shadow_label"] = shadow_label
                nested["paper_ineligible"] = paper_ineligible
                nested["paper_ineligible_reason"] = paper_ineligible_reason
                nested["paper_route_rationale_fields"] = rationale_fields
                nested["paper_route_rationale_failures"] = rationale_failures
                nested["paper_route_rationale_codes"] = rationale_codes


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

    def _multiplier(value, default=1.0):
        try:
            return float(default if value is None else value)
        except (TypeError, ValueError):
            return float(default)

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
        score_multiplier *= _multiplier(gate.get("score_multiplier", 1.0))
    else:
        score_multiplier *= 0.0

    if not cohort_gate.get("suppressed", False):
        score_multiplier *= _multiplier(cohort_gate.get("score_multiplier", 1.0))
    else:
        score_multiplier *= 0.0

    if cross_market_gate.get("enabled", False):
        score_multiplier *= _multiplier(cross_market_gate.get("score_multiplier", 1.0))

    if route_feasibility_gate.get("enabled", False) and route_feasibility_gate.get("applied", False):
        score_multiplier *= _multiplier(route_feasibility_gate.get("score_multiplier", 1.0))
        if route_feasibility_gate.get("suppressed", False):
            allow = False
            suppressed = True

    return {
        "enabled": bool(enabled),
        "allow": allow,
        "suppressed": suppressed,
        "active_scoring_eligible": bool(
            route_feasibility_gate.get("active_scoring_eligible", not suppressed)
        ),
        "route_feasibility_reason": route_feasibility_gate.get("route_feasibility_reason"),
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
    "AUD",
    "EUR",
    "GBP",
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
# These venues publish spot prices in a local fiat currency.  The flag is
# deliberately descriptive: a fresh, priceable local quote remains a paper
# candidate and is ranked with its FX/premium telemetry rather than discarded.
LOCAL_FIAT_CEX_VENUES = frozenset({"INDODAX", "BITSO", "VALR", "LUNO", "BUDA"})

DEFAULT_FRONTIER_MARKETABILITY_GATES = {
    "enabled": True,
    "paper_only": True,
    # Health evidence changes simulated routing and sizing.  It must not
    # discard a priceable paper experiment: exploration remains available
    # through the conservative counterfactual route below.
    "diagnostic_only": True,
    "confirmed_route_allocation_multiplier": 1.0,
    "conservative_route_allocation_multiplier": 0.25,
    "max_book_age_seconds": 30.0,
    "max_spread_bps": 8.0,
    "min_top_of_book_notional_usd": 1000.0,
    "max_cross_venue_deviation_bps": 250.0,
    "min_reference_venues": 1,
    "min_route_confidence": 0.70,
    "min_venue_health_score": 60.0,
    "known_paper_route_statuses": ["standard"],
}
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
            "intraday": {
                "url_template": "https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={symbol}&interval=1m&limit=62",
                "parser": "gate_1m_candles",
            },
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
            "intraday": {
                "url_template": "https://api.mexc.com/api/v3/klines?symbol={symbol}&interval=1m&limit=62",
                "parser": "binance_style_1m_klines",
            },
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
            "intraday": {
                "url_template": "https://api.binance.us/api/v3/klines?symbol={symbol}&interval=1m&limit=62",
                "parser": "binance_style_1m_klines",
            },
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
            "intraday": {
                "url_template": "https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=1m&limit=62",
                "parser": "okx_1m_candles",
            },
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
        {
            "venue": "COINJAR",
            "enabled": True,
            "market_type": "spot",
            "route_id": "coinjar_spot_public",
            "region": "Australia",
            "url": "https://api.exchange.coinjar.com/products",
            "parser": "coinjar_products",
            "quote_assets": ["AUD", "EUR", "GBP", "USD", "USDC", "USDT"],
            "max_product_tickers": 24,
            "depth": {
                "url_template": "https://data.exchange.coinjar.com/products/{symbol}/book?level=2",
                "parser": "coinjar_book",
                "max_levels": 20,
                "timestamp_capability": "response_received",
            },
        },
        {
            "venue": "RIPIO",
            "enabled": True,
            "market_type": "spot",
            "route_id": "ripio_spot_public",
            "region": "LATAM",
            "url": "https://api.ripio.com/trade/public/tickers",
            "parser": "ripio_tickers",
            "quote_assets": ["ARS", "BRL", "MXN", "USD", "USDC", "USDT"],
            "depth": {
                "url_template": "https://api.ripio.com/trade/public/orders/level-2?pair={symbol}&limit={limit}",
                "parser": "ripio_level2",
                "max_levels": 20,
                "timestamp_capability": "exchange",
            },
        },
        {
            "venue": "WHITEBIT",
            "enabled": True,
            "market_type": "spot",
            "route_id": "whitebit_spot_public",
            "url": "https://whitebit.com/api/v4/public/ticker",
            "parser": "whitebit_tickers",
            "quote_assets": ["USD", "USDC", "USDT"],
            "depth": {
                "url_template": "https://whitebit.com/api/v4/public/orderbook/depth/{symbol}",
                "parser": "whitebit_depth",
                "max_levels": 50,
                "timestamp_capability": "exchange",
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
        "quote_ccy": quote,
        "fx_to_usd": None,
        "fx_age_minutes": None,
        "normalized_mid_usd": None,
        "premium_vs_reference_bps": None,
        "local_quote_flag": False,
        "usd_normalized_last": None,
        "native_quote_currency": quote,
        "canonical_quote_currency": None,
        "canonical_normalized_price": None,
        "fx_source": None,
        "fx_age_seconds": None,
        "suppression_reason": None,
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
    row["liquidity_score"] = liquidity_score(row.get("quote_volume_24h"))
    row.setdefault("return_1m_bps", 0.0)
    row.setdefault("quote_volume_1m", 0.0)
    row.setdefault("relative_volume_1m_60m", 0.0)
    row.setdefault("microstructure_history_ready", 0.0)
    row.setdefault("microstructure_status", "not_requested")
    return _apply_paper_only_review_policy(row)


def _intraday_candle_rows(config: dict, result: dict) -> list[dict]:
    """Normalize closed one-minute public candles without accepting partial bars."""

    parser = str(config.get("parser") or "")
    payload = result.get("payload")
    raw_rows = (payload or {}).get("data") or [] if parser == "okx_1m_candles" else payload or []
    rows: list[dict] = []
    for raw in raw_rows:
        if not isinstance(raw, (list, tuple)):
            continue
        if parser == "okx_1m_candles" and len(raw) >= 9:
            if str(raw[8]) != "1":
                continue
            timestamp, close, quote_volume = raw[0], raw[4], raw[7]
        elif parser == "binance_style_1m_klines" and len(raw) >= 8:
            timestamp, close, quote_volume = raw[0], raw[4], raw[7]
        elif parser == "gate_1m_candles" and len(raw) >= 3:
            timestamp, quote_volume, close = raw[0], raw[1], raw[2]
        else:
            continue
        timestamp_value = as_float(timestamp, None)
        close_value = as_float(close, None)
        volume_value = as_float(quote_volume, None)
        if timestamp_value is None or close_value is None or close_value <= 0 or volume_value is None or volume_value < 0:
            continue
        rows.append(
            {
                "timestamp": float(timestamp_value),
                "close": float(close_value),
                "quote_volume": float(volume_value),
            }
        )
    rows.sort(key=lambda item: item["timestamp"])
    # OKX exposes an explicit completion flag. The other public endpoints include
    # the currently forming candle, so conservatively exclude their newest row.
    if parser != "okx_1m_candles" and rows:
        rows = rows[:-1]
    return rows


def _intraday_features(config: dict, result: dict) -> dict:
    rows = _intraday_candle_rows(config, result) if result.get("ok") else []
    if len(rows) < 61:
        return {
            "return_1m_bps": 0.0,
            "quote_volume_1m": 0.0,
            "relative_volume_1m_60m": 0.0,
            "microstructure_history_ready": 0.0,
            "microstructure_status": "insufficient_closed_candles" if result.get("ok") else "unavailable",
        }
    recent = rows[-61:]
    latest = recent[-1]
    previous = recent[-2]
    baseline_volumes = [item["quote_volume"] for item in recent[:-1] if item["quote_volume"] > 0]
    baseline = statistics.median(baseline_volumes) if len(baseline_volumes) == 60 else 0.0
    if baseline <= 0:
        return {
            "return_1m_bps": 0.0,
            "quote_volume_1m": 0.0,
            "relative_volume_1m_60m": 0.0,
            "microstructure_history_ready": 0.0,
            "microstructure_status": "invalid_volume_baseline",
        }
    return {
        "return_1m_bps": round(bps(latest["close"], previous["close"]), 6),
        "quote_volume_1m": round(latest["quote_volume"], 6),
        "relative_volume_1m_60m": round(latest["quote_volume"] / baseline, 6),
        "microstructure_history_ready": 1.0,
        "microstructure_status": "ready",
    }


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _active_strategy_intraday_requirements(conn) -> dict:
    """Read active Strategy Lab intraday requirements without coupling to Strategy Lab imports."""

    empty = {"active_program_count": 0, "required_features": [], "programs": []}
    if conn is None or not callable(getattr(conn, "execute", None)):
        return empty
    try:
        rows = conn.execute(
            """
            select strategy_lab_id, strategy_logic_json, compiled_strategy_logic_json,
                   data_requirements_json
            from strategy_lab_experiments
            where experiment_type = 'market_strategy'
              and status in ('active_testing', 'needs_data', 'needs_more_evidence',
                             'needs_contract_revision')
            """
        ).fetchall()
    except Exception:  # pragma: no cover - optional Strategy Lab storage during standalone scans
        return empty

    programs = []
    required_features: set[str] = set()
    for raw in rows:
        try:
            row = dict(raw)
        except (TypeError, ValueError):
            continue
        logic = _json_object(row.get("compiled_strategy_logic_json"))
        if not logic:
            logic = _json_object(row.get("strategy_logic_json"))
        if str(logic.get("type") or "") != "observation_program":
            continue
        data_requirements = _json_object(row.get("data_requirements_json"))
        explicit = data_requirements.get("required_snapshot_features") or []
        serialized_logic = json.dumps(logic, sort_keys=True)
        required = {
            feature
            for feature in INTRADAY_CONFIRMATION_FEATURES
            if feature in explicit or feature in serialized_logic
        }
        if not required:
            continue
        universe = logic.get("universe") if isinstance(logic.get("universe"), dict) else {}
        programs.append(
            {
                "strategy_lab_id": str(row.get("strategy_lab_id") or ""),
                "required_features": sorted(required),
                "universe": universe,
            }
        )
        required_features.update(required)
    return {
        "active_program_count": len(programs),
        "required_features": sorted(required_features),
        "programs": programs,
    }


def _strategy_universe_matches_observation(observation: dict, universe: dict) -> bool:
    fields = {
        "venues": "venue",
        "inst_ids": "inst_id",
        "trade_types": "trade_type",
        "asset_classes": "asset_class",
        "regions": "region",
        "market_types": "market_type",
        "quotes": "quote",
        "bases": "base",
    }
    for plural, field in fields.items():
        allowed = universe.get(plural)
        if not allowed:
            continue
        values = allowed if isinstance(allowed, list) else [allowed]
        observed = observation.get(field)
        if field == "inst_id":
            observed = observed or observation.get("instrument_id")
        if str(observed or "").upper() not in {str(value).upper() for value in values}:
            return False
    return True


def _strategy_intraday_priority(observation: dict, strategy_requirements: dict | None) -> int:
    programs = (strategy_requirements or {}).get("programs") or []
    return sum(
        1
        for program in programs
        if _strategy_universe_matches_observation(observation, program.get("universe") or {})
    )


def enrich_intraday_features(
    observations: list[dict],
    settings: dict,
    registry: dict | None = None,
    strategy_requirements: dict | None = None,
) -> tuple[list[dict], dict]:
    """Add bounded public-candle features, prioritizing active program universes."""

    cfg = settings.get("frontier_crypto_adapter", {})
    output = [dict(row) for row in observations]
    for row in output:
        row.setdefault("return_1m_bps", 0.0)
        row.setdefault("quote_volume_1m", 0.0)
        row.setdefault("relative_volume_1m_60m", 0.0)
        row.setdefault("microstructure_history_ready", 0.0)
        row.setdefault("microstructure_status", "not_requested")
    coverage_base = {
        "strategy_required_features": list((strategy_requirements or {}).get("required_features") or []),
        "strategy_required_program_count": int((strategy_requirements or {}).get("active_program_count") or 0),
        "strategy_required_eligible_count": 0,
        "strategy_required_selected_count": 0,
        "attempted_count": 0,
        "unavailable_count": 0,
        "microstructure_status_by_venue": {},
    }
    if not cfg.get("intraday_features_enabled", True):
        return output, {"enabled": False, "selected_count": 0, "ready_count": 0, **coverage_base}
    targets = {
        str(item.get("venue") or "").upper(): item
        for item in (registry or load_venue_registry()).get("venues", [])
        if isinstance(item.get("intraday"), dict)
    }
    eligible = [
        row
        for row in output
        if str(row.get("venue") or "").upper() in targets
        and row.get("market_type") == "spot"
        and row.get("data_status") == "reachable"
        and str(row.get("quote") or "").upper() in USD_LIKE_QUOTES
        and float(row.get("last") or 0.0) > 0
    ]
    priority_by_id = {
        str(row.get("instrument_id") or ""): _strategy_intraday_priority(row, strategy_requirements)
        for row in eligible
    }
    coverage_base["strategy_required_eligible_count"] = sum(
        1 for priority in priority_by_id.values() if priority > 0
    )
    ranked = sorted(
        eligible,
        key=lambda row: (
            -priority_by_id.get(str(row.get("instrument_id") or ""), 0),
            -float(row.get("quote_volume_24h") or 0.0),
            str(row.get("venue") or ""),
            str(row.get("instrument_id") or ""),
        ),
    )
    total_limit = max(0, int(cfg.get("intraday_feature_max_observations", 24)))
    venue_limit = max(1, int(cfg.get("intraday_feature_max_per_venue", 6)))
    selected: list[dict] = []
    venue_counts: collections.Counter = collections.Counter()
    for row in ranked:
        venue = str(row.get("venue") or "").upper()
        if len(selected) >= total_limit:
            break
        if venue_counts[venue] >= venue_limit:
            continue
        selected.append(row)
        venue_counts[venue] += 1

    def fetch_one(row: dict) -> tuple[str, dict]:
        inst_id = str(row.get("instrument_id") or "")
        target = targets[str(row.get("venue") or "").upper()]
        intraday = target["intraday"]
        try:
            url = str(intraday["url_template"]).format(symbol=str(row.get("symbol") or ""))
            result = fetch_json(url, timeout=int(cfg.get("intraday_feature_timeout_seconds", 4)))
            return inst_id, _intraday_features(intraday, result)
        except Exception:  # noqa: BLE001 - one public venue must not fail the scan
            return inst_id, _intraday_features(intraday, {"ok": False, "payload": None})

    features_by_id: dict[str, dict] = {}
    workers = max(1, min(int(cfg.get("intraday_feature_workers", 6)), len(selected) or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_one, row) for row in selected]
        for future in concurrent.futures.as_completed(futures):
            inst_id, features = future.result()
            features_by_id[inst_id] = features
    for row in output:
        features = features_by_id.get(str(row.get("instrument_id") or ""))
        if features:
            row.update(features)
    status_by_venue: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for row in selected:
        venue = str(row.get("venue") or "UNKNOWN")
        features = features_by_id.get(str(row.get("instrument_id") or ""), {})
        status_by_venue[venue][str(features.get("microstructure_status") or "unavailable")] += 1
    coverage_base.update(
        {
            "strategy_required_selected_count": sum(
                1
                for row in selected
                if priority_by_id.get(str(row.get("instrument_id") or ""), 0) > 0
            ),
            "attempted_count": len(selected),
            "unavailable_count": sum(
                1
                for item in features_by_id.values()
                if item.get("microstructure_status") != "ready"
            ),
            "microstructure_status_by_venue": {
                venue: dict(counts) for venue, counts in sorted(status_by_venue.items())
            },
        }
    )
    return output, {
        "enabled": True,
        "selected_count": len(selected),
        "ready_count": sum(1 for item in features_by_id.values() if item["microstructure_history_ready"] >= 1),
        "selected_by_venue": dict(venue_counts),
        **coverage_base,
    }


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
    rows = _valr_payload_rows(result)
    observations = []
    for data in rows:
        symbol = str(data.get("currencyPair") or data.get("pair") or data.get("symbol") or "").upper()
        if not symbol:
            continue
        row = _base_observation(target, result, symbol)
        last_trade_at = _paper_only_parse_timestamp(
            _paper_only_valr_pick_first(
                data,
                "lastTradedTimestamp",
                "lastTradedAt",
                "lastTradeTimestamp",
                "lastTradeAt",
                "timestamp",
            )
        )
        row.update(
            {
                "bid": as_float(data.get("bidPrice") or data.get("bid")),
                "ask": as_float(data.get("askPrice") or data.get("ask")),
                "last": as_float(data.get("lastTradedPrice") or data.get("last") or data.get("price")),
                "quote_volume_24h": as_float(data.get("quoteVolume") or data.get("quote_volume")),
                "venue_symbol": symbol,
                "best_bid": as_float(data.get("bidPrice") or data.get("bid")),
                "best_ask": as_float(data.get("askPrice") or data.get("ask")),
                "last_trade_timestamp": last_trade_at.isoformat() if last_trade_at is not None else None,
                "exchange_timestamp": last_trade_at.isoformat() if last_trade_at is not None else None,
                "instrument_metadata": {
                    "venue": "VALR",
                    "venue_symbol": symbol,
                    "base_asset": row.get("base"),
                    "quote_asset": row.get("quote"),
                    "market_type": "spot",
                    "public_read_only": True,
                },
            }
        )
        if row.get("quote_volume_24h") is None:
            row["quote_volume_24h"] = (as_float(data.get("baseVolume"), 0.0) or 0.0) * float(row.get("last") or 0.0)
        output = _finalize_observation(row)
        output.update(native_spot_surface_fields(output))
        observations.append(output)
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
    payload = result.get("payload") or []
    if isinstance(payload, dict) and isinstance(payload.get("result"), (dict, list)):
        payload = payload["result"]
    current_v3_list = isinstance(payload, list)
    if current_v3_list:
        rows = [
            (str(item.get("symbol") or "").upper(), item)
            for item in payload
            if isinstance(item, dict) and item.get("symbol")
        ]
    elif isinstance(payload, dict):
        rows = [(str(symbol).upper(), item) for symbol, item in payload.items() if isinstance(item, dict)]
    else:
        rows = []
    observations = []
    for symbol, row_data in rows:
        parts = [part for part in str(symbol).upper().split("_") if part]
        row = _base_observation(target, result, str(symbol).upper())
        if len(parts) == 2:
            if current_v3_list:
                row["base"] = _canonical_asset(parts[0])
                row["quote"] = parts[1]
            else:
                row["quote"] = parts[0]
                row["base"] = _canonical_asset(parts[1])
            row["comparison_key"] = row["base"]
        last = as_float(row_data.get("last"))
        row.update(
            {
                "bid": as_float(row_data.get("highest_bid") or row_data.get("highestBid") or row_data.get("bid")),
                "ask": as_float(row_data.get("lowest_ask") or row_data.get("lowestAsk") or row_data.get("ask")),
                "last": last,
                "quote_volume_24h": as_float(row_data.get("quote_volume") or row_data.get("quoteVolume")),
            }
        )
        if row.get("quote_volume_24h") is None:
            row["quote_volume_24h"] = (
                as_float(row_data.get("base_volume") or row_data.get("baseVolume") or row_data.get("volume"), 0.0)
                or 0.0
            ) * float(last or 0.0)
        observations.append(_finalize_observation(row))
    return observations


def _bybit_direct_probe_due(conn, interval_seconds: int) -> bool:
    if conn is None:
        return True
    try:
        row = conn.execute(
            """
            select last_seen_at, blocker_code
            from market_admission_states
            where venue = 'BYBIT_SPOT'
              and blocker_code = 'network_region_blocked'
            order by last_seen_at desc
            limit 1
            """
        ).fetchone()
    except Exception:  # noqa: BLE001 - table may not exist during first migration/test setup.
        return True
    if not row or not row["last_seen_at"]:
        return True
    try:
        checked = dt.datetime.fromisoformat(str(row["last_seen_at"]).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return True
    return (dt.datetime.now(dt.timezone.utc) - checked).total_seconds() >= max(1, interval_seconds)


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


def _parse_coinjar_products(target: dict, result: dict) -> list[dict]:
    products = result.get("payload") or []
    by_symbol = {
        str(item.get("id") or "").upper(): item
        for item in products
        if isinstance(item, dict) and item.get("id")
    }
    symbols = _top_symbols_for_subfetch(target, list(by_symbol))
    ticker_results: dict[str, tuple[str, dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(symbols) or 1)) as pool:
        futures = {}
        for symbol in symbols:
            ticker_url = f"https://data.exchange.coinjar.com/products/{urllib.request.pathname2url(symbol)}/ticker"
            futures[pool.submit(fetch_json, ticker_url)] = (symbol, ticker_url)
        for future in concurrent.futures.as_completed(futures):
            symbol, ticker_url = futures[future]
            try:
                ticker_results[symbol] = (ticker_url, future.result())
            except Exception as exc:  # noqa: BLE001 - preserve the rest of the public venue.
                ticker_results[symbol] = (
                    ticker_url,
                    {
                        "ok": False,
                        "data_status": "unavailable",
                        "http_status": None,
                        "latency_ms": None,
                        "error": str(exc)[:300],
                    },
                )
    observations = []
    for symbol in symbols:
        ticker_url, ticker = ticker_results[symbol]
        product = by_symbol[symbol]
        row = _base_observation(target, ticker, symbol)
        row["source_url"] = ticker_url
        row["base"] = _canonical_asset((product.get("base_currency") or {}).get("iso_code"))
        row["quote"] = str((product.get("counter_currency") or {}).get("iso_code") or "").upper() or None
        if not ticker["ok"]:
            row["notes"].append(f"CoinJar ticker fetch failed: {ticker['http_status']}")
            observations.append(_finalize_observation(row))
            continue
        data = ticker.get("payload") or {}
        last = as_float(data.get("last") or data.get("mark_price"))
        base_volume = as_float(data.get("volume_24h") or data.get("volume"), 0.0) or 0.0
        row.update(
            {
                "bid": as_float(data.get("bid")),
                "ask": as_float(data.get("ask")),
                "last": last,
                "quote_volume_24h": base_volume * float(last or 0.0),
                "change_24h_pct": as_float(data.get("change_24h"), 0.0) * 100.0,
                "session_status": str(data.get("status") or "unknown"),
                "exchange_timestamp": data.get("current_time"),
            }
        )
        observations.append(_finalize_observation(row))
    return observations


def _parse_ripio_tickers(target: dict, result: dict) -> list[dict]:
    payload = result.get("payload") or {}
    rows = payload.get("data") if isinstance(payload, dict) else payload
    observations = []
    for data in rows if isinstance(rows, list) else []:
        if not isinstance(data, dict):
            continue
        symbol = str(data.get("pair") or "").upper()
        if not symbol:
            continue
        row = _base_observation(target, result, symbol)
        row["base"] = _canonical_asset(data.get("base_code"))
        row["quote"] = str(data.get("quote_code") or "").upper() or None
        row.update(
            {
                "bid": as_float(data.get("bid")),
                "ask": as_float(data.get("ask")),
                "last": as_float(data.get("last")),
                "quote_volume_24h": as_float(data.get("quote_volume")),
                "change_24h_pct": as_float(data.get("price_change_percent_24h"), 0.0),
                "exchange_timestamp": data.get("date"),
            }
        )
        if data.get("is_frozen"):
            row["data_status"] = "degraded"
            row["notes"].append("Ripio market is frozen.")
        observations.append(_finalize_observation(row))
    return observations


def _parse_whitebit_tickers(target: dict, result: dict) -> list[dict]:
    payload = result.get("payload") or {}
    rows = payload.items() if isinstance(payload, dict) else []
    observations = []
    for raw_symbol, data in rows:
        if not isinstance(data, dict):
            continue
        symbol = str(raw_symbol or "").upper()
        if not symbol or symbol.endswith("_PERP"):
            continue
        base, quote = _split_symbol(symbol, _target_quote_assets(target))
        if not base or not quote:
            continue
        row = _base_observation(target, result, symbol)
        row.update(
            {
                "base": base,
                "quote": quote,
                "last": as_float(data.get("last_price")),
                "quote_volume_24h": as_float(data.get("quote_volume")),
                "change_24h_pct": as_float(data.get("change"), 0.0),
            }
        )
        if data.get("isFrozen") or data.get("is_frozen"):
            row["data_status"] = "degraded"
            row["notes"].append("WhiteBIT market is frozen.")
        observations.append(_finalize_observation(row))
    observations.sort(key=lambda row: float(row.get("quote_volume_24h") or 0.0), reverse=True)
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
    "coinjar_products": _parse_coinjar_products,
    "ripio_tickers": _parse_ripio_tickers,
    "whitebit_tickers": _parse_whitebit_tickers,
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
    if row.get("canonical_normalized_price") not in (None, ""):
        return float(row.get("canonical_normalized_price") or 0.0)
    if row.get("usd_normalized_last") not in (None, ""):
        return float(row.get("usd_normalized_last") or 0.0)
    # Native prices are directly comparable only for canonical USD-like quotes.
    # A local-fiat or unknown quote must never leak into dislocation scoring.
    if str(row.get("quote") or "").upper() in USD_LIKE_QUOTES:
        return float(row.get("last") or 0.0)
    return 0.0


def _reference_age_seconds(row: dict) -> float | None:
    age = as_float(row.get("freshness_age_seconds"), None)
    if age is not None:
        return max(0.0, float(age))
    timestamp = row.get("exchange_timestamp") or row.get("last_checked_at")
    if not timestamp:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())


def _set_quote_suppression(output: dict, status: str, reason: str) -> None:
    output["usd_normalized_last"] = None
    output["canonical_normalized_price"] = None
    output["canonical_quote_currency"] = None
    output["comparison_price"] = None
    output["quote_normalization_status"] = status
    output["local_quote_observe_only"] = True
    output["suppression_reason"] = reason


def _set_stale_fx_ranking_state(output: dict, reason: str = "stale_fx_reference") -> None:
    output["quote_normalization_status"] = "stale_fx_reference"
    output["suppression_reason"] = reason
    output["quote_ranking_eligible"] = False
    output["quote_ranking_reason"] = reason


def _local_quote_normalization_telemetry(row: dict) -> dict:
    """Expose stable paper-only FX fields alongside legacy normalization data."""
    output = dict(row)
    quote = str(output.get("quote") or "").upper() or None
    venue = str(output.get("venue") or "").upper()
    is_local_quote = bool(venue in LOCAL_FIAT_CEX_VENUES and quote in REGIONAL_FIAT_QUOTES)
    raw_last = as_float(output.get("last"), None)
    normalized_last = as_float(output.get("usd_normalized_last"), None)
    bid = as_float(output.get("bid"), None)
    ask = as_float(output.get("ask"), None)
    native_mid = (
        (bid + ask) / 2.0
        if bid is not None and ask is not None and bid > 0 and ask > 0 and bid <= ask
        else raw_last
    )
    fx_to_usd = None
    if quote in USD_LIKE_QUOTES:
        fx_to_usd = 1.0
    elif raw_last is not None and raw_last > 0 and normalized_last is not None and normalized_last > 0:
        # Both external FX and same-venue USDT references express local quote
        # units per USD, so the conversion to USD is the observed ratio.
        fx_to_usd = normalized_last / raw_last
    fx_age_seconds = as_float(output.get("fx_age_seconds"), None)
    output.update(
        {
            "quote_ccy": quote,
            "fx_to_usd": round(fx_to_usd, 12) if fx_to_usd is not None else None,
            "fx_age_minutes": round(fx_age_seconds / 60.0, 3) if fx_age_seconds is not None else None,
            "normalized_mid_usd": (
                round(native_mid * fx_to_usd, 12)
                if native_mid is not None and native_mid > 0 and fx_to_usd is not None
                else None
            ),
            "premium_vs_reference_bps": output.get("premium_vs_reference_bps"),
            "local_quote_flag": is_local_quote,
        }
    )
    return output


def _annotate_local_quote_premiums(observations: list[dict]) -> list[dict]:
    """Add leave-local-venue-out premium telemetry without affecting eligibility."""
    reference_prices: dict[str, list[float]] = collections.defaultdict(list)
    for row in observations:
        if (
            row.get("data_status") == "reachable"
            and row.get("market_type") == "spot"
            and not row.get("local_quote_flag")
        ):
            normalized_mid = as_float(row.get("normalized_mid_usd"), None)
            if row.get("comparison_key") and normalized_mid is not None and normalized_mid > 0:
                reference_prices[str(row["comparison_key"])].append(normalized_mid)

    annotated = []
    for row in observations:
        output = dict(row)
        reference = reference_prices.get(str(output.get("comparison_key") or ""), [])
        normalized_mid = as_float(output.get("normalized_mid_usd"), None)
        if output.get("local_quote_flag") and reference and normalized_mid is not None and normalized_mid > 0:
            output["premium_vs_reference_bps"] = round(bps(normalized_mid, statistics.median(reference)), 3)
        else:
            output["premium_vs_reference_bps"] = None
        annotated.append(output)
    return annotated


def _normalize_regional_quotes(
    observations: list[dict],
    fx_references: dict[str, dict] | None = None,
    policy: dict | None = None,
) -> list[dict]:
    fx_references = fx_references or {}
    policy = policy or DEFAULT_REGISTRY["filters"]
    normalization_enabled = bool(policy.get("regional_fx_normalization_enabled", True))
    require_fresh_reference = bool(policy.get("regional_fx_require_fresh_reference", True))
    max_fx_age_seconds = float(policy.get("regional_fx_max_age_seconds", 21_600))
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
        quote = str(output.get("quote") or "").upper()
        base = str(output.get("base") or "").upper()
        last = float(output.get("last") or 0.0)
        output.update(
            {
                "native_quote_currency": quote or None,
                "canonical_quote_currency": None,
                "canonical_normalized_price": None,
                "fx_source": None,
                "fx_age_seconds": None,
                "suppression_reason": None,
                "paper_only_quote_normalization": True,
                "conversion_path_validated": False,
                "quote_ranking_eligible": True,
                "quote_ranking_reason": None,
                "product_metadata_validated": bool(
                    base
                    and quote
                    and base != quote
                    and output.get("symbol")
                    and output.get("instrument_id")
                    and last > 0
                ),
            }
        )
        output["comparison_price"] = None
        if not output["product_metadata_validated"]:
            _set_quote_suppression(output, "invalid_product_metadata", "invalid_product_metadata")
            normalized.append(output)
            continue
        if quote in USD_LIKE_QUOTES:
            output["usd_normalized_last"] = last
            output["canonical_normalized_price"] = last
            output["canonical_quote_currency"] = "USD"
            output["comparison_price"] = last
            output["quote_normalization_status"] = "usd_like"
            output["quote_normalization_source"] = quote
            output["fx_source"] = f"identity:{quote}"
            output["fx_age_seconds"] = 0.0
            output["conversion_path_validated"] = True
            output["local_quote_observe_only"] = False
        elif quote in REGIONAL_FIAT_QUOTES:
            if not normalization_enabled:
                _set_quote_suppression(output, "normalization_disabled", "fx_normalization_disabled")
                normalized.append(output)
                continue
            fx = by_venue_quote.get((str(output.get("venue")), quote))
            if fx and last > 0 and float(fx.get("last") or 0.0) > 0:
                fx_price = float(fx["last"])
                fx_age = _reference_age_seconds(fx)
                output["quote_normalization_source"] = fx.get("instrument_id")
                output["fx_source"] = fx.get("instrument_id")
                output["fx_age_seconds"] = round(fx_age, 3) if fx_age is not None else None
                output["usd_normalized_last"] = round(last / fx_price, 12)
                output["canonical_normalized_price"] = output["usd_normalized_last"]
                output["canonical_quote_currency"] = "USD"
                output["comparison_price"] = output["usd_normalized_last"]
                output["conversion_path_validated"] = True
                output["local_quote_observe_only"] = base in STABLE_OR_FIAT_BASES
                if output.get("quote_volume_24h") is not None:
                    output["local_quote_volume_24h"] = output.get("quote_volume_24h")
                    output["quote_volume_24h"] = round(float(output["quote_volume_24h"]) / fx_price, 3)
                if require_fresh_reference and (fx_age is None or fx_age > max_fx_age_seconds):
                    _set_stale_fx_ranking_state(output)
                else:
                    output["quote_normalization_status"] = "same_venue_stablecoin_reference"
            elif quote in fx_references and last > 0 and float(fx_references[quote].get("rate") or 0.0) > 0:
                ref = fx_references[quote]
                fx_price = float(ref["rate"])
                fx_age = as_float(ref.get("age_seconds"), None)
                output["quote_normalization_source"] = f"{ref.get('provider')}:USD/{quote}"
                output["fx_source"] = output["quote_normalization_source"]
                output["fx_age_seconds"] = round(float(fx_age), 3) if fx_age is not None else None
                output["fx_reference_rate"] = fx_price
                output["fx_reference_provider"] = ref.get("provider")
                output["fx_reference_age_seconds"] = output["fx_age_seconds"]
                output["fx_reference_source_url"] = ref.get("source_url")
                output["usd_normalized_last"] = round(last / fx_price, 12)
                output["canonical_normalized_price"] = output["usd_normalized_last"]
                output["canonical_quote_currency"] = "USD"
                output["comparison_price"] = output["usd_normalized_last"]
                output["conversion_path_validated"] = True
                output["local_quote_observe_only"] = base in STABLE_OR_FIAT_BASES
                if output.get("quote_volume_24h") is not None:
                    output["local_quote_volume_24h"] = output.get("quote_volume_24h")
                    output["quote_volume_24h"] = round(float(output["quote_volume_24h"]) / fx_price, 3)
                if require_fresh_reference and (
                    bool(ref.get("stale"))
                    or fx_age is None
                    or float(fx_age) > max_fx_age_seconds
                ):
                    _set_stale_fx_ranking_state(output)
                else:
                    output["quote_normalization_status"] = "external_fx_reference"
            else:
                output["quote_normalization_source"] = None
                _set_quote_suppression(
                    output,
                    "missing_same_venue_stablecoin_reference",
                    "missing_fx_conversion_path",
                )
                output["notes"] = [
                    *list(output.get("notes") or []),
                    "Regional fiat quote observed, but no same-venue stablecoin/fiat reference was available.",
                ]
        else:
            _set_quote_suppression(output, "unsupported_quote", "unmatched_quote_currency")
        normalized.append(output)
    return [_local_quote_normalization_telemetry(row) for row in normalized]


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
        venue = str(target.get("venue") or "").upper()
        if venue == "BYBIT_SPOT" and not _bybit_direct_probe_due(
            conn,
            int(cfg.get("bybit_region_blocked_probe_interval_seconds", 300)),
        ):
            result = {
                "ok": False,
                "data_status": "blocked",
                "received_at": _utc_now(),
                "http_status": "probe_deferred_after_network_region_blocked",
                "latency_ms": 0.0,
                "payload": None,
            }
        else:
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
                if venue == "BYBIT_SPOT" and (
                    "403" in str(result.get("http_status") or "")
                    or "451" in str(result.get("http_status") or "")
                    or "network_region_blocked" in str(result.get("http_status") or "")
                    or "probe_deferred" in str(result.get("http_status") or "")
                ):
                    parsed[0]["access_blocker_code"] = "network_region_blocked"
                    parsed[0]["notes"].append(
                        "Direct public BYBIT access is region/IP blocked while the VPN is off; this is not an adapter, route, or strategy failure."
                    )
                else:
                    parsed[0]["notes"].append("Public endpoint blocked from this machine; captured as access evidence.")
        observations.extend(_finalize_observation(row) for row in parsed)
    fx_references = get_regional_fx_references(conn, settings or {})
    observations = _normalize_regional_quotes(
        observations,
        fx_references=fx_references,
        policy=registry.get("filters", {}),
    )
    observations = _annotate_local_quote_premiums(observations)
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
        if not bool(row.get("quote_ranking_eligible", True)):
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
        if not bool(row.get("quote_ranking_eligible", True)):
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


DEFAULT_FRONTIER_DISLOCATION_QUALITY_POLICY = {
    "enabled": True,
    "paper_only": True,
    "reference_breadth_target": 3,
    "max_leave_one_out_shift_bps": 25.0,
    "freshness_target_seconds": 30.0,
    "freshness_max_seconds": 90.0,
    "ranking_quality_weight": 0.35,
    "reference_breadth_weight": 0.25,
    "reference_stability_weight": 0.30,
    "cost_efficiency_weight": 0.30,
    "freshness_weight": 0.15,
}

# This model is intentionally a paper-routing and ranking control.  A failed
# primary admission remains a priceable counterfactual observation so that the
# paper loop can measure whether the missing cost estimate was conservative.
DEFAULT_FRONTIER_EFFECTIVE_EDGE_POLICY = {
    "enabled": True,
    "paper_only": True,
    "minimum_effective_edge_bps": 0.0,
    "max_freshness_age_seconds": 90.0,
    "freshness_penalty_window_seconds": 30.0,
    "freshness_penalty_bps_per_window": 2.0,
    "max_freshness_penalty_bps": 12.0,
    "min_liquidity_score": 0.35,
    "min_venue_reliability_score": 0.60,
    "max_venue_reliability_penalty_bps": 5.0,
    "external_quote_conversion_cost_bps": 4.0,
    "synthetic_proxy_drag_bps": 4.0,
    "allocation_edge_bps_cap": 24.0,
    "synthetic_allocation_cap": 0.25,
    "low_reliability_allocation_cap": 0.50,
    "low_reliability_score": 0.70,
    "counterfactual_allocation_multiplier": 0.25,
}

# Short frontier spot observations can look attractive on a gross venue
# deviation while still carrying distinctly asymmetric execution costs.  This
# stays a paper-only ranking/allocation model: a negative result moves the
# observation to counterfactual routing instead of suppressing its emission.
DEFAULT_FRONTIER_SHORT_COST_DECOMPOSITION_POLICY = {
    "enabled": True,
    "paper_only": True,
    "venue_quality_penalty_bps_cap": 8.0,
    "missing_venue_quality_penalty_bps": 6.0,
    "synthetic_route_penalty_bps": 6.0,
    "unconfirmed_borrow_proxy_penalty_bps": 6.0,
    "allocation_edge_bps_cap": 24.0,
}

# This is deliberately a paper-research context model, not an entry gate.
# Weak frontier-short conditions stay emitted, but are visibly downranked and
# routed as smaller counterfactual experiments so their guard value can be
# measured instead of inferred from a suppression rule.
DEFAULT_FRONTIER_SHORT_MARKET_CONTEXT_POLICY = {
    "enabled": True,
    "paper_only": True,
    "minimum_reference_breadth": 3,
    "max_spread_plus_slippage_to_edge_ratio": 1.0,
    "min_broader_risk_off_ratio": 0.60,
    "minimum_local_reversal_bps": 1.0,
    "ranking_weight": 0.20,
    "minimum_counterfactual_allocation_multiplier": 0.25,
}

# Quote context is intentionally a paper-only ordering signal. A weak public
# book remains observable for counterfactual measurement rather than becoming
# a new admission or route gate.
DEFAULT_FRONTIER_QUOTE_CONTEXT_POLICY = {
    "enabled": True,
    "paper_only": True,
    "stale_quote_age_ms": 90_000.0,
    "wide_spread_bps": 12.0,
    "shallow_depth_notional": 1_000.0,
    "stale_penalty_points_cap": 8.0,
    "wide_penalty_points_cap": 6.0,
    "shallow_penalty_points_cap": 4.0,
    "synthetic_route_penalty_points": 3.0,
    "total_penalty_points_cap": 16.0,
}


def _frontier_dislocation_quality_policy(settings: dict) -> dict:
    policy = dict(DEFAULT_FRONTIER_DISLOCATION_QUALITY_POLICY)
    configured = settings.get("frontier_crypto_adapter", {}).get("dislocation_quality", {})
    if isinstance(configured, dict):
        policy.update({key: value for key, value in configured.items() if value is not None})
    return policy


def _frontier_effective_edge_policy(settings: dict) -> dict:
    policy = dict(DEFAULT_FRONTIER_EFFECTIVE_EDGE_POLICY)
    configured = settings.get("frontier_crypto_adapter", {}).get("effective_edge", {})
    if isinstance(configured, dict):
        policy.update({key: value for key, value in configured.items() if value is not None})
    if configured is False:
        policy["enabled"] = False
    return policy


def _frontier_short_cost_decomposition_policy(settings: dict) -> dict:
    policy = dict(DEFAULT_FRONTIER_SHORT_COST_DECOMPOSITION_POLICY)
    configured = settings.get("frontier_crypto_adapter", {}).get("short_cost_decomposition", {})
    if isinstance(configured, dict):
        policy.update({key: value for key, value in configured.items() if value is not None})
    if configured is False:
        policy["enabled"] = False
    return policy


def _frontier_short_market_context_policy(settings: dict) -> dict:
    policy = dict(DEFAULT_FRONTIER_SHORT_MARKET_CONTEXT_POLICY)
    configured = settings.get("frontier_crypto_adapter", {}).get("short_market_context", {})
    if isinstance(configured, dict):
        policy.update({key: value for key, value in configured.items() if value is not None})
    if configured is False:
        policy["enabled"] = False
    return policy


def _frontier_quote_context_policy(settings: dict) -> dict:
    policy = dict(DEFAULT_FRONTIER_QUOTE_CONTEXT_POLICY)
    configured = settings.get("frontier_crypto_adapter", {}).get("paper_quote_context", {})
    if isinstance(configured, dict):
        policy.update({key: value for key, value in configured.items() if value is not None})
    if configured is False:
        policy["enabled"] = False
    return policy


def _annotate_frontier_quote_context(candidate: dict, observation: dict, settings: dict) -> dict:
    """Record real quote context and a capped ranking penalty without blocking paper."""

    policy = _frontier_quote_context_policy(settings)
    quote_age_ms = as_float(observation.get("quote_age_ms"), None)
    if quote_age_ms is None:
        freshness_age = as_float(candidate.get("freshness_age_seconds"), None)
        quote_age_ms = freshness_age * 1000.0 if freshness_age is not None else None
    quote_age_ms = max(0.0, quote_age_ms) if quote_age_ms is not None else None
    bid_depth, bid_depth_basis = _top_of_book_notional_usd(observation, "bid")
    ask_depth, ask_depth_basis = _top_of_book_notional_usd(observation, "ask")
    depth_levels = [value for value in (bid_depth, ask_depth) if value is not None]
    top_depth = min(depth_levels) if depth_levels else None
    depth_basis = "two_sided_top_level" if len(depth_levels) == 2 else (
        bid_depth_basis if bid_depth is not None else ask_depth_basis if ask_depth is not None else "unavailable"
    )
    spread = max(0.0, as_float(candidate.get("spread_bps"), 0.0) or 0.0)
    route_text = " ".join(
        str(candidate.get(field) or "")
        for field in ("route_id", "paper_route_status", "route_status", "paper_execution_semantics")
    ).lower()
    synthetic_route = bool(candidate.get("synthetic_research_paper")) or any(
        token in route_text for token in ("synthetic", "proxy", "counterfactual")
    )
    stale_threshold = max(1.0, float(policy["stale_quote_age_ms"]))
    wide_threshold = max(0.001, float(policy["wide_spread_bps"]))
    shallow_threshold = max(1.0, float(policy["shallow_depth_notional"]))
    stale_cap = max(0.0, float(policy["stale_penalty_points_cap"]))
    wide_cap = max(0.0, float(policy["wide_penalty_points_cap"]))
    shallow_cap = max(0.0, float(policy["shallow_penalty_points_cap"]))
    synthetic_penalty = max(0.0, float(policy["synthetic_route_penalty_points"])) if synthetic_route else 0.0
    stale_penalty = (
        stale_cap * min(1.0, max(0.0, quote_age_ms - stale_threshold) / stale_threshold)
        if quote_age_ms is not None
        else 0.0
    )
    wide_penalty = wide_cap * min(1.0, max(0.0, spread - wide_threshold) / wide_threshold)
    shallow_penalty = (
        shallow_cap * min(1.0, max(0.0, shallow_threshold - top_depth) / shallow_threshold)
        if top_depth is not None
        else 0.0
    )
    total_cap = max(0.0, float(policy["total_penalty_points_cap"]))
    ranking_penalty = min(
        total_cap,
        stale_penalty + wide_penalty + shallow_penalty + synthetic_penalty,
    ) if bool(policy.get("enabled", True)) else 0.0
    quote_penalty = stale_penalty + wide_penalty + shallow_penalty
    quote_quality_score = max(
        0.0,
        100.0 - 100.0 * min(1.0, quote_penalty / max(1.0, stale_cap + wide_cap + shallow_cap)),
    )
    route_quality_score = max(
        0.0,
        100.0 - 100.0 * min(1.0, synthetic_penalty / max(1.0, total_cap)),
    )
    health_score = as_float(candidate.get("venue_health_score"), None)
    if health_score is None:
        health_score = as_float(candidate.get("quality_score"), 50.0) or 50.0
    health_score = max(0.0, min(100.0, health_score))
    candidate["quote_age_ms"] = round(quote_age_ms, 3) if quote_age_ms is not None else None
    candidate["top_of_book_depth_notional"] = round(top_depth, 3) if top_depth is not None else None
    candidate["top_of_book_depth_basis"] = depth_basis
    candidate["synthetic_route_flag"] = synthetic_route
    candidate["venue_score"] = round((health_score * 0.50) + (quote_quality_score * 0.35) + (route_quality_score * 0.15), 3)
    candidate["staleness_reason"] = (
        "quote_age_exceeds_ranking_threshold"
        if quote_age_ms is not None and quote_age_ms > stale_threshold
        else None
    )
    candidate["frontier_quote_context"] = {
        "paper_only": True,
        "emission_action": "ranked_not_blocked",
        "quote_quality_score": round(quote_quality_score, 3),
        "route_quality_score": round(route_quality_score, 3),
        "stale_penalty_points": round(stale_penalty, 3),
        "wide_penalty_points": round(wide_penalty, 3),
        "shallow_penalty_points": round(shallow_penalty, 3),
        "synthetic_route_penalty_points": round(synthetic_penalty, 3),
        "ranking_penalty_points": round(ranking_penalty, 3),
        "ranking_penalty_points_cap": round(total_cap, 3),
    }
    candidate["quote_context_ranking_penalty"] = round(ranking_penalty, 3)
    return candidate


def _frontier_short_route_support_score(status: str) -> float:
    normalized = str(status or "").strip().lower()
    if normalized == "supported":
        return 1.0
    if normalized == "conditional":
        return 0.7
    if normalized == "unsupported":
        return 0.2
    if normalized == "unspecified":
        return 0.35
    return 0.3


def _annotate_frontier_short_paper_diagnostics(
    candidate: dict,
    observation: dict,
    settings: dict,
) -> dict:
    """Expose paper-only short diagnostics without changing candidate eligibility."""

    if str(candidate.get("direction") or "").strip().lower() != "short_frontier_spot":
        return candidate

    route_intelligence = candidate.get("frontier_short_spot_route_intelligence")
    route_intelligence = dict(route_intelligence) if isinstance(route_intelligence, dict) else {}
    route_telemetry = route_intelligence.get("route_economics_telemetry")
    route_telemetry = dict(route_telemetry) if isinstance(route_telemetry, dict) else {}
    registry = candidate.get("paper_route_registry")
    registry = dict(registry) if isinstance(registry, dict) else {}
    route_costs = candidate.get("paper_route_estimated_cost_bps")
    route_costs = dict(route_costs) if isinstance(route_costs, dict) else {}

    quote_policy = _frontier_quote_context_policy(settings)
    stale_threshold_ms = max(1.0, float(quote_policy["stale_quote_age_ms"]))
    shallow_threshold_usd = max(1.0, float(quote_policy["shallow_depth_notional"]))
    wide_threshold_bps = max(0.001, float(quote_policy["wide_spread_bps"]))

    quote_age_ms = as_float(candidate.get("quote_age_ms"), None)
    if quote_age_ms is None:
        freshness_age_seconds = as_float(candidate.get("freshness_age_seconds"), None)
        quote_age_ms = freshness_age_seconds * 1000.0 if freshness_age_seconds is not None else None
    quote_age_ms = max(0.0, quote_age_ms) if quote_age_ms is not None else None

    spread_bps_value = as_float(candidate.get("spread_bps"), None)
    if spread_bps_value is None:
        spread_bps_value = as_float(route_telemetry.get("spread_bps"), None)
    spread_bps_value = max(0.0, spread_bps_value) if spread_bps_value is not None else None

    top_depth_usd = as_float(candidate.get("top_of_book_depth_notional"), None)
    if top_depth_usd is None:
        bid_depth, _ = _top_of_book_notional_usd(observation, "bid")
        ask_depth, _ = _top_of_book_notional_usd(observation, "ask")
        depth_candidates = [value for value in (bid_depth, ask_depth) if value is not None]
        top_depth_usd = min(depth_candidates) if depth_candidates else None

    entry_slippage_bps = as_float(candidate.get("entry_slippage_bps_estimate"), None)
    exit_slippage_bps = as_float(candidate.get("exit_slippage_bps_estimate"), None)
    estimated_slippage_bps = max(
        value for value in (entry_slippage_bps, exit_slippage_bps) if value is not None
    ) if any(value is not None for value in (entry_slippage_bps, exit_slippage_bps)) else as_float(
        route_telemetry.get("slippage_estimate_bps"), None
    )

    support_status = str(
        candidate.get("paper_route_registry_status")
        or registry.get("support_status")
        or candidate.get("route_status")
        or "unknown"
    ).strip().lower()
    shortability_status = str(
        route_telemetry.get("shortability_status")
        or route_intelligence.get("borrow_availability")
        or "unknown"
    ).strip().lower()
    is_shortable_paper = bool(
        support_status == "supported"
        or shortability_status == "observed"
    )

    borrow_proxy_bps = as_float(route_costs.get("borrow"), None)
    if borrow_proxy_bps is None:
        borrow_proxy_bps = as_float(
            ((route_telemetry.get("borrow") or {}) if isinstance(route_telemetry.get("borrow"), dict) else {}).get(
                "estimated_fee_bps"
            ),
            None,
        )

    route_health = candidate.get("route_health_confirmation")
    route_health = dict(route_health) if isinstance(route_health, dict) else {}
    counterfactual_route = route_health.get("mode") == "conservative_counterfactual_route"
    synthetic_short_method = None
    if not is_shortable_paper:
        if support_status == "conditional":
            synthetic_short_method = "conditional_margin_borrow_assumption"
        elif candidate.get("synthetic_route_flag") or counterfactual_route:
            synthetic_short_method = "counterfactual_paper_short"
        elif borrow_proxy_bps is not None:
            synthetic_short_method = "borrow_cost_proxy_assumption"
        else:
            synthetic_short_method = "unconfirmed_short_route_assumption"

    route_id = str(candidate.get("route_id") or observation.get("route_id") or "").strip()
    route_path = [
        route_id or "public_spot_quote",
        "native_margin_borrow_short" if is_shortable_paper else str(synthetic_short_method or "paper_short_assumption"),
        "paper_cover",
    ]

    health_score = as_float(candidate.get("venue_score"), None)
    if health_score is None:
        health_score = as_float(candidate.get("venue_health_score"), None)
    if health_score is None:
        health_score = as_float(candidate.get("quality_score"), 50.0)
    health_component = max(0.0, min(1.0, (health_score or 0.0) / 100.0))
    route_component = _frontier_short_route_support_score(support_status)
    freshness_component = (
        max(0.0, min(1.0, stale_threshold_ms / max(stale_threshold_ms, quote_age_ms)))
        if quote_age_ms is not None
        else 0.35
    )
    depth_component = (
        max(0.0, min(1.0, top_depth_usd / shallow_threshold_usd))
        if top_depth_usd is not None
        else 0.35
    )
    spread_component = (
        max(0.0, min(1.0, wide_threshold_bps / max(wide_threshold_bps, spread_bps_value)))
        if spread_bps_value is not None
        else 0.35
    )
    venue_confidence_score = round(
        max(
            0.0,
            min(
                1.0,
                (
                    health_component * 0.35
                    + route_component * 0.30
                    + freshness_component * 0.15
                    + depth_component * 0.10
                    + spread_component * 0.10
                ),
            ),
        ),
        6,
    )

    diagnostics = {
        "paper_only": True,
        "emission_action": "diagnostics_and_ranking_only",
        "is_shortable_paper": is_shortable_paper,
        "synthetic_short_method": synthetic_short_method,
        "route_path": route_path,
        "best_bid_ask_spread_bps": round(spread_bps_value, 6) if spread_bps_value is not None else None,
        "top_of_book_depth_usd": round(top_depth_usd, 6) if top_depth_usd is not None else None,
        "estimated_slippage_bps": round(estimated_slippage_bps, 6) if estimated_slippage_bps is not None else None,
        "quote_age_ms": round(quote_age_ms, 6) if quote_age_ms is not None else None,
        "market_data_freshness_ms": round(quote_age_ms, 6) if quote_age_ms is not None else None,
        "borrow_proxy_bps_if_applicable": round(borrow_proxy_bps, 6) if borrow_proxy_bps is not None else None,
        "venue_confidence_score": venue_confidence_score,
        "route_support_status": support_status,
        "shortability_status": shortability_status,
    }
    candidate["frontier_short_paper_diagnostics"] = diagnostics
    for field_name in (
        "is_shortable_paper",
        "synthetic_short_method",
        "route_path",
        "best_bid_ask_spread_bps",
        "top_of_book_depth_usd",
        "estimated_slippage_bps",
        "quote_age_ms",
        "market_data_freshness_ms",
        "borrow_proxy_bps_if_applicable",
        "venue_confidence_score",
    ):
        candidate[field_name] = diagnostics[field_name]
    return candidate


def _paper_venue_diagnostics_lookup(candidate: dict, observation: dict, *keys: str):
    containers = [candidate, observation]
    nested_names = (
        "route_quality",
        "venue_health",
        "instrument_metadata",
        "venue_constraints",
        "frontier_short_paper_diagnostics",
    )
    for container in (candidate, observation):
        if not isinstance(container, dict):
            continue
        for nested_name in nested_names:
            nested = container.get(nested_name)
            if isinstance(nested, dict):
                containers.append(nested)
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if value not in (None, "", [], {}, (), set(), frozenset()):
                return value
    return None


def _paper_venue_diagnostics_timestamp(candidate: dict, observation: dict) -> str | None:
    for key in (
        "quote_timestamp",
        "freshness_timestamp",
        "last_trade_timestamp",
        "book_observed_at",
        "observed_at",
        "seen_at",
        "last_checked_at",
    ):
        parsed = _paper_only_parse_timestamp(_paper_venue_diagnostics_lookup(candidate, observation, key))
        if parsed is not None:
            return parsed.isoformat()
    return None


def _paper_venue_diagnostics_venue_symbol(candidate: dict, observation: dict):
    for container in (candidate, observation):
        if not isinstance(container, dict):
            continue
        metadata = container.get("instrument_metadata")
        if isinstance(metadata, dict):
            value = metadata.get("venue_symbol")
            if value not in (None, "", [], {}, (), set(), frozenset()):
                return value
        value = container.get("venue_symbol")
        if value not in (None, "", [], {}, (), set(), frozenset()):
            return value
    return None


def _paper_venue_diagnostics_confidence(candidate: dict, observation: dict) -> tuple[float | None, str | None]:
    direct = as_float(
        _paper_venue_diagnostics_lookup(
            candidate,
            observation,
            "route_mapping_confidence",
            "mapping_confidence",
            "symbol_mapping_confidence",
        ),
        None,
    )
    if direct is not None:
        return max(0.0, min(1.0, direct)), "observed_mapping_confidence"
    venue_symbol = _paper_venue_diagnostics_venue_symbol(candidate, observation)
    symbol = _paper_venue_diagnostics_lookup(candidate, observation, "symbol")
    if venue_symbol and symbol:
        return 1.0, "parser_symbol_normalization"
    return None, None


def _paper_venue_diagnostics_access_status(candidate: dict, observation: dict, prefix: str) -> tuple[str, bool]:
    keys = (
        f"{prefix}_status",
        f"{prefix}_enabled",
        f"{prefix}_available",
        f"{prefix}_open",
        f"{prefix}s_enabled",
        f"{prefix}s_available",
    )
    raw_value = _paper_venue_diagnostics_lookup(candidate, observation, *keys)
    if raw_value is None:
        return "unknown", False
    if isinstance(raw_value, bool):
        return ("available" if raw_value else "unavailable"), True
    text = str(raw_value).strip().lower()
    if text in {"true", "1", "yes", "open", "enabled", "available", "supported"}:
        return "available", True
    if text in {"false", "0", "no", "closed", "disabled", "unavailable", "unsupported", "halted"}:
        return "unavailable", True
    return text or "unknown", True


def _normalize_paper_network_identifiers(candidate: dict, observation: dict) -> list[str]:
    raw_values = []
    for key in ("network", "networks", "chain", "chains", "chain_id", "chain_ids"):
        value = _paper_venue_diagnostics_lookup(candidate, observation, key)
        if value not in (None, "", [], {}, (), set(), frozenset()):
            raw_values.append(value)
    identifiers = []
    seen = set()

    def _append(value):
        token = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip().upper()).strip("_")
        if token and token not in seen:
            seen.add(token)
            identifiers.append(token)

    for value in raw_values:
        if isinstance(value, dict):
            for nested_key in ("network", "chain", "chain_id", "id", "name"):
                nested_value = value.get(nested_key)
                if nested_value not in (None, "", [], {}, (), set(), frozenset()):
                    _append(nested_value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if isinstance(item, dict):
                    for nested_key in ("network", "chain", "chain_id", "id", "name"):
                        nested_value = item.get(nested_key)
                        if nested_value not in (None, "", [], {}, (), set(), frozenset()):
                            _append(nested_value)
                else:
                    _append(item)
        else:
            _append(value)
    return identifiers


def _build_paper_venue_diagnostics(candidate: dict, observation: dict) -> dict:
    quote_timestamp = _paper_venue_diagnostics_timestamp(candidate, observation)
    quote_age_ms = as_float(
        _paper_venue_diagnostics_lookup(candidate, observation, "quote_age_ms", "market_data_freshness_ms"),
        None,
    )
    if quote_age_ms is None:
        freshness_age_seconds = as_float(
            _paper_venue_diagnostics_lookup(candidate, observation, "freshness_age_seconds"),
            None,
        )
        if freshness_age_seconds is not None:
            quote_age_ms = freshness_age_seconds * 1000.0
    quote_age_seconds = round(quote_age_ms / 1000.0, 6) if quote_age_ms is not None else None
    best_bid = as_float(_paper_venue_diagnostics_lookup(candidate, observation, "best_bid", "bid"), None)
    best_ask = as_float(_paper_venue_diagnostics_lookup(candidate, observation, "best_ask", "ask"), None)
    spread_bps = as_float(
        _paper_venue_diagnostics_lookup(
            candidate,
            observation,
            "best_bid_ask_spread_bps",
            "spread_bps",
            "effective_spread_bps",
        ),
        None,
    )
    route_quality = candidate.get("route_quality")
    if not isinstance(route_quality, dict):
        route_quality = observation.get("route_quality")
    route_quality = route_quality if isinstance(route_quality, dict) else {}
    baseline_spread_bps = as_float(route_quality.get("venue_spread_baseline_bps"), None)
    spread_ratio = as_float(route_quality.get("spread_to_baseline_ratio"), None)
    spread_volatility_proxy_bps = (
        round(abs(spread_bps - baseline_spread_bps), 6)
        if spread_bps is not None and baseline_spread_bps is not None
        else None
    )
    spread_volatility_proxy_ratio = (
        round(abs(spread_ratio - 1.0), 6)
        if spread_ratio is not None
        else None
    )

    bid_notional_usd, bid_depth_basis = _top_of_book_notional_usd(observation, "bid")
    ask_notional_usd, ask_depth_basis = _top_of_book_notional_usd(observation, "ask")
    depth_candidates = [value for value in (bid_notional_usd, ask_notional_usd) if value is not None]
    top_depth_usd = min(depth_candidates) if depth_candidates else as_float(
        _paper_venue_diagnostics_lookup(candidate, observation, "top_of_book_depth_usd", "top_of_book_depth_notional"),
        None,
    )
    depth_basis = (
        "two_sided_top_level"
        if bid_notional_usd is not None and ask_notional_usd is not None
        else bid_depth_basis if bid_notional_usd is not None else ask_depth_basis
    )
    slippage_proxy_bps = max(
        (
            value
            for value in (
                as_float(_paper_venue_diagnostics_lookup(candidate, observation, "estimated_slippage_bps"), None),
                as_float(_paper_venue_diagnostics_lookup(candidate, observation, "entry_slippage_bps_estimate"), None),
                as_float(_paper_venue_diagnostics_lookup(candidate, observation, "exit_slippage_bps_estimate"), None),
            )
            if value is not None
        ),
        default=None,
    )
    venue_health_score = as_float(_paper_venue_diagnostics_lookup(candidate, observation, "venue_health_score"), None)
    venue_confidence_score = as_float(
        _paper_venue_diagnostics_lookup(candidate, observation, "venue_confidence_score"),
        None,
    )
    if venue_confidence_score is None:
        venue_score = as_float(_paper_venue_diagnostics_lookup(candidate, observation, "venue_score"), None)
        if venue_score is not None:
            venue_confidence_score = max(0.0, min(1.0, venue_score / 100.0))
        elif venue_health_score is not None:
            venue_confidence_score = max(0.0, min(1.0, venue_health_score / 100.0))
    mapping_confidence, mapping_source = _paper_venue_diagnostics_confidence(candidate, observation)
    deposit_status, deposit_public = _paper_venue_diagnostics_access_status(candidate, observation, "deposit")
    withdrawal_status, withdrawal_public = _paper_venue_diagnostics_access_status(candidate, observation, "withdrawal")
    network_identifiers = _normalize_paper_network_identifiers(candidate, observation)
    venue_symbol = _paper_venue_diagnostics_venue_symbol(candidate, observation)
    freshness_state = _paper_venue_diagnostics_lookup(candidate, observation, "freshness_state")
    freshness_basis = _paper_venue_diagnostics_lookup(candidate, observation, "freshness_basis")
    latency_ms = as_float(_paper_venue_diagnostics_lookup(candidate, observation, "latency_ms"), None)
    depth_latency_ms = as_float(_paper_venue_diagnostics_lookup(candidate, observation, "depth_latency_ms"), None)

    missing_data_flags = []
    if best_bid is None or best_ask is None:
        missing_data_flags.append("top_of_book_missing")
    if quote_timestamp is None:
        missing_data_flags.append("quote_timestamp_missing")
    if quote_age_seconds is None:
        missing_data_flags.append("quote_age_missing")
    if baseline_spread_bps is None:
        missing_data_flags.append("spread_baseline_missing")
    if top_depth_usd is None:
        missing_data_flags.append("displayed_depth_missing")
    if slippage_proxy_bps is None:
        missing_data_flags.append("slippage_proxy_missing")
    if freshness_state in (None, ""):
        missing_data_flags.append("freshness_state_missing")
    if venue_health_score is None:
        missing_data_flags.append("venue_health_missing")
    if not deposit_public:
        missing_data_flags.append("deposit_status_unknown")
    if not withdrawal_public:
        missing_data_flags.append("withdrawal_status_unknown")
    if not network_identifiers:
        missing_data_flags.append("network_identifiers_missing")
    if venue_symbol in (None, ""):
        missing_data_flags.append("venue_symbol_missing")
    if mapping_confidence is None:
        missing_data_flags.append("symbol_mapping_confidence_missing")

    return {
        "paper_only": True,
        "diagnostic_version": "frontier_paper_venue_diagnostics_v1",
        "emission_action": "diagnostics_ranking_sizing_only",
        "venue": str(candidate.get("venue") or observation.get("venue") or ""),
        "inst_id": str(candidate.get("inst_id") or observation.get("instrument_id") or ""),
        "symbol": str(candidate.get("symbol") or observation.get("symbol") or ""),
        "top_of_book": {
            "best_bid": round(best_bid, 12) if best_bid is not None else None,
            "best_ask": round(best_ask, 12) if best_ask is not None else None,
            "quote_timestamp": quote_timestamp,
            "quote_age_ms": round(quote_age_ms, 6) if quote_age_ms is not None else None,
            "quote_age_seconds": quote_age_seconds,
        },
        "spread_statistics": {
            "current_spread_bps": round(spread_bps, 6) if spread_bps is not None else None,
            "venue_spread_baseline_bps": round(baseline_spread_bps, 6) if baseline_spread_bps is not None else None,
            "spread_to_baseline_ratio": round(spread_ratio, 6) if spread_ratio is not None else None,
            "spread_volatility_proxy_bps": spread_volatility_proxy_bps,
            "spread_volatility_proxy_ratio": spread_volatility_proxy_ratio,
            "spread_volatility_method": (
                "baseline_deviation_proxy"
                if spread_volatility_proxy_bps is not None or spread_volatility_proxy_ratio is not None
                else "unavailable"
            ),
        },
        "displayed_depth": {
            "bid_notional_usd": round(bid_notional_usd, 6) if bid_notional_usd is not None else None,
            "ask_notional_usd": round(ask_notional_usd, 6) if ask_notional_usd is not None else None,
            "top_of_book_depth_usd": round(top_depth_usd, 6) if top_depth_usd is not None else None,
            "depth_basis": depth_basis,
            "slippage_proxy_bps": round(slippage_proxy_bps, 6) if slippage_proxy_bps is not None else None,
            "depth_to_size_ratio": (
                round(as_float(route_quality.get("depth_to_size_ratio"), None), 6)
                if as_float(route_quality.get("depth_to_size_ratio"), None) is not None
                else None
            ),
        },
        "venue_health": {
            "source_status": str(observation.get("data_status") or candidate.get("data_status") or "unknown"),
            "http_status": observation.get("http_status") or candidate.get("http_status"),
            "latency_ms": round(latency_ms, 6) if latency_ms is not None else None,
            "depth_latency_ms": round(depth_latency_ms, 6) if depth_latency_ms is not None else None,
            "freshness_state": freshness_state,
            "freshness_basis": freshness_basis,
            "freshness_age_seconds": as_float(
                _paper_venue_diagnostics_lookup(candidate, observation, "freshness_age_seconds"),
                None,
            ),
            "venue_health_score": round(venue_health_score, 6) if venue_health_score is not None else None,
            "uptime_status": _paper_venue_diagnostics_lookup(
                candidate,
                observation,
                "uptime_status",
                "availability_status",
                "health_status",
            ),
        },
        "access_status": {
            "deposit_status": deposit_status,
            "withdrawal_status": withdrawal_status,
            "publicly_reported": bool(deposit_public or withdrawal_public),
        },
        "network_normalization": {
            "identifiers": network_identifiers,
            "source": "public_observation" if network_identifiers else None,
        },
        "symbol_mapping": {
            "venue_symbol": venue_symbol,
            "base": candidate.get("base") or observation.get("base"),
            "quote": candidate.get("quote") or observation.get("quote"),
            "mapping_confidence": round(mapping_confidence, 6) if mapping_confidence is not None else None,
            "mapping_source": mapping_source,
        },
        "venue_confidence_score": round(venue_confidence_score, 6) if venue_confidence_score is not None else None,
        "missing_data_flags": missing_data_flags,
    }


def _frontier_reference_peer_rows(
    observation: dict,
    reference_observations: list[dict] | None,
) -> list[dict]:
    """Return one priceable independent spot observation per reference venue."""
    if not reference_observations:
        return []
    local_venue = str(observation.get("venue") or "")
    comparison_key = observation.get("comparison_key")
    best_by_venue: dict[str, dict] = {}
    for row in reference_observations:
        venue = str(row.get("venue") or "")
        if (
            not venue
            or venue == local_venue
            or row.get("comparison_key") != comparison_key
            or row.get("market_type") != "spot"
            or row.get("data_status") != "reachable"
            or row.get("local_quote_observe_only")
            or _comparison_price(row) <= 0
        ):
            continue
        previous = best_by_venue.get(venue)
        if previous is None or float(row.get("quote_volume_24h") or 0.0) > float(
            previous.get("quote_volume_24h") or 0.0
        ):
            best_by_venue[venue] = row
    return list(best_by_venue.values())


def _frontier_dislocation_quality(
    observation: dict,
    reference_price: float | None,
    reference_observations: list[dict] | None,
    round_trip_cost_bps: float,
    gross_edge_bps: float,
    settings: dict,
) -> dict:
    """Score paper dislocations without changing candidate eligibility.

    A thin or unstable reference, high crossing cost, or old quote is recorded
    as ranking evidence.  It is deliberately not a paper-entry gate: paper
    exploration can still measure the counterfactual for every priceable row.
    """
    policy = _frontier_dislocation_quality_policy(settings)
    peers = _frontier_reference_peer_rows(observation, reference_observations)
    peer_prices = [_comparison_price(row) for row in peers]
    peer_count = len(peer_prices)
    breadth_target = max(1, int(policy["reference_breadth_target"]))
    breadth_score = min(100.0, 100.0 * peer_count / breadth_target)

    peer_median = statistics.median(peer_prices) if peer_prices else None
    leave_one_out_shifts = []
    if peer_median is not None:
        for index in range(peer_count):
            remaining = peer_prices[:index] + peer_prices[index + 1 :]
            if remaining:
                leave_one_out_shifts.append(abs(bps(peer_median, statistics.median(remaining))))
    reference_shift_bps = (
        abs(bps(float(reference_price), peer_median))
        if reference_price and reference_price > 0 and peer_median and peer_median > 0
        else None
    )
    loo_shift_bps = max(leave_one_out_shifts, default=reference_shift_bps)
    stability_limit = max(0.001, float(policy["max_leave_one_out_shift_bps"]))
    stability_score = (
        max(0.0, 100.0 * (1.0 - loo_shift_bps / stability_limit))
        if loo_shift_bps is not None
        else 0.0
    )

    cost_ratio = round_trip_cost_bps / gross_edge_bps if gross_edge_bps > 0 else None
    cost_efficiency_score = (
        max(0.0, min(100.0, 100.0 * (1.0 - cost_ratio)))
        if cost_ratio is not None
        else 0.0
    )
    freshness_values = [as_float(observation.get("freshness_age_seconds"), None)]
    freshness_values.extend(as_float(row.get("freshness_age_seconds"), None) for row in peers)
    known_freshness = [value for value in freshness_values if value is not None and value >= 0]
    oldest_freshness = max(known_freshness) if known_freshness else None
    freshness_max = max(0.001, float(policy["freshness_max_seconds"]))
    freshness_score = (
        max(0.0, 100.0 * (1.0 - oldest_freshness / freshness_max))
        if oldest_freshness is not None
        else 25.0
    )

    weights = {
        "reference_breadth": max(0.0, float(policy["reference_breadth_weight"])),
        "reference_stability": max(0.0, float(policy["reference_stability_weight"])),
        "cost_efficiency": max(0.0, float(policy["cost_efficiency_weight"])),
        "freshness": max(0.0, float(policy["freshness_weight"])),
    }
    weight_total = sum(weights.values()) or 1.0
    component_scores = {
        "reference_breadth": round(breadth_score, 3),
        "reference_stability": round(stability_score, 3),
        "cost_efficiency": round(cost_efficiency_score, 3),
        "freshness": round(freshness_score, 3),
    }
    score = sum(component_scores[key] * weights[key] for key in component_scores) / weight_total
    diagnostics = []
    if peer_count < breadth_target:
        diagnostics.append("narrow_reference_set")
    if loo_shift_bps is None:
        diagnostics.append("leave_one_out_reference_unavailable")
    elif loo_shift_bps > stability_limit:
        diagnostics.append("leave_one_out_reference_unstable")
    if cost_ratio is None or cost_ratio >= 1.0:
        diagnostics.append("crossing_cost_consumes_dislocation")
    if oldest_freshness is None:
        diagnostics.append("reference_freshness_unavailable")
    elif oldest_freshness > float(policy["freshness_target_seconds"]):
        diagnostics.append("quote_or_reference_freshness_degraded")
    return {
        "score": round(max(0.0, min(100.0, score)), 3),
        "components": component_scores,
        "reference_peer_count": peer_count,
        "reference_peer_venues": sorted(str(row.get("venue")) for row in peers),
        "peer_reference_price": round(peer_median, 8) if peer_median is not None else None,
        "reference_shift_bps": round(reference_shift_bps, 3) if reference_shift_bps is not None else None,
        "leave_one_out_max_shift_bps": round(loo_shift_bps, 3) if loo_shift_bps is not None else None,
        "crossing_cost_to_edge_ratio": round(cost_ratio, 6) if cost_ratio is not None else None,
        "oldest_reference_freshness_seconds": round(oldest_freshness, 3) if oldest_freshness is not None else None,
        "diagnostics": diagnostics,
        "paper_only": True,
        "eligibility_effect": "ranking_and_diagnostics_only",
    }


def _refresh_frontier_short_route_requirements_report(candidate: dict, settings: dict) -> dict:
    """Attach the read-only route snapshot before paper sizing or ranking.

    Scanner candidates do not necessarily pass through ``route_resolver``
    before their allocation and ranking fields are calculated.  Keep that
    direct path honest by projecting the already-known public route facts into
    the same requirements report here.  This is deliberately metadata only:
    it does not call a venue, alter candidate emission, or make a live route
    reachable.
    """

    if str(candidate.get("direction") or "").strip().lower() != "short_frontier_spot":
        return candidate

    feasibility = candidate.get("execution_route")
    if not isinstance(feasibility, dict):
        feasibility = candidate.get("execution_feasibility")
    feasibility = dict(feasibility) if isinstance(feasibility, dict) else {}
    registry = assess_paper_route_registry(candidate, settings)
    support_status = str(registry.get("support_status") or "unknown").strip().lower()
    candidate["paper_route_registry"] = registry
    candidate["paper_route_registry_key"] = registry.get("route_key")
    candidate["paper_route_registry_status"] = support_status
    candidate["paper_route_required_permissions"] = list(registry.get("required_permissions") or [])
    candidate["paper_route_required_account_modes"] = list(registry.get("required_account_modes") or [])
    candidate["paper_route_estimated_cost_bps"] = dict(registry.get("estimated_cost_bps") or {})
    route_blockers = list(feasibility.get("route_blockers") or [])
    required_permissions = list(registry.get("required_permissions") or [])
    requires_borrow = bool(feasibility.get("requires_short_spot")) or "spot_borrow" in required_permissions
    if requires_borrow and "spot_borrow" not in route_blockers:
        route_blockers.append("spot_borrow")

    report_input = dict(candidate)
    report_input.update(
        {
            "route_status": feasibility.get("route_status") or feasibility.get("status") or "unknown",
            "route_blockers": route_blockers,
            "required_permissions": required_permissions,
            "paper_route_registry": registry,
            "paper_route_registry_status": support_status,
            "paper_route_required_permissions": required_permissions,
            "paper_route_required_account_modes": list(registry.get("required_account_modes") or []),
            "paper_route_estimated_cost_bps": dict(registry.get("estimated_cost_bps") or {}),
            "borrow_required": requires_borrow,
            "borrow_availability_status": (
                "unavailable" if support_status == "unsupported" else "unknown"
            ),
            "margin_required": requires_borrow,
            "margin_mode": "unsupported" if support_status == "unsupported" else "required_unconfirmed",
            # The scanner has a public endpoint observation only.  Do not
            # imply private/order API entitlement from that observation.
            "api_access_status": "public_data_only",
            "maker_fee_bps": candidate.get("maker_fee_bps", candidate.get("estimated_fee_bps_per_side")),
            "taker_fee_bps": candidate.get("taker_fee_bps", candidate.get("estimated_fee_bps_per_side")),
        }
    )
    route = {
        "venue": report_input.get("venue"),
        "inst_id": report_input.get("inst_id"),
        "direction": report_input.get("direction"),
        "route_status": report_input["route_status"],
        "route_blockers": route_blockers,
        "required_permissions": required_permissions,
        "borrow_required": requires_borrow,
        "margin_required": requires_borrow,
        "api_access_status": "public_data_only",
    }
    report = build_paper_route_requirement_report(report_input, route=route)
    candidate["paper_route_requirement_report"] = report
    candidate["frontier_short_spot_route_intelligence"] = report[
        "frontier_short_spot_route_intelligence"
    ]
    candidate["frontier_short_spot_route_requirements_report"] = report[
        "frontier_short_spot_route_requirements_report"
    ]
    candidate["route_requirement_summary"] = report["route_requirement_summary"]
    candidate["route_requirements_prepared_before_ranking_and_sizing"] = True
    return candidate


def frontier_short_market_context_review(
    observation: dict,
    candidate: dict,
    reference_observations: list[dict] | None,
    settings: dict,
) -> dict:
    """Score frontier-short context without suppressing a paper candidate.

    Breadth, cost burden, local reversal, and broader peer pressure distinguish
    a venue-specific premium from a move that has risk-off confirmation.  A
    failed check selects conservative counterfactual routing only; it never
    changes an otherwise priceable candidate into a blocked entry.
    """

    policy = _frontier_short_market_context_policy(settings)
    applicable = bool(
        policy.get("enabled", True)
        and policy.get("paper_only", True)
        and settings.get("mode", "paper") == "paper"
        and not settings.get("allow_live_trading", False)
        and observation.get("market_type") == "spot"
        and candidate.get("direction") == "short_frontier_spot"
    )
    if not applicable:
        return {
            "enabled": bool(policy.get("enabled", True)),
            "applicable": False,
            "paper_only": True,
            "emission_action": "unchanged",
        }

    peers = _frontier_reference_peer_rows(observation, reference_observations)
    breadth = len(peers)
    breadth_target = max(1, int(policy["minimum_reference_breadth"]))
    expected_edge = abs(as_float(candidate.get("venue_deviation_bps"), 0.0) or 0.0)
    spread = max(0.0, as_float(candidate.get("spread_bps"), 0.0) or 0.0)
    entry_slippage = max(0.0, as_float(candidate.get("entry_slippage_bps_estimate"), 0.0) or 0.0)
    exit_slippage = max(0.0, as_float(candidate.get("exit_slippage_bps_estimate"), 0.0) or 0.0)
    crossing_cost = spread + entry_slippage + exit_slippage
    cost_to_edge_ratio = crossing_cost / expected_edge if expected_edge > 0.0 else None
    max_cost_ratio = max(0.001, float(policy["max_spread_plus_slippage_to_edge_ratio"]))

    peer_returns = [
        as_float(row.get("change_24h_pct"), None)
        for row in peers
    ]
    peer_returns = [value * 100.0 for value in peer_returns if value is not None]
    negative_peer_count = sum(value < 0.0 for value in peer_returns)
    risk_off_ratio = negative_peer_count / len(peer_returns) if peer_returns else None
    min_risk_off_ratio = max(0.0, min(1.0, float(policy["min_broader_risk_off_ratio"])))
    local_trend = as_float(candidate.get("local_short_horizon_trend_bps"), None)
    reversal_floor = max(0.0, float(policy["minimum_local_reversal_bps"]))

    components = {
        "reference_breadth": min(100.0, 100.0 * breadth / breadth_target),
        "spread_plus_slippage_efficiency": (
            max(0.0, min(100.0, 100.0 * (1.0 - cost_to_edge_ratio / max_cost_ratio)))
            if cost_to_edge_ratio is not None
            else 0.0
        ),
        "broader_risk_off": (
            min(100.0, 100.0 * risk_off_ratio / min_risk_off_ratio)
            if risk_off_ratio is not None and min_risk_off_ratio > 0.0
            else 50.0
        ),
        "local_premium_reversal": (
            100.0
            if local_trend is not None and local_trend <= -reversal_floor
            else 0.0
            if local_trend is not None
            else 50.0
        ),
    }
    score = sum(components.values()) / len(components)
    diagnostics = []
    if breadth < breadth_target:
        diagnostics.append("reference_breadth_below_context_target")
    if cost_to_edge_ratio is None or cost_to_edge_ratio > max_cost_ratio:
        diagnostics.append("spread_plus_slippage_exceeds_expected_edge")
    if risk_off_ratio is None:
        diagnostics.append("broader_risk_off_reference_unavailable")
    elif risk_off_ratio < min_risk_off_ratio:
        diagnostics.append("broader_risk_off_not_confirmed")
    if local_trend is None:
        diagnostics.append("local_premium_reversal_unavailable")
    elif local_trend > -reversal_floor:
        diagnostics.append("local_premium_reversal_not_confirmed")

    confirmed = not diagnostics
    counterfactual_floor = max(
        0.01,
        min(1.0, float(policy["minimum_counterfactual_allocation_multiplier"])),
    )
    allocation_multiplier = 1.0 if confirmed else max(counterfactual_floor, score / 100.0)
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
        "reference_breadth": breadth,
        "minimum_reference_breadth": breadth_target,
        "spread_plus_slippage_bps": round(crossing_cost, 6),
        "expected_edge_bps": round(expected_edge, 6),
        "spread_plus_slippage_to_edge_ratio": round(cost_to_edge_ratio, 6) if cost_to_edge_ratio is not None else None,
        "max_spread_plus_slippage_to_edge_ratio": max_cost_ratio,
        "broader_risk_off_reference_count": len(peer_returns),
        "broader_risk_off_negative_count": negative_peer_count,
        "broader_risk_off_ratio": round(risk_off_ratio, 6) if risk_off_ratio is not None else None,
        "min_broader_risk_off_ratio": min_risk_off_ratio,
        "local_short_horizon_trend_bps": round(local_trend, 6) if local_trend is not None else None,
        "minimum_local_reversal_bps": reversal_floor,
    }


def _apply_frontier_short_market_context(
    candidate: dict,
    observation: dict,
    reference_observations: list[dict] | None,
    settings: dict,
) -> dict:
    review = frontier_short_market_context_review(
        observation, candidate, reference_observations, settings
    )
    candidate["frontier_short_market_context"] = review
    if not review.get("applicable"):
        return candidate
    candidate["market_context_score"] = review["score"]
    candidate["market_context_diagnostics"] = list(review["diagnostics"])
    candidate["market_context_allocation_multiplier"] = review["allocation_multiplier"]
    existing = as_float(candidate.get("paper_allocation_multiplier"), None)
    candidate["paper_allocation_multiplier"] = min(
        existing if existing is not None else 1.0,
        float(review["allocation_multiplier"]),
    )
    if review["diagnostics"]:
        candidate["risk_notes"] = list(candidate.get("risk_notes") or []) + [
            "frontier short context is unconfirmed; retain as a conservative counterfactual paper experiment",
        ]
    return candidate


def rank_frontier_paper_candidates(candidates: list[dict], settings: dict) -> list[dict]:
    """Rank the paper cohort while retaining every emitted, priceable candidate."""

    def _multiplier(value, default=1.0):
        try:
            return float(default if value is None else value)
        except (TypeError, ValueError):
            return float(default)

    policy = _frontier_dislocation_quality_policy(settings)
    paper_only_active = bool(
        policy.get("enabled", True)
        and policy.get("paper_only", True)
        and settings.get("mode", "paper") == "paper"
        and not settings.get("allow_live_trading", False)
    )
    quality_weight = (
        min(1.0, max(0.0, float(policy["ranking_quality_weight"])))
        if paper_only_active
        else 0.0
    )
    context_policy = _frontier_short_market_context_policy(settings)
    context_weight = (
        min(1.0, max(0.0, float(context_policy["ranking_weight"])))
        if paper_only_active and context_policy.get("enabled", True) and context_policy.get("paper_only", True)
        else 0.0
    )
    for candidate in candidates:
        _refresh_frontier_short_route_requirements_report(candidate, settings)
        route_feasibility_gate = paper_only_conditional_short_route_feasibility_gate(
            venue=str(candidate.get("venue") or ""),
            direction=str(candidate.get("direction") or ""),
            context_stats=candidate,
            enabled=paper_only_active,
        )
        candidate["route_feasibility_gate"] = route_feasibility_gate
        _annotate_frontier_route_feasibility_shadow_state(candidate, route_feasibility_gate)
        if paper_only_active:
            try:
                from paper_order_router import apply_frontier_paper_admission_guard
            except ImportError:  # pragma: no cover - package import fallback
                from src.paper_order_router import apply_frontier_paper_admission_guard
            candidate.update(apply_frontier_paper_admission_guard(candidate, settings))
        quality_score = as_float(candidate.get("dislocation_quality_score"), 0.0) or 0.0
        base_score = as_float(candidate.get("score"), 0.0) or 0.0
        effective_edge = as_float(candidate.get("effective_edge_bps"), None)
        short_cost_model = candidate.get("short_cost_decomposition") or {}
        short_net_edge = (
            as_float(short_cost_model.get("net_edge_bps"), None)
            if isinstance(short_cost_model, dict) and short_cost_model.get("applicable")
            else None
        )
        ranking_edge = short_net_edge if short_net_edge is not None else effective_edge
        ranking_edge_source = (
            "short_cost_decomposition.net_edge_bps"
            if short_net_edge is not None
            else "effective_edge_bps"
            if effective_edge is not None
            else "base_score"
        )
        effective_edge_score = max(0.0, ranking_edge or 0.0)
        local_quote_penalty = max(0.0, as_float(candidate.get("local_quote_score_penalty"), 0.0) or 0.0)
        quote_context_penalty = max(
            0.0, as_float(candidate.get("quote_context_ranking_penalty"), 0.0) or 0.0
        )
        context_review = candidate.get("frontier_short_market_context")
        context_score = (
            as_float(context_review.get("score"), None)
            if isinstance(context_review, dict) and context_review.get("applicable")
            else None
        )
        paper_ranking_score = round(
            effective_edge_score
            + quality_weight * quality_score
            + context_weight * ((context_score - 100.0) if context_score is not None else 0.0)
            - local_quote_penalty
            - quote_context_penalty
            if ranking_edge is not None
            else (
                (1.0 - quality_weight) * base_score
                + quality_weight * quality_score
                + context_weight * ((context_score - 100.0) if context_score is not None else 0.0)
                - local_quote_penalty
                - quote_context_penalty
            ),
            3,
        )
        cost_diagnostic = candidate.get("paper_cost_diagnostic") or {}
        if isinstance(cost_diagnostic, dict) and cost_diagnostic.get("emission_action") == "counterfactual_guard_value":
            paper_ranking_score = min(paper_ranking_score, as_float(cost_diagnostic.get("score_cap"), 59.999) or 59.999)
        if route_feasibility_gate.get("enabled", False) and route_feasibility_gate.get("applied", False):
            paper_ranking_score = round(
                max(0.0, paper_ranking_score)
                * _multiplier(route_feasibility_gate.get("score_multiplier", 1.0)),
                3,
            )
        frontier_paper_admission = candidate.get("frontier_paper_admission") or {}
        if (
            paper_only_active
            and isinstance(frontier_paper_admission, dict)
            and not frontier_paper_admission.get("admitted", True)
        ):
            paper_ranking_score = min(
                paper_ranking_score,
                as_float(frontier_paper_admission.get("score_cap"), 59.999) or 59.999,
            )
        quote_ranking_eligible = bool(candidate.get("quote_ranking_eligible", True))
        if not quote_ranking_eligible:
            paper_ranking_score = 0.0
            candidate["paper_active_scoring_eligible"] = False
            candidate["paper_fx_shadow_label"] = True
            candidate["paper_ineligible"] = True
            candidate["paper_ineligible_reason"] = (
                candidate.get("quote_ranking_reason")
                or candidate.get("paper_ineligible_reason")
                or "stale_fx_reference"
            )
            candidate["paper_fx_ranking_reason"] = (
                candidate.get("quote_ranking_reason") or "stale_fx_reference"
            )
        candidate["paper_ranking_score"] = paper_ranking_score
        candidate["paper_ranking_edge_bps"] = round(ranking_edge, 6) if ranking_edge is not None else None
        candidate["paper_ranking_edge_source"] = ranking_edge_source
        candidate["paper_quality_cohort"] = (
            "quality_ranked"
            if paper_only_active and quality_score >= 60.0
            else "quality_diagnostic"
            if paper_only_active
            else "baseline"
        )
        candidate["paper_quality_filter_status"] = (
            "route_feasibility_shadow_only"
            if route_feasibility_gate.get("shadow_label", False)
            else "stale_fx_reference_shadow_only"
            if not quote_ranking_eligible
            else "ranked_not_blocked"
            if paper_only_active
            else "paper_only_ranking_inactive"
        )
        candidate["paper_market_context_filter_status"] = (
            "ranked_and_counterfactually_routed"
            if context_score is not None and isinstance(context_review, dict) and not context_review.get("confirmed")
            else "ranked_with_confirmed_context"
            if context_score is not None
            else "not_applicable"
        )
    ranked = sorted(
        candidates,
        key=lambda row: (
            bool(row.get("paper_active_scoring_eligible", True)),
            float(row.get("paper_ranking_score") or 0.0),
            float(row.get("score") or 0.0),
        ),
        reverse=True,
    )
    for rank, candidate in enumerate(ranked, start=1):
        candidate["paper_quality_rank"] = rank
    return ranked


def _frontier_effective_edge_reliability(observation: dict, candidate: dict) -> tuple[float | None, str]:
    """Return normalized venue reliability, preferring explicit health telemetry."""
    for container, source in (
        (observation, "venue_health_score"),
        (candidate, "venue_health_score"),
        (observation.get("venue_health"), "venue_health.venue_quality_score"),
        (candidate.get("venue_health"), "venue_health.venue_quality_score"),
    ):
        if not isinstance(container, dict):
            continue
        value = as_float(container.get("venue_health_score", container.get("venue_quality_score")), None)
        if value is not None:
            return max(0.0, min(1.0, value / 100.0 if value > 1.0 else value)), source
    return None, "unavailable"


def frontier_short_cost_decomposition_review(observation: dict, candidate: dict, settings: dict) -> dict:
    """Estimate paper-only short-side execution drag for frontier spot.

    The output is deliberately diagnostic and ranking-oriented.  It never
    changes direction, reject reason, or ``paper_entry_blocked``; an adverse
    net edge is retained as a counterfactual paper experiment by the existing
    effective-edge routing path.
    """
    policy = _frontier_short_cost_decomposition_policy(settings)
    applicable = bool(
        policy.get("enabled", True)
        and policy.get("paper_only", True)
        and settings.get("mode", "paper") == "paper"
        and not settings.get("allow_live_trading", False)
        and observation.get("market_type") == "spot"
        and candidate.get("direction") == "short_frontier_spot"
    )
    if not applicable:
        return {
            "enabled": bool(policy.get("enabled", True)),
            "applicable": False,
            "paper_only": True,
            "emission_action": "unchanged",
        }

    gross_edge = abs(as_float(candidate.get("venue_deviation_bps"), 0.0) or 0.0)
    spread_cost = max(0.0, as_float(candidate.get("spread_bps"), 0.0) or 0.0)
    entry_slippage = max(0.0, as_float(candidate.get("entry_slippage_bps_estimate"), 0.0) or 0.0)
    exit_slippage = max(0.0, as_float(candidate.get("exit_slippage_bps_estimate"), 0.0) or 0.0)
    depth_slippage_cost = entry_slippage + exit_slippage
    taker_fee_cost = 2.0 * max(0.0, as_float(candidate.get("estimated_fee_bps_per_side"), 0.0) or 0.0)

    quality_value = as_float(candidate.get("quality_score"), None)
    quality_source = "quality_score"
    if quality_value is None:
        quality_value = as_float(observation.get("venue_health_score"), None)
        quality_source = "venue_health_score" if quality_value is not None else "unavailable"
    if quality_value is None:
        venue_quality_penalty = max(0.0, float(policy["missing_venue_quality_penalty_bps"]))
    else:
        normalized_quality = max(0.0, min(1.0, quality_value / 100.0 if quality_value > 1.0 else quality_value))
        venue_quality_penalty = (1.0 - normalized_quality) * max(
            0.0, float(policy["venue_quality_penalty_bps_cap"])
        )

    route_text = " ".join(
        str(value or "")
        for value in (
            candidate.get("route_id"),
            candidate.get("paper_route_status"),
            candidate.get("route_status"),
            candidate.get("data_source"),
        )
    ).lower()
    synthetic_route = bool(candidate.get("synthetic_research_paper")) or any(
        token in route_text for token in ("synthetic", "proxy", "counterfactual")
    )
    synthetic_route_penalty = (
        max(0.0, float(policy["synthetic_route_penalty_bps"])) if synthetic_route else 0.0
    )

    borrow_penalty = None
    borrow_source = "unconfirmed_borrow_proxy"
    for container, source, keys in (
        (candidate, "candidate", ("borrow_proxy_penalty_bps", "borrow_cost_bps", "borrow_cost_bps_estimate")),
        (observation, "observation", ("borrow_proxy_penalty_bps", "borrow_cost_bps", "borrow_cost_bps_estimate")),
    ):
        for key in keys:
            value = as_float(container.get(key), None)
            if value is not None:
                borrow_penalty = max(0.0, value)
                borrow_source = f"{source}.{key}"
                break
        if borrow_penalty is not None:
            break
    execution = candidate.get("execution_feasibility") or {}
    route_blockers = set(execution.get("route_blockers") or []) if isinstance(execution, dict) else set()
    borrow_confirmed = bool(
        candidate.get("borrow_confirmed")
        or candidate.get("borrowable")
        or observation.get("borrow_confirmed")
        or observation.get("borrowable")
        or "spot_borrow" not in route_blockers
    )
    if borrow_penalty is None:
        borrow_penalty = 0.0 if borrow_confirmed else max(
            0.0, float(policy["unconfirmed_borrow_proxy_penalty_bps"])
        )
        borrow_source = "confirmed_no_cost_observed" if borrow_confirmed else borrow_source

    net_edge = (
        gross_edge
        - spread_cost
        - depth_slippage_cost
        - taker_fee_cost
        - venue_quality_penalty
        - synthetic_route_penalty
        - borrow_penalty
    )
    costs = {
        "spread_cost_bps": round(spread_cost, 6),
        "depth_slippage_cost_bps": round(depth_slippage_cost, 6),
        "estimated_taker_fee_cost_bps": round(taker_fee_cost, 6),
        "venue_quality_penalty_bps": round(venue_quality_penalty, 6),
        "synthetic_route_penalty_bps": round(synthetic_route_penalty, 6),
        "borrow_proxy_penalty_bps": round(borrow_penalty, 6),
    }
    return {
        "enabled": True,
        "applicable": True,
        "paper_only": True,
        "emission_action": "rank_and_size_with_diagnostics",
        "gross_edge_bps": round(gross_edge, 6),
        **costs,
        "total_cost_bps": round(sum(costs.values()), 6),
        "net_edge_bps": round(net_edge, 6),
        "venue_quality_score": round(quality_value, 6) if quality_value is not None else None,
        "venue_quality_source": quality_source,
        "synthetic_route": synthetic_route,
        "borrow_confirmed": borrow_confirmed,
        "borrow_proxy_source": borrow_source,
        "allocation_edge_bps_cap": max(0.001, float(policy["allocation_edge_bps_cap"])),
    }


def frontier_effective_edge_review(observation: dict, candidate: dict, settings: dict) -> dict:
    """Decompose a frontier paper candidate's executable edge.

    This is deliberately separate from candidate emission.  ``primary_admitted``
    controls trusted simulated allocation; a false result is retained as a
    counterfactual paper route with its reason and cost terms intact.
    """
    policy = _frontier_effective_edge_policy(settings)
    applicable = bool(
        policy.get("enabled", True)
        and policy.get("paper_only", True)
        and settings.get("mode", "paper") == "paper"
        and not settings.get("allow_live_trading", False)
        and observation.get("market_type") == "spot"
    )
    if not applicable:
        return {
            "enabled": bool(policy.get("enabled", True)),
            "applicable": False,
            "paper_only": True,
            "primary_admitted": True,
            "emission_action": "unchanged",
        }

    raw_edge = abs(as_float(candidate.get("venue_deviation_bps"), 0.0) or 0.0)
    half_spread = max(0.0, as_float(candidate.get("spread_bps"), 0.0) or 0.0) / 2.0
    taker_fee = max(
        0.0,
        as_float(candidate.get("estimated_fee_bps_per_side"), None)
        if as_float(candidate.get("estimated_fee_bps_per_side"), None) is not None
        else as_float(settings.get("risk", {}).get("taker_fee_bps_per_leg"), 0.0) or 0.0,
    )
    quote_status = str(observation.get("quote_normalization_status") or "").lower()
    quote_conversion_cost = max(0.0, as_float(observation.get("quote_conversion_cost_bps"), None) or 0.0)
    if quote_conversion_cost == 0.0 and quote_status == "external_fx_reference":
        quote_conversion_cost = max(0.0, float(policy["external_quote_conversion_cost_bps"]))
    route_text = " ".join(
        str(value or "")
        for value in (
            candidate.get("route_id"),
            candidate.get("paper_route_status"),
            candidate.get("route_status"),
            candidate.get("data_source"),
        )
    ).lower()
    proxy_drag = max(0.0, as_float(observation.get("proxy_drag_bps"), None) or 0.0)
    synthetic_route = any(token in route_text for token in ("synthetic", "proxy", "counterfactual"))
    if proxy_drag == 0.0 and synthetic_route:
        proxy_drag = max(0.0, float(policy["synthetic_proxy_drag_bps"]))
    freshness_age = as_float(candidate.get("freshness_age_seconds"), None)
    freshness_window = max(0.001, float(policy["freshness_penalty_window_seconds"]))
    freshness_penalty = 0.0
    if freshness_age is not None and freshness_age > freshness_window:
        freshness_penalty = min(
            max(0.0, float(policy["max_freshness_penalty_bps"])),
            (freshness_age / freshness_window - 1.0) * float(policy["freshness_penalty_bps_per_window"]),
        )
    reliability, reliability_source = _frontier_effective_edge_reliability(observation, candidate)
    reliability_penalty = (
        (1.0 - reliability) * max(0.0, float(policy["max_venue_reliability_penalty_bps"]))
        if reliability is not None
        else max(0.0, float(policy["max_venue_reliability_penalty_bps"]))
    )
    effective_edge = raw_edge - half_spread - taker_fee - quote_conversion_cost - proxy_drag - freshness_penalty - reliability_penalty
    min_freshness = max(0.0, float(policy["max_freshness_age_seconds"]))
    min_liquidity = max(0.0, float(policy["min_liquidity_score"]))
    min_reliability = max(0.0, min(1.0, float(policy["min_venue_reliability_score"])))
    reported_liquidity = max(0.0, min(1.0, as_float(candidate.get("liquidity_score"), 0.0) or 0.0))
    bid_depth, _ = _top_of_book_notional_usd(observation, "bid")
    ask_depth, _ = _top_of_book_notional_usd(observation, "ask")
    paper_notional = max(1.0, as_float(settings.get("risk", {}).get("paper_notional_usd"), 1000.0) or 1000.0)
    depth_liquidity = (
        max(0.0, min(1.0, min(bid_depth, ask_depth) / paper_notional))
        if bid_depth is not None and ask_depth is not None
        else 0.0
    )
    # A deep public book can support a paper experiment even before enough
    # rolling turnover has accumulated to raise the volume-derived score.
    liquidity = max(reported_liquidity, depth_liquidity)
    freshness_confidence = 1.0 if freshness_age is not None and freshness_age <= min_freshness else (
        max(0.0, min(1.0, min_freshness / freshness_age)) if freshness_age and freshness_age > 0.0 else 0.0
    )
    liquidity_confidence = 1.0 if liquidity >= min_liquidity else (liquidity / min_liquidity if min_liquidity else 1.0)
    reliability_confidence = (
        1.0 if reliability is not None and reliability >= min_reliability
        else (reliability / min_reliability if reliability is not None and min_reliability else 0.0)
    )
    confidence_score = max(0.0, min(1.0, freshness_confidence * liquidity_confidence * reliability_confidence))
    reasons = []
    if effective_edge <= float(policy["minimum_effective_edge_bps"]):
        reasons.append("effective_edge_not_positive")
    if freshness_age is None or freshness_age > min_freshness:
        reasons.append("freshness_below_minimum")
    if liquidity < min_liquidity:
        reasons.append("liquidity_below_minimum")
    if reliability is None or reliability < min_reliability:
        reasons.append("venue_reliability_below_minimum")
    primary_admitted = not reasons
    edge_scale = max(0.0, min(1.0, max(0.0, effective_edge) / max(0.001, float(policy["allocation_edge_bps_cap"]))))
    primary_allocation = edge_scale * confidence_score
    allocation_cap = 1.0
    if synthetic_route:
        allocation_cap = min(allocation_cap, max(0.01, min(1.0, float(policy["synthetic_allocation_cap"]))))
    if reliability is None or reliability < float(policy["low_reliability_score"]):
        allocation_cap = min(allocation_cap, max(0.01, min(1.0, float(policy["low_reliability_allocation_cap"]))))
    primary_allocation = min(primary_allocation, allocation_cap)
    return {
        "enabled": True,
        "applicable": True,
        "paper_only": True,
        "primary_admitted": primary_admitted,
        "admission_reasons": reasons,
        "emission_action": "primary_simulated_route" if primary_admitted else "counterfactual_guard_value",
        "raw_edge_bps": round(raw_edge, 6),
        "half_spread_bps": round(half_spread, 6),
        "estimated_taker_fee_bps": round(taker_fee, 6),
        "quote_conversion_cost_bps": round(quote_conversion_cost, 6),
        "proxy_drag_bps": round(proxy_drag, 6),
        "freshness_penalty_bps": round(freshness_penalty, 6),
        "venue_reliability_penalty_bps": round(reliability_penalty, 6),
        "effective_edge_bps": round(effective_edge, 6),
        "confidence_score": round(confidence_score, 6),
        "liquidity_score": round(liquidity, 6),
        "reported_liquidity_score": round(reported_liquidity, 6),
        "depth_liquidity_score": round(depth_liquidity, 6),
        "venue_reliability_score": round(reliability, 6) if reliability is not None else None,
        "venue_reliability_source": reliability_source,
        "synthetic_or_proxy_route": synthetic_route,
        "primary_allocation_multiplier": round(primary_allocation, 6),
        "allocation_cap": round(allocation_cap, 6),
        "counterfactual_allocation_multiplier": round(
            max(0.01, min(1.0, float(policy["counterfactual_allocation_multiplier"]))), 6
        ),
        "minimums": {
            "effective_edge_bps": float(policy["minimum_effective_edge_bps"]),
            "freshness_age_seconds": min_freshness,
            "liquidity_score": min_liquidity,
            "venue_reliability_score": min_reliability,
        },
    }


def _apply_frontier_effective_edge(candidate: dict, observation: dict, settings: dict) -> dict:
    review = frontier_effective_edge_review(observation, candidate, settings)
    candidate["effective_edge_model"] = review
    if review.get("applicable"):
        candidate["effective_edge_bps"] = review["effective_edge_bps"]
        candidate["confidence_score"] = review["confidence_score"]
        candidate["effective_edge_primary_admitted"] = review["primary_admitted"]
        candidate["effective_edge_admission_reasons"] = list(review["admission_reasons"])
        candidate["effective_edge_allocation_multiplier"] = review["primary_allocation_multiplier"]
    short_cost = frontier_short_cost_decomposition_review(observation, candidate, settings)
    candidate["short_cost_decomposition"] = short_cost
    if not short_cost.get("applicable") or not review.get("applicable"):
        return candidate

    short_net_edge = float(short_cost["net_edge_bps"])
    short_edge_scale = max(0.0, min(1.0, short_net_edge / float(short_cost["allocation_edge_bps_cap"])))
    short_allocation = min(
        float(review["primary_allocation_multiplier"]),
        short_edge_scale * float(review["confidence_score"]),
    )
    short_primary_admitted = short_net_edge > float(review["minimums"]["effective_edge_bps"])
    review["ranking_sizing_edge_bps"] = round(short_net_edge, 6)
    review["ranking_sizing_edge_source"] = "short_cost_decomposition.net_edge_bps"
    review["primary_allocation_multiplier"] = round(short_allocation, 6)
    if not short_primary_admitted:
        review["primary_admitted"] = False
        review["admission_reasons"] = list(review["admission_reasons"]) + ["short_net_edge_not_positive"]
        review["emission_action"] = "counterfactual_guard_value"
    candidate["effective_edge_primary_admitted"] = review["primary_admitted"]
    candidate["effective_edge_admission_reasons"] = list(review["admission_reasons"])
    candidate["effective_edge_allocation_multiplier"] = review["primary_allocation_multiplier"]
    return candidate


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
            reference_observations=observations,
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
    return rank_frontier_paper_candidates(candidates, settings)


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


def _effective_spread_bps(observation: dict) -> float:
    last = float(observation.get("last") or 0.0)
    spread = float(observation.get("spread_bps") or spread_bps(observation.get("bid"), observation.get("ask"), last))
    book_levels = observation.get("book_levels") or {}
    book_bids = book_levels.get("bids") or []
    book_asks = book_levels.get("asks") or []
    if spread >= 900.0 and book_bids and book_asks:
        spread = spread_bps(book_bids[0][0], book_asks[0][0], last)
    return round(spread, 3)


def _frontier_marketability_policy(settings: dict) -> dict:
    policy = dict(DEFAULT_FRONTIER_MARKETABILITY_GATES)
    configured = settings.get("frontier_crypto_adapter", {}).get("marketability_gates", {})
    if isinstance(configured, dict):
        policy.update({key: value for key, value in configured.items() if value is not None})
    if configured is False:
        policy["enabled"] = False
    return policy


def _top_of_book_notional_usd(observation: dict, side: str) -> tuple[float | None, str]:
    levels = ((observation.get("book_levels") or {}).get(side + "s") or [])
    multiplier = as_float(observation.get("quote_to_usd_multiplier"), None)
    if multiplier is None and str(observation.get("quote") or "").upper() in USD_LIKE_QUOTES:
        multiplier = 1.0
    if levels and multiplier is not None and multiplier > 0:
        level = levels[0]
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            price = as_float(level[0], None)
            quantity = as_float(level[1], None)
            if price is not None and quantity is not None and price > 0 and quantity > 0:
                return round(price * quantity * multiplier, 3), "top_level"
    return None, "unavailable"


def _frontier_peer_price_confirmation(
    observation: dict,
    candidate: dict,
    reference_observations: list[dict] | None,
    max_deviation_bps: float,
) -> dict:
    local_price = _comparison_price(observation)
    local_venue = str(observation.get("venue") or "")
    comparison_key = observation.get("comparison_key")
    peers = []
    independent_venue_count = 0
    if reference_observations is not None:
        best_by_venue: dict[str, dict] = {}
        for row in reference_observations:
            venue = str(row.get("venue") or "")
            price = _comparison_price(row)
            if (
                not venue
                or venue == local_venue
                or row.get("comparison_key") != comparison_key
                or row.get("market_type") != "spot"
                or row.get("data_status") != "reachable"
                or row.get("local_quote_observe_only")
                or price <= 0
            ):
                continue
            deviation = abs(bps(local_price, price)) if local_price > 0 else math.inf
            previous = best_by_venue.get(venue)
            if previous is None or deviation < previous["deviation_bps"]:
                best_by_venue[venue] = {
                    "venue": venue,
                    "price": round(price, 8),
                    "deviation_bps": round(deviation, 3),
                }
        peers = sorted(best_by_venue.values(), key=lambda item: item["deviation_bps"])
        independent_venue_count = len(peers)
    else:
        independent_venue_count = max(0, int(candidate.get("source_venue_count") or 0) - 1)
        reference_price = as_float(candidate.get("reference_price"), None)
        if independent_venue_count > 0 and reference_price is not None and reference_price > 0 and local_price > 0:
            peers = [
                {
                    "venue": "cross_venue_reference",
                    "price": round(reference_price, 8),
                    "deviation_bps": round(abs(bps(local_price, reference_price)), 3),
                }
            ]
    confirmed = [peer for peer in peers if peer["deviation_bps"] <= max_deviation_bps]
    return {
        "confirmed": bool(confirmed),
        "reference_venue_count": independent_venue_count,
        "confirming_venue_count": len(confirmed),
        "closest_reference": peers[0] if peers else None,
        "confirming_venues": confirmed,
    }


def frontier_marketability_gate_review(
    observation: dict,
    candidate: dict,
    settings: dict,
    reference_observations: list[dict] | None = None,
) -> dict:
    """Paper-only marketability diagnostics for frontier simulated routing."""

    policy = _frontier_marketability_policy(settings)
    applicable = bool(
        policy.get("enabled", True)
        and policy.get("paper_only", True)
        and observation.get("market_type") == "spot"
    )
    if not applicable:
        return {
            "enabled": bool(policy.get("enabled", True)),
            "applicable": False,
            "passed": True,
            "status": "not_applicable",
            "paper_only": True,
            "checks": {},
            "failed_checks": [],
        }

    max_age = float(policy["max_book_age_seconds"])
    max_spread = float(policy["max_spread_bps"])
    min_depth = float(
        policy.get("min_top_of_book_notional_usd")
        or settings.get("risk", {}).get("paper_notional_usd", 1000.0)
    )
    max_reference_deviation = float(policy["max_cross_venue_deviation_bps"])
    min_reference_venues = int(policy["min_reference_venues"])
    min_route_confidence = float(policy["min_route_confidence"])
    min_venue_health_score = float(policy["min_venue_health_score"])

    age = as_float(observation.get("freshness_age_seconds"), None)
    spread = _effective_spread_bps(observation)
    bid_notional, bid_depth_source = _top_of_book_notional_usd(observation, "bid")
    ask_notional, ask_depth_source = _top_of_book_notional_usd(observation, "ask")
    confirmation = _frontier_peer_price_confirmation(
        observation,
        candidate,
        reference_observations,
        max_reference_deviation,
    )

    feasibility = candidate.get("execution_feasibility") or {}
    route_status = str(feasibility.get("status") or "unknown")
    known_statuses = {str(item) for item in policy.get("known_paper_route_statuses", [])}
    explicit_route_confidence = None
    for container in (candidate, observation, observation.get("route_quality") or {}, feasibility):
        if not isinstance(container, dict):
            continue
        for key in ("route_confidence", "route_mapping_confidence", "mapping_confidence"):
            explicit_route_confidence = as_float(container.get(key), None)
            if explicit_route_confidence is not None:
                break
        if explicit_route_confidence is not None:
            break
    known_paper_route = bool(candidate.get("route_id") and route_status in known_statuses)
    route_confidence = explicit_route_confidence
    if route_confidence is None:
        route_confidence = 1.0 if candidate.get("route_id") and route_status == "standard" else 0.0
    route_passed = bool(known_paper_route or route_confidence >= min_route_confidence)
    venue_health_score = as_float(observation.get("venue_health_score"), None)

    checks = {
        "book_freshness": {
            "passed": age is not None and age <= max_age,
            "observed_seconds": age,
            "maximum_seconds": max_age,
        },
        "spread_sanity": {
            "passed": spread <= max_spread,
            "observed_bps": spread,
            "maximum_bps": max_spread,
        },
        "top_of_book_depth": {
            "passed": bool(
                bid_notional is not None
                and ask_notional is not None
                and bid_notional >= min_depth
                and ask_notional >= min_depth
            ),
            "bid_notional_usd": bid_notional,
            "ask_notional_usd": ask_notional,
            "bid_source": bid_depth_source,
            "ask_source": ask_depth_source,
            "minimum_each_side_usd": min_depth,
        },
        "cross_venue_price_confirmation": {
            "passed": bool(
                confirmation["confirmed"]
                and confirmation["reference_venue_count"] >= min_reference_venues
            ),
            **confirmation,
            "minimum_reference_venues": min_reference_venues,
            "maximum_deviation_bps": max_reference_deviation,
        },
        "route_confidence": {
            "passed": route_passed,
            "known_paper_route": known_paper_route,
            "route_id": candidate.get("route_id"),
            "route_status": route_status,
            "observed_confidence": round(route_confidence, 6),
            "minimum_confidence": min_route_confidence,
        },
        "venue_health_score": {
            "passed": bool(
                venue_health_score is not None
                and venue_health_score >= min_venue_health_score
            ),
            "observed_score": venue_health_score,
            "minimum_score": min_venue_health_score,
            "telemetry_present": venue_health_score is not None,
            "health": observation.get("venue_health"),
        },
    }
    failed = [name for name, check in checks.items() if not check["passed"]]
    return {
        "enabled": True,
        "applicable": True,
        "passed": not failed,
        "status": "passed" if not failed else "failed",
        "paper_only": True,
        "checks": checks,
        "failed_checks": failed,
        "policy": policy,
    }


def _apply_frontier_marketability_gate(
    candidate: dict,
    observation: dict,
    settings: dict,
    reference_observations: list[dict] | None = None,
) -> dict:
    review = frontier_marketability_gate_review(
        observation,
        candidate,
        settings,
        reference_observations,
    )
    candidate["marketability_gate"] = review
    candidate["marketability_gate_status"] = review["status"]
    if not review.get("applicable"):
        return candidate

    policy = review.get("policy") if isinstance(review.get("policy"), dict) else {}
    failed = list(review.get("failed_checks") or [])
    effective_edge = candidate.get("effective_edge_model")
    if isinstance(effective_edge, dict) and effective_edge.get("applicable"):
        effective_admitted = bool(effective_edge.get("primary_admitted"))
        review["checks"]["effective_edge_admission"] = {
            "passed": effective_admitted,
            "effective_edge_bps": effective_edge.get("effective_edge_bps"),
            "confidence_score": effective_edge.get("confidence_score"),
            "minimums": effective_edge.get("minimums"),
            "admission_reasons": list(effective_edge.get("admission_reasons") or []),
        }
        if not effective_admitted:
            failed.append("effective_edge_admission")
            review["failed_checks"] = failed
            review["passed"] = False
            review["status"] = "failed"
    route_health_confirmed = not failed
    confirmed_multiplier = as_float(policy.get("confirmed_route_allocation_multiplier"), 1.0)
    conservative_multiplier = as_float(policy.get("conservative_route_allocation_multiplier"), 0.25)
    confirmed_multiplier = max(0.0, min(1.0, confirmed_multiplier if confirmed_multiplier is not None else 1.0))
    # A nonzero fallback preserves the paper experiment while clearly marking
    # it as a counterfactual route rather than a trusted simulated allocation.
    conservative_multiplier = max(0.01, min(1.0, conservative_multiplier if conservative_multiplier is not None else 0.25))
    route_multiplier = confirmed_multiplier if route_health_confirmed else conservative_multiplier
    quality_multiplier = as_float(candidate.get("quality_allocation_multiplier"), 1.0)
    quality_multiplier = max(0.0, min(1.0, quality_multiplier if quality_multiplier is not None else 1.0))

    candidate["marketability_diagnostic_reasons"] = [f"marketability_{name}" for name in failed]
    candidate["route_health_confirmation"] = {
        "diagnostic_only": bool(policy.get("diagnostic_only", True)),
        "required_for_primary_simulated_allocation": True,
        "confirmed": route_health_confirmed,
        "failed_checks": failed,
        "mode": "confirmed_simulated_route" if route_health_confirmed else "conservative_counterfactual_route",
    }
    effective_primary_multiplier = 1.0
    effective_counterfactual_multiplier = 1.0
    effective_primary_admitted = True
    if isinstance(effective_edge, dict) and effective_edge.get("applicable"):
        effective_primary_admitted = bool(effective_edge.get("primary_admitted"))
        effective_primary_multiplier = max(
            0.0, min(1.0, as_float(effective_edge.get("primary_allocation_multiplier"), 0.0) or 0.0)
        )
        effective_counterfactual_multiplier = max(
            0.01,
            min(1.0, as_float(effective_edge.get("counterfactual_allocation_multiplier"), 0.25) or 0.25),
        )
    effective_allocation = effective_primary_multiplier if effective_primary_admitted else effective_counterfactual_multiplier
    # Counterfactual routes retain their configured nonzero guard-value size.
    # The short-cost model only scales trusted primary simulation, so weak-cost
    # evidence cannot accidentally suppress the paper experiment itself.
    final_allocation = (
        route_multiplier * quality_multiplier * effective_allocation
        if route_health_confirmed and effective_primary_admitted
        else min(route_multiplier, effective_counterfactual_multiplier) * quality_multiplier
    )
    candidate["simulated_order_allocation"] = {
        "paper_only": True,
        "mode": "primary" if route_health_confirmed else "counterfactual_guard_value",
        "route_health_confirmed": route_health_confirmed,
        "route_allocation_multiplier": round(route_multiplier, 6),
        "quality_allocation_multiplier": round(quality_multiplier, 6),
        "effective_edge_allocation_multiplier": round(effective_allocation, 6),
        "allocation_multiplier": round(final_allocation, 6),
        "failed_route_health_checks": failed,
    }
    existing_allocation = as_float(candidate.get("paper_allocation_multiplier"), None)
    simulated_allocation = candidate["simulated_order_allocation"]["allocation_multiplier"]
    # Context-transfer scoring can already have capped a near-threshold paper
    # experiment.  Marketability may add a tighter cap, but must not erase it.
    candidate["paper_allocation_multiplier"] = (
        min(max(0.0, min(1.0, existing_allocation)), simulated_allocation)
        if existing_allocation is not None
        else simulated_allocation
    )
    if failed:
        candidate["risk_notes"] = list(candidate.get("risk_notes") or []) + [
            "venue-health or route-quality evidence is unconfirmed; use conservative counterfactual paper routing",
        ]
    return candidate


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
    if _effective_spread_bps(observation) > max_spread:
        return "watch_only", round(deviation, 3), "spread_too_wide"
    if abs(deviation) < min_dislocation_bps:
        return "watch_only", round(deviation, 3), "below_dislocation_threshold"
    if observation.get("market_type") == "perp":
        return ("short_frontier_perp" if deviation > 0 else "long_frontier_perp"), round(deviation, 3), None
    return ("short_frontier_spot" if deviation > 0 else "long_frontier_spot"), round(deviation, 3), None


def _candidate_from_observation(
    observation: dict,
    settings: dict,
    reference_price: float | None,
    source_venue_count: int,
    *,
    reference_observations: list[dict] | None = None,
) -> dict:
    risk = settings.get("risk", {})
    quality_cfg = settings.get("frontier_data_quality", {})
    direction, deviation, reject_reason = _direction_for_observation(observation, reference_price, settings)
    last = float(observation.get("last") or 0.0)
    comparison_price = _comparison_price(observation)
    spread = _effective_spread_bps(observation)
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
    dislocation_quality = _frontier_dislocation_quality(
        observation,
        reference_price,
        reference_observations,
        round_trip_cost_bps,
        gross_edge,
        settings,
    )
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
    regional_candidate_diagnostics = []
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
            anomaly_flags = sorted(set([*anomaly_flags, regional_candidate_gate_status]))
            regional_candidate_diagnostics.append(regional_candidate_gate_status)
    local_premium_bps = as_float(observation.get("premium_vs_reference_bps"), None)
    fx_age_minutes = as_float(observation.get("fx_age_minutes"), None)
    local_quote_score_penalty = 0.0
    if observation.get("local_quote_flag"):
        if local_premium_bps is None:
            regional_candidate_diagnostics.append("local_premium_reference_unavailable")
            local_quote_score_penalty += 2.0
        else:
            local_quote_score_penalty += min(15.0, abs(local_premium_bps) / 20.0)
            if abs(local_premium_bps) >= 25.0:
                regional_candidate_diagnostics.append("local_premium_vs_reference")
        if fx_age_minutes is not None:
            local_quote_score_penalty += min(5.0, max(0.0, fx_age_minutes) / 720.0)
        else:
            regional_candidate_diagnostics.append("local_fx_age_unavailable")
    local_quote_score_penalty = round(local_quote_score_penalty, 3)
    actionable = direction != "watch_only" and observation.get("data_status") == "reachable" and not reject_reason
    score = 0.0
    if actionable:
        score = min(100.0, 24.0 + edge * 1.25 + liq * 18.0 - min(spread * 1.2, 20.0) + min(source_venue_count, 8))
        if quality_score is not None:
            score += (float(quality_score) - 50.0) * 0.25
        # Local premium telemetry is a paper-only ordering signal.  It does
        # not turn an otherwise priceable observation into a watch-only row.
        score -= local_quote_score_penalty
        score = max(0.0, min(100.0, score))
    elif observation.get("data_status") == "reachable":
        score = min(25.0, 8.0 + abs(deviation) * 0.3 + liq * 10.0)
    feasibility = _preliminary_feasibility(direction, observation["market_type"], observation["data_status"], settings)
    funding_bps = (float(observation.get("funding_rate") or 0.0) * 10_000.0) if observation.get("funding_rate") is not None else 0.0
    change_24h_pct = float(as_float(observation.get("change_24h_pct"), 0.0) or 0.0)
    recent_volatility_bps = as_float(observation.get("realized_volatility_bps"), None)
    if recent_volatility_bps is None:
        recent_volatility_bps = abs(change_24h_pct) * 100.0
    microstructure_history_ready = as_float(
        observation.get("microstructure_history_ready"), 0.0
    ) or 0.0
    local_short_horizon_trend_bps = (
        as_float(observation.get("return_1m_bps"), None)
        if microstructure_history_ready >= 1.0
        else None
    )
    candidate = {
        "seen_at": observation["last_checked_at"],
        "venue": observation["venue"],
        "inst_id": observation["instrument_id"],
        "symbol": observation["symbol"],
        "region": observation.get("region"),
        "base": observation.get("base"),
        "quote": observation.get("quote"),
        "quote_ccy": observation.get("quote_ccy") or observation.get("quote"),
        "comparison_key": observation.get("comparison_key"),
        "source_venue_count": source_venue_count,
        "asset_class": "crypto_derivatives" if observation["market_type"] == "perp" else "crypto_spot",
        "trade_type": "frontier_crypto_venue_map",
        "frontier_paper_admission_guard_applies": True,
        "direction": direction,
        "execution_feasibility": feasibility,
        "thesis": "frontier crypto venue map price/funding dislocation candidate",
        "last": round(last, 8),
        "usd_normalized_last": observation.get("usd_normalized_last"),
        "native_quote_currency": observation.get("native_quote_currency") or observation.get("quote"),
        "canonical_quote_currency": observation.get("canonical_quote_currency"),
        "canonical_normalized_price": observation.get("canonical_normalized_price"),
        "comparison_price": round(comparison_price, 8) if comparison_price else None,
        "reference_price": round(float(reference_price or 0.0), 8),
        "quote_normalization_status": observation.get("quote_normalization_status"),
        "quote_normalization_source": observation.get("quote_normalization_source"),
        "fx_source": observation.get("fx_source"),
        "fx_age_seconds": observation.get("fx_age_seconds"),
        "fx_to_usd": observation.get("fx_to_usd"),
        "fx_age_minutes": observation.get("fx_age_minutes"),
        "normalized_mid_usd": observation.get("normalized_mid_usd"),
        "premium_vs_reference_bps": observation.get("premium_vs_reference_bps"),
        "local_quote_flag": bool(observation.get("local_quote_flag")),
        "local_quote_score_penalty": local_quote_score_penalty,
        "local_quote_diagnostics": regional_candidate_diagnostics,
        "suppression_reason": observation.get("suppression_reason"),
        "product_metadata_validated": bool(observation.get("product_metadata_validated")),
        "conversion_path_validated": bool(observation.get("conversion_path_validated")),
        "paper_only_quote_normalization": bool(observation.get("paper_only_quote_normalization")),
        "quote_ranking_eligible": bool(observation.get("quote_ranking_eligible", True)),
        "quote_ranking_reason": observation.get("quote_ranking_reason"),
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
        "change_24h_pct": round(change_24h_pct, 3),
        "quote_volume_24h": round(float(observation.get("quote_volume_24h") or 0.0), 3),
        "liquidity_score": liq,
        "depth_liquidity_score": observation.get("depth_liquidity_score"),
        "spread_bps": spread,
        "local_short_horizon_trend_bps": local_short_horizon_trend_bps,
        "local_short_horizon_trend_window": "1m",
        "local_short_horizon_trend_ready": microstructure_history_ready >= 1.0,
        "microstructure_history_ready": microstructure_history_ready,
        "microstructure_status": observation.get("microstructure_status"),
        "recent_volatility_bps": round(float(recent_volatility_bps), 3),
        "score": round(max(0.0, score), 3),
        "dislocation_quality_score": dislocation_quality["score"],
        "dislocation_quality": dislocation_quality,
        "dislocation_quality_components": dislocation_quality["components"],
        "dislocation_quality_diagnostics": dislocation_quality["diagnostics"],
        "data_status": observation["data_status"],
        "http_status": observation["http_status"],
        "latency_ms": observation["latency_ms"],
        "route_id": observation["route_id"],
        "candidate_reject_reason": reject_reason,
        "quality_status": quality_status,
        "quality_score": quality_score,
        "venue_health_score": observation.get("venue_health_score"),
        "venue_health": observation.get("venue_health"),
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
        "quality_flags": observation.get("quality_flags") or {},
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
    if observation.get("market_type") == "spot":
        candidate.update(native_spot_surface_fields(observation))
    # Route facts must be present before either context-cost sizing or the
    # frontier ranking pass reads this candidate.  The helper only attaches a
    # non-blocking paper report for short frontier spot candidates.
    candidate = _refresh_frontier_short_route_requirements_report(candidate, settings)
    candidate = annotate_paper_context_cost(candidate, settings)
    candidate = _apply_frontier_effective_edge(candidate, observation, settings)
    candidate = _apply_frontier_short_market_context(
        candidate, observation, reference_observations, settings
    )
    _annotate_frontier_quote_context(candidate, observation, settings)
    candidate = _apply_frontier_marketability_gate(candidate, observation, settings, reference_observations)
    candidate = _annotate_frontier_short_paper_diagnostics(candidate, observation, settings)
    candidate["paper_venue_diagnostics"] = _build_paper_venue_diagnostics(candidate, observation)
    return candidate


def _strategy_lab_observation(row: dict) -> dict:
    """Attach canonical frontier metadata before observation-program evaluation.

    Frontier scans build ordinary candidates and complete price observations from
    the same raw venue rows.  Candidate construction already derives these
    fields, but observation programs run on the latter path and must not lose
    either the source identity or public-candle confirmation values.
    """

    output = dict(row)
    market_type = str(output.get("market_type") or "spot").lower()
    output["market_type"] = market_type
    output["inst_id"] = output.get("inst_id") or output.get("instrument_id")
    output["observed_at"] = (
        output.get("observed_at")
        or output.get("last_checked_at")
        or _utc_now()
    )
    output["asset_class"] = (
        "crypto_derivatives" if market_type in {"perp", "future", "futures"} else "crypto_spot"
    )
    output["trade_type"] = "frontier_crypto_venue_map"
    output.setdefault("price_source", f"{output.get('venue')} public REST")
    return output


def build_scan_batch(
    settings: dict,
    limit: int | None = None,
    required_inst_ids: set[str] | None = None,
    conn=None,
    *,
    write_preliminary_report: bool = True,
) -> ScanBatch:
    registry = load_venue_registry()
    strategy_intraday_requirements = _active_strategy_intraday_requirements(conn)
    all_observations = scan_venues(
        settings,
        selected_only=False,
        required_inst_ids=required_inst_ids,
        conn=conn,
    )
    all_observations = [_strategy_lab_observation(row) for row in all_observations]
    observations = _select_observations(all_observations, registry)
    selected_ids = {row.get("instrument_id") for row in observations}
    for row in all_observations:
        if row.get("instrument_id") in (required_inst_ids or set()) and row.get("instrument_id") not in selected_ids:
            observations.append(row)
            selected_ids.add(row.get("instrument_id"))
    for row in all_observations:
        if (
            _strategy_intraday_priority(row, strategy_intraday_requirements) > 0
            and row.get("instrument_id") not in selected_ids
        ):
            observations.append(row)
            selected_ids.add(row.get("instrument_id"))
    observations, intraday_summary = enrich_intraday_features(
        observations,
        settings,
        registry,
        strategy_requirements=strategy_intraday_requirements,
    )
    enriched_by_id = {
        str(row.get("instrument_id")): row
        for row in observations
        if row.get("instrument_id")
    }
    all_observations = [
        enriched_by_id.get(str(row.get("instrument_id") or ""), row)
        for row in all_observations
    ]
    refs = _reference_prices(observations, settings)
    venue_counts = collections.Counter(
        row.get("comparison_key")
        for row in observations
        if row.get("data_status") == "reachable" and row.get("comparison_key") and not row.get("local_quote_observe_only")
    )
    candidates = [
        _candidate_from_observation(
            row,
            settings,
            refs.get(str(row.get("comparison_key"))),
            venue_counts.get(row.get("comparison_key"), 0),
            reference_observations=observations,
        )
        for row in observations
        if row.get("data_status") == "reachable" and row.get("comparison_key") in refs
    ]
    candidates = rank_frontier_paper_candidates(candidates, settings)
    if limit:
        candidates = candidates[: int(limit)]
    if write_preliminary_report:
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
            "intraday_features": intraday_summary,
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


def _venue_quote_context(candidates: list[dict]) -> dict[str, dict]:
    """Summarize paper-only quote diagnostics by frontier venue.

    The scanner still emits every priceable candidate.  This report makes the
    ranking evidence visible per venue so later tuning can distinguish a
    venue-wide quote-context issue from a single candidate's edge estimate.
    """

    grouped: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        venue = str(candidate.get("venue") or "unknown")
        bucket = grouped.setdefault(
            venue,
            {
                "candidate_count": 0,
                "synthetic_route_count": 0,
                "stale_candidate_count": 0,
                "staleness_reasons": collections.Counter(),
                "spread_bps": [],
                "quote_age_ms": [],
                "top_of_book_depth_notional": [],
                "top_of_book_depth_usd": [],
                "venue_score": [],
                "venue_confidence_score": [],
                "ranking_penalty_points": [],
            },
        )
        bucket["candidate_count"] += 1
        bucket["synthetic_route_count"] += int(bool(candidate.get("synthetic_route_flag")))
        reason = candidate.get("staleness_reason")
        if reason:
            bucket["stale_candidate_count"] += 1
            bucket["staleness_reasons"][str(reason)] += 1
        for field in (
            "spread_bps",
            "quote_age_ms",
            "top_of_book_depth_notional",
            "top_of_book_depth_usd",
            "venue_score",
            "venue_confidence_score",
            "quote_context_ranking_penalty",
        ):
            value = as_float(candidate.get(field), None)
            if value is not None:
                target = "ranking_penalty_points" if field == "quote_context_ranking_penalty" else field
                bucket[target].append(value)

    return {
        venue: {
            "paper_only": True,
            "candidate_count": values["candidate_count"],
            "synthetic_route_count": values["synthetic_route_count"],
            "stale_candidate_count": values["stale_candidate_count"],
            "staleness_reasons": dict(values["staleness_reasons"]),
            "spread_bps": _distribution(values["spread_bps"]),
            "quote_age_ms": _distribution(values["quote_age_ms"]),
            "top_of_book_depth_notional": _distribution(values["top_of_book_depth_notional"]),
            "top_of_book_depth_usd": _distribution(values["top_of_book_depth_usd"]),
            "venue_score": _distribution(values["venue_score"]),
            "venue_confidence_score": _distribution(values["venue_confidence_score"]),
            "ranking_penalty_points": _distribution(values["ranking_penalty_points"]),
        }
        for venue, values in sorted(grouped.items())
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
    by_quote_suppression = collections.Counter(
        row.get("suppression_reason")
        for row in observations
        if row.get("suppression_reason")
    )
    route_status = collections.Counter((row.get("execution_feasibility") or {}).get("status", "unknown") for row in candidates)
    route_blockers: collections.Counter[str] = collections.Counter()
    quality_statuses = collections.Counter(row.get("quality_status", "unknown") for row in observations)
    anomaly_counts: collections.Counter[str] = collections.Counter()
    freshness_values = []
    depth_latency_values = []
    buy_slippage_values = []
    sell_slippage_values = []
    local_quote_premiums = []
    local_quote_fx_ages = []
    local_quote_by_venue: collections.Counter[str] = collections.Counter()
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
        if row.get("local_quote_flag"):
            local_quote_by_venue[str(row.get("venue") or "unknown")] += 1
            premium = as_float(row.get("premium_vs_reference_bps"), None)
            if premium is not None:
                local_quote_premiums.append(premium)
            fx_age_minutes = as_float(row.get("fx_age_minutes"), None)
            if fx_age_minutes is not None:
                local_quote_fx_ages.append(fx_age_minutes)
    for row in candidates:
        for blocker in (row.get("execution_feasibility") or {}).get("route_blockers", []):
            route_blockers[blocker] += 1
    quality_known_count = sum(
        count for status, count in quality_statuses.items() if status in {"verified", "degraded"}
    )
    depth_summary = quality_summary or {}
    venue_quote_context = _venue_quote_context(candidates)
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
        "zero_quality_venue_probe": depth_summary.get("zero_quality_venue_probe", {}),
        "market_testing_progress": depth_summary.get("market_testing_progress", {}),
        "selected_by_venue": depth_summary.get("selected_by_venue", {}),
        "starved_selected_by_venue": depth_summary.get("starved_selected_by_venue", {}),
        "starved_venue_coverage": starved_coverage,
        "by_region": dict(by_region),
        "by_quote": dict(by_quote),
        "by_quote_normalization": dict(by_quote_normalization),
        "by_quote_suppression": dict(by_quote_suppression),
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
    marketability_statuses = collections.Counter(
        row.get("marketability_gate_status", "not_evaluated") for row in candidates
    )
    marketability_failures: collections.Counter[str] = collections.Counter()
    dislocation_quality_diagnostics: collections.Counter[str] = collections.Counter()
    dislocation_quality_scores = []
    short_market_context_diagnostics: collections.Counter[str] = collections.Counter()
    short_market_context_scores = []
    short_market_context_counterfactual_count = 0
    cost_swallowed_diagnostic_count = 0
    cost_swallowed_check_counts: collections.Counter[str] = collections.Counter()
    short_net_edges = []
    short_cost_components: collections.Counter[str] = collections.Counter()
    route_feasibility_reason_counts: collections.Counter[str] = collections.Counter()
    route_feasibility_status_counts: collections.Counter[str] = collections.Counter()
    route_feasibility_verified_exception_count = 0
    for row in candidates:
        marketability_failures.update((row.get("marketability_gate") or {}).get("failed_checks") or [])
        dislocation_quality_diagnostics.update(row.get("dislocation_quality_diagnostics") or [])
        score = as_float(row.get("dislocation_quality_score"), None)
        if score is not None:
            dislocation_quality_scores.append(score)
        market_context = row.get("frontier_short_market_context") or {}
        if isinstance(market_context, dict) and market_context.get("applicable"):
            context_score = as_float(market_context.get("score"), None)
            if context_score is not None:
                short_market_context_scores.append(context_score)
            short_market_context_diagnostics.update(market_context.get("diagnostics") or [])
            if market_context.get("emission_action") == "counterfactual_guard_value":
                short_market_context_counterfactual_count += 1
        cost_diagnostic = row.get("paper_cost_diagnostic") or {}
        if isinstance(cost_diagnostic, dict) and cost_diagnostic.get("reason") == "cost_swallowed_after_slippage":
            cost_swallowed_diagnostic_count += 1
            cost_swallowed_check_counts.update(str(check.get("code")) for check in (cost_diagnostic.get("checks") or []) if isinstance(check, dict) and check.get("code"))
        short_cost = row.get("short_cost_decomposition") or {}
        if isinstance(short_cost, dict) and short_cost.get("applicable"):
            net_edge = as_float(short_cost.get("net_edge_bps"), None)
            if net_edge is not None:
                short_net_edges.append(net_edge)
            for key in (
                "spread_cost_bps",
                "depth_slippage_cost_bps",
                "venue_quality_penalty_bps",
                "synthetic_route_penalty_bps",
                "borrow_proxy_penalty_bps",
            ):
                value = as_float(short_cost.get(key), None)
                if value is not None:
                    short_cost_components[key] += value
        route_feasibility_reason = str(row.get("route_feasibility_reason") or "").strip()
        if route_feasibility_reason:
            route_feasibility_reason_counts[route_feasibility_reason] += 1
        if route_feasibility_reason and bool(row.get("paper_active_scoring_eligible", True)):
            if route_feasibility_reason in {"verified_standard_short_route", "explicit_borrow_ok"}:
                route_feasibility_status_counts["allowed_verified_exception"] += 1
                route_feasibility_verified_exception_count += 1
            else:
                route_feasibility_status_counts["allowed_other_route_reason"] += 1
        elif bool(row.get("paper_route_feasibility_shadow_label")):
            route_feasibility_status_counts["shadow_only_route_feasibility"] += 1
    active_candidate_count = sum(
        1
        for row in candidates
        if row.get("direction") != "watch_only"
        and not row.get("paper_entry_blocked")
        and not row.get("candidate_reject_reason")
        and bool(row.get("paper_active_scoring_eligible", True))
    )
    shadow_only_candidate_count = sum(
        1
        for row in candidates
        if row.get("direction") == "watch_only"
        or row.get("paper_entry_blocked")
        or row.get("quality_action") == "shadow_only"
        or bool(row.get("candidate_reject_reason"))
        or not bool(row.get("paper_active_scoring_eligible", True))
    )
    route_feasibility_shadow_count = sum(
        1 for row in candidates if bool(row.get("paper_route_feasibility_shadow_label"))
    )
    candidate_activity = {
        "active_paper_review_candidates": active_candidate_count,
        "shadow_or_observe_only_candidates": shadow_only_candidate_count,
        "route_feasibility_shadow_candidates": route_feasibility_shadow_count,
        "route_feasibility_verified_exception_candidates": route_feasibility_verified_exception_count,
        "route_feasibility_reason_counts": dict(route_feasibility_reason_counts),
        "route_feasibility_status_counts": dict(route_feasibility_status_counts),
        "regional_admitted_candidates": regional_candidate_blockers.get("passed", 0),
        "regional_blocked_candidates": sum(
            count
            for gate, count in regional_candidate_blockers.items()
            if gate not in {"passed", "not_applicable"}
        ),
        "marketability_confirmed_route_candidates": marketability_statuses.get("passed", 0),
        "marketability_conservative_route_candidates": marketability_statuses.get("failed", 0),
    }
    expansion_map.update(
        {
            "candidate_activity": candidate_activity,
            "active_paper_review_candidates": active_candidate_count,
            "shadow_or_observe_only_candidates": shadow_only_candidate_count,
            "route_feasibility_shadow_candidates": route_feasibility_shadow_count,
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
            "gross_edge_bps": row.get("gross_edge_bps"),
            "modeled_cost_bps": row.get("modeled_cost_bps"),
            "net_edge_bps": row.get("net_edge_bps"),
            "freshness_minutes": row.get("freshness_minutes"),
            "gating_reason": row.get("gating_reason"),
            "quality_score": row.get("quality_score"),
            "quality_status": row.get("quality_status"),
            "quality_action": row.get("quality_action"),
            "anomaly_flags": row.get("anomaly_flags", []),
            "paper_cost_diagnostic": row.get("paper_cost_diagnostic"),
            "route_status": (row.get("execution_feasibility") or {}).get("status"),
            "route_blockers": (row.get("execution_feasibility") or {}).get("route_blockers", []),
            "spread_bps": row.get("spread_bps"),
            "quote_normalization_status": row.get("quote_normalization_status"),
            "native_quote_currency": row.get("native_quote_currency") or row.get("quote"),
            "canonical_normalized_price": row.get("canonical_normalized_price"),
            "fx_source": row.get("fx_source"),
            "fx_age_seconds": row.get("fx_age_seconds"),
            "suppression_reason": row.get("suppression_reason"),
            "marketability_gate_status": row.get("marketability_gate_status"),
            "marketability_failed_checks": (row.get("marketability_gate") or {}).get("failed_checks", []),
            "route_health_confirmed": (row.get("route_health_confirmation") or {}).get("confirmed"),
            "simulated_order_allocation": row.get("simulated_order_allocation"),
            "dislocation_quality_score": row.get("dislocation_quality_score"),
            "dislocation_quality_components": row.get("dislocation_quality_components"),
            "dislocation_quality_diagnostics": row.get("dislocation_quality_diagnostics", []),
            "paper_quality_rank": row.get("paper_quality_rank"),
            "paper_quality_cohort": row.get("paper_quality_cohort"),
            "paper_ranking_edge_bps": row.get("paper_ranking_edge_bps"),
            "paper_ranking_edge_source": row.get("paper_ranking_edge_source"),
            "short_cost_decomposition": row.get("short_cost_decomposition"),
            "frontier_short_market_context": row.get("frontier_short_market_context"),
            "paper_market_context_filter_status": row.get("paper_market_context_filter_status"),
            "quote_age_ms": row.get("quote_age_ms"),
            "top_of_book_depth_notional": row.get("top_of_book_depth_notional"),
            "synthetic_route_flag": row.get("synthetic_route_flag"),
            "venue_score": row.get("venue_score"),
            "staleness_reason": row.get("staleness_reason"),
            "quote_context_ranking_penalty": row.get("quote_context_ranking_penalty"),
            "quote_ccy": row.get("quote_ccy"),
            "fx_to_usd": row.get("fx_to_usd"),
            "fx_age_minutes": row.get("fx_age_minutes"),
            "normalized_mid_usd": row.get("normalized_mid_usd"),
            "premium_vs_reference_bps": row.get("premium_vs_reference_bps"),
            "local_quote_flag": row.get("local_quote_flag"),
            "local_quote_diagnostics": row.get("local_quote_diagnostics", []),
            "route_feasibility_reason": row.get("route_feasibility_reason"),
            "paper_active_scoring_eligible": row.get("paper_active_scoring_eligible"),
            "paper_route_feasibility_shadow_label": row.get(
                "paper_route_feasibility_shadow_label"
            ),
            "paper_venue_diagnostics": row.get("paper_venue_diagnostics"),
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
        "by_quote_suppression": dict(by_quote_suppression),
        "regional_observation_count": sum(1 for row in observations if row.get("quote") in REGIONAL_FIAT_QUOTES),
        "local_quote_normalization": {
            "paper_only": True,
            "supported_venues": sorted(LOCAL_FIAT_CEX_VENUES),
            "observation_count": sum(local_quote_by_venue.values()),
            "by_venue": dict(local_quote_by_venue),
            "premium_vs_reference_bps": _distribution(local_quote_premiums),
            "fx_age_minutes": _distribution(local_quote_fx_ages),
            "diagnostic_pattern": (
                "Bilateral failure across long and synthetic-short frontier spot signals suggests structural "
                "pricing/context error rather than a clean directional edge."
            ),
        },
        "regional_candidate_count": sum(1 for row in candidates if row.get("quote") in REGIONAL_FIAT_QUOTES),
        "active_paper_review_candidate_count": active_candidate_count,
        "shadow_or_observe_only_candidate_count": shadow_only_candidate_count,
        "candidate_activity": candidate_activity,
        "paper_short_route_gate": {
            "enabled": bool(route_feasibility_reason_counts or route_feasibility_shadow_count),
            "candidate_count": sum(route_feasibility_reason_counts.values()),
            "shadow_candidate_count": route_feasibility_shadow_count,
            "verified_exception_count": route_feasibility_verified_exception_count,
            "status_counts": dict(route_feasibility_status_counts),
            "route_feasibility_reason_counts": dict(route_feasibility_reason_counts),
        },
        "by_preliminary_route_status": dict(route_status),
        "by_route_blocker": dict(route_blockers),
        "venue_quote_context": venue_quote_context,
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
        "marketability_gate_counts": dict(marketability_statuses),
        "marketability_failure_counts": dict(marketability_failures),
        "dislocation_quality_score": _distribution(dislocation_quality_scores),
        "dislocation_quality_diagnostics": dict(dislocation_quality_diagnostics),
        "dislocation_quality_cohort_outcomes": depth_summary.get(
            "dislocation_quality_cohort_outcomes", {}
        ),
        "short_cost_decomposition": {
            "paper_only": True,
            "candidate_count": len(short_net_edges),
            "net_edge_bps": _distribution(short_net_edges),
            "cost_component_totals_bps": {
                key: round(value, 6) for key, value in short_cost_components.items()
            },
        },
        "frontier_short_market_context": {
            "paper_only": True,
            "candidate_count": len(short_market_context_scores),
            "counterfactual_candidate_count": short_market_context_counterfactual_count,
            "score": _distribution(short_market_context_scores),
            "diagnostics": dict(short_market_context_diagnostics),
        },
        "paper_cost_swallowed_diagnostics": {"paper_only": True, "candidate_count": cost_swallowed_diagnostic_count, "check_counts": dict(cost_swallowed_check_counts), "emission_action": "counterfactual_guard_value"},
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
                "modeled_cost_bps": row.get("modeled_cost_bps"),
                "net_edge_bps": row.get("net_edge_bps"),
                "freshness_minutes": row.get("freshness_minutes"),
                "gating_reason": row.get("gating_reason"),
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
    lines.append(f"- Quote suppression: `{summary.get('by_quote_suppression', {})}`")
    local_quote = summary.get("local_quote_normalization", {})
    lines.append(f"- Local-fiat telemetry: `{local_quote}`")
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
    lines.append(f"- Dislocation quality scores: `{summary.get('dislocation_quality_score', {})}`")
    lines.append(f"- Dislocation quality diagnostics: `{summary.get('dislocation_quality_diagnostics', {})}`")
    lines.append(f"- Paper cohort outcomes: `{summary.get('dislocation_quality_cohort_outcomes', {})}`")
    lines.append(f"- Paper-only short cost decomposition: `{summary.get('short_cost_decomposition', {})}`")
    lines.append(f"- Frontier-short market context: `{summary.get('frontier_short_market_context', {})}`")
    lines.append(f"- Cost-swallowed paper diagnostics: `{summary.get('paper_cost_swallowed_diagnostics', {})}`")
    lines.append(f"- Venue quote context: `{summary.get('venue_quote_context', {})}`")
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
    lines.append(f"- Zero-quality venue probes: `{expansion.get('zero_quality_venue_probe', {})}`")
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
            f"quality_rank=`{row.get('paper_quality_rank')}` dislocation_quality=`{row.get('dislocation_quality_score')}` "
            f"dev=`{row.get('venue_deviation_bps')}`bps edge=`{row.get('edge_bps_estimate')}`bps "
            f"spread=`{row.get('spread_bps')}`bps quote_age=`{row.get('quote_age_ms')}`ms "
            f"top_depth=`{row.get('top_of_book_depth_notional')}` synthetic=`{row.get('synthetic_route_flag')}` "
            f"venue_score=`{row.get('venue_score')}` stale_reason=`{row.get('staleness_reason')}` "
            f"quote_penalty=`{row.get('quote_context_ranking_penalty')}` "
            f"quality=`{row.get('quality_score')}` action=`{row.get('quality_action')}` "
            f"cost_diag=`{row.get('paper_cost_diagnostic')}` "
            f"quote_norm=`{row.get('quote_normalization_status')}` "
            f"native_quote=`{row.get('native_quote_currency')}` canonical_price=`{row.get('canonical_normalized_price')}` "
            f"fx_source=`{row.get('fx_source')}` fx_age=`{row.get('fx_age_seconds')}` suppress=`{row.get('suppression_reason')}` "
            f"quote_ccy=`{row.get('quote_ccy')}` fx_to_usd=`{row.get('fx_to_usd')}` "
            f"normalized_mid_usd=`{row.get('normalized_mid_usd')}` premium_bps=`{row.get('premium_vs_reference_bps')}` "
            f"local_quote=`{row.get('local_quote_flag')}` "
            f"route=`{row.get('route_status')}` blockers={row.get('route_blockers')} "
            f"route_reason=`{row.get('route_feasibility_reason')}` "
            f"active_scoring=`{row.get('paper_active_scoring_eligible')}` "
            f"route_health_confirmed=`{row.get('route_health_confirmed')}` "
            f"simulated_allocation=`{row.get('simulated_order_allocation')}`"
        )
    lines.extend(["", "## Venue Health Sample", ""])
    for row in report.get("observations", [])[:60]:
        lines.append(
            f"- `{row['venue']}` `{row['symbol']}` `{row['market_type']}` "
            f"status=`{row['data_status']}` http=`{row['http_status']}` latency=`{row['latency_ms']}`ms "
            f"last=`{row.get('last')}` spread=`{row.get('spread_bps')}`bps volume=`{row.get('quote_volume_24h')}` "
            f"native_quote=`{row.get('native_quote_currency')}` canonical_price=`{row.get('canonical_normalized_price')}` "
            f"fx_source=`{row.get('fx_source')}` fx_age=`{row.get('fx_age_seconds')}` suppress=`{row.get('suppression_reason')}` "
            f"quote_ccy=`{row.get('quote_ccy')}` fx_to_usd=`{row.get('fx_to_usd')}` "
            f"normalized_mid_usd=`{row.get('normalized_mid_usd')}` premium_bps=`{row.get('premium_vs_reference_bps')}` "
            f"local_quote=`{row.get('local_quote_flag')}` "
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
