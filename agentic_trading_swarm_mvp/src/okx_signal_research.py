#!/usr/bin/env python3
"""Paper-only OKX perp funding/basis signal research and shadow variants."""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import math
import random
import sqlite3
import statistics

from paper_loop import direction_sign
from storage import RUNS_DIR, add_memory_fact, add_self_improvement_experiment, signal_key, utc_now


REPORT_JSON = RUNS_DIR / "okx_signal_research_report.json"
REPORT_MD = RUNS_DIR / "okx_signal_research_report.md"
CARRY_REPORT_JSON = RUNS_DIR / "okx_carry_economics_report.json"
CARRY_REPORT_MD = RUNS_DIR / "okx_carry_economics_report.md"
SIGNAL_FAMILY = "OKX|perp_funding_basis"

DEFAULT_VARIANTS = [
    {
        "variant_id": "okx_v1_incumbent",
        "version": 1,
        "title": "Current OKX perp funding/basis behavior",
        "status": "active",
        "config": {
            "mode": "incumbent",
            "enabled_directions": ["all"],
            "min_abs_funding_bps": 0.0,
            "min_abs_basis_bps": 0.0,
            "max_spread_bps": 8.0,
            "min_liquidity_score": 0.0,
            "max_abs_24h_move_pct": 60.0,
            "allow_conditional_routes": True,
            "cap_reverse_basis_score": None,
        },
    },
    {
        "variant_id": "okx_v2_funding_alignment",
        "version": 2,
        "title": "Funding capture with basis/route alignment",
        "status": "shadow",
        "config": {
            "mode": "funding_alignment",
            "enabled_directions": [
                "funding_capture_short_perp",
                "funding_capture_long_perp",
                "short_perp_long_spot",
                "long_perp_short_spot",
            ],
            "min_abs_funding_bps": 2.5,
            "min_abs_basis_bps": 0.0,
            "max_spread_bps": 5.0,
            "min_liquidity_score": 0.35,
            "max_abs_24h_move_pct": 35.0,
            "allow_conditional_routes": False,
            "cap_reverse_basis_score": None,
        },
    },
    {
        "variant_id": "okx_v3_basis_regime_gate",
        "version": 3,
        "title": "Basis mean reversion only in safer regimes",
        "status": "shadow",
        "config": {
            "mode": "basis_regime_gate",
            "enabled_directions": [
                "basis_mean_reversion_short_perp",
                "basis_mean_reversion_long_perp",
            ],
            "min_abs_funding_bps": 0.0,
            "min_abs_basis_bps": 35.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.45,
            "max_abs_24h_move_pct": 20.0,
            "allow_conditional_routes": False,
            "cap_reverse_basis_score": None,
        },
    },
    {
        "variant_id": "okx_v4_reverse_basis_recovery",
        "version": 4,
        "title": "Reverse basis recovery monitor",
        "status": "shadow",
        "config": {
            "mode": "reverse_basis_recovery",
            "enabled_directions": [
                "long_perp_short_spot",
                "basis_mean_reversion_long_perp",
                "funding_capture_long_perp",
            ],
            "min_abs_funding_bps": 1.0,
            "min_abs_basis_bps": 15.0,
            "max_spread_bps": 5.0,
            "min_liquidity_score": 0.35,
            "max_abs_24h_move_pct": 25.0,
            "allow_conditional_routes": True,
            "cap_reverse_basis_score": 45.0,
        },
    },
    {
        "variant_id": "okx_v5_funding_capture_protected",
        "version": 5,
        "title": "Protected funding capture expansion",
        "status": "shadow",
        "config": {
            "mode": "funding_capture_protected",
            "enabled_directions": [
                "funding_capture_short_perp",
                "funding_capture_long_perp",
            ],
            "min_abs_funding_bps": 3.0,
            "min_abs_basis_bps": 0.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.45,
            "max_abs_24h_move_pct": 25.0,
            "allow_conditional_routes": True,
            "cap_reverse_basis_score": None,
        },
    },
    {
        "variant_id": "okx_v6_extreme_funding_expansion",
        "version": 6,
        "title": "Extreme funding capture expansion monitor",
        "status": "shadow",
        "config": {
            "mode": "funding_capture_protected",
            "enabled_directions": [
                "funding_capture_short_perp",
                "funding_capture_long_perp",
                "short_perp_long_spot",
            ],
            "min_abs_funding_bps": 8.0,
            "min_abs_basis_bps": 0.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.45,
            "max_abs_24h_move_pct": 30.0,
            "allow_conditional_routes": True,
            "cap_reverse_basis_score": None,
        },
    },
    {
        "variant_id": "okx_v7_net_carry_positive",
        "version": 7,
        "title": "Net carry positive after fees and slippage",
        "status": "shadow",
        "config": {
            "mode": "net_carry_positive",
            "enabled_directions": [
                "funding_capture_short_perp",
                "funding_capture_long_perp",
                "short_perp_long_spot",
            ],
            "min_abs_funding_bps": 1.0,
            "min_abs_basis_bps": 0.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.45,
            "max_abs_24h_move_pct": 30.0,
            "allow_conditional_routes": False,
            "min_net_carry_edge_bps": 3.0,
            "require_carry_alignment": True,
            "cap_reverse_basis_score": None,
        },
    },
    {
        "variant_id": "okx_v8_time_to_funding_capture",
        "version": 8,
        "title": "Near-funding carry capture monitor",
        "status": "shadow",
        "config": {
            "mode": "time_to_funding_capture",
            "enabled_directions": [
                "funding_capture_short_perp",
                "funding_capture_long_perp",
                "short_perp_long_spot",
            ],
            "min_abs_funding_bps": 2.0,
            "min_abs_basis_bps": 0.0,
            "max_spread_bps": 4.0,
            "min_liquidity_score": 0.45,
            "max_abs_24h_move_pct": 25.0,
            "allow_conditional_routes": False,
            "min_expected_funding_bps_to_next": 2.0,
            "max_time_to_funding_minutes": 180.0,
            "min_net_carry_edge_bps": 1.0,
            "require_carry_alignment": True,
            "cap_reverse_basis_score": None,
        },
    },
    {
        "variant_id": "okx_v9_basis_carry_disagree_shadow",
        "version": 9,
        "title": "Basis mean reversion stays shadow when carry disagrees",
        "status": "shadow",
        "config": {
            "mode": "basis_carry_disagree_shadow",
            "enabled_directions": [
                "basis_mean_reversion_short_perp",
                "basis_mean_reversion_long_perp",
                "long_perp_short_spot",
            ],
            "min_abs_funding_bps": 0.0,
            "min_abs_basis_bps": 25.0,
            "max_spread_bps": 5.0,
            "min_liquidity_score": 0.35,
            "max_abs_24h_move_pct": 25.0,
            "allow_conditional_routes": True,
            "shadow_only": True,
            "cap_reverse_basis_score": 35.0,
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


def ensure_initial_variants(conn: sqlite3.Connection) -> None:
    for variant in DEFAULT_VARIANTS:
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
                json.dumps(variant["config"], sort_keys=True),
                "manual_spec:154",
                "p90_okx_basis_signal_research",
                "deterministic",
                json.dumps(
                    {
                        "adapter_spec_id": 154,
                        "basis": "Focused OKX funding/basis redesign from manual intervention queue.",
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
        conn.execute("update signal_variants set status = 'active' where variant_id = 'okx_v1_incumbent'")
    add_self_improvement_experiment(
        conn,
        "manual_spec:154",
        "p90_okx_basis_signal_research",
        "okx_signal_research_validation",
        90,
        SIGNAL_FAMILY,
        SIGNAL_FAMILY,
        "OKX variants should preserve funding-capture winners while reducing basis/hedge failures.",
        "Run paired shadow variants and promote only after reliable paper outcomes.",
        _reliable_paper_metrics(conn, 60),
        {
            "paper_only": True,
            "canonical_spec_id": 154,
            "signal_family": SIGNAL_FAMILY,
        },
    )
    conn.commit()


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
        item["config"] = json.loads(item.pop("config_json") or "{}")
        item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        item["evaluation"] = json.loads(item.pop("evaluation_json") or "{}")
        output.append(item)
    return output


def _route_status(candidate: dict) -> str:
    return str((candidate.get("execution_feasibility") or {}).get("status") or "unknown")


def _carry_cfg(settings: dict) -> dict:
    risk = settings.get("risk", {})
    defaults = {
        "default_funding_interval_hours": 8.0,
        "default_taker_fee_bps_per_leg": float(risk.get("taker_fee_bps_per_leg", 5.0)),
        "default_slippage_bps_per_leg": float(risk.get("slippage_bps_per_leg", 3.0)),
        "unknown_borrow_cost_penalty_bps": 25.0,
        "min_positive_net_carry_bps": 3.0,
    }
    return {**defaults, **settings.get("okx_carry_economics", {})}


def _hours_until(value: str | None) -> float | None:
    if not value:
        return None
    try:
        target = _parse_iso(value)
    except ValueError:
        return None
    hours = (target - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600.0
    return round(max(0.0, hours), 4)


def _perp_funding_edge_bps(direction: str, funding_bps: float) -> float:
    if direction in {"short_perp_long_spot", "funding_capture_short_perp", "basis_mean_reversion_short_perp"}:
        return funding_bps
    if direction in {"long_perp_short_spot", "funding_capture_long_perp", "basis_mean_reversion_long_perp"}:
        return -funding_bps
    return 0.0


def _basis_alignment_edge_bps(direction: str, basis_bps: float) -> float:
    if direction in {"short_perp_long_spot", "basis_mean_reversion_short_perp"}:
        return basis_bps
    if direction in {"long_perp_short_spot", "basis_mean_reversion_long_perp"}:
        return -basis_bps
    return 0.0


def _leg_count(direction: str) -> int:
    return 2 if direction in {"short_perp_long_spot", "long_perp_short_spot"} else 1


def _requires_short_spot(candidate: dict) -> bool:
    feasibility = candidate.get("execution_feasibility") or {}
    if feasibility.get("requires_short_spot") is not None:
        return bool(feasibility.get("requires_short_spot"))
    return str(candidate.get("direction")) in {
        "long_perp_short_spot",
        "funding_capture_long_perp",
        "basis_mean_reversion_long_perp",
    }


def add_carry_economics(candidate: dict, settings: dict) -> dict:
    cfg = _carry_cfg(settings)
    item = dict(candidate)
    direction = str(item.get("direction") or "watch_only")
    funding_bps = float(item.get("funding_bps") or 0.0)
    basis_bps = float(item.get("basis_bps") or 0.0)
    interval_hours = float(item.get("funding_interval_hours") or cfg.get("default_funding_interval_hours", 8.0) or 8.0)
    time_to_funding_hours = _hours_until(item.get("next_funding_time"))
    expected_funding = _perp_funding_edge_bps(direction, funding_bps)
    expected_per_day = expected_funding * (24.0 / interval_hours) if interval_hours > 0 else expected_funding
    basis_edge = _basis_alignment_edge_bps(direction, basis_bps)
    legs = _leg_count(direction)
    fee = float(cfg.get("default_taker_fee_bps_per_leg", 5.0))
    slippage = float(cfg.get("default_slippage_bps_per_leg", 3.0))
    entry_fee = legs * fee
    exit_fee = legs * fee
    slippage_round_trip = legs * slippage * 2.0
    borrow_status = "not_required"
    borrow_cost = 0.0
    borrow_penalty = 0.0
    if _requires_short_spot(item):
        if _route_status(item) == "standard":
            borrow_status = "confirmed_or_not_required"
        else:
            borrow_status = "unknown"
            borrow_cost = None
            borrow_penalty = float(cfg.get("unknown_borrow_cost_penalty_bps", 25.0))
    total_cost = entry_fee + exit_fee + slippage_round_trip + borrow_penalty + (borrow_cost or 0.0)
    net_edge = expected_funding + basis_edge - total_cost
    if direction == "watch_only":
        alignment_status = "watch_only"
    elif borrow_status == "unknown":
        alignment_status = "borrow_unknown"
    elif expected_funding <= 0 and direction.startswith("funding_capture"):
        alignment_status = "negative_expected_funding"
    elif net_edge < float(cfg.get("min_positive_net_carry_bps", 3.0)):
        alignment_status = "cost_eroded"
    elif _aligned(item):
        alignment_status = "carry_aligned_positive"
    else:
        alignment_status = "carry_disagrees_with_basis"
    economics = {
        "time_to_next_funding_minutes": round(time_to_funding_hours * 60.0, 3) if time_to_funding_hours is not None else None,
        "expected_funding_bps_to_next": round(expected_funding, 3),
        "expected_funding_bps_per_day": round(expected_per_day, 3),
        "basis_bps": round(basis_bps, 3),
        "basis_alignment_edge_bps": round(basis_edge, 3),
        "mark_index_spread_bps": round(float(item.get("mark_basis_bps") or item.get("basis_bps") or 0.0), 3),
        "entry_fee_bps": round(entry_fee, 3),
        "exit_fee_bps": round(exit_fee, 3),
        "estimated_slippage_bps": round(slippage_round_trip, 3),
        "hedge_cost_bps": round(entry_fee + exit_fee + slippage_round_trip, 3),
        "borrow_cost_bps": round(borrow_cost, 3) if borrow_cost is not None else None,
        "borrow_cost_status": borrow_status,
        "borrow_cost_penalty_bps": round(borrow_penalty, 3),
        "round_trip_cost_bps": round(total_cost, 3),
        "net_carry_edge_bps": round(net_edge, 3),
        "breakeven_adverse_move_bps": round(max(0.0, net_edge), 3),
        "carry_alignment_status": alignment_status,
        "fee_model": "conservative_config_defaults",
    }
    item["okx_carry_economics"] = economics
    item.update(economics)
    return item


def _reject(candidate: dict, reason: str) -> dict:
    output = dict(candidate)
    output["direction"] = "watch_only"
    output["candidate_reject_reason"] = reason
    output["paper_entry_blocked"] = True
    output["promotion_eligible"] = False
    output["score"] = min(float(output.get("score") or 0.0), 25.0)
    return output


def _aligned(candidate: dict) -> bool:
    funding = float(candidate.get("funding_bps") or 0.0)
    basis = float(candidate.get("basis_bps") or 0.0)
    direction = candidate.get("direction")
    if direction in {"short_perp_long_spot", "funding_capture_short_perp"}:
        return funding > 0 and basis >= -5.0
    if direction in {"long_perp_short_spot", "funding_capture_long_perp"}:
        return funding < 0 and basis <= 5.0
    return False


def _basis_regime_ok(candidate: dict) -> bool:
    context_status = candidate.get("basis_context_status")
    if context_status is None:
        from okx_perp_scanner import instrument_asset_context

        context_status = instrument_asset_context(str(candidate.get("inst_id") or ""))["basis_context_status"]
    if context_status != "asset_specific":
        return False
    persistence = candidate.get("basis_persistence_status", "same_asset_persistent")
    if persistence != "same_asset_persistent":
        return False
    cooling = candidate.get("basis_momentum_cooling")
    if cooling is None:
        cooling = abs(float(candidate.get("change_24h_pct") or 0.0)) <= 20.0
    if not bool(cooling):
        return False
    funding = float(candidate.get("funding_bps") or 0.0)
    basis = float(candidate.get("basis_bps") or 0.0)
    change = abs(float(candidate.get("change_24h_pct") or 0.0))
    direction = candidate.get("direction")
    if direction == "basis_mean_reversion_short_perp":
        return basis > 0 and funding <= 3.0 and change <= 20.0
    if direction == "basis_mean_reversion_long_perp":
        return basis < 0 and funding >= -3.0 and change <= 20.0
    return False


def _asset_context_summary(candidates_by_variant: dict[str, list[dict]]) -> dict:
    rows = [row for candidates in candidates_by_variant.values() for row in candidates]
    by_asset: dict[str, dict] = {}
    for row in rows:
        asset = str(row.get("base_asset") or "unresolved")
        item = by_asset.setdefault(
            asset,
            {
                "candidate_count": 0,
                "families": set(),
                "directions": collections.Counter(),
                "basis_regime_ready_count": 0,
            },
        )
        item["candidate_count"] += 1
        if row.get("instrument_family"):
            item["families"].add(str(row["instrument_family"]))
        item["directions"][str(row.get("direction") or "unknown")] += 1
        if _basis_regime_ok(row):
            item["basis_regime_ready_count"] += 1
    return {
        "resolved_count": sum(row.get("basis_context_status") == "asset_specific" for row in rows),
        "unresolved_count": sum(row.get("basis_context_status") != "asset_specific" for row in rows),
        "by_base_asset": {
            asset: {
                **item,
                "families": sorted(item["families"]),
                "directions": dict(item["directions"]),
            }
            for asset, item in sorted(by_asset.items())
        },
        "normalization_scope": "base_asset_and_instrument_family_only",
    }


def build_variant_candidates(base_candidates: list[dict], settings: dict, variant: dict) -> list[dict]:
    config = variant.get("config", {})
    enabled = set(config.get("enabled_directions") or ["all"])
    output = []
    for candidate in base_candidates:
        item = add_carry_economics(candidate, settings)
        item["signal_variant_id"] = variant["variant_id"]
        item["okx_variant_mode"] = config.get("mode")
        direction = str(item.get("direction"))
        if "all" not in enabled and direction not in enabled:
            output.append(_reject(item, "direction_not_enabled_for_okx_variant"))
            continue
        if float(item.get("spread_bps") or 999.0) > float(config.get("max_spread_bps", 8.0)):
            output.append(_reject(item, "spread_above_variant_max"))
            continue
        if float(item.get("liquidity_score") or 0.0) < float(config.get("min_liquidity_score", 0.0)):
            output.append(_reject(item, "liquidity_below_variant_minimum"))
            continue
        if abs(float(item.get("change_24h_pct") or 0.0)) > float(config.get("max_abs_24h_move_pct", 60.0)):
            output.append(_reject(item, "move_above_variant_regime_limit"))
            continue
        if abs(float(item.get("funding_bps") or 0.0)) < float(config.get("min_abs_funding_bps", 0.0)):
            output.append(_reject(item, "funding_below_variant_minimum"))
            continue
        if abs(float(item.get("basis_bps") or 0.0)) < float(config.get("min_abs_basis_bps", 0.0)):
            output.append(_reject(item, "basis_below_variant_minimum"))
            continue
        if not bool(config.get("allow_conditional_routes", True)) and _route_status(item) != "standard":
            output.append(_reject(item, "route_not_standard_for_variant"))
            continue
        if "min_net_carry_edge_bps" in config and float(item.get("net_carry_edge_bps") or -10**9) < float(
            config["min_net_carry_edge_bps"]
        ):
            output.append(_reject(item, "net_carry_edge_below_variant_minimum"))
            continue
        if "min_expected_funding_bps_to_next" in config and float(
            item.get("expected_funding_bps_to_next") or -10**9
        ) < float(config["min_expected_funding_bps_to_next"]):
            output.append(_reject(item, "expected_funding_below_variant_minimum"))
            continue
        if "max_time_to_funding_minutes" in config:
            time_to_funding = item.get("time_to_next_funding_minutes")
            if time_to_funding is None or float(time_to_funding) > float(config["max_time_to_funding_minutes"]):
                output.append(_reject(item, "time_to_funding_above_variant_max"))
                continue
        if bool(config.get("require_carry_alignment")) and item.get("carry_alignment_status") != "carry_aligned_positive":
            output.append(_reject(item, "carry_economics_not_aligned"))
            continue

        mode = str(config.get("mode") or "incumbent")
        if mode == "funding_alignment" and not _aligned(item):
            output.append(_reject(item, "funding_and_basis_not_aligned"))
            continue
        if mode == "basis_regime_gate" and not _basis_regime_ok(item):
            output.append(_reject(item, "basis_regime_not_confirmed"))
            continue
        if mode == "reverse_basis_recovery":
            cap = config.get("cap_reverse_basis_score")
            if cap is not None:
                item["score"] = min(float(item.get("score") or 0.0), float(cap))
            item["paper_entry_blocked"] = True
            item["promotion_eligible"] = False
            item["okx_recovery_shadow_only"] = True
        if mode == "funding_capture_protected":
            item["score"] = min(100.0, float(item.get("score") or 0.0) + 5.0)
        if mode == "net_carry_positive":
            item["okx_net_carry_variant"] = True
            item["score"] = min(100.0, float(item.get("score") or 0.0) + max(0.0, float(item["net_carry_edge_bps"]) / 4.0))
        if mode == "time_to_funding_capture":
            item["okx_near_funding_variant"] = True
            time_to_funding = float(item.get("time_to_next_funding_minutes") or 9999.0)
            item["score"] = min(100.0, float(item.get("score") or 0.0) + max(0.0, (180.0 - time_to_funding) / 60.0))
        if mode == "basis_carry_disagree_shadow":
            cap = config.get("cap_reverse_basis_score")
            if cap is not None:
                item["score"] = min(float(item.get("score") or 0.0), float(cap))
            item["paper_entry_blocked"] = True
            item["promotion_eligible"] = False
            item["okx_basis_carry_shadow_only"] = True
        output.append(item)
    output.sort(key=lambda row: row.get("score", 0.0), reverse=True)
    return output


def _trial_bucket(now: dt.datetime, cadence_minutes: int) -> str:
    minute = (now.minute // cadence_minutes) * cadence_minutes
    return now.replace(minute=minute, second=0, microsecond=0).isoformat()


def _sample_retired(variant_id: str, trial_bucket: str) -> bool:
    value = int(hashlib.sha256(f"{variant_id}|{trial_bucket}".encode("utf-8")).hexdigest()[:8], 16)
    return value % 10 == 0


def record_variant_trials(
    conn: sqlite3.Connection,
    variants: list[dict],
    candidates_by_variant: dict[str, list[dict]],
    settings: dict,
    scan_id: str,
) -> dict:
    cadence = int(settings.get("signal_redesign", {}).get("trial_cadence_minutes", 15))
    bucket = _trial_bucket(dt.datetime.now(dt.timezone.utc), cadence)
    created = 0
    by_variant: dict[str, int] = {}
    for variant in variants:
        if variant["status"] not in {"active", "shadow", "retired"}:
            continue
        if variant["status"] == "retired" and not _sample_retired(variant["variant_id"], bucket):
            continue
        count = 0
        for candidate in candidates_by_variant.get(variant["variant_id"], [])[:30]:
            if candidate.get("direction") == "watch_only" or candidate.get("candidate_reject_reason"):
                continue
            entry = float(candidate.get("last") or 0.0)
            if entry <= 0:
                continue
            pair_key = f"{bucket}|{candidate['inst_id']}|{candidate['direction']}"
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
                        1 if candidate.get("promotion_eligible", True) and not candidate.get("paper_entry_blocked") else 0,
                    ),
                )
                created += 1
                count += 1
            except sqlite3.IntegrityError:
                continue
        by_variant[variant["variant_id"]] = count
    conn.commit()
    return {"created": created, "by_variant": by_variant, "trial_bucket": bucket}


def _cost_bps(candidate: dict, settings: dict) -> float:
    risk = settings.get("risk", {})
    fee = float(risk.get("taker_fee_bps_per_leg", 5.0))
    slippage = float(risk.get("slippage_bps_per_leg", 3.0))
    leg_count = 2 if candidate.get("direction") in {"short_perp_long_spot", "long_perp_short_spot"} else 1
    return leg_count * (fee + slippage) * 2.0


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
               t.candidate_json
        from signal_trials t
        join signal_variants v on v.variant_id = t.variant_id
        where v.signal_family = ?
        """,
        (SIGNAL_FAMILY,),
    ).fetchall()
    recorded = []
    for row in rows:
        opened = _parse_iso(row["created_at"])
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
                raw = observation.get("observed_at") or observation.get("seen_at") or observation.get("last_checked_at")
                observed_at = _parse_iso(raw) if raw else now
                if observed_at < target:
                    observation = None
                    observed_at = None
            if observation and observation.get("last") not in (None, ""):
                delay = max(0.0, (observed_at - target).total_seconds())
                status = "valid" if delay <= max_delay else "late"
                price = float(observation["last"])
                pnl = (price / float(row["entry_price"]) - 1.0) * 10_000.0 * sign
                pnl -= _cost_bps(candidate, settings)
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


def _bootstrap_lower_bound(differences: list[float], samples: int = 800) -> float | None:
    if not differences:
        return None
    rng = random.Random(154)
    means = []
    for _ in range(samples):
        draw = [differences[rng.randrange(len(differences))] for _ in differences]
        means.append(statistics.fmean(draw))
    return float(_percentile(means, 0.025))


def _evaluate_pair(challenger: dict, incumbent: dict, settings: dict) -> dict:
    cfg = settings.get("signal_redesign", {})
    paired_keys = sorted(set(challenger["valid"]) & set(incumbent["valid"]))
    challenger_values = [challenger["valid"][key] for key in paired_keys]
    incumbent_values = [incumbent["valid"][key] for key in paired_keys]
    differences = [new - old for new, old in zip(challenger_values, incumbent_values)]
    paired_new = _metrics(challenger_values)
    paired_old = _metrics(incumbent_values)
    uplift = statistics.fmean(differences) if differences else None
    lower = _bootstrap_lower_bound(differences)
    coverage = challenger["total_trials"] / incumbent["total_trials"] if incumbent["total_trials"] else 0.0
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
                "update signal_variants set status = 'shadow', consecutive_passes = 0 where variant_id = ?",
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
            add_memory_fact(conn, "signal_variant", challenger["variant_id"], "promoted", active["variant_id"], 0.95, "okx_signal_research", evaluation)
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
                "update signal_variants set consecutive_passes = ?, evaluation_json = ? where variant_id = ?",
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
          and p.trade_type = 'perp_funding_basis'
        """,
        (horizon,),
    ).fetchall()
    return _metrics([float(row["pnl_bps"]) for row in rows])


def _trial_label_quality(conn: sqlite3.Connection, horizon: int = 60) -> dict:
    rows = conn.execute(
        """
        select o.measurement_status, o.delay_seconds, o.pnl_bps
        from signal_trials t
        join signal_variants v on v.variant_id = t.variant_id
        left join signal_trial_outcomes o
          on o.trial_id = t.id and o.horizon_minutes = ?
        where v.signal_family = ?
        """,
        (horizon, SIGNAL_FAMILY),
    ).fetchall()
    status_counts: dict[str, int] = {}
    delays = []
    pnls = []
    for row in rows:
        status = str(row["measurement_status"] or "not_due")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "valid" and row["pnl_bps"] is not None:
            pnls.append(float(row["pnl_bps"]))
            if row["delay_seconds"] is not None:
                delays.append(float(row["delay_seconds"]))
    return {
        "status_counts": status_counts,
        "valid_delay_p95_seconds": round(float(_percentile(delays, 0.95)), 3) if delays else None,
        "reliable_60m_trial_metrics": _metrics(pnls),
    }


def _bucket(value: float, bounds: tuple[float, float, float], labels: tuple[str, str, str, str]) -> str:
    abs_value = abs(value)
    if abs_value <= bounds[0]:
        return labels[0]
    if abs_value <= bounds[1]:
        return labels[1]
    if abs_value <= bounds[2]:
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
        where p.trade_type = 'perp_funding_basis'
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
        route_status = (candidate.get("execution_feasibility") or {}).get("status") or "unknown"
        dims = {
            "signal_family": row["signal_key"],
            "direction": row["direction"],
            "instrument": row["inst_id"],
            "route_status": route_status,
            "basis_bucket": _bucket(float(candidate.get("basis_bps") or 0.0), (10, 30, 75), ("small", "moderate", "large", "extreme")),
            "funding_bucket": _bucket(float(candidate.get("funding_bps") or 0.0), (1, 3, 8), ("small", "moderate", "large", "extreme")),
            "spread_bucket": "tight" if float(candidate.get("spread_bps") or 999) <= 3 else "normal" if float(candidate.get("spread_bps") or 999) <= 8 else "wide",
            "liquidity_bucket": "thin" if float(candidate.get("liquidity_score") or 0) < 0.35 else "normal" if float(candidate.get("liquidity_score") or 0) < 0.65 else "deep",
            "basis_funding_alignment": "aligned" if _aligned(candidate) else "not_aligned",
            "carry_alignment_status": candidate.get("carry_alignment_status", "unknown"),
            "net_carry_bucket": _bucket(
                float(candidate.get("net_carry_edge_bps") or 0.0),
                (0, 3, 10),
                ("negative_or_flat", "thin_positive", "positive", "strong_positive"),
            ),
            "hour_utc": f"{_parse_iso(row['opened_at']).hour:02d}",
            "variant_id": row["signal_variant_id"] or candidate.get("signal_variant_id") or "legacy",
        }
        for dimension, value in dims.items():
            grouped.setdefault((int(row["horizon_minutes"]), dimension, str(value)), []).append(float(row["pnl_bps"]))
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
    }


def _carry_bucket_name(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 0:
        return "negative"
    if value < 3:
        return "thin_positive"
    if value < 10:
        return "positive"
    return "strong_positive"


def _carry_economics_report(candidates_by_variant: dict[str, list[dict]], settings: dict) -> dict:
    rows = [row for candidates in candidates_by_variant.values() for row in candidates if row.get("okx_carry_economics")]
    alignment_counts = collections.Counter(row.get("carry_alignment_status") for row in rows)
    variant_counts = {
        variant_id: {
            "candidate_count": len(candidates),
            "carry_positive_count": sum(float(row.get("net_carry_edge_bps") or -10**9) > 0 for row in candidates),
            "borrow_unknown_count": sum(row.get("borrow_cost_status") == "unknown" for row in candidates),
            "alignment_counts": dict(collections.Counter(row.get("carry_alignment_status") for row in candidates)),
        }
        for variant_id, candidates in candidates_by_variant.items()
    }
    by_bucket = collections.Counter(_carry_bucket_name(row.get("net_carry_edge_bps")) for row in rows)
    top_positive = sorted(
        [
            {
                "variant_id": row.get("signal_variant_id"),
                "inst_id": row.get("inst_id"),
                "direction": row.get("direction"),
                "route_status": _route_status(row),
                "expected_funding_bps_to_next": row.get("expected_funding_bps_to_next"),
                "basis_alignment_edge_bps": row.get("basis_alignment_edge_bps"),
                "round_trip_cost_bps": row.get("round_trip_cost_bps"),
                "net_carry_edge_bps": row.get("net_carry_edge_bps"),
                "carry_alignment_status": row.get("carry_alignment_status"),
                "time_to_next_funding_minutes": row.get("time_to_next_funding_minutes"),
            }
            for row in rows
            if float(row.get("net_carry_edge_bps") or -10**9) > 0
        ],
        key=lambda item: float(item.get("net_carry_edge_bps") or 0.0),
        reverse=True,
    )[:30]
    cost_eroded = sorted(
        [
            {
                "variant_id": row.get("signal_variant_id"),
                "inst_id": row.get("inst_id"),
                "direction": row.get("direction"),
                "net_carry_edge_bps": row.get("net_carry_edge_bps"),
                "round_trip_cost_bps": row.get("round_trip_cost_bps"),
                "carry_alignment_status": row.get("carry_alignment_status"),
            }
            for row in rows
            if row.get("carry_alignment_status") in {"cost_eroded", "negative_expected_funding", "borrow_unknown"}
        ],
        key=lambda item: float(item.get("net_carry_edge_bps") or 0.0),
    )[:30]
    report = {
        "generated_at": utc_now(),
        "mode": settings.get("mode"),
        "live_trading_allowed": bool(settings.get("allow_live_trading", False)),
        "summary": {
            "candidate_count": len(rows),
            "alignment_counts": dict(alignment_counts),
            "net_carry_buckets": dict(by_bucket),
            "top_positive_count": len(top_positive),
            "cost_eroded_or_blocked_count": len(cost_eroded),
            "fee_model": "conservative_config_defaults",
        },
        "variant_counts": variant_counts,
        "top_positive_carry": top_positive,
        "cost_eroded_or_blocked": cost_eroded,
        "settings": _carry_cfg(settings),
        "hard_limits": [
            "Paper-only OKX carry economics.",
            "No account-specific fees, credentials, borrow enablement, or live trading.",
            "Borrow-unknown routes remain conditional or shadow-only.",
        ],
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    CARRY_REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    CARRY_REPORT_MD.write_text(_carry_markdown(report), encoding="utf-8")
    return report


def _carry_markdown(report: dict) -> str:
    summary = report.get("summary", {})
    lines = [
        "# OKX Carry Economics Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Live trading allowed: `{report.get('live_trading_allowed')}`",
        f"- Candidate count: `{summary.get('candidate_count', 0)}`",
        f"- Alignment counts: `{summary.get('alignment_counts', {})}`",
        f"- Net carry buckets: `{summary.get('net_carry_buckets', {})}`",
        f"- Fee model: `{summary.get('fee_model')}`",
        "",
        "## Top Positive Carry Candidates",
        "",
    ]
    for item in report.get("top_positive_carry", [])[:20]:
        lines.append(
            f"- `{item.get('variant_id')}` `{item.get('inst_id')}` `{item.get('direction')}` "
            f"net=`{item.get('net_carry_edge_bps')}`bps funding=`{item.get('expected_funding_bps_to_next')}`bps "
            f"basis=`{item.get('basis_alignment_edge_bps')}`bps cost=`{item.get('round_trip_cost_bps')}`bps "
            f"status=`{item.get('carry_alignment_status')}`"
        )
    lines.extend(["", "## Cost-Eroded Or Blocked", ""])
    for item in report.get("cost_eroded_or_blocked", [])[:20]:
        lines.append(
            f"- `{item.get('variant_id')}` `{item.get('inst_id')}` `{item.get('direction')}` "
            f"net=`{item.get('net_carry_edge_bps')}`bps cost=`{item.get('round_trip_cost_bps')}`bps "
            f"status=`{item.get('carry_alignment_status')}`"
        )
    return "\n".join(lines) + "\n"


def write_report(
    conn: sqlite3.Connection,
    settings: dict,
    trial_activity: dict,
    trial_outcomes: list[dict],
    evaluations: list[dict],
    candidates_by_variant: dict[str, list[dict]],
) -> dict:
    variants = load_variants(conn)
    diagnostics = _diagnostic_groups(conn)
    trial_quality = _trial_label_quality(conn, 60)
    carry_report = _carry_economics_report(candidates_by_variant, settings)
    asset_context = _asset_context_summary(candidates_by_variant)
    active_variant = next((item["variant_id"] for item in variants if item["status"] == "active"), None)
    variant_summaries = {}
    for variant_id, candidates in candidates_by_variant.items():
        reject_counts = collections.Counter(row.get("candidate_reject_reason") for row in candidates if row.get("candidate_reject_reason"))
        direction_counts = collections.Counter(row.get("direction") for row in candidates)
        variant_summaries[variant_id] = {
            "candidate_count": len(candidates),
            "actionable_count": sum(1 for row in candidates if row.get("direction") != "watch_only" and not row.get("candidate_reject_reason")),
            "direction_counts": dict(direction_counts),
            "reject_counts": dict(reject_counts),
        }
    report = {
        "generated_at": utc_now(),
        "mode": settings.get("mode"),
        "live_trading_allowed": bool(settings.get("allow_live_trading", False)),
        "summary": {
            "active_variant": active_variant,
            "variant_count": len(variants),
            "trials_created_this_loop": trial_activity.get("created", 0),
            "trial_outcomes_recorded_this_loop": len(trial_outcomes),
            "reliable_60m_paper_metrics": _reliable_paper_metrics(conn, 60),
            "reliable_60m_trial_metrics": trial_quality["reliable_60m_trial_metrics"],
            "label_status_counts": diagnostics["label_status_counts"],
            "trial_label_status_counts": trial_quality["status_counts"],
            "valid_delay_p95_seconds": diagnostics["valid_delay_p95_seconds"],
            "trial_valid_delay_p95_seconds": trial_quality["valid_delay_p95_seconds"],
            "variant_summaries": variant_summaries,
            "carry_economics": carry_report["summary"],
            "asset_context": asset_context,
        },
        "variants": variants,
        "trial_activity": trial_activity,
        "evaluations": evaluations,
        "diagnostics": diagnostics,
        "carry_economics": {
            "summary": carry_report["summary"],
            "top_positive_carry": carry_report.get("top_positive_carry", [])[:15],
            "cost_eroded_or_blocked": carry_report.get("cost_eroded_or_blocked", [])[:15],
            "report": str(CARRY_REPORT_MD),
        },
        "asset_context": asset_context,
        "promotion_gates": {
            "min_paired_trials": settings.get("signal_redesign", {}).get("min_paired_trials", 30),
            "min_observation_hours": settings.get("signal_redesign", {}).get("min_observation_hours", 48),
            "min_valid_label_rate": settings.get("signal_redesign", {}).get("min_valid_label_rate", 0.95),
            "max_outcome_delay_seconds": settings.get("signal_redesign", {}).get("max_outcome_delay_seconds", 300),
            "min_opportunity_coverage": settings.get("signal_redesign", {}).get("min_opportunity_coverage", 0.30),
            "min_paired_uplift_bps": settings.get("signal_redesign", {}).get("min_paired_uplift_bps", 5.0),
            "consecutive_passes_to_promote": settings.get("signal_redesign", {}).get("consecutive_passes_to_promote", 2),
        },
        "hard_limits": [
            "Paper-only OKX signal variants.",
            "No credentials, order APIs, account actions, startup changes, or live trading.",
            "Late, missing, and legacy-unverified labels cannot promote a variant.",
        ],
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")
    return report


def _markdown(report: dict) -> str:
    summary = report.get("summary", {})
    lines = [
        "# OKX Signal Research Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Live trading allowed: `{report.get('live_trading_allowed')}`",
        f"- Active variant: `{summary.get('active_variant')}`",
        f"- Reliable 60m paper metrics: `{summary.get('reliable_60m_paper_metrics')}`",
        f"- Reliable 60m trial metrics: `{summary.get('reliable_60m_trial_metrics')}`",
        f"- Carry economics: `{summary.get('carry_economics')}`",
        f"- Asset context: `{summary.get('asset_context')}`",
        f"- Label status counts: `{summary.get('label_status_counts')}`",
        f"- Trial label status counts: `{summary.get('trial_label_status_counts')}`",
        f"- Valid outcome delay P95: `{summary.get('valid_delay_p95_seconds')}` seconds",
        f"- Trial valid outcome delay P95: `{summary.get('trial_valid_delay_p95_seconds')}` seconds",
        "",
        "## Variants",
        "",
    ]
    variant_summaries = summary.get("variant_summaries", {})
    for variant in report.get("variants", []):
        variant_summary = variant_summaries.get(variant.get("variant_id"), {})
        lines.append(
            f"- `{variant.get('variant_id')}` status=`{variant.get('status')}` "
            f"passes=`{variant.get('consecutive_passes')}` actionable=`{variant_summary.get('actionable_count')}` "
            f"rejects=`{variant_summary.get('reject_counts', {})}`"
        )
    lines.extend(["", "## Evaluations", ""])
    evaluations = report.get("evaluations", [])
    if not evaluations:
        lines.append("No challenger has enough paired evidence yet.")
    for item in evaluations[:15]:
        lines.append(
            f"- `{item.get('variant_id')}` decision=`{item.get('decision')}` status=`{item.get('status')}` "
            f"evidence=`{item.get('evaluation')}`"
        )
    carry = report.get("carry_economics") or {}
    lines.extend(["", "## Carry Economics", ""])
    lines.append(f"- Report: `{carry.get('report')}`")
    lines.append(f"- Summary: `{carry.get('summary', {})}`")
    for item in carry.get("top_positive_carry", [])[:10]:
        lines.append(
            f"- Positive `{item.get('inst_id')}` `{item.get('direction')}` "
            f"net=`{item.get('net_carry_edge_bps')}`bps status=`{item.get('carry_alignment_status')}`"
        )
    lines.extend(["", "## Top Failing Slices", ""])
    for item in report.get("diagnostics", {}).get("top_failures", [])[:15]:
        lines.append(
            f"- `{item.get('dimension')}={item.get('value')}` n=`{item.get('count')}` "
            f"avg=`{item.get('avg_pnl_bps')}`bps win=`{item.get('win_rate')}` worst10=`{item.get('worst_decile_bps')}`bps"
        )
    lines.extend(["", "## Top Working Slices", ""])
    for item in report.get("diagnostics", {}).get("top_working", [])[:15]:
        lines.append(
            f"- `{item.get('dimension')}={item.get('value')}` n=`{item.get('count')}` "
            f"avg=`{item.get('avg_pnl_bps')}`bps win=`{item.get('win_rate')}`"
        )
    return "\n".join(lines) + "\n"


def run_okx_signal_research(
    conn: sqlite3.Connection,
    settings: dict,
    base_candidates: list[dict],
    price_observations: list[dict],
    scan_id: str,
) -> tuple[list[dict], dict]:
    ensure_initial_variants(conn)
    variants = load_variants(conn)
    candidates_by_variant = {
        variant["variant_id"]: build_variant_candidates(base_candidates, settings, variant)
        for variant in variants
    }
    observations_by_inst = {str(row.get("inst_id")): row for row in price_observations if row.get("inst_id")}
    trial_activity = record_variant_trials(conn, variants, candidates_by_variant, settings, scan_id)
    trial_outcomes = record_trial_outcomes(conn, observations_by_inst, settings)
    evaluations = evaluate_variants(conn, settings)
    variants = load_variants(conn)
    active = next((item for item in variants if item["status"] == "active"), None)
    active_candidates = candidates_by_variant.get(active["variant_id"], []) if active else base_candidates
    if not active_candidates:
        active_candidates = base_candidates
    report = write_report(conn, settings, trial_activity, trial_outcomes, evaluations, candidates_by_variant)
    return active_candidates, report
