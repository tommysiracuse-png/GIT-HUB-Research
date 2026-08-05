#!/usr/bin/env python3
"""Paper-only signal diagnostics, shadow variants, and evidence-gated promotion."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
import sqlite3
import statistics

from frontier_crypto_adapter import (
    build_variant_candidates,
    load_venue_registry,
    write_outputs as write_frontier_outputs,
)
from frontier_data_quality import (
    annotate_venue_quality_scores,
    enrich_observations,
    market_testing_progress,
    persist_quality_snapshots,
    quality_outcome_relationship,
    select_enrichment_observations,
    venue_quality_scores,
)
from storage import (
    RUNS_DIR,
    add_memory_fact,
    add_self_improvement_experiment,
    signal_key,
    utc_now,
)


REPORT_JSON = RUNS_DIR / "signal_redesign_report.json"
REPORT_MD = RUNS_DIR / "signal_redesign_report.md"
SIGNAL_FAMILY = "frontier_crypto_venue_map"

ALLOWED_CONFIG_KEYS = {
    "reference_grouping",
    "estimator",
    "leave_one_out",
    "min_unique_venues",
    "min_dislocation_bps",
    "max_spread_bps",
    "min_liquidity_score",
    "direction_mode",
    "fee_bps_per_side",
    "slippage_bps_per_side",
    "allowed_venues",
    "blocked_venues",
    "allowed_directions",
    "allowed_route_statuses",
    "min_quality_score",
    "min_depth_adjusted_edge_bps",
    "allowed_quote_normalization_statuses",
    "min_source_venue_count",
    "max_round_trip_cost_bps",
    "require_public_order_book",
    "allow_regional_quotes",
}

DEFAULT_VARIANTS = [
    {
        "variant_id": "frontier_v1_incumbent",
        "version": 1,
        "title": "Current frontier signal",
        "status": "active",
        "config": {
            "reference_grouping": "base",
            "estimator": "median",
            "leave_one_out": False,
            "min_unique_venues": 2,
            "min_dislocation_bps": 12.0,
            "max_spread_bps": 8.0,
            "min_liquidity_score": 0.0,
            "direction_mode": "both",
            "fee_bps_per_side": 5.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": [],
            "blocked_venues": [],
            "allowed_directions": [],
            "allowed_route_statuses": [],
            "min_quality_score": 0.0,
            "min_depth_adjusted_edge_bps": 0.0,
            "allowed_quote_normalization_statuses": [],
            "min_source_venue_count": 2,
            "max_round_trip_cost_bps": 1000.0,
            "require_public_order_book": False,
            "allow_regional_quotes": True,
        },
    },
    {
        "variant_id": "frontier_v2_quote_matched",
        "version": 2,
        "title": "Quote-matched leave-one-out reference",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 12.0,
            "max_spread_bps": 8.0,
            "min_liquidity_score": 0.0,
            "direction_mode": "both",
            "fee_bps_per_side": 5.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": [],
            "blocked_venues": [],
            "allowed_directions": [],
            "allowed_route_statuses": [],
            "min_quality_score": 0.0,
            "min_depth_adjusted_edge_bps": 0.0,
            "allowed_quote_normalization_statuses": [],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 1000.0,
            "require_public_order_book": False,
            "allow_regional_quotes": True,
        },
    },
    {
        "variant_id": "frontier_v3_quality_short",
        "version": 3,
        "title": "Quality-gated frontier shorts",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 12.0,
            "max_spread_bps": 3.0,
            "min_liquidity_score": 0.35,
            "direction_mode": "short_only",
            "fee_bps_per_side": 5.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": [],
            "blocked_venues": [],
            "allowed_directions": ["short_frontier_spot"],
            "allowed_route_statuses": [],
            "min_quality_score": 0.0,
            "min_depth_adjusted_edge_bps": 0.0,
            "allowed_quote_normalization_statuses": [],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 1000.0,
            "require_public_order_book": False,
            "allow_regional_quotes": True,
        },
    },
    {
        "variant_id": "frontier_v4_long_recovery",
        "version": 4,
        "title": "Quality-gated long recovery monitor",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 12.0,
            "max_spread_bps": 3.0,
            "min_liquidity_score": 0.35,
            "direction_mode": "long_only",
            "fee_bps_per_side": 5.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": [],
            "blocked_venues": [],
            "allowed_directions": ["long_frontier_spot"],
            "allowed_route_statuses": [],
            "min_quality_score": 0.0,
            "min_depth_adjusted_edge_bps": 0.0,
            "allowed_quote_normalization_statuses": [],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 1000.0,
            "require_public_order_book": False,
            "allow_regional_quotes": True,
        },
    },
    {
        "variant_id": "frontier_v5_short_route_quality",
        "version": 5,
        "title": "Route-aware quality frontier shorts",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 18.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.35,
            "direction_mode": "short_only",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": [],
            "blocked_venues": [],
            "allowed_directions": ["short_frontier_spot"],
            "allowed_route_statuses": ["standard", "conditional"],
            "min_quality_score": 65.0,
            "min_depth_adjusted_edge_bps": 8.0,
            "allowed_quote_normalization_statuses": ["usd_like", "same_venue_stablecoin_reference"],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 45.0,
            "require_public_order_book": True,
            "allow_regional_quotes": False,
        },
    },
    {
        "variant_id": "frontier_v6_long_reversal_strict",
        "version": 6,
        "title": "Strict frontier long recovery test",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 4,
            "min_dislocation_bps": 20.0,
            "max_spread_bps": 3.0,
            "min_liquidity_score": 0.45,
            "direction_mode": "long_only",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": [],
            "blocked_venues": [],
            "allowed_directions": ["long_frontier_spot"],
            "allowed_route_statuses": ["standard"],
            "min_quality_score": 75.0,
            "min_depth_adjusted_edge_bps": 10.0,
            "allowed_quote_normalization_statuses": ["usd_like"],
            "min_source_venue_count": 4,
            "max_round_trip_cost_bps": 35.0,
            "require_public_order_book": True,
            "allow_regional_quotes": False,
        },
    },
    {
        "variant_id": "frontier_v7_venue_isolated_gate",
        "version": 7,
        "title": "Venue-isolated frontier quality gate",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 16.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.35,
            "direction_mode": "both",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": ["BINANCE_US", "COINBASE", "GATE", "KRAKEN", "MEXC"],
            "blocked_venues": [],
            "allowed_directions": ["long_frontier_spot", "short_frontier_spot"],
            "allowed_route_statuses": ["standard", "conditional"],
            "min_quality_score": 60.0,
            "min_depth_adjusted_edge_bps": 6.0,
            "allowed_quote_normalization_statuses": ["usd_like"],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 45.0,
            "require_public_order_book": True,
            "allow_regional_quotes": False,
        },
    },
    {
        "variant_id": "frontier_v8_regional_observe_then_trade",
        "version": 8,
        "title": "Regional quotes observe-then-trade gate",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 20.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.35,
            "direction_mode": "both",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": ["LUNO", "VALR", "QUIDAX", "INDODAX", "BITKUB"],
            "blocked_venues": [],
            "allowed_directions": ["long_frontier_spot", "short_frontier_spot"],
            "allowed_route_statuses": ["standard", "conditional"],
            "min_quality_score": 70.0,
            "min_depth_adjusted_edge_bps": 10.0,
            "allowed_quote_normalization_statuses": ["same_venue_stablecoin_reference"],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 40.0,
            "require_public_order_book": True,
            "allow_regional_quotes": True,
        },
    },
    {
        "variant_id": "frontier_v9_short_expansion_quality",
        "version": 9,
        "title": "Quality-gated frontier short expansion",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 16.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.35,
            "direction_mode": "short_only",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": ["BINANCE_US", "GATE", "MEXC"],
            "blocked_venues": [],
            "allowed_directions": ["short_frontier_spot"],
            "allowed_route_statuses": ["standard", "conditional"],
            "min_quality_score": 70.0,
            "min_depth_adjusted_edge_bps": 8.0,
            "allowed_quote_normalization_statuses": ["usd_like", "same_venue_stablecoin_reference"],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 40.0,
            "require_public_order_book": True,
            "allow_regional_quotes": False,
        },
    },
    {
        "variant_id": "frontier_v10_long_survivor_quality",
        "version": 10,
        "title": "Strict frontier long survivor test",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 4,
            "min_dislocation_bps": 20.0,
            "max_spread_bps": 3.0,
            "min_liquidity_score": 0.45,
            "direction_mode": "long_only",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": ["BINANCE_US", "COINBASE", "GATE", "KRAKEN", "MEXC"],
            "blocked_venues": [],
            "allowed_directions": ["long_frontier_spot"],
            "allowed_route_statuses": ["standard"],
            "min_quality_score": 80.0,
            "min_depth_adjusted_edge_bps": 12.0,
            "allowed_quote_normalization_statuses": ["usd_like"],
            "min_source_venue_count": 4,
            "max_round_trip_cost_bps": 30.0,
            "require_public_order_book": True,
            "allow_regional_quotes": False,
        },
    },
    {
        "variant_id": "frontier_v11_regional_fx_depth_probe",
        "version": 11,
        "title": "Regional FX depth-verified frontier probe",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 20.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.35,
            "direction_mode": "both",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": ["BITSO", "BUDA", "INDODAX", "LUNO", "MERCADO_BITCOIN", "VALR"],
            "blocked_venues": [],
            "allowed_directions": ["long_frontier_spot", "short_frontier_spot"],
            "allowed_route_statuses": ["standard", "conditional"],
            "min_quality_score": 70.0,
            "min_depth_adjusted_edge_bps": 10.0,
            "allowed_quote_normalization_statuses": ["same_venue_stablecoin_reference", "external_fx_reference"],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 40.0,
            "require_public_order_book": True,
            "allow_regional_quotes": True,
        },
    },
    {
        "variant_id": "frontier_v12_okx_spot_survivor",
        "version": 12,
        "title": "OKX spot survivor frontier probe",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 16.0,
            "max_spread_bps": 3.5,
            "min_liquidity_score": 0.40,
            "direction_mode": "both",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": ["OKX_SPOT"],
            "blocked_venues": [],
            "allowed_directions": ["long_frontier_spot", "short_frontier_spot"],
            "allowed_route_statuses": ["standard", "conditional"],
            "min_quality_score": 75.0,
            "min_depth_adjusted_edge_bps": 10.0,
            "allowed_quote_normalization_statuses": ["usd_like", "same_venue_stablecoin_reference"],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 35.0,
            "require_public_order_book": True,
            "allow_regional_quotes": False,
        },
    },
    {
        "variant_id": "frontier_v13_gate_mexc_short_probe",
        "version": 13,
        "title": "GATE/MEXC quality-gated short probe",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 16.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.35,
            "direction_mode": "short_only",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": ["GATE", "MEXC"],
            "blocked_venues": [],
            "allowed_directions": ["short_frontier_spot"],
            "allowed_route_statuses": ["standard", "conditional"],
            "min_quality_score": 70.0,
            "min_depth_adjusted_edge_bps": 8.0,
            "allowed_quote_normalization_statuses": ["usd_like", "same_venue_stablecoin_reference"],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 40.0,
            "require_public_order_book": True,
            "allow_regional_quotes": False,
        },
    },
    {
        "variant_id": "frontier_v14_bybit_spot_long_expansion",
        "version": 14,
        "title": "BYBIT spot long quality expansion probe",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 16.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.35,
            "direction_mode": "long_only",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 3.0,
            "allowed_venues": ["BYBIT_SPOT"],
            "blocked_venues": [],
            "allowed_directions": ["long_frontier_spot"],
            "allowed_route_statuses": ["standard"],
            "min_quality_score": 70.0,
            "min_depth_adjusted_edge_bps": 8.0,
            "allowed_quote_normalization_statuses": ["usd_like", "same_venue_stablecoin_reference"],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 40.0,
            "require_public_order_book": True,
            "allow_regional_quotes": False,
        },
    },
    {
        "variant_id": "frontier_v15_bybit_quality_decay_expand",
        "version": 15,
        "title": "BYBIT long quality-decay probation expansion",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 3,
            "min_dislocation_bps": 16.0,
            "max_spread_bps": 3.5,
            "min_liquidity_score": 0.4,
            "direction_mode": "long_only",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 2.5,
            "allowed_venues": ["BYBIT_SPOT"],
            "blocked_venues": [],
            "allowed_directions": ["long_frontier_spot"],
            "allowed_route_statuses": ["standard"],
            "min_quality_score": 75.0,
            "min_depth_adjusted_edge_bps": 8.0,
            "allowed_quote_normalization_statuses": ["usd_like", "same_venue_stablecoin_reference"],
            "min_source_venue_count": 3,
            "max_round_trip_cost_bps": 35.0,
            "require_public_order_book": True,
            "allow_regional_quotes": False,
        },
    },
    {
        "variant_id": "frontier_v16_kucoin_long_repair_probe",
        "version": 16,
        "title": "KUCOIN long diagnostic recovery probe",
        "status": "shadow",
        "config": {
            "reference_grouping": "base_quote",
            "estimator": "median",
            "leave_one_out": True,
            "min_unique_venues": 4,
            "min_dislocation_bps": 18.0,
            "max_spread_bps": 3.0,
            "min_liquidity_score": 0.45,
            "direction_mode": "long_only",
            "fee_bps_per_side": 10.0,
            "slippage_bps_per_side": 2.0,
            "allowed_venues": ["KUCOIN"],
            "blocked_venues": [],
            "allowed_directions": ["long_frontier_spot"],
            "allowed_route_statuses": ["standard"],
            "min_quality_score": 80.0,
            "min_depth_adjusted_edge_bps": 12.0,
            "allowed_quote_normalization_statuses": ["usd_like", "same_venue_stablecoin_reference"],
            "min_source_venue_count": 4,
            "max_round_trip_cost_bps": 30.0,
            "require_public_order_book": True,
            "allow_regional_quotes": False,
        },
    },
]


def _parse_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def _metrics(values: list[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "avg_pnl_bps": None,
            "median_pnl_bps": None,
            "trimmed_mean_bps": None,
            "win_rate": None,
            "worst_bps": None,
            "worst_decile_bps": None,
            "best_bps": None,
        }
    ordered = sorted(values)
    trim = int(len(ordered) * 0.1)
    trimmed = ordered[trim : len(ordered) - trim] if trim and len(ordered) > trim * 2 else ordered
    return {
        "count": len(values),
        "avg_pnl_bps": round(statistics.fmean(values), 3),
        "median_pnl_bps": round(statistics.median(values), 3),
        "trimmed_mean_bps": round(statistics.fmean(trimmed), 3),
        "win_rate": round(sum(value > 0 for value in values) / len(values), 3),
        "worst_bps": round(min(values), 3),
        "worst_decile_bps": round(float(_percentile(values, 0.1)), 3),
        "best_bps": round(max(values), 3),
    }


def validate_variant_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("variant_config must be an object")
    unknown = set(config) - ALLOWED_CONFIG_KEYS
    if unknown:
        raise ValueError(f"unsupported variant fields: {sorted(unknown)}")
    required = ALLOWED_CONFIG_KEYS
    missing = required - set(config)
    if missing:
        raise ValueError(f"missing variant fields: {sorted(missing)}")
    if config["reference_grouping"] not in {"base", "base_quote"}:
        raise ValueError("reference_grouping must be base or base_quote")
    if config["estimator"] != "median":
        raise ValueError("only the median estimator is allowed")
    if not isinstance(config["leave_one_out"], bool):
        raise ValueError("leave_one_out must be boolean")
    if config["direction_mode"] not in {"both", "short_only", "long_only"}:
        raise ValueError("unsupported direction_mode")

    bounded = {
        "min_unique_venues": (2, 12),
        "min_dislocation_bps": (5.0, 500.0),
        "max_spread_bps": (0.1, 20.0),
        "min_liquidity_score": (0.0, 1.0),
        "fee_bps_per_side": (0.0, 50.0),
        "slippage_bps_per_side": (0.0, 50.0),
        "min_quality_score": (0.0, 100.0),
        "min_depth_adjusted_edge_bps": (0.0, 500.0),
        "min_source_venue_count": (1, 20),
        "max_round_trip_cost_bps": (0.0, 1000.0),
    }
    output = dict(config)
    for key, (low, high) in bounded.items():
        value = float(config[key])
        if value < low or value > high:
            raise ValueError(f"{key} must be between {low} and {high}")
        output[key] = int(value) if key in {"min_unique_venues", "min_source_venue_count"} else value

    list_constraints = {
        "allowed_venues": None,
        "blocked_venues": None,
        "allowed_directions": {"long_frontier_spot", "short_frontier_spot"},
        "allowed_route_statuses": {"standard", "conditional", "watch_only", "blocked", "route_unknown"},
        "allowed_quote_normalization_statuses": {
            "usd_like",
            "same_venue_stablecoin_reference",
            "external_fx_reference",
            "missing_same_venue_stablecoin_reference",
            "unsupported_quote",
            "not_normalized",
        },
    }
    for key, allowed in list_constraints.items():
        values = config[key]
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"{key} must be a list of strings")
        normalized = [item.upper() if key.endswith("venues") else item for item in values]
        if allowed is not None:
            unknown_values = set(normalized) - allowed
            if unknown_values:
                raise ValueError(f"{key} contains unsupported values: {sorted(unknown_values)}")
        output[key] = normalized
    for key in ("require_public_order_book", "allow_regional_quotes"):
        if not isinstance(config[key], bool):
            raise ValueError(f"{key} must be boolean")
        output[key] = bool(config[key])
    return output


def ensure_initial_variants(conn: sqlite3.Connection) -> None:
    for variant in DEFAULT_VARIANTS:
        config = validate_variant_config(variant["config"])
        conn.execute(
            """
            insert or ignore into signal_variants (
                variant_id, created_at, signal_family, version, title, status,
                config_json, source_recommendation_id, source_agent, source_model,
                evidence_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                variant["variant_id"],
                utc_now(),
                SIGNAL_FAMILY,
                variant["version"],
                variant["title"],
                variant["status"],
                json.dumps(config, sort_keys=True),
                "manual_task:24321",
                "p95_signal_redesign",
                "deterministic",
                json.dumps(
                    {
                        "task_id": 24321,
                        "adapter_spec_id": 143,
                        "basis": "Reliable-horizon diagnostics and frontier causal redesign.",
                    },
                    sort_keys=True,
                ),
            ),
        )
    active = conn.execute(
        "select count(*) as n from signal_variants where signal_family = ? and status = 'active'",
        (SIGNAL_FAMILY,),
    ).fetchone()["n"]
    if int(active) == 0:
        conn.execute(
            "update signal_variants set status = 'active' where variant_id = 'frontier_v1_incumbent'"
        )
    tracking_started_at = conn.execute(
        "select min(created_at) as started_at from signal_variants where signal_family = ?",
        (SIGNAL_FAMILY,),
    ).fetchone()["started_at"]
    conn.execute(
        """
        update paper_trade_outcomes
        set measurement_status = 'legacy_unverified',
            price_source = coalesce(price_source, 'historical_backfill')
        where trade_id in (
              select id
              from paper_trades
              where closed_at is not null and closed_at < ?
          )
          and measurement_status in ('valid', 'late', 'missing')
        """,
        (tracking_started_at,),
    )
    baseline = _reliable_paper_metrics(conn, horizon=60)
    add_self_improvement_experiment(
        conn,
        "manual_task:24321",
        "p95_signal_redesign",
        "signal_redesign_validation",
        95,
        SIGNAL_FAMILY,
        SIGNAL_FAMILY,
        "Versioned frontier challengers should improve reliable 60-minute paper outcomes.",
        "Run paired incumbent/challenger shadow trials and promote only after fixed evidence gates.",
        baseline,
        {
            "paper_only": True,
            "promotion_horizon_minutes": 60,
            "canonical_task_id": 24321,
            "canonical_spec_id": 143,
        },
    )
    conn.commit()


