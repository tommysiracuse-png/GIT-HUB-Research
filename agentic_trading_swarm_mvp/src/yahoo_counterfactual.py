#!/usr/bin/env python3
"""Reliable-label counterfactual diagnostics for the Yahoo proxy scanner."""

from __future__ import annotations

import collections
import datetime as dt
import json
import pathlib
import sqlite3
import statistics

from proxy_signal_quality import proxy_paper_trade_diagnostic_tags


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
REPORT_JSON = RUNS_DIR / "yahoo_counterfactual_report.json"
REPORT_MD = RUNS_DIR / "yahoo_counterfactual_report.md"
PRIMARY_HORIZON_MINUTES = 60
MIN_HYPOTHESIS_SAMPLE_COUNT = 8


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _direction_sign(direction: object) -> int:
    value = str(direction or "").lower()
    if value.startswith("long") or value in {"buy_yes_event", "yes"}:
        return 1
    if value.startswith("short") or value in {"buy_no_event", "no"}:
        return -1
    return 0


def _metrics(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "avg_pnl_bps": None, "median_pnl_bps": None, "win_rate": None, "worst_decile_pnl_bps": None}
    ordered = sorted(values)
    decile_count = max(1, int(len(ordered) * 0.1))
    return {
        "count": len(values),
        "avg_pnl_bps": round(statistics.fmean(values), 3),
        "median_pnl_bps": round(statistics.median(values), 3),
        "win_rate": round(sum(value > 0 for value in values) / len(values), 3),
        "worst_decile_pnl_bps": round(statistics.fmean(ordered[:decile_count]), 3),
    }


