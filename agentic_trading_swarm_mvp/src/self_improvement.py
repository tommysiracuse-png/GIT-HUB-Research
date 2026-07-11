"""Autonomous paper-only improvement executor.

This consumes the build/research tasks already produced by the LLM swarm and
turns safe classes of recommendations into bounded, reversible experiments.
It never enables live trading, touches credentials, installs packages, or
changes startup behavior.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3

from storage import (
    RUNS_DIR,
    active_signal_policies,
    add_adapter_spec,
    add_growth_experiment,
    add_memory_fact,
    add_route_probe_task,
    add_self_improvement_experiment,
    add_signal_policy,
    expire_signal_policies,
    llm_recommendations_for_auto_execution,
    open_adapter_specs,
    open_route_probe_tasks,
    open_self_improvement_experiments,
    record_policy_application,
    record_policy_open,
    update_experiment_evaluation,
    update_llm_recommendation_status,
)
from signal_redesign import create_proposed_variant
from code_evolution import (
    code_evolution_summary,
    evaluate_code_evolution,
    process_code_change_recommendation,
    write_code_evolution_reports,
)
from self_improvement_open_pack import IMPLEMENTED_STATUS as OPEN_PACK_IMPLEMENTED_STATUS
from self_improvement_open_pack import is_duplicate_open_pack_text


ACTIVE_POLICIES_JSON = RUNS_DIR / "active_signal_policies.json"
REPORT_JSON = RUNS_DIR / "self_improvement_report.json"
REPORT_MD = RUNS_DIR / "self_improvement_report.md"
TIMELINE_JSONL = RUNS_DIR / "self_improvement_timeline.jsonl"

IMPLEMENTED_MANUAL_STATUSES = {
    "route_requirements": ("implemented_route_requirements", ("improvement_tasks", "route_probe_tasks")),
    "frontier_crypto_adapter": ("implemented_frontier_crypto_adapter", ("improvement_tasks", "adapter_specs")),
    "failure_diagnostics": ("implemented_failure_diagnostics", ("improvement_tasks", "adapter_specs")),
    "signal_redesign": ("implemented_signal_redesign", ("improvement_tasks", "adapter_specs")),
    "frontier_data_quality": (
        "implemented_frontier_data_quality",
        ("improvement_tasks", "adapter_specs"),
    ),
    "okx_basis_signal_research": (
        "implemented_okx_basis_signal_research",
        ("adapter_specs",),
    ),
    "regional_frontier_data": (
        "implemented_regional_frontier_data",
        ("improvement_tasks", "adapter_specs"),
    ),
    "frontier_systemic_redesign": (
        "implemented_frontier_systemic_redesign",
        ("improvement_tasks", "growth_experiments"),
    ),
    "okx_reliable_outcomes": (
        "implemented_okx_reliable_outcomes",
        ("improvement_tasks",),
    ),
    "strategy_reliability_pack": (
        "implemented_strategy_reliability_pack",
        ("improvement_tasks", "growth_experiments"),
    ),
    "self_improvement_open_pack": (
        OPEN_PACK_IMPLEMENTED_STATUS,
        ("improvement_tasks", "growth_experiments"),
    ),
    "regional_fx_frontier_prediction_pack": (
        "implemented_regional_fx_frontier_prediction_pack",
        ("route_probe_tasks", "improvement_tasks", "adapter_specs"),
    ),
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def classify_recommendation(payload: dict) -> str:
    action = str(payload.get("action") or "")
    if action == "propose_code_change":
        return "code_change"
    if action == "propose_signal_variant":
        return "signal_variant"
    if action == "propose_diagnostic_hypothesis":
        return "diagnostic_hypothesis"
    text = " ".join(
        str(payload.get(key, ""))
        for key in ("title", "rationale", "proposed_change", "action", "market_key", "signal_key")
    ).lower()
    if any(
        term in text
        for term in (
            "failure filter",
            "stricter",
            "entry filter",
            "demote",
            "block",
            "losing",
            "poor performing",
            "underperforming",
            "negative performance",
            "low win rate",
            "signal filtering",
        )
    ):
        return "failure_filter"
    if any(term in text for term in ("route", "borrow", "margin", "broker", "permission", "fee", "api support")):
        return "route_resolver"
    if any(term in text for term in ("adapter", "venue", "frontier", "underserved", "data source", "watchlist")):
        return "market_adapter"
    return "research_note"


def _implemented_manual_category_exists(conn: sqlite3.Connection, category: str) -> bool:
    if category not in IMPLEMENTED_MANUAL_STATUSES:
        return False
    status, tables = IMPLEMENTED_MANUAL_STATUSES[category]
    for table in tables:
        row = conn.execute(f"select 1 from {table} where status = ? limit 1", (status,)).fetchone()
        if row:
            return True
    return False


def _text_for_payload(payload: dict) -> str:
    return " ".join(
        str(payload.get(key, ""))
        for key in ("title", "rationale", "proposed_change", "action", "market_key", "signal_key")
    ).lower()


def _duplicate_route_requirements_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return (
        "route requirement" in text
        or "execution route requirements" in text
        or ("conditional opportunit" in text and "requirements" in text)
    )


def _duplicate_frontier_adapter_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "frontier crypto" in text and any(
        term in text
        for term in (
            "data adapter",
            "market coverage",
            "venue adapter",
            "undercovered",
            "poor performance",
        )
    )


def _duplicate_frontier_data_quality_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "frontier crypto" in text and any(
        term in text
        for term in (
            "enhanced data coverage",
            "data quality",
            "order book",
            "orderbook",
            "market depth",
            "freshness",
            "slippage",
            "liquidity quality",
        )
    )


def _duplicate_signal_redesign_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return any(
        term in text
        for term in (
            "signal redesign",
            "root cause analysis",
            "root-cause analysis",
            "investigate and improve poorly performing signals",
            "improve frontier crypto spot signals across venues",
        )
    )


def _duplicate_frontier_systemic_redesign_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "frontier" in text and any(
        term in text
        for term in (
            "systemic",
            "negative performance",
            "poor signal performance",
            "venue-map",
            "venue map",
            "underserved frontier",
        )
    )


def _duplicate_okx_reliable_outcomes_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "okx" in text and any(
        term in text
        for term in (
            "reliable outcome",
            "reliable label",
            "legacy_unverified",
            "variant learning",
            "valid labels",
        )
    )


def _duplicate_okx_basis_signal_research_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "okx" in text and "perp" in text and "funding" in text and any(
        term in text
        for term in (
            "basis signal",
            "basis signals",
            "funding basis signal",
            "funding basis signals",
            "investigate and improve okx",
        )
    )


def _duplicate_regional_frontier_data_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "frontier crypto" in text and any(
        term in text
        for term in (
            "africa",
            "southeast asia",
            "emerging frontier",
            "regional frontier",
            "regional venue",
        )
    )


def _duplicate_regional_fx_frontier_prediction_pack_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    regional_fx = any(
        term in text
        for term in (
            "regional fx",
            "fx reference",
            "public fx midpoint",
            "fiat-stablecoin reference",
            "quote normalization",
            "africa rail",
            "african stablecoin",
        )
    )
    adaptive_depth = "frontier" in text and any(
        term in text
        for term in (
            "adaptive depth",
            "depth enrichment",
            "known quality",
            "quality coverage",
        )
    )
    prediction_intelligence = "prediction" in text and any(
        term in text
        for term in (
            "event classification",
            "event intelligence",
            "expired",
            "resolution",
            "order-book",
            "orderbook",
        )
    )
    return regional_fx or adaptive_depth or prediction_intelligence


def _duplicate_strategy_reliability_pack_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return any(
        term in text
        for term in (
            "strategy reliability",
            "venue-direction reliability",
            "venue direction reliability",
            "frontier venue signal repair",
            "frontier long weak",
            "frontier short weak",
            "long-frontier",
            "short-frontier",
            "yahoo proxy short",
            "proxy short",
            "funding/basis split",
            "funding basis split",
            "positive slice expansion",
            "market-specific factors",
            "microstructure and liquidity",
            "microstructure divergence",
            "weak win-rate",
            "weak win rate",
            "expand okx funding",
            "expand gate frontier short",
            "expand mexc frontier short",
            "expand binance_us frontier short",
        )
    )


def _duplicate_self_improvement_open_pack_payload(payload: dict) -> bool:
    return is_duplicate_open_pack_text(_text_for_payload(payload))


def _stats_by_signal(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        select signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at
        from signal_stats
        """
    ).fetchall()
    return {row["signal_key"]: dict(row) for row in rows}


