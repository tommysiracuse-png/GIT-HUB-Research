#!/usr/bin/env python3
"""Contextual failure diagnostics and bounded paper filters.

This module looks inside signal families to find failing contexts such as
spread, edge, liquidity, route status, hour, and instrument. It creates
paper-only contextual policies through the existing self-improvement ledger.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
from typing import Iterable

from paper_exploration import exploration_enabled
from storage import (
    RUNS_DIR,
    add_memory_fact,
    add_self_improvement_experiment,
    add_signal_policy,
    active_signal_policies,
    connect,
    reliable_paper_label_eligibility_for_trade_row,
    signal_key as candidate_signal_key,
)


REPORT_JSON = RUNS_DIR / "contextual_failure_report.json"
REPORT_MD = RUNS_DIR / "contextual_failure_report.md"

POLICY_DIMENSIONS = [
    "venue",
    "base_asset",
    "quote_asset",
    "spread_bucket",
    "net_edge_bucket",
    "liquidity_bucket",
    "funding_magnitude_bucket",
    "basis_magnitude_bucket",
    "dislocation_bucket",
    "source_venue_count_bucket",
    "route_status",
    "route_blocker",
    "data_status",
    "hour_utc",
    "instrument",
]

EXACT_MARKET_TUPLE_REPORT_DIMENSIONS = [
    "trade_type",
    "direction",
    "market_context_key",
]

REPORT_DIMENSIONS = [
    *POLICY_DIMENSIONS,
    "route_id",
    "move_24h_bucket",
    "feasibility_status",
    "policy_state",
]


def _report_dimensions() -> list[str]:
    return [*REPORT_DIMENSIONS, *EXACT_MARKET_TUPLE_REPORT_DIMENSIONS]


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_json(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _bucket(value: object, thresholds: list[float], labels: list[str]) -> str:
    numeric = _as_float(value)
    for idx, threshold in enumerate(thresholds):
        if numeric <= threshold:
            return labels[idx]
    return labels[-1]


def _abs_bucket(value: object, thresholds: list[float], labels: list[str]) -> str:
    return _bucket(abs(_as_float(value)), thresholds, labels)


def _hour_utc(candidate: dict, fallback: str | None = None) -> str:
    parsed = _parse_iso(candidate.get("seen_at") or fallback)
    if not parsed:
        return "unknown"
    return f"{parsed.astimezone(dt.timezone.utc).hour:02d}"


def _first_route_blocker(feasibility: dict, route: dict, review: dict) -> str:
    blockers = (
        review.get("route_blockers")
        or feasibility.get("route_blockers")
        or route.get("route_blockers")
        or feasibility.get("missing_requirements")
        or route.get("missing_permissions")
        or []
    )
    if isinstance(blockers, str):
        blockers = [blockers]
    cleaned = sorted(str(item) for item in blockers if item)
    return cleaned[0] if cleaned else "none"


def _source_venue_count_bucket(value: object) -> str:
    if value in (None, ""):
        return "unknown"
    count = int(_as_float(value))
    if count <= 1:
        return "single_venue"
    if count <= 2:
        return "two_venues"
    if count <= 4:
        return "few_venues"
    return "broad_venue_set"


def build_context_features(
    candidate: dict,
    review: dict | None = None,
    *,
    net_edge_bps: float | None = None,
    fallback_time: str | None = None,
) -> dict:
    """Build stable context features used by diagnostics and policy matching."""
    review = review or {}
    feasibility = candidate.get("execution_feasibility") or {}
    route = candidate.get("execution_route") or {}
    edge = net_edge_bps
    if edge is None:
        edge = review.get("net_edge_bps_estimate")
    if edge is None:
        edge = candidate.get("edge_bps_estimate", 0.0)
    applied = review.get("applied_policies") or []
    any_filtered = any(item.get("filtered") for item in applied if isinstance(item, dict))
    policy_state = "policy_filtered" if any_filtered else "policy_active" if applied else "no_policy"
    route_status = (
        review.get("route_status")
        or feasibility.get("route_status")
        or route.get("route_status")
        or feasibility.get("status")
        or "unknown"
    )
    venue = str(candidate.get("venue") or "unknown")
    direction = str(candidate.get("direction") or "unknown")
    trade_type = str(
        candidate.get("trade_type")
        or review.get("trade_type")
        or feasibility.get("trade_type")
        or route.get("trade_type")
        or "unknown"
    )
    route_id = review.get("route_id") or feasibility.get("route_id") or route.get("route_id") or candidate.get("route_id")
    route_blocker = _first_route_blocker(feasibility, route, review)
    return {
        "venue": str(candidate.get("venue") or "unknown"),
        "instrument": str(candidate.get("inst_id") or "unknown"),
        "direction": str(candidate.get("direction") or "unknown"),
        "trade_type": trade_type,
        "base_asset": str(candidate.get("base") or candidate.get("comparison_key") or "unknown"),
        "quote_asset": str(candidate.get("quote") or "unknown"),
        "data_status": str(candidate.get("data_status") or "unknown"),
        "route_status": str(route_status or "unknown"),
        "route_blocker": route_blocker,
        "route_id": str(route_id or "unknown"),
        "feasibility_status": str(review.get("feasibility_status") or feasibility.get("status") or "unknown"),
        "liquidity_bucket": _bucket(
            candidate.get("liquidity_score", 0.0),
            [0.35, 0.65, 0.85],
            ["thin", "normal", "good", "deep"],
        ),
        "spread_bucket": _bucket(
            candidate.get("spread_bps", 999.0),
            [3.0, 8.0, 20.0],
            ["tight", "normal", "wide", "extreme"],
        ),
        "net_edge_bucket": _bucket(edge, [0.0, 2.0, 8.0, 20.0], ["negative", "thin", "ok", "strong", "huge"]),
        "funding_magnitude_bucket": _abs_bucket(
            candidate.get("funding_bps", 0.0),
            [1.0, 3.0, 10.0],
            ["quiet", "moderate", "high", "extreme"],
        ),
        "basis_magnitude_bucket": _abs_bucket(
            candidate.get("basis_bps", 0.0),
            [5.0, 15.0, 50.0],
            ["quiet", "moderate", "high", "extreme"],
        ),
        "dislocation_bucket": _abs_bucket(
            candidate.get("venue_deviation_bps", candidate.get("edge_bps_estimate", 0.0)),
            [12.0, 25.0, 50.0],
            ["small", "moderate", "large", "extreme"],
        ),
        "source_venue_count_bucket": _source_venue_count_bucket(candidate.get("source_venue_count")),
        "move_24h_bucket": _abs_bucket(
            candidate.get("change_24h_pct", 0.0),
            [2.0, 8.0, 20.0],
            ["calm", "active", "shock", "extreme"],
        ),
        "hour_utc": _hour_utc(candidate, fallback=fallback_time),
        "policy_state": policy_state,
        "market_context_key": f"{venue}|{trade_type}|{direction}",
    }


def context_matches(features: dict, context_filter: dict | None) -> bool:
    if not context_filter:
        return True
    for key, expected in context_filter.items():
        actual = features.get(key)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _signal_key_for(candidate: dict, review: dict | None = None) -> str:
    if review and review.get("signal_key"):
        return str(review["signal_key"])
    return candidate_signal_key(candidate)


def _closed_trade_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select id, opened_at, closed_at, venue, inst_id, direction, trade_type,
               signal_key, pnl_bps, candidate_json, review_json, context_json,
               close_measurement_status
        from paper_trades
        where status = 'closed' and pnl_bps is not null
        order by closed_at asc, id asc
        """
    ).fetchall()
    output = []
    for row in rows:
        if not reliable_paper_label_eligibility_for_trade_row(row)["paper_label_eligible"]:
            continue
        candidate = _parse_json(row["candidate_json"], {})
        review = _parse_json(row["review_json"], {})
        features = build_context_features(
            candidate,
            review,
            net_edge_bps=review.get("net_edge_bps_estimate"),
            fallback_time=row["opened_at"],
        )
        output.append(
            {
                "id": row["id"],
                "opened_at": row["opened_at"],
                "closed_at": row["closed_at"],
                "signal_key": row["signal_key"] or _signal_key_for(candidate, review),
                "pnl_bps": _as_float(row["pnl_bps"]),
                "candidate": candidate,
                "review": review,
                "features": features,
            }
        )
    return output


