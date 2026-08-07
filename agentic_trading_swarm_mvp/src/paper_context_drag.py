"""Paper-only context-drag attribution and ranking.

The overlay measures execution drag by a stable signal/venue/side/liquidity/
latency context.  It is deliberately separate from admission: it annotates and
orders priceable paper experiments but never changes a route, eligibility, or
paper fill permission.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from storage import reliable_paper_label_eligibility_for_trade_row


DEFAULT_CONTEXT_DRAG_POLICY = {
    "enabled": True,
    "paper_only": True,
    "min_closed_trades": 8,
    "underperformance_net_edge_bps": 0.0,
    "ranking_penalty_bps_cap": 30.0,
    "minimum_ranking_multiplier": 0.35,
    "delay_decay_bps_per_second": 0.02,
    "max_delay_decay_bps": 8.0,
}


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _policy(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    policy = dict(DEFAULT_CONTEXT_DRAG_POLICY)
    configured = (settings or {}).get("paper_context_drag") if isinstance(settings, Mapping) else None
    if isinstance(configured, Mapping):
        policy.update({key: value for key, value in configured.items() if value is not None})
    return policy


def _bucket(value: Any, thresholds: tuple[float, ...], labels: tuple[str, ...]) -> str:
    number = _number(value, 0.0) or 0.0
    for threshold, label in zip(thresholds, labels):
        if number <= threshold:
            return label
    return labels[-1]


def _signal_family(candidate: Mapping[str, Any]) -> str:
    return str(
        candidate.get("signal_family")
        or candidate.get("trade_type")
        or candidate.get("strategy")
        or candidate.get("signal_key")
        or "unknown"
    )


def context_identifier(candidate: Mapping[str, Any]) -> str:
    """Build the required paper context identifier without an eligibility effect."""
    source_venue = str(
        candidate.get("source_venue")
        or candidate.get("reference_venue")
        or candidate.get("source_market_venue")
        or candidate.get("reference_source")
        or "reference"
    )
    target_venue = str(candidate.get("target_venue") or candidate.get("venue") or "unknown")
    liquidity_bucket = _bucket(candidate.get("liquidity_score"), (0.35, 0.65, 0.85), ("low", "mid", "high", "very_high"))
    latency_seconds = _number(candidate.get("delay_to_fill_seconds"))
    if latency_seconds is None:
        latency_seconds = (_number(candidate.get("latency_ms"), 0.0) or 0.0) / 1000.0
    latency_bucket = _bucket(latency_seconds, (0.25, 1.0, 5.0), ("instant", "fast", "delayed", "slow"))
    return "|".join(
        (
            _signal_family(candidate),
            source_venue,
            target_venue,
            str(candidate.get("direction") or candidate.get("side") or "unknown"),
            liquidity_bucket,
            latency_bucket,
        )
    )


def _raw_alpha(candidate: Mapping[str, Any]) -> float | None:
    for field in ("raw_paper_alpha_bps", "gross_edge_bps_estimate", "gross_edge_bps", "expected_gross_edge_bps", "edge_bps_estimate"):
        value = _number(candidate.get(field))
        if value is not None:
            return value
    return None


def _adverse_move(candidate: Mapping[str, Any]) -> float:
    explicit = _number(candidate.get("adverse_move_after_signal_bps"))
    if explicit is not None:
        return max(0.0, explicit)
    move = _number(candidate.get("local_short_horizon_trend_bps"))
    if move is None:
        move = _number(candidate.get("short_horizon_return_bps"), 0.0)
    direction = str(candidate.get("direction") or candidate.get("side") or "").lower()
    return max(0.0, -move if direction.startswith(("long", "buy")) else move if direction.startswith(("short", "sell")) else 0.0)


def estimate_context_drag(candidate: Mapping[str, Any], settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Estimate the four requested drag components for one paper candidate."""
    policy = _policy(settings)
    entry_slippage = max(0.0, _number(candidate.get("entry_slippage_bps"), _number(candidate.get("entry_slippage_bps_estimate"), 0.0)) or 0.0)
    spread = max(0.0, _number(candidate.get("spread_bps"), _number(candidate.get("effective_spread_bps"), 0.0)) or 0.0)
    delay_seconds = _number(candidate.get("delay_to_fill_seconds"))
    if delay_seconds is None:
        delay_seconds = (_number(candidate.get("latency_ms"), 0.0) or 0.0) / 1000.0
    delay_decay = min(
        max(0.0, delay_seconds) * max(0.0, float(policy["delay_decay_bps_per_second"])),
        max(0.0, float(policy["max_delay_decay_bps"])),
    )
    components = {
        "entry_slippage_proxy_bps": entry_slippage,
        "spread_proxy_bps": spread / 2.0,
        "delay_to_fill_decay_bps": delay_decay,
        "adverse_move_after_signal_bps": _adverse_move(candidate),
    }
    return {
        "context_id": context_identifier(candidate),
        "raw_paper_alpha_bps": _raw_alpha(candidate),
        "components_bps": {key: round(value, 3) for key, value in components.items()},
        "estimated_drag_bps": round(sum(components.values()), 3),
        "paper_only": True,
        "ranking_only": True,
    }