def _closed_metrics_since(conn: sqlite3.Connection, signal_key: str, since: str | None = None) -> dict:
    params: list[object] = [signal_key]
    clause = "signal_key = ? and status = 'closed' and pnl_bps is not null"
    if since:
        clause += " and closed_at >= ?"
        params.append(since)
    rows = conn.execute(
        f"""
        select pnl_bps
        from paper_trades
        where {clause}
        """,
        params,
    ).fetchall()
    pnls = [float(row["pnl_bps"]) for row in rows]
    if not pnls:
        return {"closed_count": 0, "avg_pnl_bps": None, "win_rate": None, "best_bps": None, "worst_bps": None}
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "closed_count": len(pnls),
        "avg_pnl_bps": round(sum(pnls) / len(pnls), 3),
        "win_rate": round(wins / len(pnls), 3),
        "best_bps": round(max(pnls), 3),
        "worst_bps": round(min(pnls), 3),
    }


def _overall_metrics(conn: sqlite3.Connection, since: str | None = None) -> dict:
    params: list[object] = []
    clause = "status = 'closed' and pnl_bps is not null"
    if since:
        clause += " and closed_at >= ?"
        params.append(since)
    rows = conn.execute(
        f"""
        select pnl_bps
        from paper_trades
        where {clause}
        """,
        params,
    ).fetchall()
    pnls = [float(row["pnl_bps"]) for row in rows]
    if not pnls:
        return {"closed_count": 0, "avg_pnl_bps": None, "win_rate": None, "best_bps": None, "worst_bps": None}
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "closed_count": len(pnls),
        "avg_pnl_bps": round(sum(pnls) / len(pnls), 3),
        "win_rate": round(wins / len(pnls), 3),
        "best_bps": round(max(pnls), 3),
        "worst_bps": round(min(pnls), 3),
    }