def _metrics(items: list[dict]) -> dict:
    pnls = [_as_float(item["pnl_bps"]) for item in items]
    wins = sum(1 for pnl in pnls if pnl > 0)
    recent = pnls[-10:]
    recent_wins = sum(1 for pnl in recent if pnl > 0)
    avg = round(sum(pnls) / len(pnls), 3) if pnls else None
    recent_avg = round(sum(recent) / len(recent), 3) if recent else None
    return {
        "closed_count": len(pnls),
        "wins": wins,
        "win_rate": round(wins / len(pnls), 3) if pnls else None,
        "recent_closed_count": len(recent),
        "recent_win_rate": round(recent_wins / len(recent), 3) if recent else None,
        "avg_pnl_bps": avg,
        "recent_avg_pnl_bps": recent_avg,
        "recent_delta_bps": round(recent_avg - avg, 3) if recent_avg is not None and avg is not None else None,
        "best_bps": round(max(pnls), 3) if pnls else None,
        "worst_bps": round(min(pnls), 3) if pnls else None,
    }


def _observation_profile(trade: dict) -> str | None:
    """Return a named research surface for broad, paper-only attribution.

    These labels intentionally describe the source/model surface rather than a
    tradable symbol.  A bad result on one symbol is not enough to characterize
    a surface; the diversity checks in ``cross_context_failure_observations``
    below make that distinction explicit.
    """
    candidate = trade.get("candidate") or {}
    features = trade.get("features") or {}
    text = " ".join(
        str(value or "")
        for value in (
            trade.get("signal_key"),
            candidate.get("venue"),
            candidate.get("trade_type"),
            candidate.get("market_surface"),
            candidate.get("strategy_family"),
            features.get("venue"),
            features.get("trade_type"),
        )
    ).lower()
    if "yahoo_proxy" in text and "global_proxy_momentum" in text:
        return "yahoo_proxy_momentum"
    if "frontier_crypto_venue_map" in text:
        return "frontier_spot_venue_map"
    if "okx" in text and "perp_funding_basis" in text and ("basis" in text or "funding" in text):
        return "okx_basis_or_funding"
    return None


def _candidate_observation_profile(candidate: dict) -> str | None:
    """Resolve a live candidate to the same research surface as closed trades."""
    return _observation_profile(
        {
            "signal_key": candidate.get("signal_key"),
            "candidate": candidate,
            "features": build_context_features(candidate),
        }
    )