def context_drag_statistics(conn: sqlite3.Connection, settings: Mapping[str, Any] | None = None) -> dict[str, dict]:
    """Aggregate closed paper outcomes by context using recorded candidate snapshots."""
    policy = _policy(settings)
    rows = conn.execute(
        """
        select candidate_json, review_json, context_json, pnl_bps, close_measurement_status
        from paper_trades
        where status = 'closed' and pnl_bps is not null
        """
    ).fetchall()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if not reliable_paper_label_eligibility_for_trade_row(row)["paper_label_eligible"]:
            continue
        try:
            candidate = json.loads(row["candidate_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(candidate, dict):
            continue
        drag = candidate.get("paper_context_drag")
        if not isinstance(drag, dict):
            drag = estimate_context_drag(candidate, settings)
        raw_alpha = _number(drag.get("raw_paper_alpha_bps"))
        estimated_drag = _number(drag.get("estimated_drag_bps"))
        if raw_alpha is None or estimated_drag is None:
            continue
        grouped[str(drag.get("context_id") or context_identifier(candidate))].append(
            {"raw_alpha": raw_alpha, "drag": estimated_drag, "realized_pnl": float(row["pnl_bps"])}
        )
    stats: dict[str, dict] = {}
    for key, values in grouped.items():
        count = len(values)
        avg_alpha = sum(item["raw_alpha"] for item in values) / count
        avg_drag = sum(item["drag"] for item in values) / count
        stats[key] = {
            "closed_count": count,
            "avg_raw_paper_alpha_bps": round(avg_alpha, 3),
            "realized_context_drag_bps": round(avg_drag, 3),
            "context_net_edge_bps": round(avg_alpha - avg_drag, 3),
            "avg_realized_pnl_bps": round(sum(item["realized_pnl"] for item in values) / count, 3),
            "paper_only": True,
            "ranking_only": True,
        }
    return stats


def apply_context_drag_overlay(candidates: list[dict], context_stats: Mapping[str, Mapping[str, Any]], settings: Mapping[str, Any] | None = None) -> list[dict]:
    """Annotate and down-rank weak contexts without removing any candidate."""
    policy = _policy(settings)
    active = bool(policy["enabled"] and policy["paper_only"] and (settings or {}).get("mode", "paper") == "paper" and not (settings or {}).get("allow_live_trading", False))
    min_samples = max(1, int(policy["min_closed_trades"]))
    for candidate in candidates:
        drag = estimate_context_drag(candidate, settings)
        context = context_stats.get(drag["context_id"], {}) if active else {}
        count = int(context.get("closed_count", 0) or 0)
        net_edge = _number(context.get("context_net_edge_bps"))
        weak = bool(count >= min_samples and net_edge is not None and net_edge < float(policy["underperformance_net_edge_bps"]))
        penalty = min(max(0.0, -net_edge if weak and net_edge is not None else 0.0), float(policy["ranking_penalty_bps_cap"]))
        base_score = _number(candidate.get("paper_ranking_score"), _number(candidate.get("score"), 0.0)) or 0.0
        multiplier = max(float(policy["minimum_ranking_multiplier"]), 1.0 - penalty / max(1.0, float(policy["ranking_penalty_bps_cap"]))) if weak else 1.0
        candidate["paper_context_drag"] = {
            **drag,
            "historical": dict(context) if context else None,
            "ranking_status": "down_ranked_weak_context" if weak else "insufficient_history" if count < min_samples else "neutral_context",
            "ranking_penalty_bps": round(penalty, 3),
            "ranking_multiplier": round(multiplier, 6),
            "eligibility_effect": "none",
        }
        candidate["paper_context_drag_ranking_score"] = round(base_score * multiplier, 3)
        candidate["paper_context_drag_filter_status"] = "ranked_not_blocked" if active else "paper_only_ranking_inactive"
    return candidates


def context_drag_report(context_stats: Mapping[str, Mapping[str, Any]], candidates: list[Mapping[str, Any]]) -> dict[str, Any]:
    weak = [
        {"context_id": key, **dict(value)}
        for key, value in context_stats.items()
        if float(value.get("context_net_edge_bps") or 0.0) < 0.0
    ]
    weak.sort(key=lambda item: item["context_net_edge_bps"])
    return {
        "paper_only": True,
        "ranking_only": True,
        "context_count": len(context_stats),
        "weak_contexts": weak[:20],
        "annotated_candidates": sum(1 for candidate in candidates if candidate.get("paper_context_drag")),
        "down_ranked_candidates": sum(1 for candidate in candidates if (candidate.get("paper_context_drag") or {}).get("ranking_status") == "down_ranked_weak_context"),
    }