def _first_activation(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        select min(activated_at) as first_activation
        from self_improvement_experiments
        where activated_at is not null
        """
    ).fetchone()
    return row["first_activation"] if row else None


def _policy_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        select status, count(*) as count
        from signal_policies
        group by status
        order by status
        """
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


def _experiment_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        select status, count(*) as count
        from self_improvement_experiments
        group by status
        order by status
        """
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


def _policy_impact_summary(conn: sqlite3.Connection, settings: dict | None = None) -> dict:
    risk = (settings or {}).get("risk", {})
    default_notional = float(risk.get("paper_notional_usd", 1000.0))
    rows = conn.execute(
        """
        select status, allocation_multiplier, applied_count, filtered_count, opened_count
        from signal_policies
        """
    ).fetchall()
    applied = sum(int(row["applied_count"] or 0) for row in rows)
    filtered = sum(int(row["filtered_count"] or 0) for row in rows)
    opened = sum(int(row["opened_count"] or 0) for row in rows)
    blocked_notional = filtered * default_notional
    reduced_notional = 0.0
    for row in rows:
        multiplier = float(row["allocation_multiplier"] if row["allocation_multiplier"] is not None else 1.0)
        reduced_notional += int(row["opened_count"] or 0) * default_notional * max(0.0, 1.0 - multiplier)
    return {
        "policies_total": len(rows),
        "active_policy_count": len(active_signal_policies(conn)),
        "applied_checks": applied,
        "paper_entries_filtered": filtered,
        "paper_entries_opened_under_policy": opened,
        "default_paper_notional_usd": round(default_notional, 2),
        "estimated_notional_blocked_usd": round(blocked_notional, 2),
        "estimated_notional_reduced_usd": round(reduced_notional, 2),
        "estimated_total_risk_reduction_usd": round(blocked_notional + reduced_notional, 2),
    }


def _augment_experiment_progress(conn: sqlite3.Connection, item: dict) -> dict:
    output = dict(item)
    if output.get("task_type") != "failure_filter" or not output.get("signal_key"):
        return output
    activated_at = output.get("activated_at")
    baseline = output.get("baseline", {})
    post_activation = _closed_metrics_since(conn, output["signal_key"], activated_at)
    output["post_activation"] = post_activation
    if baseline.get("avg_pnl_bps") is not None and post_activation.get("avg_pnl_bps") is not None:
        output["delta_avg_pnl_bps"] = round(float(post_activation["avg_pnl_bps"]) - float(baseline["avg_pnl_bps"]), 3)
    return output


def _progress_summary(conn: sqlite3.Connection, settings: dict | None = None) -> dict:
    first_activation = _first_activation(conn)
    all_time = _overall_metrics(conn)
    since_activation = _overall_metrics(conn, first_activation) if first_activation else None
    delta = None
    if since_activation and all_time.get("avg_pnl_bps") is not None and since_activation.get("avg_pnl_bps") is not None:
        delta = round(float(since_activation["avg_pnl_bps"]) - float(all_time["avg_pnl_bps"]), 3)
    return {
        "first_activation": first_activation,
        "all_time": all_time,
        "since_first_activation": since_activation,
        "since_vs_all_time_avg_pnl_delta_bps": delta,
        "policy_status_counts": _policy_status_counts(conn),
        "experiment_status_counts": _experiment_status_counts(conn),
        "policy_impact": _policy_impact_summary(conn, settings),
        "timeline_path": str(TIMELINE_JSONL),
    }


def _append_timeline_snapshot(report: dict) -> None:
    summary = report.get("progress_summary", {})
    snapshot = {
        "generated_at": report.get("generated_at"),
        "active_policies": len(report.get("active_policies", [])),
        "consumed": len(report.get("consumed", [])),
        "evaluated": len(report.get("evaluated", [])),
        "expired": len(report.get("expired", [])),
        "policy_impact": summary.get("policy_impact", {}),
        "all_time": summary.get("all_time", {}),
        "since_first_activation": summary.get("since_first_activation"),
        "experiment_status_counts": summary.get("experiment_status_counts", {}),
    }
    with TIMELINE_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, sort_keys=True) + "\n")


def _target_signals(conn: sqlite3.Connection, payload: dict) -> list[str]:
    stats = _stats_by_signal(conn)
    targets: list[str] = []
    if payload.get("signal_key"):
        targets.append(str(payload["signal_key"]))
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    for item in evidence.get("signals", []):
        if isinstance(item, dict) and item.get("signal_key"):
            targets.append(str(item["signal_key"]))
    market_key = payload.get("market_key")
    if market_key:
        prefix = str(market_key)
        for key in stats:
            if key.startswith(prefix + "|"):
                targets.append(key)
    deduped = []
    seen = set()
    for key in targets:
        if key in seen or key not in stats:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _policy_for_signal(stats: dict, settings: dict) -> dict:
    risk = settings.get("risk", {})
    improvement_cfg = settings.get("self_improvement", {})
    safety_cfg = settings.get("signal_safety", {})
    avg = float(stats.get("avg_pnl_bps") or 0)
    win_rate = float(stats.get("win_rate") or 0)
    closed = int(stats.get("closed_count") or 0)
    severe_loss = avg <= -75 and win_rate < 0.45 and closed >= 5
    moderate_loss = avg <= -20 or win_rate < 0.4
    min_edge = max(float(risk.get("min_net_edge_bps", 2.0)) + (6.0 if severe_loss else 3.0), 5.0)
    max_spread = min(float(risk.get("max_spread_bps", 8.0)), 4.0 if severe_loss else 5.0)
    return {
        "min_score_delta": 12.0 if severe_loss else 7.0 if moderate_loss else 4.0,
        "min_net_edge_bps": round(min_edge, 3),
        "max_spread_bps": round(max_spread, 3),
        "allocation_multiplier": 0.0 if severe_loss else 0.25 if moderate_loss else 0.5,
        "pause_entries": severe_loss,
        "expires_after_trades": int(improvement_cfg.get("default_policy_trade_ttl", 30)),
        "allow_recovery_probes": True,
        "recovery_probe_every_n_reviews": int(
            improvement_cfg.get("recovery_probe_every_reviews", safety_cfg.get("recovery_probe_every_reviews", 25))
        ),
        "recovery_probe_allocation_multiplier": float(
            improvement_cfg.get(
                "recovery_probe_allocation_multiplier",
                safety_cfg.get("recovery_probe_allocation_multiplier", 0.1),
            )
        ),
        "release_criteria": {
            "min_closed_trades": int(safety_cfg.get("release_min_recovery_trades", 5)),
            "min_avg_pnl_bps": float(safety_cfg.get("release_min_avg_pnl_bps", 10.0)),
            "min_win_rate": float(safety_cfg.get("release_min_win_rate", 0.55)),
            "max_worst_bps": float(safety_cfg.get("release_max_worst_bps", -500.0)),
        },
        "reason": "severe_loss_pause" if severe_loss else "loss_tightening",
    }


def _policy_id(source_id: str, signal_key: str, policy: dict) -> str:
    raw = json.dumps({"source": source_id, "signal_key": signal_key, "policy": policy}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _active_policy_exists(conn: sqlite3.Connection, signal_key: str, policy_type: str) -> bool:
    row = conn.execute(
        """
        select 1
        from signal_policies
        where status in ('active', 'promoted')
          and signal_key = ?
          and policy_type = ?
          and (expires_after_trades is null or applied_count < expires_after_trades)
        limit 1
        """,
        (signal_key, policy_type),
    ).fetchone()
    return row is not None


def _active_governor_policy(conn: sqlite3.Connection, signal_key: str) -> dict | None:
    row = conn.execute(
        """
        select policy_id, policy_json, applied_count, filtered_count, opened_count
        from signal_policies
        where status in ('active', 'promoted')
          and signal_key = ?
          and policy_type = 'safety_governor'
        order by created_at desc
        limit 1
        """,
        (signal_key,),
    ).fetchone()
    if not row:
        return None
    try:
        policy = json.loads(row["policy_json"] or "{}")
    except json.JSONDecodeError:
        policy = {}
    return {
        "policy_id": row["policy_id"],
        "governor_mode": policy.get("governor_mode"),
        "applied_count": row["applied_count"],
        "filtered_count": row["filtered_count"],
        "opened_count": row["opened_count"],
    }


def consolidate_active_policies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select policy_id, experiment_id, signal_key, policy_type, pause_entries,
               min_score_delta, min_net_edge_bps, created_at, policy_json
        from signal_policies
        where status = 'active'
        order by signal_key, policy_type, pause_entries desc, min_score_delta desc,
                 min_net_edge_bps desc, created_at asc
        """
    ).fetchall()
    keep: set[tuple[str, str, str]] = set()
    superseded = []
    for row in rows:
        try:
            policy_payload = json.loads(row["policy_json"] or "{}")
        except json.JSONDecodeError:
            policy_payload = {}
        context_filter = policy_payload.get("context_filter") if row["policy_type"] == "contextual_failure_filter" else None
        context_key = json.dumps(context_filter or {}, sort_keys=True)
        key = (row["signal_key"], row["policy_type"], context_key)
        if key not in keep:
            keep.add(key)
            continue
        conn.execute("update signal_policies set status = 'superseded' where policy_id = ?", (row["policy_id"],))
        conn.execute(
            """
            update self_improvement_experiments
            set status = 'superseded',
                decision = 'superseded_by_policy_consolidation',
                completed_at = coalesce(completed_at, ?),
                reflection = 'Duplicate active policy for the same signal/type was consolidated.'
            where id = ? and status = 'active'
            """,
            (_utc_now(), row["experiment_id"]),
        )
        superseded.append({"policy_id": row["policy_id"], "experiment_id": row["experiment_id"], "signal_key": row["signal_key"]})
    conn.commit()
    return superseded