def annotate_candidates_with_cross_context_diagnostics(
    candidates: Iterable[dict],
    observations: Iterable[dict],
    settings: dict,
) -> list[dict]:
    """Attach recurring-loss attribution without suppressing paper experiments.

    Persistent cross-context failures lower the rank and allocation of an
    otherwise priceable paper candidate and make the reason visible to
    downstream research.  They never set a rejection, quarantine, or
    entry-block field; fresh observations continue to provide the
    rehabilitation window.
    """
    cfg = settings.get("contextual_failure_filters", {})
    allocation_cap = max(
        0.01,
        min(1.0, float(cfg.get("cross_context_failure_allocation_multiplier", 0.25))),
    )
    ranking_multiplier = max(
        0.0,
        min(1.0, float(cfg.get("cross_context_failure_score_multiplier", 0.75))),
    )
    by_context = {
        str(item.get("context")): item
        for item in observations
        if item.get("state") in {"persistent_failure", "rehabilitated"}
    }
    annotated = []
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        context = _candidate_observation_profile(candidate)
        observation = by_context.get(str(context))
        if not observation:
            annotated.append(candidate)
            continue

        diagnostic = {
            "context": context,
            "state": observation.get("state"),
            "closed_count": observation.get("closed_count"),
            "avg_pnl_bps": observation.get("avg_pnl_bps"),
            "win_rate": observation.get("win_rate"),
            "research_note": observation.get("research_note"),
            "recommendation_handling": "diagnostic_ranking_and_sizing_only",
            "paper_entry_blocked": False,
            "rehabilitation_criteria": observation.get("rehabilitation_criteria", {}),
        }
        candidate["cross_context_failure_diagnostic"] = diagnostic
        if observation.get("state") == "persistent_failure":
            try:
                candidate["score"] = round(float(candidate["score"]) * ranking_multiplier, 3)
                candidate["cross_context_failure_score_multiplier"] = ranking_multiplier
            except (KeyError, TypeError, ValueError):
                pass
            existing = candidate.get("paper_allocation_multiplier")
            try:
                existing_allocation = float(existing)
            except (TypeError, ValueError):
                existing_allocation = 1.0
            if existing_allocation > 0.0:
                candidate["paper_allocation_multiplier"] = min(existing_allocation, allocation_cap)
            candidate["cross_context_failure_allocation_cap"] = allocation_cap
        annotated.append(candidate)
    return annotated


def _direction_side(value: object) -> str:
    normalized = str(value or "").lower()
    if "long" in normalized:
        return "long"
    if "short" in normalized:
        return "short"
    return "unknown"


def _observation_sub_mode(trade: dict) -> str:
    candidate = trade.get("candidate") or {}
    text = " ".join(
        str(value or "")
        for value in (trade.get("signal_key"), candidate.get("direction"), candidate.get("trade_type"))
    ).lower()
    if "funding" in text:
        return "funding"
    if "basis" in text:
        return "basis"
    return str(candidate.get("trade_type") or (trade.get("features") or {}).get("trade_type") or "unknown")


def cross_context_failure_observations(trades: list[dict], settings: dict) -> list[dict]:
    """Attribute recurring paper losses without suppressing paper experiments.

    A context is only called persistently failing after losses recur on both
    directional sides or across the relevant independent surfaces.  The result
    is report/ranking evidence, never a candidate filter, quarantine, or
    paper-entry block.  This preserves the fresh paper validation window that
    is required to discover rehabilitation.
    """
    cfg = settings.get("contextual_failure_filters", {})
    min_closed = int(cfg.get("cross_context_min_closed", cfg.get("min_closed_for_filter", 5)))
    validation_window = max(1, int(cfg.get("cross_context_validation_window", 5)))
    release_avg = float(cfg.get("release_min_avg_pnl_bps", 10.0))
    release_win_rate = float(cfg.get("release_min_win_rate", 0.55))
    grouped: dict[str, list[dict]] = collections.defaultdict(list)
    for trade in trades:
        profile = _observation_profile(trade)
        if profile:
            grouped[profile].append(trade)

    observations = []
    for profile, items in grouped.items():
        metrics = _metrics(items)
        directions = sorted(
            {
                _direction_side(
                    (item.get("features") or {}).get("direction")
                    or (item.get("candidate") or {}).get("direction")
                )
                for item in items
            }
        )
        directions = [direction for direction in directions if direction != "unknown"]
        venues = sorted(
            {
                str(
                    (item.get("features") or {}).get("venue")
                    or (item.get("candidate") or {}).get("venue")
                    or "unknown"
                )
                for item in items
            }
        )
        sub_modes = sorted({_observation_sub_mode(item) for item in items})
        recent_items = items[-validation_window:]
        recent_metrics = _metrics(recent_items)
        prior_items = items[:-validation_window]
        prior_metrics = _metrics(prior_items)
        both_directions = {"long", "short"}.issubset(directions)
        multi_venue = len(venues) >= 2
        multi_sub_mode = len(sub_modes) >= 2
        coverage_met = (
            both_directions
            if profile == "yahoo_proxy_momentum"
            else multi_venue or both_directions
            if profile == "frontier_spot_venue_map"
            else multi_sub_mode or both_directions
        )
        prior_failure = (
            int(metrics.get("closed_count") or 0) >= min_closed
            and bool(prior_items)
            and float(prior_metrics.get("avg_pnl_bps") or 0.0) < 0.0
            and coverage_met
        )
        rehabilitation_met = (
            prior_failure
            and len(recent_items) >= validation_window
            and float(recent_metrics.get("avg_pnl_bps") or 0.0) >= release_avg
            and float(recent_metrics.get("win_rate") or 0.0) >= release_win_rate
        )
        persistent_failure = (
            int(metrics.get("closed_count") or 0) >= min_closed
            and float(metrics.get("avg_pnl_bps") or 0.0) < 0.0
            and coverage_met
            and not rehabilitation_met
        )
        state = "rehabilitated" if rehabilitation_met else "persistent_failure" if persistent_failure else "observe"
        observations.append(
            {
                "context": profile,
                "state": state,
                "closed_count": metrics["closed_count"],
                "avg_pnl_bps": metrics["avg_pnl_bps"],
                "win_rate": metrics["win_rate"],
                "directions": directions,
                "venues": venues,
                "sub_modes": sub_modes,
                "coverage": {
                    "both_directions": both_directions,
                    "multi_venue": multi_venue,
                    "multi_sub_mode": multi_sub_mode,
                    "coverage_met": coverage_met,
                },
                "fresh_validation": recent_metrics,
                "research_note": (
                    "Recurring paper losses are attributed to this model surface; keep priceable candidates "
                    "emitted for paper exploration, attach this diagnostic, and use ranking/sizing or synthetic routing."
                    if persistent_failure
                    else "Fresh paper validation meets the stated expectancy and stability criteria."
                    if rehabilitation_met
                    else "Evidence is not yet diverse or large enough to attribute a persistent model-surface failure."
                ),
                "recommendation_handling": "diagnostic_ranking_and_sizing_only",
                "paper_entry_blocked": False,
                "rehabilitation_criteria": {
                    "validation_window_closed_trades": validation_window,
                    "min_avg_pnl_bps": release_avg,
                    "min_win_rate": release_win_rate,
                },
            }
        )
    observations.sort(key=lambda item: (item["state"] != "persistent_failure", item["avg_pnl_bps"] or 0.0))
    return observations