def create_proposed_variant(
    conn: sqlite3.Connection,
    *,
    title: str,
    config: dict,
    source_recommendation_id: str,
    source_agent: str | None,
    source_model: str | None,
    evidence: dict,
) -> dict:
    validated = validate_variant_config(config)
    digest = hashlib.sha256(
        json.dumps({"title": title, "config": validated}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    variant_id = f"frontier_llm_{digest}"
    row = conn.execute("select variant_id, status from signal_variants where variant_id = ?", (variant_id,)).fetchone()
    if row:
        return {"variant_id": variant_id, "status": row["status"], "created": False}
    version = int(
        conn.execute(
            "select coalesce(max(version), 0) + 1 as version from signal_variants where signal_family = ?",
            (SIGNAL_FAMILY,),
        ).fetchone()["version"]
    )
    conn.execute(
        """
        insert into signal_variants (
            variant_id, created_at, signal_family, version, title, status,
            config_json, source_recommendation_id, source_agent, source_model,
            evidence_json
        ) values (?, ?, ?, ?, ?, 'shadow', ?, ?, ?, ?, ?)
        """,
        (
            variant_id,
            utc_now(),
            SIGNAL_FAMILY,
            version,
            title[:180],
            json.dumps(validated, sort_keys=True),
            source_recommendation_id,
            source_agent,
            source_model,
            json.dumps(evidence or {}, sort_keys=True),
        ),
    )
    add_memory_fact(
        conn,
        "signal_variant",
        variant_id,
        "created",
        title[:180],
        0.85,
        source_agent or "llm_swarm",
        {"config": validated, "evidence": evidence or {}},
    )
    conn.commit()
    return {"variant_id": variant_id, "status": "shadow", "created": True}


def load_variants(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select variant_id, created_at, signal_family, version, title, status,
               config_json, source_recommendation_id, source_agent, source_model,
               evidence_json, promoted_at, retired_at, fallback_variant_id,
               consecutive_passes, evaluation_json
        from signal_variants
        where signal_family = ?
        order by version asc
        """,
        (SIGNAL_FAMILY,),
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json"))
        item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        item["evaluation"] = json.loads(item.pop("evaluation_json") or "{}")
        output.append(item)
    return output


def _trial_bucket(now: dt.datetime, cadence_minutes: int) -> str:
    minute = (now.minute // cadence_minutes) * cadence_minutes
    return now.replace(minute=minute, second=0, microsecond=0).isoformat()


def _sample_retired(variant_id: str, trial_bucket: str) -> bool:
    value = int(hashlib.sha256(f"{variant_id}|{trial_bucket}".encode("utf-8")).hexdigest()[:8], 16)
    return value % 10 == 0


def _trial_skip_reason(candidate: dict, variant: dict, settings: dict) -> str | None:
    cfg = settings.get("signal_redesign", {})
    if candidate.get("direction") == "watch_only" or candidate.get("candidate_reject_reason"):
        return "watch_or_rejected"
    if float(candidate.get("last") or 0.0) <= 0:
        return "missing_entry_price"
    if variant.get("status") != "active":
        if cfg.get("exclude_shadow_trials_with_critical_anomalies", True) and candidate.get("critical_anomaly_flags"):
            return "critical_anomaly_shadow"
        min_quality = float(cfg.get("min_shadow_trial_quality_score", 35.0))
        quality_score = candidate.get("quality_score")
        if quality_score is not None and float(quality_score) < min_quality:
            return "low_quality_shadow"
    return None


def record_variant_trials(
    conn: sqlite3.Connection,
    variants: list[dict],
    candidates_by_variant: dict[str, list[dict]],
    settings: dict,
    scan_id: str,
) -> dict:
    cfg = settings.get("signal_redesign", {})
    cadence = int(cfg.get("trial_cadence_minutes", 15))
    max_trials_per_loop = int(cfg.get("max_trials_per_loop", 160))
    max_trials_per_variant = int(cfg.get("max_trials_per_variant_per_loop", 12))
    max_trials_per_venue = int(cfg.get("max_trials_per_venue_per_loop", 20))
    bucket = _trial_bucket(dt.datetime.now(dt.timezone.utc), cadence)
    created = 0
    by_variant: dict[str, int] = {}
    by_venue: dict[str, int] = {}
    skipped_by_reason: dict[str, int] = {}
    for variant in variants:
        if variant["status"] not in {"active", "shadow", "retired"}:
            continue
        if variant["status"] == "retired" and not _sample_retired(variant["variant_id"], bucket):
            continue
        count = 0
        for candidate in candidates_by_variant.get(variant["variant_id"], []):
            if created >= max_trials_per_loop:
                skipped_by_reason["loop_cap"] = skipped_by_reason.get("loop_cap", 0) + 1
                break
            if count >= max_trials_per_variant:
                skipped_by_reason["variant_cap"] = skipped_by_reason.get("variant_cap", 0) + 1
                break
            venue = str(candidate.get("venue") or "unknown")
            skip_reason = _trial_skip_reason(candidate, variant, settings)
            if skip_reason:
                skipped_by_reason[skip_reason] = skipped_by_reason.get(skip_reason, 0) + 1
                continue
            if by_venue.get(venue, 0) >= max_trials_per_venue:
                skipped_by_reason["venue_cap"] = skipped_by_reason.get("venue_cap", 0) + 1
                continue
            entry = float(candidate.get("last") or 0.0)
            pair_key = f"{bucket}|{candidate['inst_id']}"
            try:
                conn.execute(
                    """
                    insert into signal_trials (
                        created_at, scan_id, trial_bucket, pair_key, variant_id,
                        signal_family, signal_key, inst_id, venue, direction,
                        entry_price, candidate_json, eligible
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now(),
                        scan_id,
                        bucket,
                        pair_key,
                        variant["variant_id"],
                        SIGNAL_FAMILY,
                        signal_key(candidate),
                        candidate["inst_id"],
                        candidate["venue"],
                        candidate["direction"],
                        entry,
                        json.dumps(candidate, sort_keys=True),
                        1 if candidate.get("promotion_eligible", True) else 0,
                    ),
                )
                created += 1
                count += 1
                by_venue[venue] = by_venue.get(venue, 0) + 1
            except sqlite3.IntegrityError:
                skipped_by_reason["duplicate_trial"] = skipped_by_reason.get("duplicate_trial", 0) + 1
                continue
        by_variant[variant["variant_id"]] = count
    conn.commit()
    return {
        "created": created,
        "by_variant": by_variant,
        "by_venue": by_venue,
        "skipped_by_reason": skipped_by_reason,
        "trial_bucket": bucket,
        "caps": {
            "max_trials_per_loop": max_trials_per_loop,
            "max_trials_per_variant_per_loop": max_trials_per_variant,
            "max_trials_per_venue_per_loop": max_trials_per_venue,
        },
    }


def record_trial_outcomes(
    conn: sqlite3.Connection,
    observations: dict[str, dict],
    settings: dict,
) -> list[dict]:
    horizons = settings.get("learning", {}).get("horizon_minutes", [5, 15, 60, 240, 1440])
    max_delay = float(settings.get("learning", {}).get("max_outcome_delay_seconds", 300))
    now = dt.datetime.now(dt.timezone.utc)
    rows = conn.execute(
        """
        select t.id, t.created_at, t.inst_id, t.direction, t.entry_price,
               t.candidate_json, v.config_json
        from signal_trials t
        join signal_variants v on v.variant_id = t.variant_id
        where v.signal_family = ?
        """
        ,
        (SIGNAL_FAMILY,),
    ).fetchall()
    recorded = []
    from paper_loop import direction_sign

    for row in rows:
        opened = _parse_iso(row["created_at"])
        config = json.loads(row["config_json"])
        candidate = json.loads(row["candidate_json"] or "{}")
        sign = direction_sign(row["direction"])
        for horizon in horizons:
            target = opened + dt.timedelta(minutes=int(horizon))
            if now < target:
                continue
            exists = conn.execute(
                "select 1 from signal_trial_outcomes where trial_id = ? and horizon_minutes = ?",
                (row["id"], int(horizon)),
            ).fetchone()
            if exists:
                continue
            observation = observations.get(row["inst_id"])
            observed_at = None
            if observation:
                raw = observation.get("observed_at") or observation.get("seen_at")
                observed_at = _parse_iso(raw) if raw else now
                if observed_at < target:
                    observation = None
                    observed_at = None
            if observation and observation.get("last") not in (None, ""):
                delay = max(0.0, (observed_at - target).total_seconds())
                status = "valid" if delay <= max_delay else "late"
                price = float(observation["last"])
                pnl = (price / float(row["entry_price"]) - 1.0) * 10_000.0 * sign
                if candidate.get("estimated_round_trip_cost_bps") is not None:
                    pnl -= float(candidate["estimated_round_trip_cost_bps"])
                else:
                    pnl -= 2.0 * (
                        float(config.get("fee_bps_per_side", 5.0))
                        + float(config.get("slippage_bps_per_side", 3.0))
                    )
                price_source = observation.get("price_source") or observation.get("venue") or "scanner"
            elif (now - target).total_seconds() > max_delay:
                delay = (now - target).total_seconds()
                status = "missing"
                price = None
                pnl = None
                price_source = None
            else:
                continue
            conn.execute(
                """
                insert into signal_trial_outcomes (
                    trial_id, horizon_minutes, target_at, observed_at, delay_seconds,
                    measurement_status, price, pnl_bps, price_source
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    int(horizon),
                    target.isoformat(),
                    observed_at.isoformat() if observed_at else None,
                    round(delay, 3),
                    status,
                    price,
                    round(pnl, 3) if pnl is not None else None,
                    price_source,
                ),
            )
            recorded.append(
                {
                    "trial_id": row["id"],
                    "horizon_minutes": int(horizon),
                    "measurement_status": status,
                    "pnl_bps": round(pnl, 3) if pnl is not None else None,
                }
            )
    conn.commit()
    return recorded


def _variant_outcomes(conn: sqlite3.Connection, variant_id: str, horizon: int = 60) -> dict:
    rows = conn.execute(
        """
        select t.pair_key, t.created_at, o.pnl_bps, o.delay_seconds, o.measurement_status
        from signal_trials t
        left join signal_trial_outcomes o
          on o.trial_id = t.id and o.horizon_minutes = ?
        where t.variant_id = ? and t.eligible = 1
        order by t.created_at asc
        """,
        (horizon, variant_id),
    ).fetchall()
    valid = {
        row["pair_key"]: float(row["pnl_bps"])
        for row in rows
        if row["measurement_status"] == "valid" and row["pnl_bps"] is not None
    }
    due = [row for row in rows if row["measurement_status"] is not None]
    delays = [
        float(row["delay_seconds"])
        for row in rows
        if row["measurement_status"] == "valid" and row["delay_seconds"] is not None
    ]
    return {
        "rows": rows,
        "valid": valid,
        "metrics": _metrics(list(valid.values())),
        "total_trials": len(rows),
        "due_trials": len(due),
        "valid_label_rate": round(len(valid) / len(due), 3) if due else None,
        "delay_p95_seconds": round(float(_percentile(delays, 0.95)), 3) if delays else None,
        "first_trial_at": rows[0]["created_at"] if rows else None,
        "last_trial_at": rows[-1]["created_at"] if rows else None,
    }


def _bootstrap_lower_bound(differences: list[float], samples: int = 1000) -> float | None:
    if not differences:
        return None
    rng = random.Random(24321)
    means = []
    for _ in range(samples):
        draw = [differences[rng.randrange(len(differences))] for _ in differences]
        means.append(statistics.fmean(draw))
    return float(_percentile(means, 0.025))


def _evaluate_pair(
    challenger: dict,
    incumbent: dict,
    settings: dict,
) -> dict:
    cfg = settings.get("signal_redesign", {})
    paired_keys = sorted(set(challenger["valid"]) & set(incumbent["valid"]))
    challenger_values = [challenger["valid"][key] for key in paired_keys]
    incumbent_values = [incumbent["valid"][key] for key in paired_keys]
    differences = [new - old for new, old in zip(challenger_values, incumbent_values)]
    paired_new = _metrics(challenger_values)
    paired_old = _metrics(incumbent_values)
    uplift = statistics.fmean(differences) if differences else None
    lower = _bootstrap_lower_bound(differences)
    coverage = (
        challenger["total_trials"] / incumbent["total_trials"]
        if incumbent["total_trials"]
        else 0.0
    )
    elapsed_hours = 0.0
    if challenger["first_trial_at"] and challenger["last_trial_at"]:
        elapsed_hours = (
            _parse_iso(challenger["last_trial_at"]) - _parse_iso(challenger["first_trial_at"])
        ).total_seconds() / 3600.0
    prerequisites = {
        "paired_valid_trials": len(paired_keys),
        "elapsed_hours": round(elapsed_hours, 3),
        "valid_label_rate": challenger["valid_label_rate"],
        "delay_p95_seconds": challenger["delay_p95_seconds"],
        "opportunity_coverage": round(coverage, 3),
    }
    ready = (
        len(paired_keys) >= int(cfg.get("min_paired_trials", 30))
        and elapsed_hours >= float(cfg.get("min_observation_hours", 48))
        and float(challenger["valid_label_rate"] or 0.0) >= float(cfg.get("min_valid_label_rate", 0.95))
        and float(challenger["delay_p95_seconds"] or 10**9) <= float(cfg.get("max_outcome_delay_seconds", 300))
        and coverage >= float(cfg.get("min_opportunity_coverage", 0.30))
    )
    win_delta = None
    tail_delta = None
    if paired_new["win_rate"] is not None and paired_old["win_rate"] is not None:
        win_delta = paired_new["win_rate"] - paired_old["win_rate"]
    if paired_new["worst_decile_bps"] is not None and paired_old["worst_decile_bps"] is not None:
        tail_delta = paired_new["worst_decile_bps"] - paired_old["worst_decile_bps"]
    passed = bool(
        ready
        and paired_new["avg_pnl_bps"] is not None
        and paired_new["avg_pnl_bps"] > 0
        and uplift is not None
        and uplift >= float(cfg.get("min_paired_uplift_bps", 5.0))
        and lower is not None
        and lower > 0
        and win_delta is not None
        and win_delta >= -float(cfg.get("max_win_rate_decline", 0.05))
        and tail_delta is not None
        and tail_delta >= -float(cfg.get("max_worst_decile_regression_bps", 50.0))
    )
    regressed = bool(
        ready
        and (
            (uplift is not None and uplift <= -float(cfg.get("revert_regression_bps", 8.0)))
            or (
                tail_delta is not None
                and tail_delta < -float(cfg.get("max_worst_decile_regression_bps", 50.0))
            )
        )
    )
    return {
        "ready": ready,
        "passed": passed,
        "regressed": regressed,
        "prerequisites": prerequisites,
        "challenger_metrics": challenger["metrics"],
        "paired_challenger_metrics": paired_new,
        "paired_incumbent_metrics": paired_old,
        "paired_uplift_bps": round(uplift, 3) if uplift is not None else None,
        "bootstrap_lower_95_bps": round(lower, 3) if lower is not None else None,
        "win_rate_delta": round(win_delta, 3) if win_delta is not None else None,
        "worst_decile_delta_bps": round(tail_delta, 3) if tail_delta is not None else None,
    }


def evaluate_variants(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    variants = load_variants(conn)
    active = next((item for item in variants if item["status"] == "active"), None)
    if not active:
        return []
    active_outcomes = _variant_outcomes(conn, active["variant_id"])
    results = []
    required_passes = int(settings.get("signal_redesign", {}).get("consecutive_passes_to_promote", 2))
    for challenger in variants:
        if challenger["variant_id"] == active["variant_id"] or challenger["status"] not in {"shadow", "retired"}:
            continue
        outcomes = _variant_outcomes(conn, challenger["variant_id"])
        evaluation = _evaluate_pair(outcomes, active_outcomes, settings)
        passes = int(challenger.get("consecutive_passes") or 0)
        if evaluation["passed"]:
            passes += 1
        elif evaluation["ready"]:
            passes = 0
        status = challenger["status"]
        decision = "needs_more_data"
        if evaluation["passed"] and passes >= required_passes:
            conn.execute(
                """
                update signal_variants
                set status = 'shadow', consecutive_passes = 0
                where variant_id = ?
                """,
                (active["variant_id"],),
            )
            conn.execute(
                """
                update signal_variants
                set status = 'active', promoted_at = ?, fallback_variant_id = ?,
                    consecutive_passes = ?, evaluation_json = ?
                where variant_id = ?
                """,
                (
                    utc_now(),
                    active["variant_id"],
                    passes,
                    json.dumps(evaluation, sort_keys=True),
                    challenger["variant_id"],
                ),
            )
            status = "active"
            decision = "promoted"
            add_memory_fact(
                conn,
                "signal_variant",
                challenger["variant_id"],
                "promoted",
                active["variant_id"],
                0.95,
                "signal_redesign",
                evaluation,
            )
        elif evaluation["regressed"] and challenger["status"] == "shadow":
            conn.execute(
                """
                update signal_variants
                set status = 'retired', retired_at = ?, consecutive_passes = 0,
                    evaluation_json = ?
                where variant_id = ?
                """,
                (utc_now(), json.dumps(evaluation, sort_keys=True), challenger["variant_id"]),
            )
            status = "retired"
            decision = "retired_after_regression"
        else:
            conn.execute(
                """
                update signal_variants
                set consecutive_passes = ?, evaluation_json = ?
                where variant_id = ?
                """,
                (passes, json.dumps(evaluation, sort_keys=True), challenger["variant_id"]),
            )
        results.append(
            {
                "variant_id": challenger["variant_id"],
                "status": status,
                "decision": decision,
                "consecutive_passes": passes,
                "evaluation": evaluation,
            }
        )

    if active.get("fallback_variant_id"):
        fallback = next(
            (item for item in variants if item["variant_id"] == active["fallback_variant_id"]),
            None,
        )
        if fallback:
            fallback_outcomes = _variant_outcomes(conn, fallback["variant_id"])
            active_eval = _evaluate_pair(active_outcomes, fallback_outcomes, settings)
            if active_eval["regressed"]:
                conn.execute(
                    """
                    update signal_variants
                    set status = 'reverted', retired_at = ?, evaluation_json = ?
                    where variant_id = ?
                    """,
                    (utc_now(), json.dumps(active_eval, sort_keys=True), active["variant_id"]),
                )
                conn.execute(
                    """
                    update signal_variants
                    set status = 'active', promoted_at = ?, fallback_variant_id = null
                    where variant_id = ?
                    """,
                    (utc_now(), fallback["variant_id"]),
                )
                results.append(
                    {
                        "variant_id": active["variant_id"],
                        "status": "reverted",
                        "decision": f"reverted_to_{fallback['variant_id']}",
                        "evaluation": active_eval,
                    }
                )
    conn.commit()
    return results


def _reliable_paper_metrics(conn: sqlite3.Connection, horizon: int = 60) -> dict:
    rows = conn.execute(
        """
        select o.pnl_bps
        from paper_trade_outcomes o
        join paper_trades p on p.id = o.trade_id
        where o.horizon_minutes = ?
          and o.measurement_status = 'valid'
          and o.pnl_bps is not null
          and p.trade_type = 'frontier_crypto_venue_map'
          and coalesce(json_extract(p.context_json, '$.signal_stats_scope'), 'direct') != 'synthetic_research'
        """,
        (horizon,),
    ).fetchall()
    return _metrics([float(row["pnl_bps"]) for row in rows])


def dislocation_quality_cohort_outcomes(conn: sqlite3.Connection, horizon: int = 60) -> dict:
    """Compare score-ranked frontier paper trades with the active baseline.

    The score is a paper-only cohort label persisted in ``candidate_json``.
    Older trades remain part of the baseline, which keeps the comparison useful
    while the new ranked cohort accumulates its first evaluation window.
    """
    rows = conn.execute(
        """
        select o.pnl_bps, p.candidate_json
        from paper_trade_outcomes o
        join paper_trades p on p.id = o.trade_id
        where o.horizon_minutes = ?
          and o.measurement_status = 'valid'
          and o.pnl_bps is not null
          and p.trade_type = 'frontier_crypto_venue_map'
          and coalesce(json_extract(p.context_json, '$.signal_stats_scope'), 'direct') != 'synthetic_research'
        """,
        (horizon,),
    ).fetchall()
    baseline = []
    ranked = []
    diagnostic = []
    for row in rows:
        pnl_bps = float(row["pnl_bps"])
        baseline.append(pnl_bps)
        candidate = json.loads(row["candidate_json"] or "{}")
        cohort = str(candidate.get("paper_quality_cohort") or "baseline")
        if cohort == "quality_ranked":
            ranked.append(pnl_bps)
        elif cohort == "quality_diagnostic":
            diagnostic.append(pnl_bps)
    return {
        "horizon_minutes": horizon,
        "baseline_all_frontier": _metrics(baseline),
        "quality_ranked": _metrics(ranked),
        "quality_diagnostic": _metrics(diagnostic),
        "comparison_note": "Compare avg_pnl_bps, win_rate, and worst_decile_bps after the next evaluation window; cohorts do not change paper eligibility.",
    }


def _bucket_numeric(value: object, bounds: tuple[float, float, float], labels: tuple[str, str, str, str]) -> str:
    try:
        numeric = float(value or 0.0)
    except (TypeError, ValueError):
        numeric = 0.0
    if numeric <= bounds[0]:
        return labels[0]
    if numeric <= bounds[1]:
        return labels[1]
    if numeric <= bounds[2]:
        return labels[2]
    return labels[3]


def _diagnostic_groups(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        select o.horizon_minutes, o.pnl_bps, o.delay_seconds, o.measurement_status,
               p.signal_key, p.inst_id, p.venue, p.direction, p.candidate_json,
               p.opened_at, p.signal_variant_id
        from paper_trade_outcomes o
        join paper_trades p on p.id = o.trade_id
        where p.trade_type = 'frontier_crypto_venue_map'
          and coalesce(json_extract(p.context_json, '$.signal_stats_scope'), 'direct') != 'synthetic_research'
        order by o.horizon_minutes, p.id
        """
    ).fetchall()
    grouped: dict[tuple, list[float]] = {}
    label_status: dict[str, int] = {}
    delays = []
    for row in rows:
        status = str(row["measurement_status"] or "unknown")
        label_status[status] = label_status.get(status, 0) + 1
        if status != "valid" or row["pnl_bps"] is None:
            continue
        if row["delay_seconds"] is not None:
            delays.append(float(row["delay_seconds"]))
        candidate = json.loads(row["candidate_json"] or "{}")
        direction = str(row["direction"] or "")
        route_status = (
            candidate.get("route_status")
            or (candidate.get("execution_feasibility") or {}).get("status")
            or "unknown"
        )
        dims = {
            "signal_family": row["signal_key"],
            "direction": direction,
            "direction_side": "long" if direction.startswith("long") else "short" if direction.startswith("short") else direction,
            "venue": row["venue"],
            "instrument": row["inst_id"],
            "route_status": route_status,
            "quality_bucket": _bucket_numeric(
                candidate.get("quality_score"),
                (35, 60, 80),
                ("low", "conditional", "good", "excellent"),
            ),
            "depth_adjusted_edge_bucket": _bucket_numeric(
                candidate.get("edge_bps_estimate"),
                (0, 6, 15),
                ("zero_or_negative", "thin", "good", "large"),
            ),
            "round_trip_cost_bucket": _bucket_numeric(
                candidate.get("estimated_round_trip_cost_bps"),
                (20, 40, 80),
                ("low", "normal", "high", "extreme"),
            ),
            "quote_normalization": candidate.get("quote_normalization_status") or "unknown",
            "spread_bucket": (
                "tight"
                if float(candidate.get("spread_bps") or 999) <= 3
                else "normal"
                if float(candidate.get("spread_bps") or 999) <= 8
                else "wide"
            ),
            "liquidity_bucket": (
                "thin"
                if float(candidate.get("liquidity_score") or 0) < 0.35
                else "normal"
                if float(candidate.get("liquidity_score") or 0) < 0.65
                else "deep"
            ),
            "dislocation_bucket": (
                "small"
                if abs(float(candidate.get("venue_deviation_bps") or 0)) <= 25
                else "moderate"
                if abs(float(candidate.get("venue_deviation_bps") or 0)) <= 50
                else "large"
            ),
            "source_count_bucket": (
                "two"
                if int(candidate.get("source_venue_count") or 0) <= 2
                else "few"
                if int(candidate.get("source_venue_count") or 0) <= 4
                else "broad"
            ),
            "quote": candidate.get("quote") or "unknown",
            "reference_quality": (
                "quote_matched_leave_one_out"
                if candidate.get("variant_reference_grouping") == "base_quote"
                and candidate.get("variant_leave_one_out")
                else "cross_quote_possible"
            ),
            "market_regime": (
                "calm"
                if abs(float(candidate.get("change_24h_pct") or 0.0)) <= 2.0
                else "active"
                if abs(float(candidate.get("change_24h_pct") or 0.0)) <= 8.0
                else "shock"
            ),
            "hour_utc": f"{_parse_iso(row['opened_at']).hour:02d}",
            "variant_id": row["signal_variant_id"] or candidate.get("signal_variant_id") or "legacy",
        }
        for dimension, value in dims.items():
            grouped.setdefault((int(row["horizon_minutes"]), dimension, str(value)), []).append(
                float(row["pnl_bps"])
            )
    results = [
        {
            "horizon_minutes": horizon,
            "dimension": dimension,
            "value": value,
            **_metrics(values),
        }
        for (horizon, dimension, value), values in grouped.items()
        if len(values) >= 3
    ]
    results.sort(
        key=lambda item: (
            item["horizon_minutes"] != 60,
            -item["count"],
            item["avg_pnl_bps"] or 0,
        )
    )
    return {
        "label_status_counts": label_status,
        "valid_delay_p95_seconds": round(float(_percentile(delays, 0.95)), 3) if delays else None,
        "groups": results,
        "top_failures": sorted(
            [item for item in results if item["horizon_minutes"] == 60],
            key=lambda item: item["avg_pnl_bps"] or 0,
        )[:20],
        "top_working": sorted(
            [item for item in results if item["horizon_minutes"] == 60],
            key=lambda item: item["avg_pnl_bps"] or 0,
            reverse=True,
        )[:20],
        "direction_side": [
            item
            for item in results
            if item["horizon_minutes"] == 60 and item["dimension"] == "direction_side"
        ],
        "venue_route_quality_failures": sorted(
            [
                item
                for item in results
                if item["horizon_minutes"] == 60
                and item["dimension"]
                in {
                    "venue",
                    "route_status",
                    "quality_bucket",
                    "depth_adjusted_edge_bucket",
                    "round_trip_cost_bucket",
                    "quote_normalization",
                }
            ],
            key=lambda item: item["avg_pnl_bps"] or 0,
        )[:20],
    }


def _variant_trial_summaries(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        select t.variant_id,
               count(*) as total_trials,
               sum(case when t.eligible = 1 then 1 else 0 end) as eligible_trials,
               sum(case when o.horizon_minutes = 60 and o.measurement_status = 'valid' then 1 else 0 end) as valid_60m_labels
        from signal_trials t
        left join signal_trial_outcomes o on o.trial_id = t.id
        where t.signal_family = ?
        group by t.variant_id
        """,
        (SIGNAL_FAMILY,),
    ).fetchall()
    return {row["variant_id"]: dict(row) for row in rows}


def _promotion_blockers(evaluations: list[dict], settings: dict) -> dict[str, list[str]]:
    cfg = settings.get("signal_redesign", {})
    blockers: dict[str, list[str]] = {}
    for item in evaluations:
        evaluation = item.get("evaluation") or {}
        prerequisites = evaluation.get("prerequisites") or {}
        reasons = []
        if not evaluation.get("ready"):
            if int(prerequisites.get("paired_valid_trials") or 0) < int(cfg.get("min_paired_trials", 30)):
                reasons.append("insufficient_paired_valid_labels")
            if float(prerequisites.get("elapsed_hours") or 0.0) < float(cfg.get("min_observation_hours", 48)):
                reasons.append("insufficient_observation_hours")
            if float(prerequisites.get("valid_label_rate") or 0.0) < float(cfg.get("min_valid_label_rate", 0.95)):
                reasons.append("valid_label_rate_below_gate")
            if float(prerequisites.get("delay_p95_seconds") or 10**9) > float(cfg.get("max_outcome_delay_seconds", 300)):
                reasons.append("outcome_delay_above_gate")
            if float(prerequisites.get("opportunity_coverage") or 0.0) < float(cfg.get("min_opportunity_coverage", 0.30)):
                reasons.append("opportunity_coverage_below_gate")
        elif not evaluation.get("passed"):
            paired = evaluation.get("paired_challenger_metrics") or {}
            if paired.get("avg_pnl_bps") is not None and float(paired["avg_pnl_bps"]) <= 0:
                reasons.append("challenger_expectancy_not_positive")
            if float(evaluation.get("paired_uplift_bps") or 0.0) < float(cfg.get("min_paired_uplift_bps", 5.0)):
                reasons.append("paired_uplift_below_gate")
            if float(evaluation.get("bootstrap_lower_95_bps") or -10**9) <= 0:
                reasons.append("bootstrap_confidence_not_positive")
        blockers[str(item.get("variant_id"))] = reasons or ["needs_more_data"]
    return blockers


def _trial_to_promotion_rate(variants: list[dict]) -> float | None:
    candidates = [item for item in variants if item.get("status") in {"active", "shadow", "retired"}]
    if not candidates:
        return None
    promoted = [
        item
        for item in candidates
        if item.get("promoted_at") or (item.get("status") == "active" and item.get("variant_id") != "frontier_v1_incumbent")
    ]
    return round(len(promoted) / len(candidates), 4)


def write_report(
    conn: sqlite3.Connection,
    settings: dict,
    trial_activity: dict,
    trial_outcomes: list[dict],
    evaluations: list[dict],
    frontier_quality: dict | None = None,
) -> dict:
    variants = load_variants(conn)
    diagnostics = _diagnostic_groups(conn)
    trial_summaries = _variant_trial_summaries(conn)
    promotion_blockers = _promotion_blockers(evaluations, settings)
    report = {
        "generated_at": utc_now(),
        "mode": settings.get("mode"),
        "live_trading_allowed": bool(settings.get("allow_live_trading", False)),
        "summary": {
            "active_variant": next(
                (item["variant_id"] for item in variants if item["status"] == "active"),
                None,
            ),
            "variant_count": len(variants),
            "trials_created_this_loop": trial_activity.get("created", 0),
            "trial_outcomes_recorded_this_loop": len(trial_outcomes),
            "trial_containment": {
                "caps": trial_activity.get("caps", {}),
                "by_venue": trial_activity.get("by_venue", {}),
                "skipped_by_reason": trial_activity.get("skipped_by_reason", {}),
                "trial_to_promotion_rate": _trial_to_promotion_rate(variants),
                "trial_pollution_risk": "contained" if trial_activity.get("skipped_by_reason") or trial_activity.get("caps") else "unbounded",
            },
            "reliable_60m_paper_metrics": _reliable_paper_metrics(conn, 60),
            "label_status_counts": diagnostics["label_status_counts"],
            "valid_delay_p95_seconds": diagnostics["valid_delay_p95_seconds"],
            "variant_trial_summaries": trial_summaries,
            "promotion_blockers": promotion_blockers,
            "direction_side_60m": diagnostics.get("direction_side", []),
        },
        "variants": variants,
        "trial_activity": trial_activity,
        "evaluations": evaluations,
        "promotion_blockers": promotion_blockers,
        "frontier_quality": frontier_quality or {},
        "diagnostics": diagnostics,
        "promotion_gates": {
            "min_paired_trials": settings.get("signal_redesign", {}).get("min_paired_trials", 30),
            "min_observation_hours": settings.get("signal_redesign", {}).get("min_observation_hours", 48),
            "min_valid_label_rate": settings.get("signal_redesign", {}).get("min_valid_label_rate", 0.95),
            "max_outcome_delay_seconds": settings.get("signal_redesign", {}).get(
                "max_outcome_delay_seconds", 300
            ),
            "min_opportunity_coverage": settings.get("signal_redesign", {}).get(
                "min_opportunity_coverage", 0.30
            ),
            "min_paired_uplift_bps": settings.get("signal_redesign", {}).get(
                "min_paired_uplift_bps", 5.0
            ),
            "consecutive_passes_to_promote": settings.get("signal_redesign", {}).get(
                "consecutive_passes_to_promote", 2
            ),
        },
        "hard_limits": [
            "Paper-only signal variants.",
            "Only validated configuration fields are accepted.",
            "Late, missing, and legacy-unverified labels cannot promote a variant.",
            "No credentials, live orders, startup changes, or arbitrary code generation.",
        ],
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")
    return report


def _markdown(report: dict) -> str:
    summary = report.get("summary", {})
    lines = [
        "# Signal Redesign Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Live trading allowed: `{report.get('live_trading_allowed')}`",
        f"- Active variant: `{summary.get('active_variant')}`",
        f"- Trials created this loop: `{summary.get('trials_created_this_loop')}`",
        f"- Trial containment: `{summary.get('trial_containment', {})}`",
        f"- Reliable 60m paper metrics: `{summary.get('reliable_60m_paper_metrics')}`",
        f"- Label status counts: `{summary.get('label_status_counts')}`",
        f"- Valid outcome delay P95: `{summary.get('valid_delay_p95_seconds')}` seconds",
        "",
        "## Variants",
        "",
    ]
    for item in report.get("variants", []):
        evaluation = item.get("evaluation") or {}
        lines.append(
            f"- `{item['variant_id']}` status=`{item['status']}` passes=`{item['consecutive_passes']}` "
            f"paired_uplift=`{evaluation.get('paired_uplift_bps')}` config=`{item['config']}`"
        )
    lines.extend(["", "## Evaluations", ""])
    if not report.get("evaluations"):
        lines.append("No variant reached an evaluation decision this loop.")
    for item in report.get("evaluations", []):
        blockers = report.get("promotion_blockers", {}).get(item["variant_id"], [])
        lines.append(
            f"- `{item['variant_id']}` decision=`{item['decision']}` "
            f"status=`{item['status']}` blockers=`{blockers}` evidence=`{item.get('evaluation')}`"
        )
    lines.extend(["", "## Direction Side 60m", ""])
    for item in report.get("diagnostics", {}).get("direction_side", []):
        lines.append(
            f"- `{item['value']}` n=`{item['count']}` avg=`{item['avg_pnl_bps']}`bps "
            f"win=`{item['win_rate']}` worst10=`{item['worst_decile_bps']}`bps"
        )
    lines.extend(["", "## Top Reliable 60m Failures", ""])
    for item in report.get("diagnostics", {}).get("top_failures", [])[:15]:
        lines.append(
            f"- `{item['dimension']}={item['value']}` n=`{item['count']}` "
            f"avg=`{item['avg_pnl_bps']}`bps median=`{item['median_pnl_bps']}`bps "
            f"win=`{item['win_rate']}` worst10=`{item['worst_decile_bps']}`bps"
        )
    lines.extend(["", "## Venue Route Quality Failures", ""])
    for item in report.get("diagnostics", {}).get("venue_route_quality_failures", [])[:15]:
        lines.append(
            f"- `{item['dimension']}={item['value']}` n=`{item['count']}` "
            f"avg=`{item['avg_pnl_bps']}`bps win=`{item['win_rate']}`"
        )
    lines.extend(["", "## Top Reliable 60m Working Contexts", ""])
    for item in report.get("diagnostics", {}).get("top_working", [])[:15]:
        lines.append(
            f"- `{item['dimension']}={item['value']}` n=`{item['count']}` "
            f"avg=`{item['avg_pnl_bps']}`bps median=`{item['median_pnl_bps']}`bps "
            f"win=`{item['win_rate']}`"
        )
    return "\n".join(lines) + "\n"


def run_frontier_redesign(
    conn: sqlite3.Connection,
    settings: dict,
    selected_observations: list[dict],
    price_observations: dict[str, dict],
    *,
    active_limit: int,
    scan_id: str,
) -> tuple[list[dict], dict]:
    ensure_initial_variants(conn)
    variants = load_variants(conn)
    preliminary_candidates = {
        variant["variant_id"]: build_variant_candidates(
            selected_observations,
            settings,
            variant["variant_id"],
            variant["config"],
        )
        for variant in variants
        if variant["status"] in {"active", "shadow", "retired"}
    }
    selected_for_depth = select_enrichment_observations(
        conn,
        selected_observations,
        variants,
        preliminary_candidates,
        settings,
    )
    enriched_observations, enrichment_summary = enrich_observations(
        conn,
        selected_observations,
        selected_for_depth,
        settings,
        load_venue_registry(),
    )
    snapshot_summary = persist_quality_snapshots(conn, enriched_observations, settings)
    venue_quality_leaderboard = venue_quality_scores(conn)
    annotate_venue_quality_scores(enriched_observations, venue_quality_leaderboard)
    # Keep the caller-owned selected observation list synchronized so the
    # Strategy Lab snapshot path receives the same depth-quality evidence used
    # by the paper-only frontier variants.
    selected_observations[:] = enriched_observations
    candidates_by_variant = {
        variant["variant_id"]: build_variant_candidates(
            enriched_observations,
            settings,
            variant["variant_id"],
            variant["config"],
        )
        for variant in variants
        if variant["status"] in {"active", "shadow", "retired"}
    }
    trial_activity = record_variant_trials(
        conn,
        variants,
        candidates_by_variant,
        settings,
        scan_id,
    )
    trial_outcomes = record_trial_outcomes(conn, price_observations, settings)
    evaluations = evaluate_variants(conn, settings)
    active = next((item for item in load_variants(conn) if item["status"] == "active"), None)
    active_candidates = candidates_by_variant.get(active["variant_id"], []) if active else []
    quality_summary = {
        **enrichment_summary,
        **snapshot_summary,
        "venue_quality_leaderboard": venue_quality_leaderboard,
        "quality_outcome_relationship_60m": quality_outcome_relationship(conn),
        "market_testing_progress": market_testing_progress(conn),
        "dislocation_quality_cohort_outcomes": dislocation_quality_cohort_outcomes(conn),
    }
    write_frontier_outputs(
        enriched_observations,
        active_candidates[:active_limit],
        settings,
        quality_summary=quality_summary,
    )
    report = write_report(
        conn,
        settings,
        trial_activity,
        trial_outcomes,
        evaluations,
        frontier_quality=quality_summary,
    )
    return active_candidates[:active_limit], report