def _execute_failure_filter(conn: sqlite3.Connection, rec: dict, settings: dict) -> list[dict]:
    payload = rec["payload"]
    if _duplicate_regional_fx_frontier_prediction_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "regional_fx_frontier_prediction_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "regional_fx_frontier_prediction_pack_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    if _duplicate_self_improvement_open_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "self_improvement_open_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "self_improvement_open_pack_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    if _duplicate_strategy_reliability_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "strategy_reliability_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "strategy_reliability_pack_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    if _duplicate_frontier_systemic_redesign_payload(payload) and _implemented_manual_category_exists(
        conn, "frontier_systemic_redesign"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "frontier_systemic_redesign_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    if _duplicate_okx_reliable_outcomes_payload(payload) and _implemented_manual_category_exists(
        conn, "okx_reliable_outcomes"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "okx_reliable_outcomes_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    if _duplicate_signal_redesign_payload(payload) and _implemented_manual_category_exists(conn, "signal_redesign"):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "signal_redesign_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    stats = _stats_by_signal(conn)
    created = []
    max_policies = int(settings.get("self_improvement", {}).get("max_policies_per_task", 5))
    for signal_key in _target_signals(conn, payload)[:max_policies]:
        if _active_policy_exists(conn, signal_key, "failure_filter"):
            created.append(
                {
                    "signal_key": signal_key,
                    "action_status": "skipped",
                    "skip_reason": "failure_filter_already_active",
                }
            )
            continue
        governor = _active_governor_policy(conn, signal_key)
        if governor:
            created.append(
                {
                    "signal_key": signal_key,
                    "action_status": "skipped",
                    "skip_reason": "covered_by_signal_safety_governor",
                    "governor_policy": governor,
                }
            )
            continue
        item = stats[signal_key]
        if int(item.get("closed_count") or 0) < int(settings.get("learning", {}).get("min_samples_for_adjustment", 3)):
            created.append(
                {
                    "signal_key": signal_key,
                    "action_status": "skipped",
                    "skip_reason": "not_enough_closed_trades",
                    "signal_stats": item,
                }
            )
            continue
        if float(item.get("avg_pnl_bps") or 0) >= 0 and float(item.get("score_adjustment") or 0) >= 0:
            created.append(
                {
                    "signal_key": signal_key,
                    "action_status": "skipped",
                    "skip_reason": "signal_not_losing_after_learning_adjustment",
                    "signal_stats": item,
                }
            )
            continue
        policy = _policy_for_signal(item, settings)
        baseline = _closed_metrics_since(conn, signal_key)
        source_agent = payload.get("agent_name")
        experiment_id = add_self_improvement_experiment(
            conn,
            rec["recommendation_id"],
            source_agent,
            "failure_filter",
            int(payload.get("priority", 80)),
            payload.get("market_key"),
            signal_key,
            f"LLM failure filter should improve paper outcomes for {signal_key}.",
            payload.get("proposed_change") or payload.get("rationale") or "Apply stricter paper-only failure filter.",
            baseline,
            policy,
        )
        if not experiment_id:
            continue
        pid = _policy_id(rec["recommendation_id"], signal_key, policy)
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        inserted = add_signal_policy(
            conn,
            pid,
            experiment_id,
            rec["recommendation_id"],
            signal_key,
            payload.get("market_key"),
            "failure_filter",
            policy,
            {"recommendation": payload, "signal_stats": item, "baseline": baseline, "evidence": evidence},
        )
        if inserted:
            add_memory_fact(
                conn,
                "self_improvement_policy",
                signal_key,
                "activated",
                policy["reason"],
                0.82,
                "self_improvement_executor",
                {"experiment_id": experiment_id, "policy_id": pid, "policy": policy, "baseline": baseline},
            )
            created.append(
                {
                    "experiment_id": experiment_id,
                    "policy_id": pid,
                    "signal_key": signal_key,
                    "policy": policy,
                    "action_status": "created",
                }
            )
    return created


def _execute_route_resolver(conn: sqlite3.Connection, rec: dict) -> list[dict]:
    payload = rec["payload"]
    if _duplicate_regional_fx_frontier_prediction_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "regional_fx_frontier_prediction_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "regional_fx_frontier_prediction_pack_already_implemented",
                "market_key": payload.get("market_key") or "execution_routes",
                "route_key": payload.get("signal_key") or "conditional_opportunities",
            }
        ]
    if _duplicate_self_improvement_open_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "self_improvement_open_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "self_improvement_open_pack_already_implemented",
                "market_key": payload.get("market_key") or "execution_routes",
                "route_key": payload.get("signal_key") or "conditional_opportunities",
            }
        ]
    if _duplicate_route_requirements_payload(payload) and _implemented_manual_category_exists(conn, "route_requirements"):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "route_requirements_already_implemented",
                "market_key": payload.get("market_key") or "execution_routes",
                "route_key": payload.get("signal_key") or "conditional_opportunities",
            }
        ]
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    market_key = str(payload.get("market_key") or "execution_routes")
    route_key = str(payload.get("signal_key") or "conditional_opportunities")
    created = add_route_probe_task(
        conn,
        rec["recommendation_id"],
        market_key,
        route_key,
        int(payload.get("priority", 75)),
        "read_only_capability_probe",
        payload.get("proposed_change") or payload.get("rationale") or "Discover paper route capability gaps.",
        evidence,
    )
    if not created:
        return []
    experiment_id = add_self_improvement_experiment(
        conn,
        rec["recommendation_id"],
        payload.get("agent_name"),
        "route_resolver",
        int(payload.get("priority", 75)),
        market_key,
        route_key,
        "Route capability discovery should reduce conditional unknowns.",
        "Create read-only broker/borrow/margin/fee/API capability probe task.",
        {"conditional_count": evidence.get("conditional_count")},
        {"probe_type": "read_only_capability_probe"},
    )
    return [{"experiment_id": experiment_id, "market_key": market_key, "route_key": route_key}]