def _candidate(row: sqlite3.Row) -> dict:
    try:
        parsed = json.loads(row["candidate_json"] or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _safe_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_timestamp(value: object) -> dt.datetime | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
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


def _lookup_nested(containers: tuple[dict, ...], *keys: str) -> object:
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if value not in (None, ""):
                return value
    return None


def _provider_age_seconds(candidate: dict) -> float | None:
    for value in (
        candidate.get("provider_age_seconds"),
        (candidate.get("data_source") or {}).get("provider_age_seconds"),
    ):
        if value not in (None, ""):
            return max(0.0, _as_float(value))
    if candidate.get("stale_minutes") not in (None, ""):
        return max(0.0, _as_float(candidate.get("stale_minutes")) * 60.0)
    return None


def _signal_age_seconds(candidate: dict, outcome_context: dict) -> float | None:
    age_seconds = _provider_age_seconds(candidate)
    if age_seconds is not None:
        return age_seconds
    direct = _lookup_nested(
        (candidate, outcome_context),
        "source_quote_age_seconds",
        "quote_age_seconds",
        "proxy_quote_age_seconds",
        "data_age_seconds",
        "freshness_age_seconds",
    )
    if direct not in (None, ""):
        return max(0.0, _as_float(direct))
    seen_at = _parse_timestamp(
        candidate.get("decision_time_utc") or candidate.get("seen_at") or candidate.get("opened_at")
    )
    source_at = _parse_timestamp(
        candidate.get("source_quote_timestamp")
        or candidate.get("source_bar_end_utc")
        or candidate.get("last_bar_utc")
        or candidate.get("last_trade_timestamp")
    )
    if seen_at is not None and source_at is not None:
        return max(0.0, (seen_at - source_at).total_seconds())
    return None


def _quote_age_bucket(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "unknown"
    if age_seconds <= 15.0 * 60.0:
        return "fresh_le_15m"
    if age_seconds <= 60.0 * 60.0:
        return "aging_15m_to_60m"
    return "stale_gt_60m"


def _holding_horizon_bucket(horizon_minutes: int) -> str:
    if horizon_minutes <= 15:
        return "scalp_le_15m"
    if horizon_minutes <= 60:
        return "intraday_16m_to_60m"
    if horizon_minutes <= 240:
        return "swing_61m_to_240m"
    return "position_gt_240m"


def _realized_cost_bucket(total_cost_bps: float | None) -> str:
    if total_cost_bps is None:
        return "unknown"
    if total_cost_bps <= 2.0:
        return "friction_le_2bps"
    if total_cost_bps <= 8.0:
        return "light_2_to_8bps"
    if total_cost_bps <= 16.0:
        return "moderate_8_to_16bps"
    return "heavy_gt_16bps"


def _cost_drag_bucket(
    net_pnl_bps: float | None,
    gross_return_bps: float | None,
    realized_total_cost_bps: float | None,
) -> str:
    if net_pnl_bps is None or gross_return_bps is None:
        return "unpriced_or_missing_cost"
    if gross_return_bps >= 0.0 and net_pnl_bps < 0.0:
        return "cost_drag_flipped_negative"
    if gross_return_bps < 0.0:
        return "negative_before_cost"
    if net_pnl_bps >= 0.0 and (realized_total_cost_bps or 0.0) <= 2.0:
        return "positive_with_minimal_cost"
    if net_pnl_bps >= 0.0:
        return "positive_after_cost"
    return "negative_after_cost_without_gross_reversal"


def _spread_bps(candidate: dict, outcome_context: dict) -> float | None:
    value = _lookup_nested(
        (candidate, outcome_context),
        "spread_bps",
        "effective_spread_bps",
        "best_bid_ask_spread_bps",
    )
    return max(0.0, _as_float(value)) if value not in (None, "") else None


def _slippage_bps(candidate: dict, outcome_context: dict, row: sqlite3.Row) -> float | None:
    direct = _lookup_nested(
        (candidate, outcome_context),
        "estimated_slippage_bps",
        "round_trip_slippage_bps_estimate",
        "slippage_bps_estimate",
    )
    if direct not in (None, ""):
        return max(0.0, _as_float(direct))
    entry_est = _lookup_nested((candidate, outcome_context), "entry_slippage_bps_estimate", "entry_slippage_bps")
    exit_est = _lookup_nested((candidate, outcome_context), "exit_slippage_bps_estimate", "exit_slippage_bps")
    values = [value for value in (_as_float(entry_est, default=None), _as_float(exit_est, default=None)) if value is not None]
    if values:
        return round(sum(max(0.0, value) for value in values), 3)
    if row["entry_slippage_bps"] not in (None, ""):
        return max(0.0, _as_float(row["entry_slippage_bps"]))
    return None


def _family_leg_label(signal_key: object, direction: object, candidate: dict, outcome_context: dict) -> str:
    direction_text = str(direction or "unknown").strip().lower() or "unknown"
    signal_stats_scope = str(
        candidate.get("signal_stats_scope")
        or outcome_context.get("signal_stats_scope")
        or ""
    ).strip().lower()
    if signal_stats_scope == "synthetic_research":
        return f"synthetic_{direction_text}"
    variant = ""
    parts = str(signal_key or "").split("|")
    if parts:
        variant = str(parts[-1]).strip().lower()
    if variant in {"standard", "conditional"}:
        return f"{direction_text}_{variant}"
    return direction_text


def _aggregate_records(rows: list[dict]) -> dict:
    net_values = [float(item["net_pnl_bps"]) for item in rows if item.get("net_pnl_bps") is not None]
    gross_values = [float(item["gross_return_bps"]) for item in rows if item.get("gross_return_bps") is not None]
    realized_costs = [float(item["realized_total_cost_bps"]) for item in rows if item.get("realized_total_cost_bps") is not None]
    charged_costs = [float(item["charged_cost_bps"]) for item in rows if item.get("charged_cost_bps") is not None]
    modeled_costs = [float(item["modeled_context_cost_bps"]) for item in rows if item.get("modeled_context_cost_bps") is not None]
    spreads = [float(item["spread_bps"]) for item in rows if item.get("spread_bps") is not None]
    slippages = [float(item["slippage_bps"]) for item in rows if item.get("slippage_bps") is not None]
    ages = [float(item["quote_age_seconds"]) for item in rows if item.get("quote_age_seconds") is not None]
    return {
        "count": len(rows),
        "avg_net_pnl_bps": round(statistics.fmean(net_values), 3) if net_values else None,
        "avg_gross_return_bps": round(statistics.fmean(gross_values), 3) if gross_values else None,
        "avg_total_realized_cost_bps": round(statistics.fmean(realized_costs), 3) if realized_costs else None,
        "avg_charged_cost_bps": round(statistics.fmean(charged_costs), 3) if charged_costs else None,
        "avg_modeled_context_cost_bps": round(statistics.fmean(modeled_costs), 3) if modeled_costs else None,
        "avg_estimated_spread_bps": round(statistics.fmean(spreads), 3) if spreads else None,
        "avg_estimated_slippage_bps": round(statistics.fmean(slippages), 3) if slippages else None,
        "avg_quote_age_seconds": round(statistics.fmean(ages), 3) if ages else None,
        "net_win_rate": round(sum(value > 0.0 for value in net_values) / len(net_values), 3) if net_values else None,
        "gross_win_rate": round(sum(value > 0.0 for value in gross_values) / len(gross_values), 3) if gross_values else None,
    }


def _aggregate_labeled(rows: list[dict], label_field: str) -> list[dict]:
    grouped: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        grouped[str(row.get(label_field) or "unknown")].append(row)
    ordered = []
    for label, items in grouped.items():
        ordered.append({label_field: label, **_aggregate_records(items)})
    ordered.sort(
        key=lambda item: (
            item.get("avg_net_pnl_bps") is None,
            item.get("avg_net_pnl_bps") if item.get("avg_net_pnl_bps") is not None else 0.0,
            str(item.get(label_field) or ""),
        )
    )
    return ordered


def _aggregate_closed_trade_buckets(rows: list[dict], label_field: str) -> list[dict]:
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    for row in rows:
        pnl = row.get("net_pnl_bps")
        if pnl is None:
            continue
        grouped[str(row.get(label_field) or "unknown")].append(float(pnl))
    ordered = []
    for label, values in grouped.items():
        ordered.append(
            {
                label_field: label,
                "count": len(values),
                "avg_pnl_bps": round(statistics.fmean(values), 3),
                "median_pnl_bps": round(statistics.median(values), 3),
                "win_rate": round(sum(value > 0.0 for value in values) / len(values), 3),
            }
        )
    ordered.sort(
        key=lambda item: (
            item.get("avg_pnl_bps") is None,
            item.get("avg_pnl_bps") if item.get("avg_pnl_bps") is not None else 0.0,
            str(item.get(label_field) or ""),
        )
    )
    return ordered


def _recommendations(horizons: dict[str, dict], inverted_60m: dict, freshness: dict[str, dict]) -> list[dict]:
    recommendations = []
    baseline = horizons.get("60") or {}
    baseline_avg = baseline.get("avg_pnl_bps")
    if (
        inverted_60m.get("count", 0) >= 12
        and inverted_60m.get("avg_pnl_bps") is not None
        and baseline_avg is not None
        and inverted_60m["avg_pnl_bps"] > 0
        and inverted_60m["avg_pnl_bps"] - baseline_avg >= 6.0
    ):
        recommendations.append(
            {
                "counterfactual": "direction_flip_60m",
                "action": "create_shadow_variant",
                "uplift_bps": round(inverted_60m["avg_pnl_bps"] - baseline_avg, 3),
                "evidence_count": inverted_60m["count"],
            }
        )
    for label, metrics in freshness.items():
        if (
            metrics.get("count", 0) >= 12
            and metrics.get("avg_pnl_bps") is not None
            and baseline_avg is not None
            and metrics["avg_pnl_bps"] > 0
            and metrics["avg_pnl_bps"] - baseline_avg >= 6.0
        ):
            recommendations.append(
                {
                    "counterfactual": f"freshness_{label}_60m",
                    "action": "create_shadow_variant",
                    "uplift_bps": round(metrics["avg_pnl_bps"] - baseline_avg, 3),
                    "evidence_count": metrics["count"],
                }
            )
    eligible_horizons = [
        (int(horizon), metrics)
        for horizon, metrics in horizons.items()
        if metrics.get("count", 0) >= 12 and metrics.get("avg_pnl_bps") is not None
    ]
    if eligible_horizons:
        best_horizon, best = max(eligible_horizons, key=lambda item: item[1]["avg_pnl_bps"])
        if (
            best_horizon != 60
            and baseline_avg is not None
            and best["avg_pnl_bps"] > 0
            and best["avg_pnl_bps"] - baseline_avg >= 6.0
        ):
            recommendations.append(
                {
                    "counterfactual": f"hold_{best_horizon}m",
                    "action": "feed_existing_hold_optimizer",
                    "uplift_bps": round(best["avg_pnl_bps"] - baseline_avg, 3),
                    "evidence_count": best["count"],
                }
            )
    return recommendations


def build_report(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select p.id, p.opened_at, p.inst_id, p.direction, p.signal_key, p.entry,
                   p.entry_fee_bps, p.entry_slippage_bps, p.candidate_json,
                   p.context_json as trade_context_json, p.status as trade_status,
                   p.pnl_bps as realized_trade_pnl_bps, p.selected_hold_minutes,
                   o.horizon_minutes, o.price, o.pnl_bps, o.delay_seconds, o.context_json
            from paper_trade_outcomes o
            join paper_trades p on p.id = o.trade_id
            where p.venue = 'YAHOO_PROXY'
              and p.trade_type = 'global_proxy_momentum'
              and (p.strategy_lab_id is null or p.strategy_lab_id = '')
              and o.measurement_status = 'valid'
              and o.pnl_bps is not null
            order by p.id, o.horizon_minutes
            """
        ).fetchall()
    except sqlite3.OperationalError:
        try:
            rows = conn.execute(
                """
                select p.id, p.opened_at, p.inst_id, p.direction, '' as signal_key, p.entry,
                       p.entry_fee_bps, p.entry_slippage_bps, p.candidate_json,
                       '{}' as trade_context_json, 'closed' as trade_status,
                       p.pnl_bps as realized_trade_pnl_bps, null as selected_hold_minutes,
                       o.horizon_minutes, o.price, o.pnl_bps, o.delay_seconds, '{}' as context_json
                from paper_trade_outcomes o
                join paper_trades p on p.id = o.trade_id
                where p.venue = 'YAHOO_PROXY'
                  and p.trade_type = 'global_proxy_momentum'
                  and (p.strategy_lab_id is null or p.strategy_lab_id = '')
                  and o.measurement_status = 'valid'
                  and o.pnl_bps is not null
                order by p.id, o.horizon_minutes
                """
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                """
                select p.id, p.opened_at, p.inst_id, p.direction, '' as signal_key, p.entry,
                       p.entry_fee_bps, p.entry_slippage_bps, p.candidate_json,
                       '{}' as trade_context_json, 'closed' as trade_status,
                       null as realized_trade_pnl_bps, null as selected_hold_minutes,
                       o.horizon_minutes, o.price, o.pnl_bps, o.delay_seconds, '{}' as context_json
                from paper_trade_outcomes o
                join paper_trades p on p.id = o.trade_id
                where p.venue = 'YAHOO_PROXY'
                  and p.trade_type = 'global_proxy_momentum'
                  and (p.strategy_lab_id is null or p.strategy_lab_id = '')
                  and o.measurement_status = 'valid'
                  and o.pnl_bps is not null
                order by p.id, o.horizon_minutes
                """
            ).fetchall()
    by_horizon: dict[int, list[float]] = {}
    inverted_60m = []
    freshness_60m: dict[int, list[float]] = {30: [], 60: [], 90: []}
    timing_coverage = {"provider_age_present": 0, "source_bar_end_present": 0, "decision_time_present": 0}
    seen_trades = set()
    attribution_rows: list[dict] = []
    closed_trade_attribution_rows: list[dict] = []
    horizons_seen: collections.Counter[int] = collections.Counter()
    for row in rows:
        pnl = _as_float(row["pnl_bps"])
        horizon = int(row["horizon_minutes"])
        by_horizon.setdefault(horizon, []).append(pnl)
        horizons_seen[horizon] += 1
        candidate = _candidate(row)
        candidate_for_tags = {
            "venue": "YAHOO_PROXY",
            "trade_type": "global_proxy_momentum",
            **candidate,
        }
        trade_context = _safe_dict(row["trade_context_json"])
        outcome_context = _safe_dict(row["context_json"])
        selected_hold_minutes = None
        if row["selected_hold_minutes"] not in (None, ""):
            try:
                selected_hold_minutes = int(row["selected_hold_minutes"])
            except (TypeError, ValueError):
                selected_hold_minutes = None
        if row["id"] not in seen_trades:
            seen_trades.add(row["id"])
            provider_age = _provider_age_seconds(candidate)
            timing_coverage["provider_age_present"] += int(provider_age is not None)
            timing_coverage["source_bar_end_present"] += int(bool(candidate.get("source_bar_end_utc") or candidate.get("last_bar_utc")))
            timing_coverage["decision_time_present"] += int(bool(candidate.get("decision_time_utc") or candidate.get("seen_at")))
            if str(row["trade_status"] or "").lower() == "closed" and row["realized_trade_pnl_bps"] is not None:
                persisted_trade_tags = trade_context.get("paper_trade_diagnostic_tags")
                persisted_trade_tags = persisted_trade_tags if isinstance(persisted_trade_tags, dict) else {}
                computed_trade_tags = proxy_paper_trade_diagnostic_tags(
                    candidate_for_tags,
                    context=trade_context,
                    selected_hold_minutes=selected_hold_minutes,
                )
                trade_tags = {**computed_trade_tags, **persisted_trade_tags}
                closed_trade_attribution_rows.append(
                    {
                        "trade_id": int(row["id"]),
                        "net_pnl_bps": float(row["realized_trade_pnl_bps"]),
                        "selected_holding_horizon_bucket": trade_tags.get("selected_holding_horizon_bucket") or "unknown",
                        "quote_staleness_bucket": trade_tags.get("quote_staleness_bucket") or "unknown",
                        "session_bucket": trade_tags.get("session_bucket") or "unknown",
                        "time_of_day_bucket": trade_tags.get("time_of_day_bucket") or "unknown",
                        "session_time_bucket": trade_tags.get("session_time_bucket") or "unknown",
                        "spread_regime_bucket": trade_tags.get("spread_regime_bucket") or "unknown",
                        "routing_path_bucket": trade_tags.get("routing_path_bucket") or "unknown",
                    }
                )
        sign = _direction_sign(row["direction"])
        entry = _as_float(row["entry"])
        price = _as_float(row["price"])
        gross = None
        if sign and entry > 0 and price > 0:
            gross = (price / entry - 1.0) * 10_000.0 * sign
        cost_audit = _safe_dict(outcome_context.get("paper_realized_cost_audit"))
        charged_cost = None
        modeled_backfill = None
        if cost_audit:
            charged_cost = _as_float(cost_audit.get("charged_cost_bps"), default=None)
            modeled_backfill = _as_float(
                cost_audit.get("realized_cost_backfill_bps", cost_audit.get("modeled_context_cost_bps")),
                default=None,
            )
        realized_total_cost = None
        if charged_cost is not None or modeled_backfill is not None:
            realized_total_cost = max(0.0, (charged_cost or 0.0) + (modeled_backfill or 0.0))
        if gross is None and realized_total_cost is not None:
            gross = pnl + realized_total_cost
        if realized_total_cost is None and gross is not None:
            realized_total_cost = gross - pnl
        persisted_outcome_tags = outcome_context.get("paper_trade_diagnostic_tags")
        persisted_outcome_tags = persisted_outcome_tags if isinstance(persisted_outcome_tags, dict) else {}
        computed_outcome_tags = proxy_paper_trade_diagnostic_tags(
            candidate_for_tags,
            context=outcome_context,
            selected_hold_minutes=selected_hold_minutes,
            outcome_horizon_minutes=horizon,
        )
        outcome_tags = {**computed_outcome_tags, **persisted_outcome_tags}
        quote_age_seconds = outcome_tags.get("quote_age_seconds")
        attribution_rows.append(
            {
                "trade_id": int(row["id"]),
                "inst_id": str(row["inst_id"] or ""),
                "direction": str(row["direction"] or "unknown"),
                "family_leg": _family_leg_label(row["signal_key"], row["direction"], candidate, outcome_context),
                "horizon_minutes": horizon,
                "holding_horizon_bucket": (
                    outcome_tags.get("outcome_holding_horizon_bucket")
                    or _holding_horizon_bucket(horizon)
                ),
                "net_pnl_bps": pnl,
                "gross_return_bps": round(gross, 3) if gross is not None else None,
                "realized_total_cost_bps": round(realized_total_cost, 3) if realized_total_cost is not None else None,
                "realized_total_cost_bucket": _realized_cost_bucket(realized_total_cost),
                "charged_cost_bps": round(charged_cost, 3) if charged_cost is not None else None,
                "modeled_context_cost_bps": round(modeled_backfill, 3) if modeled_backfill is not None else None,
                "spread_bps": _spread_bps(candidate, outcome_context),
                "slippage_bps": _slippage_bps(candidate, outcome_context, row),
                "quote_age_seconds": round(float(quote_age_seconds), 3) if quote_age_seconds not in (None, "") else None,
                "quote_age_bucket": (
                    str(outcome_tags.get("quote_staleness_bucket") or "")
                    or _quote_age_bucket(quote_age_seconds if isinstance(quote_age_seconds, (int, float)) else None)
                ),
                "cost_drag_bucket": _cost_drag_bucket(pnl, gross, realized_total_cost),
            }
        )
        if horizon != 60:
            continue
        provider_age = _provider_age_seconds(candidate)
        if provider_age is not None:
            for threshold in freshness_60m:
                if provider_age <= threshold * 60.0:
                    freshness_60m[threshold].append(pnl)
        if sign and entry > 0 and price > 0:
            gross = (price / entry - 1.0) * 10_000.0 * sign
            observed_cost = max(0.0, gross - pnl)
            inverted_60m.append(-gross - observed_cost)

    horizon_metrics = {str(horizon): _metrics(values) for horizon, values in sorted(by_horizon.items())}
    inverted_metrics = _metrics(inverted_60m)
    freshness_metrics = {f"le_{minutes}m": _metrics(values) for minutes, values in freshness_60m.items()}
    recommendations = _recommendations(horizon_metrics, inverted_metrics, freshness_metrics)
    primary_horizon = PRIMARY_HORIZON_MINUTES if horizons_seen.get(PRIMARY_HORIZON_MINUTES) else (min(horizons_seen) if horizons_seen else None)
    primary_rows = [
        item for item in attribution_rows if primary_horizon is not None and item["horizon_minutes"] == primary_horizon
    ]
    forward_return_horizons = {
        str(horizon): _aggregate_records([item for item in attribution_rows if item["horizon_minutes"] == horizon])
        for horizon in sorted(horizons_seen)
    }
    family_leg_outcomes = _aggregate_labeled(primary_rows, "family_leg")
    direction_outcomes = _aggregate_labeled(primary_rows, "direction")
    holding_horizon_bucket_outcomes = _aggregate_labeled(attribution_rows, "holding_horizon_bucket")
    quote_age_outcomes = _aggregate_labeled(primary_rows, "quote_age_bucket")
    realized_cost_bucket_outcomes = _aggregate_labeled(primary_rows, "realized_total_cost_bucket")
    cost_drag_bucket_outcomes = _aggregate_labeled(primary_rows, "cost_drag_bucket")
    closed_trade_bucket_attribution = {
        "closed_trade_count": len(closed_trade_attribution_rows),
        "selected_holding_horizon_outcomes": _aggregate_closed_trade_buckets(
            closed_trade_attribution_rows,
            "selected_holding_horizon_bucket",
        ),
        "quote_staleness_outcomes": _aggregate_closed_trade_buckets(
            closed_trade_attribution_rows,
            "quote_staleness_bucket",
        ),
        "session_outcomes": _aggregate_closed_trade_buckets(
            closed_trade_attribution_rows,
            "session_bucket",
        ),
        "time_of_day_outcomes": _aggregate_closed_trade_buckets(
            closed_trade_attribution_rows,
            "time_of_day_bucket",
        ),
        "session_time_outcomes": _aggregate_closed_trade_buckets(
            closed_trade_attribution_rows,
            "session_time_bucket",
        ),
        "spread_regime_outcomes": _aggregate_closed_trade_buckets(
            closed_trade_attribution_rows,
            "spread_regime_bucket",
        ),
        "routing_path_outcomes": _aggregate_closed_trade_buckets(
            closed_trade_attribution_rows,
            "routing_path_bucket",
        ),
    }
    primary_cost_summary = _aggregate_records(primary_rows)
    family_leg_primary_map = {row["family_leg"]: row for row in family_leg_outcomes}
    mature_family_legs = {
        label: row for label, row in family_leg_primary_map.items() if row.get("count", 0) >= MIN_HYPOTHESIS_SAMPLE_COUNT
    }
    cost_drag_legs = sorted(
        label
        for label, row in mature_family_legs.items()
        if row.get("avg_net_pnl_bps") is not None
        and row.get("avg_net_pnl_bps") < 0.0
        and row.get("avg_gross_return_bps") is not None
        and row.get("avg_gross_return_bps") >= 0.0
    )
    cost_drag_status = "insufficient_evidence"
    if primary_cost_summary.get("count", 0) >= MIN_HYPOTHESIS_SAMPLE_COUNT:
        cost_drag_status = "confirmed" if cost_drag_legs else "rejected"
    fresh_primary = next((row for row in quote_age_outcomes if row["quote_age_bucket"] == "fresh_le_15m"), {})
    stale_primary = next((row for row in quote_age_outcomes if row["quote_age_bucket"] == "stale_gt_60m"), {})
    stale_proxy_status = "insufficient_evidence"
    stale_negative_share = None
    negative_primary = [item for item in primary_rows if item.get("net_pnl_bps") is not None and item["net_pnl_bps"] < 0.0]
    if negative_primary:
        stale_negative_share = round(
            sum(1 for item in negative_primary if item.get("quote_age_bucket") == "stale_gt_60m") / len(negative_primary),
            3,
        )
    if fresh_primary.get("count", 0) >= 4 and stale_primary.get("count", 0) >= 4:
        fresh_avg = fresh_primary.get("avg_net_pnl_bps")
        stale_avg = stale_primary.get("avg_net_pnl_bps")
        stale_proxy_status = (
            "confirmed"
            if fresh_avg is not None
            and stale_avg is not None
            and stale_avg < 0.0
            and stale_avg + 6.0 <= fresh_avg
            else "rejected"
        )
    adjacent_horizons = []
    if primary_horizon is not None:
        adjacent_horizons = [
            horizon
            for horizon in sorted(horizons_seen, key=lambda value: (abs(value - primary_horizon), value))
            if horizon != primary_horizon
        ][:2]
    adjacent_metrics = {
        str(horizon): forward_return_horizons[str(horizon)]
        for horizon in adjacent_horizons
        if str(horizon) in forward_return_horizons
    }
    baseline_primary_avg = primary_cost_summary.get("avg_net_pnl_bps")
    best_adjacent = None
    for horizon in adjacent_horizons:
        metrics = forward_return_horizons.get(str(horizon)) or {}
        if metrics.get("count", 0) < 4 or metrics.get("avg_net_pnl_bps") is None:
            continue
        if best_adjacent is None or metrics["avg_net_pnl_bps"] > best_adjacent["avg_net_pnl_bps"]:
            best_adjacent = {"horizon_minutes": horizon, **metrics}
    sign_mismatch = bool(
        inverted_metrics.get("count", 0) >= 12
        and baseline_primary_avg is not None
        and inverted_metrics.get("avg_pnl_bps") is not None
        and baseline_primary_avg < 0.0
        and inverted_metrics["avg_pnl_bps"] > 0.0
        and inverted_metrics["avg_pnl_bps"] - baseline_primary_avg >= 6.0
    )
    horizon_mismatch = bool(
        best_adjacent
        and baseline_primary_avg is not None
        and baseline_primary_avg < 0.0
        and best_adjacent["avg_net_pnl_bps"] is not None
        and best_adjacent["avg_net_pnl_bps"] > 0.0
        and best_adjacent["avg_net_pnl_bps"] - baseline_primary_avg >= 6.0
    )
    mismatch_status = "insufficient_evidence"
    if primary_cost_summary.get("count", 0) >= MIN_HYPOTHESIS_SAMPLE_COUNT:
        mismatch_status = "confirmed" if (sign_mismatch or horizon_mismatch) else "rejected"
    hypothesis_tests = [
        {
            "hypothesis": "cost_drag",
            "status": cost_drag_status,
            "summary": "Net losses disappear in gross terms, implying cost drag.",
            "primary_horizon_minutes": primary_horizon,
            "avg_net_pnl_bps": primary_cost_summary.get("avg_net_pnl_bps"),
            "avg_gross_return_bps": primary_cost_summary.get("avg_gross_return_bps"),
            "avg_total_realized_cost_bps": primary_cost_summary.get("avg_total_realized_cost_bps"),
            "affected_family_legs": cost_drag_legs,
        },
        {
            "hypothesis": "stale_proxy_data",
            "status": stale_proxy_status,
            "summary": "Losses concentrate when quote age is high.",
            "primary_horizon_minutes": primary_horizon,
            "fresh_bucket": fresh_primary,
            "stale_bucket": stale_primary,
            "stale_negative_share": stale_negative_share,
        },
        {
            "hypothesis": "horizon_or_sign_mismatch",
            "status": mismatch_status,
            "summary": "Adverse outcomes are specific to the current horizon or sign, not nearby alternatives.",
            "primary_horizon_minutes": primary_horizon,
            "baseline_primary": primary_cost_summary,
            "direction_flip_60m": inverted_metrics,
            "adjacent_horizons": adjacent_metrics,
            "sign_mismatch": sign_mismatch,
            "horizon_mismatch": horizon_mismatch,
        },
    ]
    leading_hypothesis = next(
        (item["hypothesis"] for item in hypothesis_tests if item["status"] == "confirmed"),
        "broad_negative_edge_without_single_diagnostic_driver",
    )
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "paper_only": True,
        "signal_key": "YAHOO_PROXY|global_proxy_momentum",
        "reliable_label_count": len(rows),
        "unique_trade_count": len(seen_trades),
        "horizon_metrics": horizon_metrics,
        "counterfactuals": {
            "direction_flip_60m": inverted_metrics,
            "freshness_gates_60m": freshness_metrics,
            "next_session_entry": {
                "status": "forward_observation_required",
                "reason": "Historical labels do not contain a next-session executable entry price.",
            },
        },
        "timing_metadata_coverage": timing_coverage,
        "diagnostic_attribution": {
            "primary_horizon_minutes": primary_horizon,
            "forward_return_horizons": forward_return_horizons,
            "cost_summary": primary_cost_summary,
            "family_leg_outcomes": family_leg_outcomes,
            "direction_outcomes": direction_outcomes,
            "holding_horizon_bucket_outcomes": holding_horizon_bucket_outcomes,
            "quote_age_outcomes": quote_age_outcomes,
            "realized_cost_bucket_outcomes": realized_cost_bucket_outcomes,
            "cost_drag_bucket_outcomes": cost_drag_bucket_outcomes,
            "closed_trade_bucket_attribution": closed_trade_bucket_attribution,
            "hypothesis_tests": hypothesis_tests,
            "leading_hypothesis": leading_hypothesis,
        },
        "shadow_recommendations": recommendations,
        "decision": "shadow_candidate_available" if recommendations else "diagnose_only_no_positive_counterfactual",
        "hard_limits": [
            "Reliable valid labels only.",
            "Counterfactuals never alter active paper policy directly.",
            "Next-session entry claims require forward executable observations.",
        ],
    }
    return report


def _markdown(report: dict) -> str:
    lines = [
        "# Yahoo Proxy Counterfactual Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Reliable labels: `{report.get('reliable_label_count', 0)}` across `{report.get('unique_trade_count', 0)}` trades",
        f"- Decision: `{report.get('decision')}`",
        "",
        "## Reliable Horizon Performance",
        "",
    ]
    for horizon, metrics in report.get("horizon_metrics", {}).items():
        lines.append(f"- `{horizon}m`: `{metrics}`")
    lines.extend(["", "## Counterfactuals", ""])
    lines.append(f"- Direction flip at 60m: `{report.get('counterfactuals', {}).get('direction_flip_60m', {})}`")
    for label, metrics in report.get("counterfactuals", {}).get("freshness_gates_60m", {}).items():
        lines.append(f"- Freshness `{label}`: `{metrics}`")
    attribution = report.get("diagnostic_attribution") or {}
    lines.extend(["", "## Diagnostic Attribution", ""])
    lines.append(f"- Primary horizon: `{attribution.get('primary_horizon_minutes')}`")
    lines.append(f"- Leading hypothesis: `{attribution.get('leading_hypothesis')}`")
    lines.append(f"- Primary cost summary: `{attribution.get('cost_summary', {})}`")
    lines.append(f"- Family leg outcomes: `{attribution.get('family_leg_outcomes', [])}`")
    lines.append(f"- Direction outcomes: `{attribution.get('direction_outcomes', [])}`")
    lines.append(f"- Holding horizon buckets: `{attribution.get('holding_horizon_bucket_outcomes', [])}`")
    lines.append(f"- Quote age buckets: `{attribution.get('quote_age_outcomes', [])}`")
    lines.append(f"- Realized cost buckets: `{attribution.get('realized_cost_bucket_outcomes', [])}`")
    lines.append(f"- Cost drag buckets: `{attribution.get('cost_drag_bucket_outcomes', [])}`")
    lines.append(f"- Closed-trade bucket attribution: `{attribution.get('closed_trade_bucket_attribution', {})}`")
    for test in attribution.get("hypothesis_tests", []):
        lines.append(
            f"- Hypothesis `{test.get('hypothesis')}` status=`{test.get('status')}` details=`{test}`"
        )
    lines.extend(["", "## Shadow Recommendations", ""])
    recommendations = report.get("shadow_recommendations", [])
    if not recommendations:
        lines.append("No counterfactual currently clears the evidence gate.")
    for item in recommendations:
        lines.append(f"- `{item.get('counterfactual')}`: `{item}`")
    return "\n".join(lines) + "\n"


def run_yahoo_counterfactual_analysis(conn: sqlite3.Connection, settings: dict | None = None) -> dict:
    del settings
    report = build_report(conn)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")
    report["report_json"] = str(REPORT_JSON)
    report["report_markdown"] = str(REPORT_MD)
    return report
