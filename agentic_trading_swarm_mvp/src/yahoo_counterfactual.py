#!/usr/bin/env python3
"""Reliable-label counterfactual diagnostics for the Yahoo proxy scanner."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import statistics


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
REPORT_JSON = RUNS_DIR / "yahoo_counterfactual_report.json"
REPORT_MD = RUNS_DIR / "yahoo_counterfactual_report.md"


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
    rows = conn.execute(
        """
        select p.id, p.opened_at, p.inst_id, p.direction, p.entry,
               p.entry_fee_bps, p.entry_slippage_bps, p.candidate_json,
               o.horizon_minutes, o.price, o.pnl_bps, o.delay_seconds
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
    for row in rows:
        pnl = _as_float(row["pnl_bps"])
        horizon = int(row["horizon_minutes"])
        by_horizon.setdefault(horizon, []).append(pnl)
        candidate = _candidate(row)
        if row["id"] not in seen_trades:
            seen_trades.add(row["id"])
            provider_age = _provider_age_seconds(candidate)
            timing_coverage["provider_age_present"] += int(provider_age is not None)
            timing_coverage["source_bar_end_present"] += int(bool(candidate.get("source_bar_end_utc") or candidate.get("last_bar_utc")))
            timing_coverage["decision_time_present"] += int(bool(candidate.get("decision_time_utc") or candidate.get("seen_at")))
        if horizon != 60:
            continue
        provider_age = _provider_age_seconds(candidate)
        if provider_age is not None:
            for threshold in freshness_60m:
                if provider_age <= threshold * 60.0:
                    freshness_60m[threshold].append(pnl)
        sign = _direction_sign(row["direction"])
        entry = _as_float(row["entry"])
        price = _as_float(row["price"])
        if sign and entry > 0 and price > 0:
            gross = (price / entry - 1.0) * 10_000.0 * sign
            observed_cost = max(0.0, gross - pnl)
            inverted_60m.append(-gross - observed_cost)

    horizon_metrics = {str(horizon): _metrics(values) for horizon, values in sorted(by_horizon.items())}
    inverted_metrics = _metrics(inverted_60m)
    freshness_metrics = {f"le_{minutes}m": _metrics(values) for minutes, values in freshness_60m.items()}
    recommendations = _recommendations(horizon_metrics, inverted_metrics, freshness_metrics)
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