def _execute_adapter_spec(conn: sqlite3.Connection, rec: dict) -> list[dict]:
    payload = rec["payload"]
    if _duplicate_regional_fx_frontier_prediction_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "regional_fx_frontier_prediction_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "regional_fx_frontier_prediction_pack_already_implemented",
                "market_key": payload.get("market_key") or payload.get("signal_key") or "regional_fx_frontier_prediction_pack",
            }
        ]
    if _duplicate_self_improvement_open_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "self_improvement_open_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "self_improvement_open_pack_already_implemented",
                "market_key": payload.get("market_key") or payload.get("signal_key") or "self_improvement_open_pack",
            }
        ]
    if _duplicate_strategy_reliability_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "strategy_reliability_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "strategy_reliability_pack_already_implemented",
                "market_key": payload.get("market_key") or payload.get("signal_key") or "strategy_reliability",
            }
        ]
    if _duplicate_frontier_systemic_redesign_payload(payload) and _implemented_manual_category_exists(
        conn, "frontier_systemic_redesign"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "frontier_systemic_redesign_already_implemented",
                "market_key": payload.get("market_key") or "frontier_crypto_venue_map",
            }
        ]
    if _duplicate_okx_reliable_outcomes_payload(payload) and _implemented_manual_category_exists(
        conn, "okx_reliable_outcomes"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "okx_reliable_outcomes_already_implemented",
                "market_key": payload.get("market_key") or "OKX|perp_funding_basis",
            }
        ]
    if _duplicate_frontier_data_quality_payload(payload) and _implemented_manual_category_exists(
        conn, "frontier_data_quality"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "frontier_data_quality_already_implemented",
                "market_key": payload.get("market_key") or "frontier_crypto_venue_map",
            }
        ]
    if _duplicate_signal_redesign_payload(payload) and _implemented_manual_category_exists(conn, "signal_redesign"):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "signal_redesign_already_implemented",
                "market_key": payload.get("market_key") or "signal_redesign",
            }
        ]
    if _duplicate_okx_basis_signal_research_payload(payload) and _implemented_manual_category_exists(
        conn, "okx_basis_signal_research"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "okx_basis_signal_research_already_implemented",
                "market_key": payload.get("market_key") or "OKX|perp_funding_basis",
            }
        ]
    if _duplicate_regional_frontier_data_payload(payload) and _implemented_manual_category_exists(
        conn, "regional_frontier_data"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "regional_frontier_data_already_implemented",
                "market_key": payload.get("market_key") or "frontier_crypto_venue_map",
            }
        ]
    if _duplicate_frontier_adapter_payload(payload) and _implemented_manual_category_exists(conn, "frontier_crypto_adapter"):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "frontier_crypto_adapter_already_implemented",
                "market_key": payload.get("market_key") or "market_adapter",
            }
        ]
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    market_key = str(payload.get("market_key") or "market_adapter")
    title = str(payload.get("title") or "Market adapter spec")[:180]
    spec = {
        "goal": payload.get("proposed_change") or payload.get("rationale"),
        "source_agent": payload.get("agent_name"),
        "required_checks": [
            "public API/data availability",
            "latency/reachability",
            "market hours or funding cadence",
            "paper-trade feasibility",
            "route and jurisdiction caveats",
        ],
        "allowed_mode": "research_spec_only",
    }
    created = add_adapter_spec(
        conn,
        rec["recommendation_id"],
        market_key,
        int(payload.get("priority", 70)),
        title,
        spec,
        evidence,
    )
    if not created:
        return []
    experiment_id = add_self_improvement_experiment(
        conn,
        rec["recommendation_id"],
        payload.get("agent_name"),
        "market_adapter",
        int(payload.get("priority", 70)),
        market_key,
        None,
        "Adapter research should expand market coverage without live execution risk.",
        "Create adapter research spec and rank by data availability/latency.",
        {},
        spec,
    )
    return [{"experiment_id": experiment_id, "market_key": market_key, "title": title}]


def _execute_signal_variant(conn: sqlite3.Connection, rec: dict) -> list[dict]:
    payload = rec["payload"]
    config = payload.get("variant_config")
    if not isinstance(config, dict):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "missing_or_invalid_variant_config",
            }
        ]
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    try:
        created = create_proposed_variant(
            conn,
            title=str(payload.get("title") or "LLM frontier signal challenger"),
            config=config,
            source_recommendation_id=rec["recommendation_id"],
            source_agent=payload.get("agent_name"),
            source_model=model.get("name"),
            evidence={
                **evidence,
                "rationale": payload.get("rationale"),
                "proposed_change": payload.get("proposed_change"),
                "estimated_cost_usd": model.get("estimated_cost_usd"),
            },
        )
    except ValueError as exc:
        return [
            {
                "action_status": "skipped",
                "skip_reason": "variant_validation_failed",
                "validation_error": str(exc),
            }
        ]
    return [
        {
            **created,
            "action_status": "created" if created.get("created") else "skipped",
            "skip_reason": None if created.get("created") else "variant_already_exists",
        }
    ]


def _execute_diagnostic_hypothesis(conn: sqlite3.Connection, rec: dict) -> list[dict]:
    payload = rec["payload"]
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    signal = str(payload.get("signal_key") or payload.get("market_key") or "signal_redesign")
    add_growth_experiment(
        conn,
        int(payload.get("priority", 75)),
        signal,
        str(payload.get("title") or "LLM diagnostic hypothesis"),
        str(payload.get("proposed_change") or payload.get("rationale") or "Test causal signal hypothesis."),
        {
            **evidence,
            "agent_name": payload.get("agent_name"),
            "model": payload.get("model"),
            "diagnostic_only": True,
        },
    )
    return [{"action_status": "created", "signal_key": signal}]