def _failure_score(metrics: dict) -> float:
    count = int(metrics.get("closed_count") or 0)
    avg = float(metrics.get("avg_pnl_bps") or 0.0)
    win_rate = float(metrics.get("win_rate") or 0.0)
    worst = float(metrics.get("worst_bps") or 0.0)
    score = max(0.0, -avg) * min(count, 25) / 5.0
    score += max(0.0, 0.45 - win_rate) * 120.0
    if worst <= -500:
        score += min(abs(worst) / 50.0, 60.0)
    return round(score, 3)


def _recovery_score(metrics: dict) -> float:
    count = int(metrics.get("closed_count") or 0)
    avg = float(metrics.get("avg_pnl_bps") or 0.0)
    win_rate = float(metrics.get("win_rate") or 0.0)
    recent_delta = float(metrics.get("recent_delta_bps") or 0.0)
    score = max(0.0, avg) * min(count, 25) / 8.0
    score += max(0.0, win_rate - 0.5) * 100.0
    score += max(0.0, recent_delta) * 0.25
    return round(score, 3)


def _failure_domain(dimension: str) -> str:
    if dimension == "signal_family":
        return "signal_family"
    if dimension in {"route_status", "route_id", "route_blocker", "feasibility_status"}:
        return "route_or_feasibility"
    if dimension == "data_status":
        return "data_quality"
    if dimension in {
        "spread_bucket",
        "net_edge_bucket",
        "liquidity_bucket",
        "funding_magnitude_bucket",
        "basis_magnitude_bucket",
        "dislocation_bucket",
        "source_venue_count_bucket",
        "move_24h_bucket",
    }:
        return "signal_quality"
    return "market_context"


def _classify(metrics: dict, settings: dict, dimension: str = "") -> str:
    cfg = settings.get("contextual_failure_filters", {})
    min_closed = int(cfg.get("min_closed_for_filter", 5))
    count = int(metrics.get("closed_count") or 0)
    avg = float(metrics.get("avg_pnl_bps") or 0.0)
    win_rate = float(metrics.get("win_rate") or 0.0)
    worst = float(metrics.get("worst_bps") or 0.0)
    recent_avg = metrics.get("recent_avg_pnl_bps")
    recent_win_rate = metrics.get("recent_win_rate")
    if count < min_closed:
        return "low_sample_observe"
    if (
        avg < 10.0
        and recent_avg is not None
        and recent_win_rate is not None
        and float(recent_avg) >= float(cfg.get("release_min_avg_pnl_bps", 10.0))
        and float(recent_win_rate) >= float(cfg.get("release_min_win_rate", 0.55))
        and worst > float(cfg.get("release_max_worst_bps", -500.0))
    ):
        return "recovery_candidate"
    if avg >= 20.0 and win_rate >= 0.5:
        return "working_slice"
    if avg <= -30.0 or (avg < 5.0 and win_rate < 0.4) or worst <= -1000.0:
        return "structural_failure" if dimension == "signal_family" else "contextual_failure"
    return "mixed"


def _is_strategy_lab_signal(signal_key: str) -> bool:
    return str(signal_key or "").startswith("STRATEGY_LAB|")


def _segment_promotion_guard_enabled(settings: dict) -> bool:
    cfg = settings.get("contextual_failure_filters", {})
    return bool(cfg.get("strategy_lab_exact_market_promotion_guard", True))


def _has_sufficient_segment_evidence(metrics: dict, settings: dict) -> bool:
    cfg = settings.get("contextual_failure_filters", {})
    min_closed = int(cfg.get("promotion_min_closed", cfg.get("min_closed_for_filter", 5)))
    min_win_rate = float(cfg.get("promotion_min_win_rate", 0.5))
    min_avg_pnl_bps = float(cfg.get("promotion_min_avg_pnl_bps", 0.0))
    count = int(metrics.get("closed_count") or 0)
    avg = float(metrics.get("avg_pnl_bps") or 0.0)
    win_rate = float(metrics.get("win_rate") or 0.0)
    return count >= min_closed and avg >= min_avg_pnl_bps and win_rate >= min_win_rate


