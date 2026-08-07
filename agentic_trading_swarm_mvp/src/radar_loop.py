#!/usr/bin/env python3
"""Self-improving radar orchestrator.

Loop:
1. Scan live market data.
2. Close due paper trades.
3. Update signal-family learning stats.
4. Review new candidates with deterministic agents.
5. Open approved paper trades.
6. Persist state and improvement backlog.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import re
import sqlite3
import sys
import time
from types import SimpleNamespace

from agent_review import review_candidate
from adapter_capabilities import reconcile_adapter_specs
from adapter_runtime import build_scan_batch as build_public_adapter_scan_batch
from autonomous_builder import run_autonomous_builder
from contextual_failure_filters import (
    annotate_candidates_with_cross_context_diagnostics,
    run_contextual_failure_filters,
)
from crypto_venue_scanner import scan as scan_crypto_venues, write_outputs as write_crypto_venue_health
from execution_engine import execute_order
from frontier_crypto_adapter import REPORT_JSON as FRONTIER_CRYPTO_REPORT_JSON
from frontier_crypto_adapter import build_scan_batch as build_frontier_crypto_scan_batch
from global_market_discovery_scanner import build_scan_batch as build_global_market_discovery_scan_batch
from global_proxy_scanner import build_scan_batch as build_global_proxy_scan_batch
from hunter_allocation import allocate_candidate_review, write_hunter_allocation_report
from learning import load_adjustments, stats_snapshot, update_signal_stats
from llm_bridge import (
    compact_json_value,
    cross_context_reliability,
    ingest_llm_recommendations,
    route_requirement_candidate_inputs,
    write_llm_state_packet,
)
from market_admission import admission_key_for, run_market_admission_monitor
from market_admission_bridge import run_market_admission_bridge
from llm_swarm_runner import run_once as run_llm_swarm_once
from market_hunter import run_market_hunter
from memory_graph import ingest_radar_memory, memory_summary
from okx_perp_scanner import build_scan_batch as build_okx_scan_batch
from paper_context_cost import paper_context_cost_report
from paper_context_drag import apply_context_drag_overlay, context_drag_report, context_drag_statistics
from paper_admission_queue import (
    enqueue_paper_admission_candidates,
    paper_admission_queue_config,
    paper_admission_queue_summary,
    reconcile_paper_admission_queue,
    select_paper_admission_candidates,
)
from due_outcome_collector import collect_due_outcome_prices
from paper_expansion_campaign import apply_campaign_controls, record_campaign_cycle
from okx_signal_research import run_okx_signal_research
from paper_exploration import exploration_enabled, fair_lineage_order, prepare_candidate_for_exploration
from paper_exploration_report import compact_paper_exploration_report, write_paper_exploration_report
from prediction_market_scanner import build_scan_batch as build_prediction_market_scan_batch
from route_resolver import enrich_candidates, write_route_resolver_report
from scan_batch import merge_observations, normalize_observation
from signals.runtime import run_signal_plugins
from self_improvement import (
    record_open_policy_effects,
    record_review_policy_effects,
    run_auto_improvement,
    write_reports as write_self_improvement_reports,
)
from self_improvement_open_pack import build_open_pack_report, write_open_pack_reports
from signal_safety import run_signal_safety_governor
from signal_redesign import run_frontier_redesign
from settings import load_settings
from strategy_lab import (
    RECOVERY_CANARY_STRATEGY_LAB_ID,
    evaluate_strategy_lab,
    generate_strategy_lab_candidates,
    recovery_canary_direct_label_eligible,
    strategy_lab_surface_activation_eligible,
    write_strategy_lab_reports,
)
from strategy_reliability import apply_strategy_reliability
from storage import (
    DB_PATH,
    RUNS_DIR,
    active_signal_policies,
    close_due_trades,
    connect,
    count_open_trades,
    execution_summary,
    has_open_trade,
    llm_inbox_summary,
    llm_cost_summary,
    open_hunter_directives,
    open_experiments,
    open_trade_instruments,
    open_signal_trial_instruments,
    open_paper_trade,
    open_tasks,
    perform_maintenance,
    performance_summary,
    load_due_paper_outcome_targets,
    mark_due_paper_outcome_windows_attempted,
    reconcile_pending_opportunities,
    record_due_horizon_outcomes,
    record_paper_funding_coverage,
    record_paper_price_observations,
    reliable_paper_label_eligibility_for_trade_row,
    save_opportunity,
    update_opportunity_decision,
    utc_now,
)
from yahoo_counterfactual import run_yahoo_counterfactual_analysis


def _auxiliary_runtime_policy(settings: dict) -> dict:
    """Return explicit slow-task policy for this radar process.

    The market loop must stay independent from long LLM/code-evolution work.
    Slow research/build tasks belong in ``evolution_worker.py`` unless an
    operator deliberately opts back into in-radar execution.
    """

    llm_cfg = settings.get("llm_swarm", {})
    builder_cfg = settings.get("autonomous_builder", {})
    worker_cfg = settings.get("evolution_worker", {})
    return {
        "llm_swarm_in_radar": bool(llm_cfg.get("run_in_radar_loop", False)),
        "autonomous_builder_in_radar": bool(builder_cfg.get("run_in_radar_loop", False)),
        "evolution_worker_expected": bool(worker_cfg.get("enabled", True)),
    }


def _strategy_lab_runtime_summary(strategy_lab_generation: dict | None, runtime_selection: dict | None = None) -> dict:
    generation = strategy_lab_generation or {}
    accepted = generation.get("accepted_candidates")
    rejected = generation.get("rejected_candidates")
    generated_count = generation.get("generated_count")
    if generated_count is None:
        generated_count = generation.get("generated_candidates", 0)
    summary = {
        "enabled": bool(generation.get("enabled", True)),
        "generated_count": int(generated_count or 0),
        "accepted_count": (
            len(accepted)
            if isinstance(accepted, list)
            else int(generation.get("accepted_count", 0) or 0)
        ),
        "rejected_count": (
            len(rejected)
            if isinstance(rejected, list)
            else int(generation.get("rejected_count", 0) or 0)
        ),
        "report": str(RUNS_DIR / "strategy_lab_report.md"),
    }
    generated_at = generation.get("generated_at")
    if generated_at:
        summary["generated_at"] = generated_at
    selection_mode = generation.get("selection_mode")
    if selection_mode:
        summary["selection_mode"] = selection_mode
    runtime = runtime_selection or {}
    for key in (
        "runtime_candidate_filters_enabled",
        "available_candidate_filter_count",
        "selected_count",
        "skipped_non_candidate_filter_count",
    ):
        if key in runtime:
            summary[key] = runtime[key]
    runtime_selection_mode = runtime.get("selection_mode")
    if runtime_selection_mode:
        summary["runtime_selection_mode"] = runtime_selection_mode
    return summary


def _is_strategy_lab_candidate_filter(item: dict) -> bool:
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    logic_type = item.get("strategy_lab_logic_type") or item.get("strategy_lab_type")
    return bool(
        logic_type == "candidate_filter"
        or "candidate-filter" in tags
        or metadata.get("artifact_type") == "filter"
    )


def _is_strategy_lab_runtime_candidate(item: dict) -> bool:
    return bool(
        _is_strategy_lab_candidate_filter(item)
        or item.get("strategy_lab_logic_type") in {"observation_program", "generated_signal_plugin"}
        or item.get("signal_plugin_id")
    )


def _strategy_lab_surface_rank_eligible(item: dict) -> bool:
    return strategy_lab_surface_activation_eligible(item)


def _strategy_lab_lineage_root_id(candidate: dict) -> str:
    explicit = str(candidate.get("strategy_lab_lineage_root_id") or "").strip()
    if explicit:
        return explicit
    relaxation = candidate.get("strategy_lab_relaxation")
    parent = relaxation.get("parent") if isinstance(relaxation, dict) else None
    current = str(
        candidate.get("strategy_lab_root_id")
        or candidate.get("parent_strategy_lab_id")
        or parent
        or candidate.get("strategy_lab_id")
        or ""
    ).strip()
    previous = None
    while current and current != previous:
        previous = current
        current = re.sub(r"__relaxed_r\d+$", "", current)
    return current


def _is_recovery_canary_candidate(candidate: dict) -> bool:
    """Identify the one Strategy Lab lineage allowed during canary recovery."""

    return bool(
        str(candidate.get("strategy_lab_id") or "").strip()
        == RECOVERY_CANARY_STRATEGY_LAB_ID
        and _strategy_lab_lineage_root_id(candidate)
        == RECOVERY_CANARY_STRATEGY_LAB_ID
        and str(candidate.get("venue") or "").strip().upper() == "OKX"
        and str(candidate.get("trade_type") or "").strip().lower()
        == "perp_funding_basis"
        and str(candidate.get("direction") or "").strip().lower()
        == "short_perp_long_spot"
    )


def _strategy_lab_runtime_selection_summary(
    candidates: list[dict],
    *,
    runtime_selection_mode: str = "disabled",
    generated_count: int = 0,
    accepted_count: int = 0,
    skipped_non_candidate_filter_count: int | None = None,
) -> dict:
    summary = {
        "generated_count": int(generated_count or 0),
        "accepted_count": int(accepted_count or 0),
        "runtime_selection_mode": runtime_selection_mode,
    }
    if runtime_selection_mode != "disabled":
        summary["selection_mode"] = runtime_selection_mode
    if skipped_non_candidate_filter_count is not None:
        summary["skipped_non_candidate_filter_count"] = int(skipped_non_candidate_filter_count)
        summary["available_candidate_filter_count"] = sum(
            1 for item in candidates if _is_strategy_lab_candidate_filter(item)
        )
        summary["selected_count"] = int(accepted_count or 0)
    return summary


def _reserve_strategy_lab_review_candidates(candidates: list[dict], settings: dict, total_slots: int) -> tuple[list[dict], dict]:
    cfg = settings.get("strategy_lab", {})
    requested = max(0, int(cfg.get("runtime_review_reserved_slots", 5)))
    limit = min(max(0, int(total_slots)), requested)
    selected: list[dict] = []
    selected_experiments: set[str] = set()
    selected_roots: set[str] = set()
    selected_sources: set[tuple[str, str, str, str]] = set()

    def route_rank(row: dict) -> int:
        status = str(
            row.get("route_status")
            or (row.get("execution_route") or {}).get("route_status")
            or (row.get("execution_feasibility") or {}).get("status")
            or "unknown"
        ).lower()
        return {"standard": 0, "feasible": 0, "paper_proxy": 1, "conditional": 2}.get(status, 3)

    by_experiment: dict[str, list[dict]] = {}
    by_root: dict[str, list[dict]] = {}
    for candidate in candidates:
        strategy_lab_id = str(candidate.get("strategy_lab_id") or "").strip()
        if not strategy_lab_id:
            continue
        if str(candidate.get("direction") or "").lower() == "watch_only":
            continue
        if not _is_strategy_lab_runtime_candidate(candidate):
            continue
        if not _strategy_lab_surface_rank_eligible(candidate):
            continue
        by_experiment.setdefault(strategy_lab_id, []).append(candidate)
        by_root.setdefault(_strategy_lab_lineage_root_id(candidate) or strategy_lab_id, []).append(candidate)

    def select_one(options: list[dict]) -> tuple[dict | None, tuple[str, str, str, str] | None]:
        candidate = None
        source_key = None
        for option in sorted(options, key=lambda row: (route_rank(row), -float(row.get("score") or 0.0))):
            option_key = (
                str(option.get("venue") or ""),
                str(option.get("inst_id") or ""),
                str(option.get("direction") or ""),
                str(option.get("trade_type") or ""),
            )
            if option_key not in selected_sources:
                candidate = option
                source_key = option_key
                break
        return candidate, source_key

    for root_id, root_candidates in by_root.items():
        candidate, source_key = select_one(root_candidates)
        if candidate is None or source_key is None:
            continue
        row = dict(candidate)
        row["_hunter_bucket"] = "explore"
        row["_hunter_directive_id"] = None
        row["_hunter_allocation_reason"] = "strategy_lab_distinct_experiment_reserve"
        row["strategy_lab_lineage_root_id"] = root_id
        selected.append(row)
        selected_experiments.add(str(candidate.get("strategy_lab_id") or ""))
        selected_roots.add(root_id)
        selected_sources.add(source_key)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for strategy_lab_id, experiment_candidates in by_experiment.items():
            if strategy_lab_id in selected_experiments:
                continue
            candidate, source_key = select_one(experiment_candidates)
            if candidate is None or source_key is None:
                continue
            root_id = _strategy_lab_lineage_root_id(candidate) or strategy_lab_id
            row = dict(candidate)
            row["_hunter_bucket"] = "explore"
            row["_hunter_directive_id"] = None
            row["_hunter_allocation_reason"] = "strategy_lab_additional_descendant_reserve"
            row["strategy_lab_lineage_root_id"] = root_id
            selected.append(row)
            selected_experiments.add(strategy_lab_id)
            selected_roots.add(root_id)
            selected_sources.add(source_key)
            if len(selected) >= limit:
                break
    return selected, {
        "configured_slots": requested,
        "reserved_count": len(selected),
        "strategy_lab_ids": sorted(selected_experiments),
        "strategy_lab_lineage_roots": sorted(selected_roots),
        "distinct_lineage_root_count": len(selected_roots),
        "distinct_source_count": len(selected_sources),
    }


strategy_lab_runtime = SimpleNamespace(
    is_candidate_filter=_is_strategy_lab_candidate_filter,
    build_runtime_summary=_strategy_lab_runtime_selection_summary,
    reserve_review_candidates=_reserve_strategy_lab_review_candidates,
)


_PAPER_APPROVAL_DECISIONS = frozenset(
    {"approve_paper_trade", "approve_conditional_paper_trade"}
)


def _pending_execution_review(review: dict) -> dict:
    """Persist approvals as in-flight until execution produces a real state."""

    intended = str(review.get("decision") or "unknown")
    if intended not in _PAPER_APPROVAL_DECISIONS:
        return dict(review)
    return {
        **review,
        "decision": "pending_execution",
        "intended_decision": intended,
        "execution_status": "pending",
    }


def _not_queued_execution_review(review: dict) -> dict:
    """Persist a review that was not admitted to the bounded execution queue."""

    intended = str(review.get("decision") or "unknown")
    return {
        **review,
        "decision": "reviewed_not_queued",
        "intended_decision": intended,
        "execution_status": "not_selected_for_bounded_paper_queue",
    }


def _paper_lane_limits(total: int) -> dict[str, int]:
    """Split a bounded quota evenly, with a single odd slot favoring evidence."""

    total = max(0, int(total))
    return {"evidence": (total + 1) // 2, "discovery": total // 2}


def _paper_queue_identity(candidate: dict) -> str:
    return str(candidate.get("admission_key") or admission_key_for(candidate))


def _paper_queue_claim_identity(candidate: dict) -> tuple[str, str, str, str]:
    metadata = candidate.get("paper_admission")
    metadata = metadata if isinstance(metadata, dict) else {}
    return (
        str(candidate.get("_paper_admission_queue_id") or metadata.get("queue_id") or ""),
        str(candidate.get("admission_key") or metadata.get("admission_key") or ""),
        str(
            candidate.get("admission_episode_id")
            or candidate.get("episode_id")
            or metadata.get("episode_id")
            or ""
        ),
        str(
            candidate.get("_paper_admission_claim_token")
            or candidate.get("paper_admission_claim_token")
            or metadata.get("claim_token")
            or ""
        ),
    )


def _reviewed_for_accounting(reviewed: list[dict]) -> list[dict]:
    """Exclude unqueued approvals while retaining queued admission intent."""

    output = []
    for item in reviewed:
        intended = item.get("review") if isinstance(item.get("review"), dict) else {}
        execution_review = (
            item.get("execution_review")
            if isinstance(item.get("execution_review"), dict)
            else {}
        )
        effective = (
            execution_review
            if str(execution_review.get("decision") or "") == "reviewed_not_queued"
            else intended
        )
        output.append({**item, "intended_review": intended, "review": effective})
    return output


def _current_peak_rss_mb() -> float | None:
    try:
        import psutil  # type: ignore

        info = psutil.Process().memory_info()
        rss = max(int(getattr(info, "rss", 0) or 0), int(getattr(info, "peak_wset", 0) or 0))
        return round(rss / (1024.0 * 1024.0), 3)
    except Exception:
        return None


def _database_storage_footprint(path) -> int:
    """Measure SQLite main + WAL bytes so uncheckpointed writes are visible."""

    main = int(path.stat().st_size) if path.exists() else 0
    wal_path = path.with_name(path.name + "-wal")
    wal = int(wal_path.stat().st_size) if wal_path.exists() else 0
    return main + wal


def _sqlite_logical_footprint_bytes(conn) -> int:
    page_count = int(conn.execute("pragma page_count").fetchone()[0] or 0)
    page_size = int(conn.execute("pragma page_size").fetchone()[0] or 0)
    return max(0, page_count) * max(0, page_size)


def _database_logical_footprint(path) -> int:
    """Read committed SQLite page allocation without counting transient WAL duplication."""

    if not path.exists():
        return 0
    read_only = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return _sqlite_logical_footprint_bytes(read_only)
    finally:
        read_only.close()


def _lineage_coverage(
    conn,
    table: str,
    time_column: str,
    started_at: str,
    *,
    extra_where: str = "",
    queue_link_column: str | None = None,
) -> tuple[int, float]:
    artifact_link = (
        f"and queue.{queue_link_column} = artifact.id" if queue_link_column else ""
    )
    row = conn.execute(
        f"""
        select count(*) as total,
               sum(case when exists (
                              select 1 from paper_admission_queue queue
                              where queue.admission_key = artifact.admission_key
                                and queue.episode_id = artifact.admission_episode_id
                                {artifact_link}
                            )
                         then 1 else 0 end) as linked
        from {table} artifact
        where julianday(artifact.{time_column}) >= julianday(?)
          {extra_where}
        """,
        (started_at,),
    ).fetchone()
    total = int(row["total"] or 0)
    linked = int(row["linked"] or 0)
    return total, 1.0 if total == 0 else linked / total


def _exact_queue_lineage_exists(
    conn,
    admission_key: object,
    episode_id: object,
    *,
    paper_trade_id: int | None = None,
    route_status: str | None = None,
) -> bool:
    key = str(admission_key or "").strip()
    episode = str(episode_id or "").strip()
    if not key or not episode:
        return False
    sql = "select 1 from paper_admission_queue where admission_key=? and episode_id=?"
    params: list[object] = [key, episode]
    if paper_trade_id is not None:
        sql += " and paper_trade_id=?"
        params.append(int(paper_trade_id))
    if route_status is not None:
        sql += " and lower(trim(route_status))=?"
        params.append(str(route_status).strip().lower())
    return (
        conn.execute(sql + " limit 1", params).fetchone()
        is not None
    )


def _parse_metric_timestamp(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _exact_primary_phase_trades(
    conn,
    *,
    phase_started_at: str,
    captured_at: str,
    default_hold_minutes: int,
) -> list:
    """Return the exact standard-route direct cohort opened in this phase."""

    rows = conn.execute(
        """
        select trade.*,
               exists (
                   select 1
                   from paper_trade_outcomes outcome
                   where outcome.trade_id=trade.id
                     and outcome.horizon_minutes=coalesce(
                         trade.selected_hold_minutes, ?
                     )
                     and julianday(outcome.measured_at) <= julianday(?)
               ) as selected_hold_outcome_present
        from paper_trades trade
        where julianday(trade.opened_at) >= julianday(?)
          and julianday(trade.opened_at) <= julianday(?)
          and exists (
              select 1
              from paper_admission_queue queue
              where queue.paper_trade_id=trade.id
                and queue.admission_key=trade.admission_key
                and queue.episode_id=trade.admission_episode_id
                and lower(trim(coalesce(queue.route_status,'')))='standard'
          )
        order by trade.id
        """,
        (default_hold_minutes, captured_at, phase_started_at, captured_at),
    ).fetchall()
    exact = []
    for row in rows:
        eligibility = reliable_paper_label_eligibility_for_trade_row(row)
        scope = str(eligibility.get("paper_signal_stats_scope") or "").lower()
        signal = str(row["signal_key"] or "").upper()
        if scope == "direct" and not signal.startswith(
            ("SYNTHETIC_RESEARCH|", "PAPER_PROXY|")
        ):
            exact.append(row)
    return exact


def _reliable_recovery_canary_pnls(
    conn,
    measured_since: str,
    *,
    opened_since: str,
    captured_at: str,
) -> list[float]:
    rows = conn.execute(
        """
        select trade.id,trade.status,trade.signal_key,trade.candidate_json,
               trade.review_json,trade.context_json,trade.close_measurement_status,
               trade.strategy_lab_id,trade.strategy_lab_version,
               trade.strategy_lineage_root_id,trade.admission_key,
               trade.admission_episode_id,outcome.measurement_status,
               outcome.pnl_bps,outcome.context_json as outcome_context_json,
               outcome.admission_key as outcome_admission_key,
               outcome.admission_episode_id as outcome_admission_episode_id
        from paper_trades trade
        join paper_trade_outcomes outcome
          on outcome.trade_id=trade.id and outcome.horizon_minutes=60
        where trade.strategy_lab_id=? and trade.strategy_lab_version=1
          and julianday(outcome.measured_at) >= julianday(?)
          and julianday(outcome.measured_at) <= julianday(?)
          and julianday(trade.opened_at) >= julianday(?)
          and julianday(trade.opened_at) <= julianday(?)
          and exists (
              select 1
              from paper_admission_queue queue
              where queue.paper_trade_id=trade.id
                and queue.admission_key=trade.admission_key
                and queue.episode_id=trade.admission_episode_id
                and lower(trim(coalesce(queue.route_status,'')))='standard'
          )
        """,
        (
            RECOVERY_CANARY_STRATEGY_LAB_ID,
            measured_since,
            captured_at,
            opened_since,
            captured_at,
        ),
    ).fetchall()
    return [
        float(row["pnl_bps"])
        for row in rows
        if recovery_canary_direct_label_eligible(conn, row, expected_version=1)
    ]


def _bounded_campaign_metrics(
    conn,
    cycle_state: dict,
    *,
    settings: dict,
    reviewed: list[dict],
    frontier_crypto_venues: dict,
    runtime_seconds: float,
    db_size_before: int,
    captured_at: str | None = None,
) -> dict:
    """Build current-cycle diagnostics and authoritative phase gate cohorts."""

    if not cycle_state.get("enabled"):
        return {}
    started_at = str(cycle_state["cycle_started_at"])
    phase_started_at = str(cycle_state.get("phase_started_at") or started_at)
    captured_at = str(captured_at or utc_now())
    captured_dt = _parse_metric_timestamp(captured_at) or dt.datetime.now(dt.timezone.utc)
    started_dt = _parse_metric_timestamp(started_at) or captured_dt
    learning_cfg = settings.get("learning") or {}
    scanner_cfg = settings.get("scanner") or {}
    try:
        max_outcome_delay_seconds = max(
            0.0, float(learning_cfg.get("max_outcome_delay_seconds", 300.0))
        )
    except (TypeError, ValueError):
        max_outcome_delay_seconds = 300.0
    try:
        default_hold_minutes = max(1, int(scanner_cfg.get("hold_minutes", 60) or 60))
    except (TypeError, ValueError):
        default_hold_minutes = 60
    phase_trade_rows = _exact_primary_phase_trades(
        conn,
        phase_started_at=phase_started_at,
        captured_at=captured_at,
        default_hold_minutes=default_hold_minutes,
    )
    direct_closes = []
    timely_direct_closes = []
    reliable_direct_closes = []
    phase_due_direct_closes = 0
    phase_reliable_direct_closes = 0
    phase_timely_direct_closes = 0
    reliable_phase_trade_ids: set[int] = set()
    for row in phase_trade_rows:
        closed_at = _parse_metric_timestamp(row["closed_at"])
        final_close_present = closed_at is not None and closed_at <= captured_dt
        try:
            selected_hold_minutes = max(
                1, int(row["selected_hold_minutes"] or default_hold_minutes)
            )
        except (TypeError, ValueError):
            selected_hold_minutes = default_hold_minutes
        opened_at = _parse_metric_timestamp(row["opened_at"])
        close_deadline = (
            opened_at
            + dt.timedelta(
                minutes=selected_hold_minutes,
                seconds=max_outcome_delay_seconds,
            )
            if opened_at is not None
            else None
        )
        close_due = (
            final_close_present
            or bool(row["selected_hold_outcome_present"])
            or (close_deadline is not None and close_deadline <= captured_dt)
        )
        if close_due:
            phase_due_direct_closes += 1
        eligibility = reliable_paper_label_eligibility_for_trade_row(row)
        reliable_close = bool(
            final_close_present
            and str(row["status"] or "").lower() == "closed"
            and _finite_number(row["pnl_bps"])
            and eligibility.get("paper_label_eligible")
        )
        if reliable_close:
            reliable_phase_trade_ids.add(int(row["id"]))
            phase_reliable_direct_closes += 1
            phase_timely_direct_closes += 1
        if final_close_present and closed_at >= started_dt:
            direct_closes.append(row)
            if reliable_close:
                reliable_direct_closes.append(row)
                timely_direct_closes.append(row)

    # Query the full phase so an early horizon recorded in a prior cycle can
    # become reliable once its trade later obtains a valid final close.
    phase_horizon_rows = conn.execute(
        """
        select outcome.id,outcome.trade_id,outcome.horizon_minutes,
               outcome.measured_at,outcome.target_at,outcome.observed_at,
               outcome.delay_seconds,outcome.pnl_bps,outcome.measurement_status,
               outcome.context_json as outcome_context_json,
               outcome.admission_key as outcome_admission_key,
               outcome.admission_episode_id as outcome_admission_episode_id,
               trade.opened_at,trade.closed_at,trade.pnl_bps as trade_pnl_bps,
               trade.selected_hold_minutes,trade.signal_key,trade.candidate_json,
               trade.review_json,trade.context_json,trade.close_measurement_status,
               trade.status,trade.strategy_lab_id,trade.strategy_lab_version,
               trade.strategy_lineage_root_id,trade.admission_key,
               trade.admission_episode_id
        from paper_trade_outcomes outcome
        join paper_trades trade on trade.id = outcome.trade_id
        where julianday(outcome.measured_at) >= julianday(?)
          and julianday(outcome.measured_at) <= julianday(?)
          and julianday(trade.opened_at) >= julianday(?)
          and julianday(trade.opened_at) <= julianday(?)
        """,
        (phase_started_at, captured_at, phase_started_at, captured_at),
    ).fetchall()
    exact_phase_trade_ids = {int(row["id"]) for row in phase_trade_rows}
    horizon_rows = [
        row
        for row in phase_horizon_rows
        if (_parse_metric_timestamp(row["measured_at"]) or captured_dt) >= started_dt
    ]
    direct_horizons = []
    valid_direct_horizons = []
    synthetic_primary = 0
    synthetic_diagnostic_outcomes = 0
    for row in horizon_rows:
        eligibility = reliable_paper_label_eligibility_for_trade_row(row)
        scope = str(eligibility.get("paper_signal_stats_scope") or "").lower()
        signal = str(row["signal_key"] or "").upper()
        non_direct = scope in {
            "synthetic_research",
            "paper_proxy",
            "frontier_shadow_observation",
        } or signal.startswith(("SYNTHETIC_RESEARCH|", "PAPER_PROXY|"))
        if non_direct:
            valid_measurement = str(row["measurement_status"] or "").lower() == "valid"
            synthetic_diagnostic_outcomes += int(valid_measurement)
            # Diagnostic observations are expected and are excluded by the
            # shared label contract.  Only a non-direct row that somehow
            # becomes eligible for a primary consumer is a campaign breach.
            synthetic_primary += int(
                valid_measurement and bool(eligibility.get("paper_label_eligible"))
            )
            continue
        exact_primary_lineage = (
            scope == "direct"
            and int(row["trade_id"]) in exact_phase_trade_ids
            and row["outcome_admission_key"] == row["admission_key"]
            and row["outcome_admission_episode_id"] == row["admission_episode_id"]
        )
        if not exact_primary_lineage:
            continue
        direct_horizons.append(row)
        if (
            str(row["measurement_status"] or "").lower() == "valid"
            and _finite_number(row["pnl_bps"])
            and int(row["trade_id"]) in reliable_phase_trade_ids
        ):
            valid_direct_horizons.append(row)

    configured_horizons = sorted(
        {
            int(value)
            for value in (learning_cfg.get("horizon_minutes") or [])
            if str(value).strip().lstrip("-").isdigit() and int(value) > 0
        }
    )
    phase_outcomes_by_pair = {
        (int(row["trade_id"]), int(row["horizon_minutes"])): row
        for row in phase_horizon_rows
        if int(row["trade_id"]) in exact_phase_trade_ids
        and int(row["horizon_minutes"]) in configured_horizons
    }
    phase_due_horizon_outcomes = 0
    phase_timely_horizon_outcomes = 0
    for trade in phase_trade_rows:
        opened_at = _parse_metric_timestamp(trade["opened_at"])
        if opened_at is None:
            continue
        trade_id = int(trade["id"])
        for horizon in configured_horizons:
            outcome = phase_outcomes_by_pair.get((trade_id, horizon))
            deadline = opened_at + dt.timedelta(
                minutes=horizon,
                seconds=max_outcome_delay_seconds,
            )
            if outcome is None and deadline > captured_dt:
                continue
            phase_due_horizon_outcomes += 1
            if outcome is None:
                continue
            exact_outcome = (
                outcome["outcome_admission_key"] == outcome["admission_key"]
                and outcome["outcome_admission_episode_id"]
                == outcome["admission_episode_id"]
            )
            if (
                exact_outcome
                and trade_id in reliable_phase_trade_ids
                and str(outcome["measurement_status"] or "").lower() == "valid"
                and _finite_number(outcome["pnl_bps"])
            ):
                phase_timely_horizon_outcomes += 1

    canary_new = _reliable_recovery_canary_pnls(
        conn,
        started_at,
        opened_since=phase_started_at,
        captured_at=captured_at,
    )

    admission_keys_evaluated = int(
        conn.execute(
            """
            select count(distinct transition.admission_key)
            from market_admission_transitions transition
            join paper_admission_queue queue
              on queue.admission_key = transition.admission_key
             and queue.episode_id = transition.episode_id
            where julianday(transition.occurred_at) >= julianday(?)
              and julianday(transition.occurred_at) <= julianday(?)
              and transition.to_stage in ('paper_evaluated','queue:completed_valid')
            """,
            (phase_started_at, captured_at),
        ).fetchone()[0]
        or 0
    )
    opportunity_total, opportunity_coverage = _lineage_coverage(
        conn,
        "opportunities",
        "seen_at",
        started_at,
        extra_where="and artifact.decision <> 'reviewed_not_queued'",
        queue_link_column="opportunity_id",
    )
    order_total, order_coverage = _lineage_coverage(
        conn,
        "execution_orders",
        "created_at",
        started_at,
        queue_link_column="execution_order_id",
    )
    trade_total, trade_coverage = _lineage_coverage(
        conn,
        "paper_trades",
        "opened_at",
        started_at,
        queue_link_column="paper_trade_id",
    )
    opportunity_complete = round(opportunity_total * opportunity_coverage)
    order_complete = round(order_total * order_coverage)
    trade_complete = round(trade_total * trade_coverage)
    lineage_corruption = int(
        conn.execute(
            """
            select count(*)
            from paper_trade_outcomes outcome
            join paper_trades trade on trade.id = outcome.trade_id
            where julianday(outcome.measured_at) >= julianday(?)
              and julianday(outcome.measured_at) <= julianday(?)
              and (coalesce(outcome.admission_key,'') <> coalesce(trade.admission_key,'')
                   or coalesce(outcome.admission_episode_id,'') <>
                      coalesce(trade.admission_episode_id,''))
            """,
            (started_at, captured_at),
        ).fetchone()[0]
        or 0
    )
    terminal = sum(
        str((item.get("execution_review") or item.get("review") or {}).get("decision") or "")
        != "pending_execution"
        for item in reviewed
    )
    terminal_rate = 1.0 if not reviewed else terminal / len(reviewed)

    canary_pnls = _reliable_recovery_canary_pnls(
        conn,
        phase_started_at,
        opened_since=phase_started_at,
        captured_at=captured_at,
    )
    sorted_canary = sorted(canary_pnls)
    worst_count = max(1, int(len(sorted_canary) * 0.1)) if sorted_canary else 0
    active_canary_count = int(
        conn.execute(
            """
            select count(*) from strategy_lab_experiments
            where strategy_lab_id=?
              and parent_strategy_lab_id is null
              and experiment_type='market_strategy'
              and compile_status='compiled'
              and status in (
                  'active_testing','needs_more_evidence','needs_contract_revision'
              )
            """,
            (RECOVERY_CANARY_STRATEGY_LAB_ID,),
        ).fetchone()[0]
        or 0
    )
    frontier_summary = frontier_crypto_venues.get("summary") or {}
    db_physical_size_after = _database_storage_footprint(DB_PATH)
    db_size_after = _sqlite_logical_footprint_bytes(conn)
    artifact_names = ((cycle_state.get("campaign_config") or {}).get("health") or {}).get(
        "max_artifact_bytes", {}
    )
    artifact_sizes = {
        name: int((RUNS_DIR / name).stat().st_size) if (RUNS_DIR / name).exists() else 0
        for name in artifact_names
    }
    def countish(value) -> int:
        if isinstance(value, (list, tuple, set, dict)):
            return len(value)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    metrics = {
        "cycle_success": True,
        "exit_code": 0,
        "runtime_seconds": round(float(runtime_seconds), 3),
        "peak_rss_mb": _current_peak_rss_mb(),
        "db_growth_bytes": max(0, int(db_size_after) - int(db_size_before)),
        "db_footprint_start_bytes": int(db_size_before),
        "db_footprint_start_at": started_at,
        "db_footprint_bytes": int(db_size_after),
        "db_physical_footprint_bytes": int(db_physical_size_after),
        "artifact_sizes": artifact_sizes,
        "terminal_opportunity_rate": terminal_rate,
        "new_admission_keys_paper_evaluated": admission_keys_evaluated,
        "new_exact_attributed_admission_keys_paper_evaluated": admission_keys_evaluated,
        "phase_distinct_exact_attributed_admission_keys_paper_evaluated": (
            admission_keys_evaluated
        ),
        "new_direct_closes": len(direct_closes),
        "new_valid_direct_closes": len(reliable_direct_closes),
        "new_reliable_direct_closes": len(reliable_direct_closes),
        "new_timely_direct_closes": len(timely_direct_closes),
        "phase_due_direct_closes": phase_due_direct_closes,
        "phase_reliable_direct_closes": phase_reliable_direct_closes,
        "phase_timely_direct_closes": phase_timely_direct_closes,
        "new_horizon_outcomes": len(direct_horizons),
        "new_valid_horizon_outcomes": len(valid_direct_horizons),
        "new_timely_horizon_outcomes": len(valid_direct_horizons),
        "phase_due_horizon_outcomes": phase_due_horizon_outcomes,
        "phase_timely_horizon_outcomes": phase_timely_horizon_outcomes,
        "opportunity_lineage_total": opportunity_total,
        "opportunity_lineage_coverage": opportunity_coverage,
        "new_opportunity_lineage_records": opportunity_total,
        "new_opportunity_lineage_complete": opportunity_complete,
        "execution_order_lineage_total": order_total,
        "execution_order_lineage_coverage": order_coverage,
        "new_order_lineage_records": order_total,
        "new_order_lineage_complete": order_complete,
        "paper_trade_lineage_total": trade_total,
        "paper_trade_lineage_coverage": trade_coverage,
        "new_trade_lineage_records": trade_total,
        "new_trade_lineage_complete": trade_complete,
        "lineage_corruption_count": lineage_corruption,
        "synthetic_or_proxy_primary_labels": synthetic_primary,
        "new_synthetic_proxy_primary": synthetic_primary,
        "new_synthetic_proxy_diagnostic_outcomes": synthetic_diagnostic_outcomes,
        "new_canary_valid_labels": len(canary_new),
        "new_canary_reliable_direct_labels": len(canary_new),
        "phase_canary_reliable_direct_labels": len(canary_pnls),
        "active_canary_count": active_canary_count,
        "canary_reliable_labels": len(canary_pnls),
        "canary_avg_net_pnl_bps": (
            sum(canary_pnls) / len(canary_pnls) if canary_pnls else None
        ),
        "canary_win_rate": (
            sum(value > 0 for value in canary_pnls) / len(canary_pnls) if canary_pnls else None
        ),
        "canary_worst_decile_bps": (
            sum(sorted_canary[:worst_count]) / worst_count if worst_count else None
        ),
        "frontier_observation_count": countish(
            frontier_summary.get("observation_count")
            or frontier_summary.get("observations")
            or 0
        ),
        "reachable_venue_count": countish(
            frontier_summary.get("reachable_venue_count")
            or frontier_summary.get("reachable_venues")
            or 0
        ),
    }
    for env_name, metric_name in (
        ("RADAR_BOUNDED_SUPERVISOR_COUNT", "supervisor_count"),
        ("RADAR_BOUNDED_CHILD_COUNT", "child_count"),
        ("RADAR_FORBIDDEN_WORKER_COUNT", "forbidden_worker_count"),
    ):
        raw = os.environ.get(env_name)
        if raw is not None:
            metrics[metric_name] = int(raw)
    return metrics


def _terminal_execution_review(review: dict, execution: dict, decision: str) -> dict:
    order = execution.get("order") if isinstance(execution.get("order"), dict) else {}
    return {
        **review,
        "decision": decision,
        "intended_decision": review.get("intended_decision") or review.get("decision"),
        "execution_status": order.get("status") or decision,
        "execution_order_id": execution.get("order_id"),
        "execution_reason": order.get("shadow_reason") or order.get("status") or decision,
    }


def _attach_execution_review(item: dict, execution_review: dict) -> None:
    """Record the terminal execution state without erasing admission intent."""

    item["execution_review"] = execution_review


def _select_runtime_strategy_lab_candidates(candidates: list[dict], settings: dict) -> tuple[list[dict], dict]:
    cfg = settings.get("strategy_lab", {})
    enabled = bool(cfg.get("runtime_candidate_filters_enabled", True))
    if not enabled:
        return [], _strategy_lab_runtime_selection_summary(candidates)

    max_selected = int(cfg.get("runtime_max_candidates_per_loop", cfg.get("max_candidates_per_loop", 25)))
    selected = []
    skipped = 0
    for candidate in candidates:
        if not _is_strategy_lab_runtime_candidate(candidate):
            skipped += 1
            continue
        if not _strategy_lab_surface_rank_eligible(candidate):
            skipped += 1
            continue
        if candidate.get("enabled") is False:
            skipped += 1
            continue
        selected.append(candidate)
        if len(selected) >= max_selected:
            break

    summary = _strategy_lab_runtime_selection_summary(
        candidates,
        runtime_selection_mode="lab_generation",
        generated_count=len(candidates),
        accepted_count=len(selected),
        skipped_non_candidate_filter_count=skipped,
    )
    summary["runtime_candidate_filters_enabled"] = True
    return selected, summary


def _build_expansion_map(
    frontier_crypto_venues: dict,
    route_resolver_report: dict,
    prediction_summary: dict,
    global_market_discovery_scan: dict | None = None,
    strategy_lab_generation: dict | None = None,
    strategy_lab_runtime: dict | None = None,
    public_market_adapters: dict | None = None,
    adapter_capabilities: dict | None = None,
) -> dict:
    frontier_summary = (frontier_crypto_venues or {}).get("summary", {})
    frontier_expansion = frontier_summary.get("expansion_map", {})
    route_intelligence = (route_resolver_report or {}).get("route_intelligence", {})
    fx_path = RUNS_DIR / "regional_fx_reference_latest.json"
    regional_fx = {}
    if fx_path.exists():
        try:
            regional_fx = json.loads(fx_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            regional_fx = {"status": "unreadable", "path": str(fx_path)}
    return {
        "frontier_crypto": frontier_expansion,
        "global_market_discovery_scan": global_market_discovery_scan or {},
        "regional_fx_reference": {
            "reference_count": regional_fx.get("reference_count", 0),
            "stale_count": regional_fx.get("stale_count", 0),
            "from_cache": regional_fx.get("from_cache"),
            "provider_status": regional_fx.get("provider_status", []),
            "report": str(fx_path),
        },
        "prediction_markets": prediction_summary,
        "route_intelligence": {
            "blocker_counts": route_intelligence.get("blocker_counts", {}),
            "spot_borrow_assets": route_intelligence.get("spot_borrow_assets", {}),
            "route_decision_pack": route_intelligence.get("route_decision_pack", {}),
            "interesting_but_not_executable_count": route_intelligence.get("interesting_but_not_executable_count", 0),
            "potentially_executable_soon_count": route_intelligence.get("potentially_executable_soon_count", 0),
            "report": str(RUNS_DIR / "route_intelligence_report.md"),
        },
        "strategy_lab": _strategy_lab_runtime_summary(strategy_lab_generation, strategy_lab_runtime),
        "public_market_adapters": (public_market_adapters or {}).get("summary", {}),
        "adapter_capabilities": (adapter_capabilities or {}).get("summary", {}),
        "reports": {
            "frontier": str(RUNS_DIR / "frontier_crypto_venues_report.md"),
            "global_market_discovery_scan": str(RUNS_DIR / "global_market_discovery_scan_report.md"),
            "regional_fx_reference": str(fx_path),
            "prediction_markets": str(RUNS_DIR / "prediction_markets_latest.json"),
            "route_intelligence": str(RUNS_DIR / "route_intelligence_report.md"),
            "market_admission": str(RUNS_DIR / "market_admission_report.md"),
            "public_market_adapters": str(RUNS_DIR / "public_market_adapters_report.md"),
            "adapter_capabilities": str(RUNS_DIR / "adapter_capability_inventory.md"),
        },
    }


def _required_global_proxy_instruments(required: dict[str, set[str]]) -> set[str]:
    instruments = set(required.get("global_proxy_momentum", set()))
    instruments.update(required.get("global_proxy_shock_reversal", set()))
    return instruments


class _StorageDueOutcomeProvider:
    """Small adapter that keeps the collector independent from SQLite."""

    def __init__(self, conn: sqlite3.Connection, settings: dict):
        self.conn = conn
        self.settings = settings
        self.loaded_targets: list[dict[str, object]] = []

    def load_due_instruments(self, *, limit: int) -> list[dict[str, object]]:
        self.loaded_targets = load_due_paper_outcome_targets(
            self.conn,
            self.settings,
            limit=limit,
        )
        return list(self.loaded_targets)


def _collect_and_persist_due_outcomes(
    conn: sqlite3.Connection,
    settings: dict,
    *,
    collector=collect_due_outcome_prices,
) -> dict[str, object]:
    """Collect credential-free historical labels and journal them atomically.

    The due-window cursor advances for a public fetch success or failure, but
    only in the same commit as any accepted observations.  A local persistence
    failure rolls everything back so the evidence is fetched again rather
    than silently skipped.
    """

    provider = _StorageDueOutcomeProvider(conn, settings)
    try:
        report = collector(provider, settings=settings)
    except Exception as exc:  # noqa: BLE001 - collection must fail closed per cycle
        return {
            "enabled": bool(
                (settings.get("paper_due_outcome_collection") or {}).get(
                    "enabled", False
                )
            ),
            "status": "collector_failed",
            "error": type(exc).__name__,
            "loaded_due_count": len(provider.loaded_targets),
            "record_count": 0,
            "funding_coverage_count": 0,
            "attempted_window_count": 0,
        }

    records = list(report.get("records") or [])
    complete_funding = [
        item
        for item in list(report.get("funding_coverage") or [])
        if isinstance(item, dict) and item.get("coverage_status") == "complete"
    ]
    attempted_keys = {
        str(value)
        for value in list(report.get("attempted_window_keys") or [])
        if str(value or "").strip()
    }
    attempted_targets = [
        target
        for target in provider.loaded_targets
        if str(target.get("due_window_key") or "") in attempted_keys
    ]
    price_persistence: dict[str, object] = {
        "accepted": 0,
        "duplicates": 0,
        "rejected": 0,
    }
    funding_persistence: dict[str, object] = {
        "accepted": 0,
        "duplicates": 0,
        "rejected": 0,
    }
    attempted_window_count = 0
    persistence_error: str | None = None
    try:
        price_persistence = record_paper_price_observations(
            conn,
            records,
            commit=False,
        )
        if int(price_persistence.get("rejected") or 0):
            raise ValueError("price_observation_persistence_rejected")
        funding_persistence = record_paper_funding_coverage(
            conn,
            complete_funding,
            commit=False,
        )
        if int(funding_persistence.get("rejected") or 0):
            raise ValueError("funding_coverage_persistence_rejected")
        attempted_window_count = mark_due_paper_outcome_windows_attempted(
            conn,
            attempted_targets,
            attempted_at=dt.datetime.now(dt.timezone.utc),
            commit=False,
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - retry the same window next cycle
        conn.rollback()
        persistence_error = type(exc).__name__
        attempted_window_count = 0

    return {
        "enabled": bool(report.get("enabled")),
        "status": "persisted" if persistence_error is None else "persistence_failed",
        "error": persistence_error,
        "loaded_due_count": int(report.get("loaded_due_count") or 0),
        "unique_instrument_count": int(report.get("unique_instrument_count") or 0),
        "fetched_instrument_count": int(report.get("fetched_instrument_count") or 0),
        "funding_fetch_count": int(report.get("funding_fetch_count") or 0),
        "total_public_request_count": int(report.get("total_public_request_count") or 0),
        "record_count": len(records),
        "funding_event_count": len(list(report.get("funding_events") or [])),
        "funding_coverage_count": len(complete_funding),
        "attempted_window_count": attempted_window_count,
        "rejection_count": len(list(report.get("rejections") or [])),
        "deferred_outcome_count": len(list(report.get("deferred_outcome_keys") or [])),
        "price_persistence": price_persistence,
        "funding_persistence": funding_persistence,
        "limits": report.get("limits") or {},
    }


def run_once(settings: dict) -> dict:
    cycle_started = time.perf_counter()
    db_size_before = _database_logical_footprint(DB_PATH)
    with connect() as conn:
        settings, campaign_cycle = apply_campaign_controls(conn, settings)
        bounded_recovery = bool(campaign_cycle.get("enabled"))
        auxiliary_policy = _auxiliary_runtime_policy(settings)
        capabilities = settings["account_capabilities"]
        scan_cfg = settings["scanner"]
        risk_cfg = settings["risk"]
        allow_short_spot = bool(capabilities.get("spot_borrow", False))
        pending_opportunity_reconciliation = reconcile_pending_opportunities(conn)
        paper_queue_reconciliation_before = reconcile_paper_admission_queue(conn, settings)
        required = open_trade_instruments(conn)
        required_okx = set(required.get("perp_funding_basis", set()))
        required_okx.update(open_signal_trial_instruments(conn, "OKX|perp_funding_basis"))
        batches = []
        admission_observations = []
        okx_batch = build_okx_scan_batch(
            scan_cfg["scan_universe"],
            allow_short_spot=allow_short_spot,
            required_inst_ids=required_okx,
            settings=settings,
        )
        okx_signal_research = {}
        okx_candidates = okx_batch.candidates
        if settings.get("okx_signal_research", {}).get("enabled", True):
            okx_candidates, okx_signal_research = run_okx_signal_research(
                conn,
                settings,
                okx_batch.candidates,
                okx_batch.observations,
                scan_id=okx_batch.generated_at,
            )
        batches.append(okx_batch)
        admission_observations.extend(okx_batch.observations)
        candidates = list(okx_candidates)
        if scan_cfg.get("enable_global_proxy_scan", False):
            required_global_proxy = _required_global_proxy_instruments(required)
            global_batch = build_global_proxy_scan_batch(
                settings,
                limit=int(scan_cfg.get("global_review_top", 40)),
                required_inst_ids=required_global_proxy,
            )
            batches.append(global_batch)
            candidates.extend(global_batch.candidates)
            candidates.sort(key=lambda row: row["score"], reverse=True)
        public_market_adapters = {
            "summary": {"enabled": False, "status": "disabled_by_scanner_config"}
        }
        adapter_capabilities = {
            "summary": {"enabled": False, "status": "disabled_by_scanner_config"},
            "specs": [],
        }
        if scan_cfg.get("enable_public_market_adapter_scan", True):
            public_adapter_batch = build_public_adapter_scan_batch(settings)
            public_market_adapters = public_adapter_batch.metadata.get("public_market_adapters", {})
            batches.append(public_adapter_batch)
            candidates.extend(public_adapter_batch.candidates)
            admission_observations.extend(public_adapter_batch.observations)
            adapter_capabilities = reconcile_adapter_specs(conn)
        global_market_discovery_scan = {}
        if scan_cfg.get("enable_global_market_discovery_scan", True) and settings.get(
            "global_market_discovery_scanner", {}
        ).get("enabled", True):
            global_discovery_batch = build_global_market_discovery_scan_batch(
                settings,
                limit=int(scan_cfg.get("global_market_discovery_review_top", 35)),
                required_inst_ids=required.get("global_market_discovery_proxy", set()),
            )
            batches.append(global_discovery_batch)
            candidates.extend(global_discovery_batch.candidates)
            candidates.sort(key=lambda row: row["score"], reverse=True)
            global_market_discovery_scan = global_discovery_batch.metadata.get("global_market_discovery_scan", {})
            admission_observations.extend(global_discovery_batch.candidates)
        if scan_cfg.get("enable_prediction_market_scan", False):
            prediction_batch = build_prediction_market_scan_batch(
                settings,
                limit=int(scan_cfg.get("prediction_review_top", 40)),
                required_inst_ids=required.get("prediction_market_probability", set()),
            )
            batches.append(prediction_batch)
            candidates.extend(prediction_batch.candidates)
            candidates.sort(key=lambda row: row["score"], reverse=True)
            prediction_summary = prediction_batch.metadata.get("prediction_market_summary", {})
        else:
            prediction_summary = {}
        venue_health = []
        if scan_cfg.get("enable_crypto_venue_health_scan", False):
            venue_health = scan_crypto_venues()
            write_crypto_venue_health(venue_health)
        frontier_crypto_venues = {}
        frontier_intraday_coverage = {}
        signal_redesign = {}
        if scan_cfg.get("enable_frontier_crypto_adapter_scan", False) and settings.get("frontier_crypto_adapter", {}).get(
            "enabled", True
        ):
            frontier_limit = int(scan_cfg.get("frontier_crypto_review_top", 10))
            redesign_enabled = bool(settings.get("signal_redesign", {}).get("enabled", True))
            frontier_batch = build_frontier_crypto_scan_batch(
                settings,
                limit=frontier_limit,
                required_inst_ids=required.get("frontier_crypto_venue_map", set()),
                conn=conn,
                write_preliminary_report=not redesign_enabled,
            )
            batches.append(frontier_batch)
            frontier_intraday_coverage = dict(
                frontier_batch.metadata.get("intraday_features") or {}
            )
            price_observations = merge_observations(batches)
            if redesign_enabled:
                frontier_candidates, signal_redesign = run_frontier_redesign(
                    conn,
                    settings,
                    frontier_batch.metadata.get("selected_observations", []),
                    price_observations,
                    active_limit=frontier_limit,
                    scan_id=frontier_batch.generated_at,
                )
                frontier_report_summary = signal_redesign.get("frontier_report_summary", {})
                frontier_report_artifact_compaction = signal_redesign.get(
                    "frontier_report_artifact_compaction", {}
                )
                frontier_report_observation_sample = signal_redesign.get(
                    "frontier_report_observation_sample", []
                )
                frontier_report_candidate_sample = signal_redesign.get(
                    "frontier_report_candidate_sample", []
                )
                frontier_batch.observations.extend(
                    normalize_observation(
                        row,
                        source=f"{row.get('venue')} public REST + depth quality",
                    )
                    for row in frontier_batch.metadata.get("selected_observations", [])
                    if row.get("data_status") == "reachable" and float(row.get("last") or 0.0) > 0
                )
            else:
                frontier_candidates = frontier_batch.candidates
                frontier_report_summary = frontier_batch.metadata.get("report_summary", {})
                frontier_report_artifact_compaction = frontier_batch.metadata.get(
                    "report_artifact_compaction", {}
                )
                frontier_report_observation_sample = frontier_batch.metadata.get(
                    "report_observation_sample", []
                )
                frontier_report_candidate_sample = frontier_batch.metadata.get(
                    "report_candidate_sample", []
                )
            candidates.extend(frontier_candidates)
            candidates.sort(key=lambda row: row["score"], reverse=True)
            frontier_crypto_venues = {
                "summary": frontier_report_summary,
                "artifact_compaction": frontier_report_artifact_compaction,
                "observations": frontier_report_observation_sample,
                "candidates": frontier_report_candidate_sample,
                "report": str(FRONTIER_CRYPTO_REPORT_JSON),
            }
            admission_observations.extend(frontier_batch.metadata.get("selected_observations", []))
        price_observations = merge_observations(batches)
        promoted_signal_candidates, promoted_signal_runtime = run_signal_plugins(
            conn,
            price_observations,
            settings,
        )
        if promoted_signal_candidates:
            candidates.extend(promoted_signal_candidates)
            candidates.sort(key=lambda row: row["score"], reverse=True)
        candidates = enrich_candidates(candidates, settings)
        strategy_lab_candidates, strategy_lab_generation = generate_strategy_lab_candidates(
            conn,
            settings,
            candidates,
            price_observations,
            runtime_diagnostics={"frontier_crypto_intraday": frontier_intraday_coverage},
        )
        strategy_lab_generation["promoted_signal_runtime"] = promoted_signal_runtime
        selected_strategy_lab_candidates, strategy_lab_runtime = _select_runtime_strategy_lab_candidates(
            strategy_lab_candidates,
            settings,
        )
        if selected_strategy_lab_candidates:
            candidates.extend(enrich_candidates(selected_strategy_lab_candidates, settings))
            candidates.sort(key=lambda row: row["score"], reverse=True)
        candidates, strategy_reliability = apply_strategy_reliability(candidates, settings, conn=conn)
        candidates = [prepare_candidate_for_exploration(candidate, settings) for candidate in candidates]
        route_resolver_report = write_route_resolver_report(candidates, settings)
        expansion_map = _build_expansion_map(
            frontier_crypto_venues,
            route_resolver_report,
            prediction_summary,
            global_market_discovery_scan,
            strategy_lab_generation,
            strategy_lab_runtime,
            public_market_adapters,
            adapter_capabilities,
        )
        if bounded_recovery:
            self_improvement_open_pack = {
                "enabled": False,
                "reason": "bounded_recovery_profile",
                "summary": {"candidate_count": len(candidates)},
            }
        else:
            self_improvement_open_pack = build_open_pack_report(
                conn,
                settings,
                candidates=candidates[:250],
                prediction_summary=prediction_summary,
                expansion_map=expansion_map,
            )
            write_open_pack_reports(self_improvement_open_pack)
        due_outcome_collection = _collect_and_persist_due_outcomes(conn, settings)
        horizon_outcomes = record_due_horizon_outcomes(conn, price_observations, settings)
        closed = close_due_trades(
            conn,
            price_observations,
            int(scan_cfg["hold_minutes"]),
            settings=settings,
        )
        stats = update_signal_stats(conn, settings) if settings["learning"]["enabled"] else {}
        yahoo_counterfactual = (
            {"enabled": False, "reason": "bounded_crypto_recovery_profile"}
            if bounded_recovery
            else run_yahoo_counterfactual_analysis(conn, settings)
        )
        reliability_cards = (
            {"enabled": False, "reason": "bounded_recovery_profile"}
            if bounded_recovery
            else cross_context_reliability(conn)
        )
        if settings.get("strategy_lab", {}).get("enabled", False):
            strategy_lab_evaluation = evaluate_strategy_lab(conn, settings)
            strategy_lab_report = write_strategy_lab_reports(
                conn, strategy_lab_generation, strategy_lab_evaluation
            )
        else:
            strategy_lab_evaluation = {
                "enabled": False,
                "reason": "strategy_lab_disabled",
                "evaluated": [],
            }
            strategy_lab_report = {
                "enabled": False,
                "reason": "strategy_lab_disabled",
                "summary": {"enabled": False},
            }
        adjustments = load_adjustments(conn)
        llm_recommendations_ingested = (
            [] if bounded_recovery else ingest_llm_recommendations(conn, settings)
        )
        auto_improvement = (
            {"enabled": False, "reason": "bounded_recovery_profile"}
            if bounded_recovery
            else run_auto_improvement(conn, settings, include_code_changes=False)
        )
        signal_safety_governor = (
            {"enabled": False, "reason": "bounded_exact_queue_contract"}
            if bounded_recovery
            else run_signal_safety_governor(conn, settings)
        )
        contextual_failure_filters = (
            {
                "enabled": False,
                "reason": "bounded_exact_queue_contract",
                "cross_context_observations": [],
            }
            if bounded_recovery
            else run_contextual_failure_filters(conn, settings)
        )
        candidates = annotate_candidates_with_cross_context_diagnostics(
            candidates,
            contextual_failure_filters.get("cross_context_observations", []),
            settings,
        )
        if bounded_recovery:
            context_drag_stats = {}
            paper_context_drag = {
                "enabled": False,
                "reason": "bounded_exact_queue_contract",
            }
            policies = []
        else:
            context_drag_stats = context_drag_statistics(conn, settings)
            candidates = apply_context_drag_overlay(candidates, context_drag_stats, settings)
            paper_context_drag = context_drag_report(context_drag_stats, candidates)
            policies = active_signal_policies(conn)
        review_limit = int(scan_cfg["review_top"])
        canary_review_isolation = bool(
            bounded_recovery
            and str(
                (settings.get("paper_expansion") or {}).get("runtime_phase") or ""
            )
            == "strategy_lab_canary"
        )
        review_source_candidates = (
            [
                candidate
                for candidate in candidates
                if _is_recovery_canary_candidate(candidate)
            ][:1]
            if canary_review_isolation
            else candidates
        )
        paper_queue_cfg = paper_admission_queue_config(settings)
        queue_new_work_allowed = not campaign_cycle.get("enabled") or str(
            campaign_cycle.get("run_status") or "running"
        ) == "running"
        paper_queue_enqueue = (
            enqueue_paper_admission_candidates(
                conn, settings, review_source_candidates
            )
            if queue_new_work_allowed and review_limit > 0
            else {
                "enabled": paper_queue_cfg["enabled"],
                "considered": len(review_source_candidates),
                "enqueued": 0,
                "status": "new_work_paused",
            }
        )
        queued_review_candidates = (
            select_paper_admission_candidates(
                conn,
                settings,
                limit=review_limit,
                paper_fill_slots_by_lane=_paper_lane_limits(
                    int(scan_cfg["max_new_paper_trades"])
                ),
                required_lineage_root=(
                    RECOVERY_CANARY_STRATEGY_LAB_ID
                    if canary_review_isolation
                    else None
                ),
            )
            if queue_new_work_allowed and review_limit > 0
            else []
        )
        for candidate in queued_review_candidates:
            candidate.setdefault("_hunter_bucket", "paper_admission_queue")
            candidate.setdefault("_hunter_directive_id", None)
            candidate.setdefault("_hunter_allocation_reason", "bounded_paper_admission_queue")
        queued_identities = {
            _paper_queue_identity(candidate) for candidate in queued_review_candidates
        }
        selected_queue_claims = {
            claim
            for candidate in queued_review_candidates
            if all(claim := _paper_queue_claim_identity(candidate))
        }
        remaining_candidates = [
            candidate
            for candidate in review_source_candidates
            if _paper_queue_identity(candidate) not in queued_identities
        ]
        reserved_lab_candidates, strategy_lab_review_reserve = _reserve_strategy_lab_review_candidates(
            remaining_candidates,
            settings,
            max(0, review_limit - len(queued_review_candidates)),
        )
        reserved_lab_identities = {
            _paper_queue_identity(candidate) for candidate in reserved_lab_candidates
        }
        non_lab_candidates = fair_lineage_order(
            [
                candidate
                for candidate in remaining_candidates
                if not candidate.get("strategy_lab_id")
                and _paper_queue_identity(candidate) not in reserved_lab_identities
            ],
            int(time.time() // 60),
            settings,
        )
        hunter_review_slots = max(
            0,
            review_limit - len(queued_review_candidates) - len(reserved_lab_candidates),
        )
        hunter_cfg = settings.get("hunter_allocation", {})
        if hunter_cfg.get("enabled", True) and hunter_cfg.get("apply_to_candidate_review", True):
            hunter_review_candidates, hunter_allocation = allocate_candidate_review(
                non_lab_candidates,
                open_hunter_directives(conn),
                hunter_review_slots,
                buckets=hunter_cfg.get("buckets"),
            )
        else:
            hunter_review_candidates = [
                dict(candidate, _hunter_bucket="disabled")
                for candidate in non_lab_candidates[:hunter_review_slots]
            ]
            hunter_allocation = {
                "enabled": False,
                "selected_count": len(hunter_review_candidates),
                "selected_by_bucket": {"disabled": len(hunter_review_candidates)},
                "slot_targets": {},
                "directive_counts": {},
                "minimum_exploration_floor": 0,
            }
        review_candidates = (
            queued_review_candidates + reserved_lab_candidates + hunter_review_candidates
        )[:review_limit]
        hunter_allocation["selected_count"] = len(review_candidates)
        hunter_allocation["canary_review_isolation"] = {
            "enabled": canary_review_isolation,
            "allowed_strategy_lab_id": (
                RECOVERY_CANARY_STRATEGY_LAB_ID
                if canary_review_isolation
                else None
            ),
            "eligible_candidate_count": len(review_source_candidates),
        }
        hunter_allocation["strategy_lab_review_reserve"] = strategy_lab_review_reserve
        hunter_allocation["paper_admission_queue_reserve"] = {
            "configured_slots": min(30, int(paper_queue_cfg["max_select_per_cycle"])),
            "reserved_count": len(queued_review_candidates),
            "by_lane": {
                lane: sum(
                    candidate.get("_paper_admission_lane") == lane
                    for candidate in queued_review_candidates
                )
                for lane in ("evidence", "discovery")
            },
        }
        hunter_allocation = write_hunter_allocation_report(hunter_allocation, review_candidates, candidates, settings)

        reviewed = []
        opened = []
        for candidate in review_candidates:
            review = review_candidate(candidate, settings, adjustments, policies=policies)
            queued_for_execution = (
                _paper_queue_claim_identity(candidate) in selected_queue_claims
            )
            persisted_review = (
                _pending_execution_review(review)
                if not paper_queue_cfg["enabled"] or queued_for_execution
                else _not_queued_execution_review(review)
            )
            opportunity_id = save_opportunity(conn, candidate, persisted_review)
            record_review_policy_effects(conn, review)
            item = {
                "candidate": candidate,
                "review": review,
                "opportunity_id": opportunity_id,
                "paper_queue_claim_verified": queued_for_execution,
            }
            if paper_queue_cfg["enabled"] and not queued_for_execution:
                _attach_execution_review(item, persisted_review)
            reviewed.append(item)

        execution_queue = (
            [
                item
                for item in reviewed
                if item.get("paper_queue_claim_verified")
            ]
            if paper_queue_cfg["enabled"]
            else reviewed
        )
        if exploration_enabled(settings) and execution_queue and not paper_queue_cfg["enabled"]:
            # Rotate the starting lineage each minute so a stable top-ranked set
            # cannot permanently monopolize a bounded paper-execution budget.
            offset = int(time.time() // 60) % len(execution_queue)
            execution_queue = execution_queue[offset:] + execution_queue[:offset]

        paper_fill_count = 0
        paper_observation_count = 0
        paper_fill_limit = int(scan_cfg["max_new_paper_trades"])
        paper_observation_limit = max(
            0,
            int(scan_cfg.get("max_new_paper_observations", paper_fill_limit)),
        )
        paper_fill_lane_limits = _paper_lane_limits(paper_fill_limit)
        paper_observation_lane_limits = _paper_lane_limits(paper_observation_limit)
        paper_fill_count_by_lane = {"evidence": 0, "discovery": 0}
        paper_observation_count_by_lane = {"evidence": 0, "discovery": 0}
        for item in execution_queue:
            candidate = item["candidate"]
            review = item["review"]
            if review["decision"] not in _PAPER_APPROVAL_DECISIONS:
                continue
            paper_lane = str(candidate.get("_paper_admission_lane") or "discovery")
            if paper_lane not in paper_fill_count_by_lane:
                paper_lane = "discovery"
            open_trade_capacity_available = count_open_trades(conn) < int(
                risk_cfg["max_open_paper_trades"]
            )
            fill_capacity_reason = (
                "max_new_paper_trades"
                if paper_fill_count >= paper_fill_limit
                else f"max_new_paper_trades_{paper_lane}"
                if paper_fill_count_by_lane[paper_lane] >= paper_fill_lane_limits[paper_lane]
                else "max_open_paper_trades"
                if not open_trade_capacity_available
                else None
            )
            lineage_root = (
                _strategy_lab_lineage_root_id(candidate)
                if candidate.get("strategy_lab_id")
                else None
            )
            if has_open_trade(
                conn,
                candidate["inst_id"],
                candidate["direction"],
                strategy_lineage_root=lineage_root,
            ):
                duplicate = dict(
                    review,
                    decision="reject_duplicate_open_exposure",
                    hard_blocks=list(review.get("hard_blocks") or []) + ["duplicate open exposure"],
                )
                update_opportunity_decision(
                    conn,
                    item["opportunity_id"],
                    "reject_duplicate_open_exposure",
                    duplicate,
                )
                _attach_execution_review(item, duplicate)
                continue
            try:
                execution = execute_order(
                    conn,
                    candidate,
                    review,
                    settings,
                    opportunity_id=item["opportunity_id"],
                    record_shadow_observation=(
                        paper_observation_count < paper_observation_limit
                        and paper_observation_count_by_lane[paper_lane]
                        < paper_observation_lane_limits[paper_lane]
                    ),
                    allow_paper_fill=fill_capacity_reason is None,
                )
            except Exception as exc:
                failed = {
                    **review,
                    "decision": "execution_error",
                    "intended_decision": review.get("decision"),
                    "execution_status": "error",
                    "execution_error": f"{type(exc).__name__}: {exc}"[:500],
                }
                update_opportunity_decision(
                    conn,
                    item["opportunity_id"],
                    "execution_error",
                    failed,
                )
                _attach_execution_review(item, failed)
                continue

            if execution.get("shadow_observation_recorded"):
                paper_observation_count += 1
                paper_observation_count_by_lane[paper_lane] += 1
                terminal_status = str((execution.get("order") or {}).get("status") or "shadow_observed")
                terminal = _terminal_execution_review(review, execution, terminal_status)
                terminal["shadow_observation_id"] = execution.get("shadow_observation_id")
                update_opportunity_decision(
                    conn,
                    item["opportunity_id"],
                    terminal_status,
                    terminal,
                )
                _attach_execution_review(item, terminal)
                continue
            if execution.get("shadow_observation_deferred"):
                deferred = _terminal_execution_review(
                    review,
                    execution,
                    "deferred_observation_capacity",
                )
                deferred["deferral_reason"] = "max_new_paper_observations"
                update_opportunity_decision(
                    conn,
                    item["opportunity_id"],
                    "deferred_observation_capacity",
                    deferred,
                )
                _attach_execution_review(item, deferred)
                continue

            if execution.get("paper_fill_deferred"):
                deferred = _terminal_execution_review(
                    review,
                    execution,
                    "deferred_capacity",
                )
                deferred["deferral_reason"] = (
                    fill_capacity_reason or "paper_fill_capacity_unavailable"
                )
                update_opportunity_decision(
                    conn,
                    item["opportunity_id"],
                    "deferred_capacity",
                    deferred,
                )
                _attach_execution_review(item, deferred)
                continue

            actual_paper_fill = bool(execution.get("paper_filled") and execution.get("fills"))
            paper_observation_ready = bool(
                execution.get("paper_observation_ready")
                or (execution.get("paper_filled") and not execution.get("fills"))
            )
            if not actual_paper_fill:
                if (
                    paper_observation_ready
                    and paper_observation_count < paper_observation_limit
                    and paper_observation_count_by_lane[paper_lane]
                    < paper_observation_lane_limits[paper_lane]
                    and open_trade_capacity_available
                ):
                    trade_id = open_paper_trade(conn, candidate, review, execution=execution, settings=settings)
                    paper_observation_count += 1
                    paper_observation_count_by_lane[paper_lane] += 1
                    terminal = _terminal_execution_review(
                        review,
                        execution,
                        "paper_observation_opened",
                    )
                    update_opportunity_decision(
                        conn,
                        item["opportunity_id"],
                        "paper_observation_opened",
                        terminal,
                    )
                    _attach_execution_review(item, terminal)
                    opened.append(
                        {
                            "id": trade_id,
                            "order_id": execution["order_id"],
                            "order_route_id": execution["order"]["route_id"],
                            "inst_id": candidate["inst_id"],
                            "direction": candidate["direction"],
                            "learned_score": review["learned_score"],
                            "confidence": review["confidence"],
                            "net_edge_bps_estimate": review["net_edge_bps_estimate"],
                            "gross_edge_bps": review.get("gross_edge_bps"),
                            "modeled_cost_bps": review.get("modeled_cost_bps"),
                            "net_edge_bps": review.get("net_edge_bps"),
                            "freshness_minutes": review.get("freshness_minutes"),
                            "gating_reason": execution["order"].get("shadow_reason"),
                            "route_id": review.get("route_id"),
                            "effective_route_id": review.get("effective_route_id"),
                            "route_status": execution["order"].get("status"),
                            "route_alternative_used": review.get("route_alternative_used"),
                            "execution_semantics": execution["order"].get("execution_semantics"),
                            "proxy_not_live_equivalent": execution["order"].get("proxy_not_live_equivalent", False),
                            "signal_stats_scope": execution["order"].get("signal_stats_scope", "synthetic_research"),
                            "strategy_lab_id": candidate.get("strategy_lab_id"),
                            "paper_filled": False,
                        }
                    )
                elif paper_observation_ready:
                    deferred = _terminal_execution_review(
                        review,
                        execution,
                        "deferred_observation_capacity",
                    )
                    deferred["deferral_reason"] = (
                        "max_open_paper_trades"
                        if not open_trade_capacity_available
                        else "max_new_paper_observations"
                    )
                    update_opportunity_decision(
                        conn,
                        item["opportunity_id"],
                        "deferred_observation_capacity",
                        deferred,
                    )
                    _attach_execution_review(item, deferred)
                else:
                    terminal_status = str(
                        (execution.get("order") or {}).get("status")
                        or "not_paper_filled"
                    )
                    terminal = _terminal_execution_review(
                        review,
                        execution,
                        terminal_status,
                    )
                    update_opportunity_decision(
                        conn,
                        item["opportunity_id"],
                        terminal_status,
                        terminal,
                    )
                    _attach_execution_review(item, terminal)
                continue
            trade_id = execution.get("paper_trade_id")
            if trade_id is None:
                trade_id = open_paper_trade(
                    conn,
                    candidate,
                    review,
                    execution=execution,
                    settings=settings,
                )
            record_open_policy_effects(conn, review)
            paper_fill_count += 1
            paper_fill_count_by_lane[paper_lane] += 1
            terminal = _terminal_execution_review(review, execution, "paper_filled")
            update_opportunity_decision(
                conn,
                item["opportunity_id"],
                "paper_filled",
                terminal,
            )
            _attach_execution_review(item, terminal)
            opened.append(
                {
                    "id": trade_id,
                    "order_id": execution["order_id"],
                    "order_route_id": execution["order"]["route_id"],
                    "inst_id": candidate["inst_id"],
                    "direction": candidate["direction"],
                    "learned_score": review["learned_score"],
                    "confidence": review["confidence"],
                    "net_edge_bps_estimate": review["net_edge_bps_estimate"],
                    "gross_edge_bps": review.get("gross_edge_bps"),
                    "modeled_cost_bps": review.get("modeled_cost_bps"),
                    "net_edge_bps": review.get("net_edge_bps"),
                    "freshness_minutes": review.get("freshness_minutes"),
                    "gating_reason": review.get("gating_reason"),
                    "route_id": review.get("route_id"),
                    "effective_route_id": review.get("effective_route_id"),
                    "route_status": review.get("route_status"),
                    "route_alternative_used": review.get("route_alternative_used"),
                    "execution_semantics": execution["order"].get("execution_semantics"),
                    "proxy_not_live_equivalent": execution["order"].get("proxy_not_live_equivalent", False),
                    "signal_stats_scope": execution["order"].get("signal_stats_scope", "direct"),
                    "strategy_lab_id": candidate.get("strategy_lab_id"),
                    "paper_filled": True,
                }
            )

        paper_queue_reconciliation_after = reconcile_paper_admission_queue(conn, settings)
        paper_queue_report = {
            "enabled": paper_queue_cfg["enabled"],
            "enqueue": paper_queue_enqueue,
            "reconciliation_before": paper_queue_reconciliation_before,
            "reconciliation_after": paper_queue_reconciliation_after,
            "selected_for_review": len(queued_review_candidates),
            "fills_by_lane": dict(paper_fill_count_by_lane),
            "shadows_by_lane": dict(paper_observation_count_by_lane),
            "summary": paper_admission_queue_summary(conn, settings),
        }
        accounting_reviewed = _reviewed_for_accounting(reviewed)
        if bounded_recovery:
            paper_exploration = {
                "enabled": False,
                "reason": "bounded_queue_replaces_legacy_exploration",
            }
            paper_exploration_packet = dict(paper_exploration)
        else:
            paper_exploration = write_paper_exploration_report(
                conn,
                settings,
                reviewed=accounting_reviewed,
            )
            paper_exploration_packet = compact_paper_exploration_report(paper_exploration)
        market_admission = run_market_admission_monitor(
            conn,
            settings,
            candidates,
            accounting_reviewed,
            admission_observations,
        )
        market_admission_bridge = run_market_admission_bridge(conn, settings, market_admission)
        market_admission["bridge"] = market_admission_bridge
        market_admission["paper_admission_queue"] = paper_queue_report
        expansion_map["market_admission"] = market_admission.get("summary", {})
        expansion_map["market_admission_bridge"] = market_admission_bridge.get("summary", {})
        expansion_map["paper_admission_queue"] = paper_queue_report.get("summary", {})
        auto_improvement["signal_safety_governor"] = signal_safety_governor
        auto_improvement["contextual_failure_filters"] = contextual_failure_filters
        auto_improvement["signal_redesign"] = signal_redesign
        auto_improvement["okx_signal_research"] = okx_signal_research
        auto_improvement["strategy_reliability"] = strategy_reliability
        auto_improvement["yahoo_counterfactual"] = yahoo_counterfactual
        auto_improvement["cross_context_reliability"] = reliability_cards
        auto_improvement["strategy_lab"] = strategy_lab_report.get("summary", {})
        auto_improvement["market_admission"] = market_admission.get("summary", {})
        auto_improvement["market_admission_bridge"] = market_admission_bridge.get("summary", {})
        auto_improvement["expansion_map"] = expansion_map
        auto_improvement["self_improvement_open_pack"] = self_improvement_open_pack
        auto_improvement["paper_exploration"] = paper_exploration_packet
        auto_improvement["paper_context_drag"] = paper_context_drag
        if not bounded_recovery:
            auto_improvement = write_self_improvement_reports(conn, auto_improvement)
        summary = performance_summary(conn)
        maintenance = {
            **perform_maintenance(conn, settings),
            "pending_opportunity_reconciliation": pending_opportunity_reconciliation,
        }
        payload = {
            "mode": settings["mode"],
            "closed": closed,
            "horizon_outcomes": horizon_outcomes,
            "due_outcome_collection": due_outcome_collection,
            "crypto_venue_health": venue_health,
            "frontier_crypto_venues": frontier_crypto_venues,
            "global_market_discovery_scan": global_market_discovery_scan,
            "public_market_adapters": public_market_adapters,
            "adapter_capabilities": adapter_capabilities,
            "signal_redesign": signal_redesign,
            "okx_signal_research": okx_signal_research,
            "strategy_reliability": strategy_reliability,
            "yahoo_counterfactual": yahoo_counterfactual,
            "cross_context_reliability": reliability_cards,
            "strategy_lab": strategy_lab_report.get("summary", {}),
            "market_admission": market_admission,
            "market_admission_bridge": market_admission_bridge,
            "paper_admission_queue": paper_queue_report,
            "opened": opened,
            "paper_exploration": paper_exploration_packet,
            "summary": summary,
            "execution_summary": execution_summary(conn),
            "maintenance": maintenance,
            # The bridge projects these existing paper candidates into a
            # compact read-only route-feasibility packet.  The raw candidates
            # are not persisted in the LLM packet and no routing decision is
            # made from this hand-off.
            "route_requirement_candidates": route_requirement_candidate_inputs(candidates),
            "top_reviewed": [
                {
                    "inst_id": item["candidate"]["inst_id"],
                    "direction": item["candidate"]["direction"],
                    "base_score": item["candidate"]["score"],
                    "learned_score": item["review"]["learned_score"],
                    "decision": (
                        item.get("execution_review") or item["review"]
                    )["decision"],
                    "intended_decision": item["review"]["decision"],
                    "confidence": item["review"]["confidence"],
                    "gross_edge_bps": item["review"].get("gross_edge_bps"),
                    "modeled_cost_bps": item["review"].get("modeled_cost_bps"),
                    "net_edge_bps": item["review"].get("net_edge_bps"),
                    "freshness_minutes": item["review"].get("freshness_minutes"),
                    "gating_reason": item["review"].get("gating_reason"),
                    "blocks": item["review"]["hard_blocks"],
                    "route_id": item["review"].get("route_id"),
                    "effective_route_id": item["review"].get("effective_route_id"),
                    "route_alternative_used": item["review"].get("route_alternative_used"),
                    "route_status": item["review"].get("route_status"),
                    "missing_requirements": item["review"].get("missing_requirements", []),
                    "hunter_bucket": item["candidate"].get("_hunter_bucket"),
                    "hunter_allocation_reason": item["candidate"].get("_hunter_allocation_reason"),
                    "strategy_lab_id": item["candidate"].get("strategy_lab_id"),
                }
                for item in reviewed[:10]
            ],
            "paper_net_edge_gates": paper_context_cost_report(candidates),
            "paper_context_drag": paper_context_drag,
            "route_resolver": route_resolver_report,
            "expansion_map": expansion_map,
            "self_improvement_open_pack": self_improvement_open_pack,
            "signal_stats": stats_snapshot(conn),
            "open_growth_experiments": open_experiments(conn),
            "open_improvement_tasks": open_tasks(conn),
            "self_improvement": auto_improvement,
            "signal_safety_governor": signal_safety_governor,
            "contextual_failure_filters": contextual_failure_filters,
            "strategy_reliability": strategy_reliability,
            "hunter_allocation": hunter_allocation,
            "auxiliary_runtime": auxiliary_policy,
        }
        payload["market_hunter_directives"] = run_market_hunter(conn, settings)
        payload["llm_recommendations_ingested"] = llm_recommendations_ingested
        payload["llm_inbox"] = llm_inbox_summary()
        memory_changes = ingest_radar_memory(conn, payload, settings)
        payload["memory_facts_added"] = sum(item.get("operation") == "inserted" for item in memory_changes)
        payload["memory_facts_reinforced"] = sum(item.get("operation") == "reinforced" for item in memory_changes)
        payload["memory_profiles_updated"] = sum(item.get("operation") == "updated_profile" for item in memory_changes)
        payload["agent_memory"] = memory_summary(conn, settings)
        payload["llm_cost_summary"] = llm_cost_summary(conn)
        write_llm_state_packet(conn, payload, settings)
        if auxiliary_policy["llm_swarm_in_radar"]:
            payload["llm_swarm_generated"] = run_llm_swarm_once(settings=settings)
        else:
            payload["llm_swarm_generated"] = []
            payload["llm_swarm_status"] = {
                "status": "skipped",
                "reason": "separate_evolution_worker",
                "worker_expected": auxiliary_policy["evolution_worker_expected"],
            }
        if auxiliary_policy["autonomous_builder_in_radar"]:
            payload["autonomous_builder"] = run_autonomous_builder(settings=settings, conn=conn)
        else:
            payload["autonomous_builder"] = {
                "status": "skipped",
                "reason": "separate_evolution_worker",
                "worker_expected": auxiliary_policy["evolution_worker_expected"],
            }
        payload["llm_inbox"] = llm_inbox_summary()
        payload["llm_cost_summary"] = llm_cost_summary(conn)
        payload["paper_expansion_campaign"] = {
            "enabled": bool(campaign_cycle.get("enabled")),
            "status": "inflight_artifacts_complete",
            "cycle_id": campaign_cycle.get("cycle_id"),
            "phase": campaign_cycle.get("phase"),
            "run_status": campaign_cycle.get("run_status"),
        }
        # All report/artifact work must finish before campaign success is
        # persisted.  If any write below fails, the in-flight token remains for
        # the supervisor/next cycle to finalize as a failure.
        write_llm_state_packet(conn, payload, settings)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        latest_path = RUNS_DIR / "radar_state_latest.json"
        latest_snapshot = compact_json_value(
            payload,
            max_depth=5,
            list_limit=10,
            dict_limit=60,
            string_limit=800,
        )
        latest_snapshot = {
            "state_snapshot_compaction": {
                "enabled": True,
                "policy": "representative_evidence_with_explicit_omission_counts",
            },
            **latest_snapshot,
        }
        latest_path.write_text(json.dumps(latest_snapshot, indent=2), encoding="utf-8")
        campaign_metrics = _bounded_campaign_metrics(
            conn,
            campaign_cycle,
            settings=settings,
            reviewed=reviewed,
            frontier_crypto_venues=frontier_crypto_venues,
            runtime_seconds=time.perf_counter() - cycle_started,
            db_size_before=db_size_before,
        )
        payload["paper_expansion_campaign"] = record_campaign_cycle(
            conn,
            campaign_cycle,
            campaign_metrics,
        )
    return payload


def print_payload(payload: dict) -> None:
    print(f"Mode: {payload['mode']}")
    print(f"Opened: {len(payload['opened'])} | Closed: {len(payload['closed'])} | Summary: {payload['summary']}")
    print(f"Execution: {payload.get('execution_summary')}")
    route_summary = payload.get("route_resolver", {}).get("summary", {})
    if route_summary:
        print(f"Routes: {route_summary.get('by_route_status')} missing={route_summary.get('by_missing_requirement')}")
    print(f"LLM inbox: {payload.get('llm_inbox')}")
    print(f"LLM cost: {payload.get('llm_cost_summary')}")
    print(f"Maintenance: {payload['maintenance']}")
    if payload["opened"]:
        print("Opened paper trades:")
        for row in payload["opened"]:
            print(
                f"  #{row['id']} order={row.get('order_id')} {row['inst_id']:<20} {row['direction']:<28} "
                f"score={row['learned_score']:<6} conf={row['confidence']:<5} edge={row['net_edge_bps_estimate']}bps"
            )
    print("Top reviewed:")
    for row in payload["top_reviewed"][:8]:
        block = "; ".join(row["blocks"][:2])
        print(
            f"  {row['inst_id']:<20} {row['decision']:<22} "
            f"base={row['base_score']:<6} learned={row['learned_score']:<6} {block}"
        )
    if payload["open_improvement_tasks"]:
        print("Improvement tasks:")
        for task in payload["open_improvement_tasks"][:5]:
            print(f"  P{task['priority']} #{task['id']}: {task['title']}")
    if payload["open_growth_experiments"]:
        print("Growth experiments:")
        for experiment in payload["open_growth_experiments"][:5]:
            print(f"  P{experiment['priority']} #{experiment['id']}: {experiment['hypothesis']}")
    if payload.get("self_improvement"):
        report = payload["self_improvement"]
        print("Self-improvement:")
        print(
            f"  consumed={len(report.get('consumed', []))} "
            f"evaluated={len(report.get('evaluated', []))} "
            f"active_policies={len(report.get('active_policies', []))}"
        )
    if payload.get("market_hunter_directives"):
        print("Market hunter:")
        for directive in payload["market_hunter_directives"][:6]:
            print(f"  P{directive['priority']} {directive['directive']}: {directive['market_key']}")
    if payload.get("llm_recommendations_ingested"):
        print("LLM recommendations ingested:")
        for item in payload["llm_recommendations_ingested"][:5]:
            print(f"  P{item['priority']} {item['action']}: {item['title']}")
    if payload.get("llm_swarm_generated"):
        print("LLM swarm generated:")
        for item in payload["llm_swarm_generated"][:5]:
            print(f"  P{item.get('priority')} {item.get('action')}: {item.get('title')}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the self-improving inefficiency radar.")
    parser.add_argument("--config", type=pathlib.Path, default=None)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--hold-minutes", type=int, default=None)
    parser.add_argument("--scan-universe", type=int, default=None)
    parser.add_argument("--review-top", type=int, default=None)
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    if args.hold_minutes is not None:
        settings["scanner"]["hold_minutes"] = args.hold_minutes
    if args.scan_universe is not None:
        settings["scanner"]["scan_universe"] = args.scan_universe
    if args.review_top is not None:
        settings["scanner"]["review_top"] = args.review_top

    if settings.get("allow_live_trading"):
        print("Live trading is intentionally not implemented in this MVP.", file=sys.stderr)
        return 2

    for idx in range(args.iterations):
        payload = run_once(settings)
        print_payload(payload)
        if idx < args.iterations - 1:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