def evaluate_active_experiments(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    cfg = settings.get("self_improvement", {})
    min_trades = int(cfg.get("min_eval_closed_trades", 10))
    min_improvement = float(cfg.get("promote_min_improvement_bps", 3.0))
    max_regression = float(cfg.get("revert_max_regression_bps", 8.0))
    evaluated = []
    rows = conn.execute(
        """
        select id, activated_at, task_type, signal_key, baseline_json, status
        from self_improvement_experiments
        where status = 'active' and task_type = 'failure_filter' and signal_key is not null
        """
    ).fetchall()
    for row in rows:
        activated_at = row["activated_at"]
        baseline = json.loads(row["baseline_json"] or "{}")
        current = _closed_metrics_since(conn, row["signal_key"], activated_at)
        evaluation = {"baseline": baseline, "post_activation": current, "checked_at": _utc_now()}
        if int(current["closed_count"] or 0) < min_trades:
            continue
        baseline_avg = baseline.get("avg_pnl_bps")
        current_avg = current.get("avg_pnl_bps")
        if baseline_avg is None or current_avg is None:
            continue
        delta = float(current_avg) - float(baseline_avg)
        evaluation["delta_avg_pnl_bps"] = round(delta, 3)
        baseline_wr = baseline.get("win_rate") or 0
        current_wr = current.get("win_rate") or 0
        if delta >= min_improvement and float(current_wr) >= float(baseline_wr) - 0.05:
            status = "promoted"
            decision = "promote"
            reflection = "Paper outcomes improved after the policy; keep the policy active as promoted."
        elif delta <= -max_regression:
            status = "reverted"
            decision = "revert"
            reflection = "Paper outcomes regressed after the policy; revert this policy."
        else:
            status = "active"
            decision = "needs_more_data"
            reflection = "Evaluation window is mixed; keep collecting evidence."
        update_experiment_evaluation(conn, int(row["id"]), status, decision, evaluation, reflection)
        evaluated.append({"experiment_id": int(row["id"]), "status": status, "decision": decision, "evaluation": evaluation})
    return evaluated


def run_auto_improvement(
    conn: sqlite3.Connection,
    settings: dict,
    *,
    include_code_changes: bool | None = None,
) -> dict:
    cfg = settings.get("self_improvement", {})
    if not cfg.get("enabled", True):
        return write_reports(conn, {"enabled": False}, settings=settings)
    if include_code_changes is None:
        include_code_changes = bool(cfg.get("process_code_changes_in_radar_loop", False))

    expired = expire_signal_policies(conn)
    evaluated = evaluate_active_experiments(conn, settings)
    code_evolution_evaluated = evaluate_code_evolution(conn, settings)
    consumed = []
    max_tasks = int(cfg.get("max_tasks_per_loop", 5))
    for rec in llm_recommendations_for_auto_execution(
        conn,
        limit=max_tasks,
        include_code_changes=include_code_changes,
    ):
        payload = rec["payload"]
        task_type = classify_recommendation(payload)
        created: list[dict] = []
        if task_type == "failure_filter":
            created = _execute_failure_filter(conn, rec, settings)
        elif task_type == "route_resolver":
            created = _execute_route_resolver(conn, rec)
        elif task_type == "market_adapter":
            created = _execute_adapter_spec(conn, rec)
        elif task_type == "signal_variant":
            created = _execute_signal_variant(conn, rec)
        elif task_type == "diagnostic_hypothesis":
            created = _execute_diagnostic_hypothesis(conn, rec)
        elif task_type == "code_change":
            created = process_code_change_recommendation(conn, rec, settings)

        created_artifacts = [item for item in created if item.get("action_status", "created") == "created"]
        if created_artifacts:
            update_llm_recommendation_status(conn, rec["recommendation_id"], "auto_executed")
            consumed.append(
                {
                    "recommendation_id": rec["recommendation_id"],
                    "task_type": task_type,
                    "title": rec["title"],
                    "created": created,
                }
            )
        else:
            update_llm_recommendation_status(conn, rec["recommendation_id"], "auto_skipped")
            consumed.append(
                {
                    "recommendation_id": rec["recommendation_id"],
                    "task_type": task_type,
                    "title": rec["title"],
                    "created": [],
                    "status": "auto_skipped",
                }
            )

    superseded = consolidate_active_policies(conn)
    report = {
        "enabled": True,
        "generated_at": _utc_now(),
        "code_changes_enabled": bool(include_code_changes),
        "consumed": consumed,
        "evaluated": evaluated,
        "code_evolution_evaluated": code_evolution_evaluated,
        "expired": expired,
        "superseded": superseded,
        "active_policies": active_signal_policies(conn),
        "experiments": open_self_improvement_experiments(conn, limit=50),
        "route_probe_tasks": open_route_probe_tasks(conn, limit=50),
        "adapter_specs": open_adapter_specs(conn, limit=50),
        "code_evolution": write_code_evolution_reports(conn, settings),
    }
    return write_reports(conn, report, settings=settings)


def record_review_policy_effects(conn: sqlite3.Connection, review: dict) -> None:
    for item in review.get("applied_policies", []):
        record_policy_application(
            conn,
            item["policy_id"],
            filtered=item.get("filtered", False),
        )


def record_open_policy_effects(conn: sqlite3.Connection, review: dict) -> None:
    for item in review.get("applied_policies", []):
        if item.get("filtered", False):
            continue
        record_policy_open(conn, item["policy_id"])


def write_reports(conn: sqlite3.Connection, report: dict | None = None, settings: dict | None = None) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    expired = expire_signal_policies(conn)
    if report is None:
        report = {
            "enabled": True,
            "generated_at": _utc_now(),
        }
    else:
        report = dict(report)
    report["generated_at"] = _utc_now()
    report["expired"] = [*report.get("expired", []), *expired]
    report["active_policies"] = active_signal_policies(conn)
    report["experiments"] = [
        _augment_experiment_progress(conn, item)
        for item in open_self_improvement_experiments(conn, limit=50)
    ]
    report["route_probe_tasks"] = open_route_probe_tasks(conn, limit=50)
    report["adapter_specs"] = open_adapter_specs(conn, limit=50)
    report["progress_summary"] = _progress_summary(conn, settings)
    report["code_evolution"] = report.get("code_evolution") or write_code_evolution_reports(conn, settings)
    ACTIVE_POLICIES_JSON.write_text(json.dumps(report.get("active_policies", []), indent=2), encoding="utf-8")
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_report_markdown(report), encoding="utf-8")
    _append_timeline_snapshot(report)
    return report