def _annotate_strategy_lab_promotion_guards(groups: list[dict], settings: dict) -> None:
    if not _segment_promotion_guard_enabled(settings):
        return
    by_signal: dict[str, list[dict]] = collections.defaultdict(list)
    for item in groups:
        by_signal[str(item.get("signal_key") or "unknown")].append(item)
    for signal_key, items in by_signal.items():
        if not _is_strategy_lab_signal(signal_key):
            continue
        promotable_market_context_keys = sorted(
            str(item.get("value") or "unknown")
            for item in items
            if item.get("dimension") == "market_context_key" and _has_sufficient_segment_evidence(item, settings)
        )
        promotable_keys = set(promotable_market_context_keys)
        for item in items:
            if item.get("dimension") == "signal_family":
                item["promotion_scope"] = "segment_matched_only"
                item["promotion_guard"] = "segment_matched_only" if promotable_market_context_keys else "blocked_pending_segment_evidence"
                item["promotable_market_context_keys"] = promotable_market_context_keys
            elif item.get("dimension") == "market_context_key":
                item["promotion_scope"] = "exact_market_context_only"
                item["promotion_guard"] = "eligible_exact_market_context" if str(item.get("value") or "unknown") in promotable_keys else "blocked_pending_segment_evidence"
                item["promotable_market_context_keys"] = promotable_market_context_keys


def _build_groups(trades: list[dict], settings: dict) -> list[dict]:
    grouped: dict[tuple[str, str, str], list[dict]] = collections.defaultdict(list)
    for trade in trades:
        signal = trade["signal_key"]
        grouped[(signal, "signal_family", "all")].append(trade)
        features = trade["features"]
        for dim in _report_dimensions():
            value = features.get(dim, "unknown")
            grouped[(signal, dim, str(value))].append(trade)
    output = []
    for (signal, dimension, value), items in grouped.items():
        metrics = _metrics(items)
        status = _classify(metrics, settings, dimension)
        context_filter = {}
        if dimension in POLICY_DIMENSIONS:
            context_filter = {dimension: value}
        output.append(
            {
                "signal_key": signal,
                "dimension": dimension,
                "value": value,
                "context_filter": context_filter,
                "status": status,
                "failure_domain": _failure_domain(dimension),
                "failure_score": _failure_score(metrics),
                "recovery_score": _recovery_score(metrics),
                **metrics,
            }
        )
    _annotate_strategy_lab_promotion_guards(output, settings)
    output.sort(key=lambda item: (item["failure_score"], item["closed_count"]), reverse=True)
    return output


def _augment_counts(
    conn: sqlite3.Connection,
    groups: list[dict],
    opportunity_sample_limit: int = 5_000,
) -> None:
    index = {(item["signal_key"], item["dimension"], item["value"]): item for item in groups}
    for item in groups:
        item["opportunity_count"] = 0
        item["paper_entries_opened"] = 0
        item["paper_entries_filtered_by_policy"] = 0

    rows = conn.execute(
        """
        select candidate_json, review_json, seen_at
        from opportunities
        order by id desc
        limit ?
        """,
        (max(0, int(opportunity_sample_limit)),),
    )
    for row in rows:
        candidate = _parse_json(row["candidate_json"], {})
        review = _parse_json(row["review_json"], {})
        signal = _signal_key_for(candidate, review)
        features = build_context_features(candidate, review, fallback_time=row["seen_at"])
        filtered = any(item.get("filtered") for item in review.get("applied_policies", []) if isinstance(item, dict))
        for dim in _report_dimensions():
            key = (signal, dim, str(features.get(dim, "unknown")))
            if key not in index:
                continue
            index[key]["opportunity_count"] += 1
            if filtered:
                index[key]["paper_entries_filtered_by_policy"] += 1

    rows = conn.execute(
        """
        select opened_at, candidate_json, review_json, signal_key
        from paper_trades
        """
    )
    for row in rows:
        candidate = _parse_json(row["candidate_json"], {})
        review = _parse_json(row["review_json"], {})
        signal = row["signal_key"] or _signal_key_for(candidate, review)
        features = build_context_features(candidate, review, fallback_time=row["opened_at"])
        for dim in _report_dimensions():
            key = (signal, dim, str(features.get(dim, "unknown")))
            if key in index:
                index[key]["paper_entries_opened"] += 1


def _upsert_contextual_stats(conn: sqlite3.Connection, groups: Iterable[dict]) -> None:
    now = _utc_now()
    for item in groups:
        if item.get("closed_count", 0) <= 0:
            continue
        if item.get("dimension") not in POLICY_DIMENSIONS and item.get("dimension") != "signal_family":
            continue
        key = f"{item['signal_key']}|{item['dimension']}={item['value']}"
        conn.execute(
            """
            insert into contextual_stats (
                context_key, closed_count, wins, avg_pnl_bps, win_rate, updated_at
            ) values (?, ?, ?, ?, ?, ?)
            on conflict(context_key) do update set
                closed_count = excluded.closed_count,
                wins = excluded.wins,
                avg_pnl_bps = excluded.avg_pnl_bps,
                win_rate = excluded.win_rate,
                updated_at = excluded.updated_at
            """,
            (
                key,
                int(item["closed_count"]),
                int(item["wins"]),
                float(item["avg_pnl_bps"] or 0.0),
                float(item["win_rate"] or 0.0),
                now,
            ),
        )
    conn.commit()


def _active_contextual_fingerprints(policies: list[dict]) -> set[str]:
    fingerprints = set()
    for policy in policies:
        if policy.get("policy_type") != "contextual_failure_filter":
            continue
        payload = policy.get("policy") or {}
        raw = json.dumps(
            {"signal_key": policy.get("signal_key"), "context_filter": payload.get("context_filter", {})},
            sort_keys=True,
        )
        fingerprints.add(hashlib.sha256(raw.encode("utf-8")).hexdigest())
    return fingerprints