def _report_markdown(report: dict) -> str:
    def metric(value: object, suffix: str = "") -> str:
        if value is None:
            return "n/a"
        return f"{value}{suffix}"

    progress = report.get("progress_summary", {})
    impact = progress.get("policy_impact", {})
    all_time = progress.get("all_time", {})
    since_activation = progress.get("since_first_activation")
    lines = [
        "# Self-Improvement Report",
        "",
        "This report tracks autonomous paper-only changes created from LLM recommendations.",
        "",
        f"- Enabled: `{report.get('enabled')}`",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Timeline: `{progress.get('timeline_path') or TIMELINE_JSONL}`",
        "",
        "## Progress Summary",
        "",
        f"- Active policies: `{impact.get('active_policy_count', 0)}` of `{impact.get('policies_total', 0)}` total policy artifacts",
        f"- Policy checks applied: `{impact.get('applied_checks', 0)}`",
        f"- Paper entries filtered: `{impact.get('paper_entries_filtered', 0)}`",
        f"- Paper entries opened under policy: `{impact.get('paper_entries_opened_under_policy', 0)}`",
        f"- Estimated paper notional blocked/reduced: `${impact.get('estimated_total_risk_reduction_usd', 0)}`",
        f"- Policy statuses: `{progress.get('policy_status_counts', {})}`",
        f"- Experiment statuses: `{progress.get('experiment_status_counts', {})}`",
        f"- All-time closed paper PnL: `{metric(all_time.get('avg_pnl_bps'), ' bps')}` avg over `{all_time.get('closed_count', 0)}` trades, win rate `{metric(all_time.get('win_rate'))}`",
    ]
    if since_activation:
        lines.append(
            f"- Since first auto-policy: `{metric(since_activation.get('avg_pnl_bps'), ' bps')}` avg over "
            f"`{since_activation.get('closed_count', 0)}` trades, win rate `{metric(since_activation.get('win_rate'))}`, "
            f"delta vs all-time `{metric(progress.get('since_vs_all_time_avg_pnl_delta_bps'), ' bps')}`"
        )
    expired = report.get("expired", [])
    evaluated = report.get("evaluated", [])
    if expired or evaluated:
        lines.append(
            f"- This loop: `{len(expired)}` policy artifact(s) expired, `{len(evaluated)}` experiment(s) evaluated"
        )
    code_evolution = report.get("code_evolution") or {}
    if code_evolution:
        evo_summary = code_evolution.get("summary", {})
        lines.append(f"- Code evolution status: `{evo_summary.get('status_counts', {})}`")
    lines.extend(
        [
            "",
            "## Latest Executor Activity",
            "",
        ]
    )
    consumed = report.get("consumed", [])
    if not consumed:
        lines.append("No new LLM tasks consumed this loop.")
    for item in consumed[:20]:
        created_items = [row for row in item.get("created", []) if row.get("action_status", "created") == "created"]
        skipped_items = [row for row in item.get("created", []) if row.get("action_status") == "skipped"]
        suffix = f", {len(skipped_items)} skipped" if skipped_items else ""
        lines.append(
            f"- `{item.get('task_type')}` {item.get('title')} -> "
            f"{len(created_items)} artifact(s){suffix}"
        )
        for skipped in skipped_items[:5]:
            lines.append(
                f"  - Skipped `{skipped.get('signal_key')}`: {skipped.get('skip_reason')}"
            )
    superseded = report.get("superseded", [])
    if superseded:
        lines.append("")
        lines.append(f"Consolidated {len(superseded)} duplicate active policy artifact(s).")
    if expired:
        lines.append("")
        lines.append(f"Expired {len(expired)} policy artifact(s) that reached their review TTL.")
    if evaluated:
        lines.append("")
        for item in evaluated[:10]:
            lines.append(
                f"Evaluated experiment #{item.get('experiment_id')} -> {item.get('decision')} "
                f"({item.get('status')})"
            )

    lines.extend(["", "## Active Policies", ""])
    policies = report.get("active_policies", [])
    if not policies:
        lines.append("No active signal policies.")
    for policy in policies[:30]:
        mode = "pause" if policy.get("pause_entries") else "tighten"
        lines.append(
            f"- `{policy['signal_key']}` {mode} policy `{policy['policy_id']}` "
            f"spread<={policy.get('max_spread_bps')} edge>={policy.get('min_net_edge_bps')} "
            f"score_delta={policy.get('min_score_delta')} applied={policy.get('applied_count')} "
            f"filtered={policy.get('filtered_count')} opened={policy.get('opened_count')} "
            f"ttl={policy.get('expires_after_trades')}"
        )
        context_filter = (policy.get("policy") or {}).get("context_filter")
        if context_filter:
            lines.append(f"  - Context filter: {context_filter}")

    safety = report.get("signal_safety_governor") or {}
    if safety:
        safety_summary = safety.get("summary", {})
        lines.extend(["", "## Signal Safety Governor", ""])
        lines.append(
            f"- Active governor policies: `{safety_summary.get('active_count', 0)}` "
            f"(quarantine `{safety_summary.get('quarantine_count', 0)}`, "
            f"probation `{safety_summary.get('probation_count', 0)}`)"
        )
        lines.append(f"- Released this loop: `{safety_summary.get('released_this_loop', 0)}`")
        active_governor = safety.get("active_governor_policies", [])
        if not active_governor:
            lines.append("- No active governor policies.")
        for item in active_governor[:20]:
            policy = item.get("policy", {})
            lines.append(
                f"- `{item['signal_key']}` `{policy.get('governor_mode')}` "
                f"allocation={item.get('allocation_multiplier')} applied={item.get('applied_count')} "
                f"filtered={item.get('filtered_count')} opened={item.get('opened_count')}"
            )
            lines.append(f"  - Recovery evidence: {item.get('recovery')}")
            lines.append(f"  - Release criteria: {policy.get('release_criteria')}")

    redesign = report.get("signal_redesign") or {}
    if redesign:
        redesign_summary = redesign.get("summary", {})
        lines.extend(["", "## Signal Redesign", ""])
        lines.append(f"- Active variant: `{redesign_summary.get('active_variant')}`")
        lines.append(
            f"- Reliable 60m metrics: `{redesign_summary.get('reliable_60m_paper_metrics')}`"
        )
        lines.append(f"- Label quality: `{redesign_summary.get('label_status_counts')}`")
        lines.append(
            f"- Valid outcome delay P95: `{redesign_summary.get('valid_delay_p95_seconds')}` seconds"
        )
        for variant in redesign.get("variants", [])[:10]:
            lines.append(
                f"- `{variant.get('variant_id')}` status=`{variant.get('status')}` "
                f"passes=`{variant.get('consecutive_passes')}`"
            )

    okx_research = report.get("okx_signal_research") or {}
    if okx_research:
        okx_summary = okx_research.get("summary", {})
        lines.extend(["", "## OKX Signal Research", ""])
        lines.append(f"- Active variant: `{okx_summary.get('active_variant')}`")
        lines.append(
            f"- Reliable 60m metrics: `{okx_summary.get('reliable_60m_paper_metrics')}`"
        )
        lines.append(f"- Label quality: `{okx_summary.get('label_status_counts')}`")
        lines.append(
            f"- Valid outcome delay P95: `{okx_summary.get('valid_delay_p95_seconds')}` seconds"
        )
        if okx_summary.get("carry_economics"):
            lines.append(f"- Carry economics: `{okx_summary.get('carry_economics')}`")
        carry = okx_research.get("carry_economics") or {}
        if carry:
            lines.append(f"- Carry report: `{carry.get('report')}`")
            for item in carry.get("top_positive_carry", [])[:5]:
                lines.append(
                    f"- Carry `{item.get('inst_id')}` `{item.get('direction')}` "
                    f"net=`{item.get('net_carry_edge_bps')}`bps status=`{item.get('carry_alignment_status')}`"
                )
        for variant in okx_research.get("variants", [])[:10]:
            lines.append(
                f"- `{variant.get('variant_id')}` status=`{variant.get('status')}` "
                f"passes=`{variant.get('consecutive_passes')}`"
            )

    strategy_reliability = report.get("strategy_reliability") or {}
    if strategy_reliability:
        reliability_summary = strategy_reliability.get("summary", {})
        lines.extend(["", "## Strategy Reliability Pack", ""])
        lines.append(f"- Candidates reviewed: `{reliability_summary.get('candidate_count', 0)}`")
        lines.append(f"- Annotated candidates: `{reliability_summary.get('annotated_count', 0)}`")
        lines.append(f"- Shadow/blocked: `{reliability_summary.get('shadow_or_blocked_count', 0)}`")
        lines.append(f"- Protected working slices: `{reliability_summary.get('protected_working_slice_count', 0)}`")
        lines.append(f"- Actions: `{reliability_summary.get('by_action', {})}`")
        lines.append(f"- Profiles: `{reliability_summary.get('by_profile', {})}`")
        for item in strategy_reliability.get("top_adjustments", [])[:10]:
            lines.append(
                f"- `{item.get('signal_key')}` `{item.get('direction')}` "
                f"action=`{item.get('action')}` allocation=`{item.get('allocation_multiplier')}` "
                f"reasons={item.get('reasons')}"
            )

    expansion = report.get("expansion_map") or {}
    if expansion:
        frontier = expansion.get("frontier_crypto") or {}
        regional_fx = expansion.get("regional_fx_reference") or {}
        prediction = expansion.get("prediction_markets") or {}
        route = expansion.get("route_intelligence") or {}
        lines.extend(["", "## Expansion Map", ""])
        lines.append(
            f"- Frontier observations: `{frontier.get('observation_count', 0)}` "
            f"venues=`{frontier.get('venue_count', 0)}` symbols=`{frontier.get('symbol_count', 0)}`"
        )
        lines.append(
            f"- Frontier depth: selected `{frontier.get('depth_selected_count', 0)}`, "
            f"enriched `{frontier.get('depth_enriched_count', 0)}`, "
            f"known quality rate `{frontier.get('known_quality_rate')}`"
        )
        lines.append(
            f"- Frontier regional observations: `{frontier.get('regional_observation_count', 0)}` "
            f"quote_norm=`{frontier.get('by_quote_normalization', {})}`"
        )
        if regional_fx:
            lines.append(
                f"- Regional FX references: `{regional_fx.get('reference_count', 0)}` "
                f"stale=`{regional_fx.get('stale_count', 0)}` "
                f"providers=`{regional_fx.get('provider_status', [])}`"
            )
        lines.append(
            f"- Prediction markets: candidates `{prediction.get('candidate_count', 0)}` "
            f"orderbooks=`{prediction.get('by_orderbook_status', {})}` "
            f"expired_filtered=`{prediction.get('expired_filtered_count', 0)}` "
            f"event_review_queue=`{len(prediction.get('prediction_event_review_queue', []))}`"
        )
        lines.append(
            f"- Route intelligence: blockers `{route.get('blocker_counts', {})}`, "
            f"potentially executable soon `{route.get('potentially_executable_soon_count', 0)}`"
        )

    research_path = RUNS_DIR / "research_worker_latest.json"
    if research_path.exists():
        try:
            research = json.loads(research_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            research = {"status": "unreadable"}
        research_summary = research.get("summary", {})
        lines.extend(["", "## Global Market Discovery", ""])
        lines.append(f"- Status: `{research.get('status')}`")
        lines.append(
            f"- Candidates this run: `{research_summary.get('candidate_count', 0)}`, "
            f"new `{research_summary.get('new_candidate_count', 0)}`, "
            f"total known `{research_summary.get('total_known_candidate_count', 0)}`"
        )
        lines.append(f"- Surface types: `{research_summary.get('by_surface_type', {})}`")
        lines.append(f"- Regions: `{research_summary.get('by_region', {})}`")
        lines.append(f"- Artifact inserts: `{research_summary.get('inserted_artifact_counts', {})}`")
        lines.append(f"- Report: `{RUNS_DIR / 'research_worker_report.md'}`")

    allocation_path = RUNS_DIR / "hunter_allocation_report.json"
    if allocation_path.exists():
        try:
            allocation = json.loads(allocation_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            allocation = {"enabled": "unreadable"}
        lines.extend(["", "## Hunter Allocation", ""])
        lines.append(f"- Enabled: `{allocation.get('enabled')}`")
        lines.append(f"- Slot targets: `{allocation.get('slot_targets', {})}`")
        lines.append(f"- Selected by bucket: `{allocation.get('selected_by_bucket', {})}`")
        lines.append(f"- Global discovery: `{allocation.get('global_discovery', {})}`")
        lines.append(f"- Report: `{RUNS_DIR / 'hunter_allocation_report.md'}`")

    open_pack = report.get("self_improvement_open_pack") or {}
    if open_pack:
        borrow = open_pack.get("route_borrow_intelligence") or {}
        africa = open_pack.get("africa_rail_watchlist") or {}
        kalshi = open_pack.get("kalshi_public_coverage") or {}
        diagnostics = open_pack.get("signal_repair_diagnostics") or {}
        lines.extend(["", "## Self-Improvement Open Pack", ""])
        lines.append(f"- Report: `{RUNS_DIR / 'self_improvement_open_pack.md'}`")
        lines.append(
            f"- Route-borrow records: `{borrow.get('record_count', 0)}`, "
            f"shadow-only unconfirmed `{borrow.get('shadow_only_unconfirmed_count', 0)}`"
        )
        lines.append(
            f"- Africa rail watchlist: venues `{africa.get('venue_count', 0)}`, "
            f"instruments `{africa.get('instrument_count', 0)}`, availability `{africa.get('by_venue_availability', {})}`"
        )
        lines.append(
            f"- Kalshi public coverage: candidates `{kalshi.get('current_candidate_count', 0)}`, "
            f"route blockers `{kalshi.get('route_blockers', {})}`"
        )
        lines.append(
            f"- Weak-signal diagnostics: frontier `{len(diagnostics.get('frontier_weak_signal_diagnostics', []))}`, "
            f"Yahoo `{len(diagnostics.get('yahoo_proxy_diagnostics', []))}`, "
            f"OKX `{len(diagnostics.get('okx_basis_funding_diagnostics', []))}`"
        )

    code_evolution = report.get("code_evolution") or {}
    if code_evolution:
        evo_summary = code_evolution.get("summary", {})
        lines.extend(["", "## AI Code Evolution", ""])
        lines.append(f"- Report: `{evo_summary.get('report')}`")
        lines.append(f"- Ledger: `{evo_summary.get('ledger')}`")
        lines.append(f"- Status counts: `{evo_summary.get('status_counts', {})}`")
        for item in evo_summary.get("latest", [])[:10]:
            lines.append(
                f"- `{item.get('proposal_id')}` `{item.get('category')}` "
                f"status=`{item.get('status')}` files=`{item.get('changed_files')}`"
            )

    lines.extend(["", "## Experiments", ""])
    experiments = report.get("experiments", [])
    if not experiments:
        lines.append("No experiments yet.")
    for exp in experiments[:30]:
        lines.append(
            f"- P{exp['priority']} #{exp['id']} `{exp['task_type']}` `{exp.get('signal_key') or exp.get('market_key')}` "
            f"status={exp['status']} decision={exp.get('decision')}"
        )
        baseline = exp.get("baseline", {})
        if baseline:
            lines.append(f"  - Baseline: {baseline}")
        if exp.get("post_activation"):
            lines.append(f"  - Post activation: {exp['post_activation']}")
        if exp.get("delta_avg_pnl_bps") is not None:
            lines.append(f"  - Delta avg PnL: {exp['delta_avg_pnl_bps']} bps")
        if exp.get("evaluation"):
            lines.append(f"  - Evaluation: {exp['evaluation']}")

    lines.extend(["", "## Route Probe Tasks", ""])
    probes = report.get("route_probe_tasks", [])
    if not probes:
        lines.append("No open route probe tasks.")
    for probe in probes[:20]:
        lines.append(f"- P{probe['priority']} `{probe['route_key']}` {probe['probe_type']}: {probe['rationale']}")

    lines.extend(["", "## Adapter Specs", ""])
    specs = report.get("adapter_specs", [])
    if not specs:
        lines.append("No open adapter specs.")
    for spec in specs[:20]:
        lines.append(f"- P{spec['priority']} `{spec['market_key']}` {spec['title']}")
    return "\n".join(lines) + "\n"