def _active_contextual_counts(policies: list[dict]) -> collections.Counter[str]:
    counts: collections.Counter[str] = collections.Counter()
    for policy in policies:
        if policy.get("policy_type") == "contextual_failure_filter":
            counts[str(policy.get("signal_key"))] += 1
    return counts


def _consolidate_contextual_policy_caps(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    cfg = settings.get("contextual_failure_filters", {})
    max_active = int(cfg.get("max_active_policies_per_signal", 4))
    if max_active <= 0:
        return []
    rows = conn.execute(
        """
        select policy_id, experiment_id, signal_key, evidence_json, created_at
        from signal_policies
        where status = 'active' and policy_type = 'contextual_failure_filter'
        order by signal_key, created_at asc
        """
    ).fetchall()
    by_signal: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        evidence = _parse_json(row["evidence_json"], {})
        group = evidence.get("context_group") or {}
        by_signal[str(row["signal_key"])].append(
            {
                "policy_id": row["policy_id"],
                "experiment_id": row["experiment_id"],
                "signal_key": row["signal_key"],
                "failure_score": _as_float(group.get("failure_score")),
                "closed_count": int(group.get("closed_count") or 0),
                "context_filter": group.get("context_filter", {}),
                "created_at": row["created_at"],
            }
        )
    superseded = []
    now = _utc_now()
    for signal, policies in by_signal.items():
        if len(policies) <= max_active:
            continue
        policies.sort(key=lambda item: (item["failure_score"], item["closed_count"]), reverse=True)
        for item in policies[max_active:]:
            conn.execute("update signal_policies set status = 'superseded' where policy_id = ?", (item["policy_id"],))
            conn.execute(
                """
                update self_improvement_experiments
                set status = 'superseded',
                    decision = 'superseded_by_contextual_policy_cap',
                    completed_at = coalesce(completed_at, ?),
                    reflection = 'Contextual policy exceeded the active per-signal cap and was superseded to preserve exploration.'
                where id = ? and status = 'active'
                """,
                (now, item["experiment_id"]),
            )
            superseded.append(item)
    conn.commit()
    return superseded


def _quarantined_signals(policies: list[dict]) -> set[str]:
    output = set()
    for policy in policies:
        payload = policy.get("policy") or {}
        if policy.get("policy_type") == "safety_governor" and payload.get("governor_mode") == "quarantine":
            output.add(policy.get("signal_key"))
    return {item for item in output if item}


def _policy_payload(group: dict, settings: dict) -> dict:
    cfg = settings.get("contextual_failure_filters", {})
    risk = settings.get("risk", {})
    severe = float(group.get("avg_pnl_bps") or 0.0) <= -100.0 or float(group.get("worst_bps") or 0.0) <= -1000.0
    exploration = exploration_enabled(settings)
    would_pause = bool(severe and group["dimension"] not in {"instrument", "hour_utc"})
    return {
        "context_filter": group["context_filter"],
        "min_score_delta": 10.0 if severe else 6.0,
        "min_net_edge_bps": max(float(risk.get("min_net_edge_bps", 2.0)) + (8.0 if severe else 4.0), 6.0),
        "max_spread_bps": min(float(risk.get("max_spread_bps", 8.0)), 4.0 if severe else 5.0),
        "allocation_multiplier": 0.1 if severe else 0.25,
        "pause_entries": would_pause and not exploration,
        "would_pause_outside_exploration": would_pause and exploration,
        "expires_after_trades": int(cfg.get("default_policy_trade_ttl", 30)),
        "allow_recovery_probes": True,
        "recovery_probe_every_n_reviews": int(cfg.get("recovery_probe_every_reviews", 25)),
        "recovery_probe_allocation_multiplier": float(cfg.get("recovery_probe_allocation_multiplier", 0.1)),
        "release_criteria": {
            "min_closed_trades": int(cfg.get("release_min_recovery_trades", 5)),
            "min_avg_pnl_bps": float(cfg.get("release_min_avg_pnl_bps", 10.0)),
            "min_win_rate": float(cfg.get("release_min_win_rate", 0.55)),
            "max_worst_bps": float(cfg.get("release_max_worst_bps", -500.0)),
        },
        "reason": "contextual_severe_failure" if severe else "contextual_failure_tightening",
    }


def _policy_id(group: dict, policy: dict) -> str:
    raw = json.dumps({"group": group, "policy": policy}, sort_keys=True)
    return "ctx_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _market_key(signal_key: str) -> str:
    parts = signal_key.split("|")
    return "|".join(parts[:2]) if len(parts) >= 2 else signal_key


def _create_policies(conn: sqlite3.Connection, groups: list[dict], settings: dict) -> tuple[list[dict], list[dict]]:
    cfg = settings.get("contextual_failure_filters", {})
    if not cfg.get("create_policies", True):
        return [], []
    active = active_signal_policies(conn)
    fingerprints = _active_contextual_fingerprints(active)
    contextual_counts = _active_contextual_counts(active)
    quarantined = _quarantined_signals(active)
    max_new = int(cfg.get("max_new_policies_per_loop", 3))
    max_active = int(cfg.get("max_active_policies_per_signal", 4))
    created = []
    skipped = []
    candidates = [
        item
        for item in groups
        if item["status"] == "contextual_failure"
        and item["dimension"] in POLICY_DIMENSIONS
        and int(item.get("closed_count") or 0) >= int(cfg.get("min_closed_for_filter", 5))
    ]
    candidates.sort(key=lambda item: (item["failure_score"], item["closed_count"]), reverse=True)
    for group in candidates:
        if len(created) >= max_new:
            break
        if group.get("failure_domain") in {"route_or_feasibility", "data_quality"}:
            skipped.append({**group, "skip_reason": "diagnostic_only_route_or_data_issue"})
            continue
        if group["signal_key"] in quarantined:
            skipped.append({**group, "skip_reason": "covered_by_signal_safety_quarantine"})
            continue
        fingerprint_raw = json.dumps(
            {"signal_key": group["signal_key"], "context_filter": group["context_filter"]},
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(fingerprint_raw.encode("utf-8")).hexdigest()
        if fingerprint in fingerprints:
            skipped.append({**group, "skip_reason": "contextual_policy_already_active"})
            continue
        if contextual_counts[group["signal_key"]] >= max_active:
            skipped.append({**group, "skip_reason": "active_contextual_policy_cap_reached"})
            continue
        policy = _policy_payload(group, settings)
        source_id = f"contextual_failure_filter:{fingerprint[:24]}"
        action = (
            f"Apply contextual paper filter to {group['signal_key']} when "
            f"{group['dimension']}={group['value']}."
        )
        experiment_id = add_self_improvement_experiment(
            conn,
            source_id,
            "contextual_failure_filters",
            "contextual_failure_filter",
            int(cfg.get("policy_priority", 91)),
            _market_key(group["signal_key"]),
            group["signal_key"],
            f"Contextual filter should improve paper outcomes for {group['signal_key']} in failing context.",
            action,
            group,
            policy,
        )
        if not experiment_id:
            skipped.append({**group, "skip_reason": "experiment_already_exists"})
            continue
        pid = _policy_id(group, policy)
        inserted = add_signal_policy(
            conn,
            pid,
            experiment_id,
            source_id,
            group["signal_key"],
            _market_key(group["signal_key"]),
            "contextual_failure_filter",
            policy,
            {"context_group": group, "generated_by": "contextual_failure_filters"},
        )
        if not inserted:
            skipped.append({**group, "skip_reason": "policy_already_exists"})
            continue
        fingerprints.add(fingerprint)
        contextual_counts[group["signal_key"]] += 1
        created_item = {"experiment_id": experiment_id, "policy_id": pid, "group": group, "policy": policy}
        add_memory_fact(
            conn,
            "contextual_failure_policy",
            group["signal_key"],
            "activated",
            f"{group['dimension']}={group['value']}",
            0.86,
            "contextual_failure_filters",
            created_item,
        )
        created.append(created_item)
    return created, skipped[:20]


def _protected_working_slices(groups: list[dict]) -> list[dict]:
    failing_signals = {
        item["signal_key"]
        for item in groups
        if item["dimension"] == "signal_family" and item["status"] == "structural_failure"
    }
    slices = [
        item
        for item in groups
        if item["signal_key"] in failing_signals
        and item["dimension"] != "signal_family"
        and item["status"] in {"working_slice", "recovery_candidate"}
    ]
    slices.sort(key=lambda item: (item["recovery_score"], item["closed_count"]), reverse=True)
    return slices


def run_contextual_failure_filters(conn: sqlite3.Connection, settings: dict) -> dict:
    cfg = settings.get("contextual_failure_filters", {})
    if not cfg.get("enabled", True):
        return write_reports({"enabled": False, "generated_at": _utc_now()})

    trades = _closed_trade_rows(conn)
    groups = _build_groups(trades, settings)
    cross_context_observations = cross_context_failure_observations(trades, settings)
    _augment_counts(
        conn,
        groups,
        opportunity_sample_limit=int(cfg.get("opportunity_sample_limit", 5_000)),
    )
    _upsert_contextual_stats(conn, groups)
    capped = _consolidate_contextual_policy_caps(conn, settings)
    created, skipped = _create_policies(conn, groups, settings)

    structural = [item for item in groups if item["status"] == "structural_failure"]
    contextual = [item for item in groups if item["status"] == "contextual_failure"]
    failing = [*structural, *contextual]
    working = [item for item in groups if item["status"] == "working_slice"]
    recovery = [item for item in groups if item["status"] == "recovery_candidate"]
    watched = [item for item in groups if item["status"] in {"low_sample_observe", "mixed"}]
    route_or_data = [
        item
        for item in contextual
        if item.get("failure_domain") in {"route_or_feasibility", "data_quality"}
    ]
    protected = _protected_working_slices(groups)
    working.sort(key=lambda item: (item["recovery_score"], item["closed_count"]), reverse=True)
    recovery.sort(key=lambda item: (item["recovery_score"], item["closed_count"]), reverse=True)

    report = {
        "enabled": True,
        "generated_at": _utc_now(),
        "summary": {
            "closed_trades_analyzed": len(trades),
            "groups_analyzed": len(groups),
            "failing_context_count": len(failing),
            "structural_failure_count": len(structural),
            "contextual_failure_count": len(contextual),
            "working_context_count": len(working),
            "recovery_candidate_count": len(recovery),
            "protected_working_slice_count": len(protected),
            "route_or_data_quality_failure_count": len(route_or_data),
            "persistent_cross_context_failure_count": sum(
                item["state"] == "persistent_failure" for item in cross_context_observations
            ),
            "rehabilitated_cross_context_count": sum(
                item["state"] == "rehabilitated" for item in cross_context_observations
            ),
            "low_sample_observe_count": len([item for item in watched if item["status"] == "low_sample_observe"]),
            "created_policy_count": len(created),
            "skipped_policy_count": len(skipped),
            "superseded_by_cap_count": len(capped),
        },
        "smart_failure_diagnostics": {
            "structural_failures": structural[:20],
            "contextual_failures": contextual[:30],
            "protected_working_slices": protected[:20],
            "recovery_candidates": recovery[:20],
            "route_or_data_quality_failures": route_or_data[:20],
            "low_sample_observe_contexts": [item for item in watched if item["status"] == "low_sample_observe"][:20],
        },
        "superseded_by_cap": capped,
        "created_policies": created,
        "skipped_policy_candidates": skipped,
        "top_failing_contexts": failing[:30],
        "top_working_contexts": working[:20],
        "recovery_candidates": recovery[:20],
        "protected_working_slices": protected[:20],
        "route_or_data_quality_failures": route_or_data[:20],
        "cross_context_observations": cross_context_observations,
        "watch_contexts": watched[:20],
        "hard_limits": [
            "Paper-only contextual policies.",
            "No live trading or broker/API actions.",
            "Cross-context failure evidence is diagnostic/ranking/sizing only; it never blocks a priceable paper experiment.",
            "Every contextual policy uses TTL and recovery probes.",
            "Route/data-quality failures are diagnosed separately from signal-quality failures.",
            "Working and recovery slices are reported so broad filters do not erase changing markets.",
        ],
    }
    return write_reports(report)


def write_reports(report: dict) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")
    return report


def _context_line(item: dict) -> str:
    return (
        f"`{item['signal_key']}` `{item['dimension']}={item['value']}` "
        f"n=`{item.get('closed_count')}` avg=`{item.get('avg_pnl_bps')}`bps "
        f"win=`{item.get('win_rate')}` recent=`{item.get('recent_avg_pnl_bps')}`bps "
        f"recent_win=`{item.get('recent_win_rate')}` worst=`{item.get('worst_bps')}` "
        f"opp=`{item.get('opportunity_count')}` opened=`{item.get('paper_entries_opened')}`"
    )


def _markdown(report: dict) -> str:
    lines = [
        "# Contextual Failure Report",
        "",
        f"- Enabled: `{report.get('enabled')}`",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Summary: `{report.get('summary', {})}`",
        "",
        "## Created Policies",
        "",
    ]
    created = report.get("created_policies", [])
    if not created:
        lines.append("No new contextual policies created this loop.")
    for item in created:
        group = item.get("group", {})
        policy = item.get("policy", {})
        lines.append(
            f"- `{item.get('policy_id')}` for {_context_line(group)} "
            f"score_delta=`{policy.get('min_score_delta')}` edge>=`{policy.get('min_net_edge_bps')}` "
            f"allocation=`{policy.get('allocation_multiplier')}` pause=`{policy.get('pause_entries')}`"
        )

    lines.extend(["", "## Top Failing Contexts", ""])
    for item in report.get("top_failing_contexts", [])[:20]:
        lines.append(
            f"- {_context_line(item)} failure_score=`{item.get('failure_score')}` "
            f"status=`{item.get('status')}` domain=`{item.get('failure_domain')}`"
        )

    lines.extend(["", "## Recovery Candidates", ""])
    recovery = report.get("recovery_candidates", [])
    if not recovery:
        lines.append("No recovery candidates yet.")
    for item in recovery[:15]:
        lines.append(f"- {_context_line(item)} recovery_score=`{item.get('recovery_score')}`")

    lines.extend(["", "## Cross-Context Paper Attribution", ""])
    observations = report.get("cross_context_observations", [])
    if not observations:
        lines.append("No targeted cross-context observations yet.")
    for item in observations:
        lines.append(
            f"- `{item.get('context')}` state=`{item.get('state')}` n=`{item.get('closed_count')}` "
            f"avg=`{item.get('avg_pnl_bps')}`bps directions=`{item.get('directions')}` "
            f"venues=`{item.get('venues')}` modes=`{item.get('sub_modes')}`; {item.get('research_note')}"
        )

    lines.extend(["", "## Protected Working Slices", ""])
    protected = report.get("protected_working_slices", [])
    if not protected:
        lines.append("No protected working slices inside structurally weak families yet.")
    for item in protected[:15]:
        lines.append(f"- {_context_line(item)} status=`{item.get('status')}` recovery_score=`{item.get('recovery_score')}`")

    lines.extend(["", "## Top Working Contexts", ""])
    for item in report.get("top_working_contexts", [])[:15]:
        lines.append(f"- {_context_line(item)} recovery_score=`{item.get('recovery_score')}`")

    lines.extend(["", "## Route Or Data Quality Failures", ""])
    route_or_data = report.get("route_or_data_quality_failures", [])
    if not route_or_data:
        lines.append("No route/data-quality diagnostic failures.")
    for item in route_or_data[:15]:
        lines.append(f"- {_context_line(item)} domain=`{item.get('failure_domain')}`")

    lines.extend(["", "## Skipped Policy Candidates", ""])
    skipped = report.get("skipped_policy_candidates", [])
    if not skipped:
        lines.append("No skipped policy candidates.")
    for item in skipped[:15]:
        lines.append(f"- {_context_line(item)} skip=`{item.get('skip_reason')}`")

    lines.extend(["", "## Watch Contexts", ""])
    for item in report.get("watch_contexts", [])[:15]:
        lines.append(f"- {_context_line(item)} status=`{item.get('status')}`")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run contextual failure diagnostics.")
    parser.parse_args(argv)
    from settings import load_settings

    with connect() as conn:
        report = run_contextual_failure_filters(conn, load_settings())
    print(f"Wrote {REPORT_JSON}")
    print(f"Wrote {REPORT_MD}")
    print(json.dumps(report.get("summary", {}), indent=2))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
