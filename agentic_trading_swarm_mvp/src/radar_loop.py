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
import json
import pathlib
import sys
import time
from types import SimpleNamespace

from agent_review import review_candidate
from adapter_capabilities import reconcile_adapter_specs
from adapter_runtime import build_scan_batch as build_public_adapter_scan_batch
from autonomous_builder import run_autonomous_builder
from contextual_failure_filters import run_contextual_failure_filters
from crypto_venue_scanner import scan as scan_crypto_venues, write_outputs as write_crypto_venue_health
from execution_engine import execute_order
from frontier_crypto_adapter import REPORT_JSON as FRONTIER_CRYPTO_REPORT_JSON
from frontier_crypto_adapter import build_scan_batch as build_frontier_crypto_scan_batch
from global_market_discovery_scanner import build_scan_batch as build_global_market_discovery_scan_batch
from global_proxy_scanner import build_scan_batch as build_global_proxy_scan_batch
from hunter_allocation import allocate_candidate_review, write_hunter_allocation_report
from learning import load_adjustments, stats_snapshot, update_signal_stats
from llm_bridge import cross_context_reliability, ingest_llm_recommendations, write_llm_state_packet
from market_admission import run_market_admission_monitor
from market_admission_bridge import run_market_admission_bridge
from llm_swarm_runner import run_once as run_llm_swarm_once
from market_hunter import run_market_hunter
from memory_graph import ingest_radar_memory, memory_summary
from okx_perp_scanner import build_scan_batch as build_okx_scan_batch
from okx_signal_research import run_okx_signal_research
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
from strategy_lab import evaluate_strategy_lab, generate_strategy_lab_candidates, write_strategy_lab_reports
from strategy_reliability import apply_strategy_reliability
from storage import (
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
    record_due_horizon_outcomes,
    save_opportunity,
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
    summary = {
        "enabled": bool(generation.get("enabled", True)),
        "generated_count": int(generation.get("generated_count", 0) or 0),
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
    policy = item.get("strategy_lab_surface_policy")
    return isinstance(policy, dict) and policy.get("eligible") is True


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

    for strategy_lab_id, experiment_candidates in by_experiment.items():
        candidate = None
        source_key = None
        for option in sorted(experiment_candidates, key=lambda row: (route_rank(row), -float(row.get("score") or 0.0))):
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
        if candidate is None or source_key is None:
            continue
        row = dict(candidate)
        row["_hunter_bucket"] = "explore"
        row["_hunter_directive_id"] = None
        row["_hunter_allocation_reason"] = "strategy_lab_distinct_experiment_reserve"
        selected.append(row)
        selected_experiments.add(strategy_lab_id)
        selected_sources.add(source_key)
        if len(selected) >= limit:
            break
    return selected, {
        "configured_slots": requested,
        "reserved_count": len(selected),
        "strategy_lab_ids": sorted(selected_experiments),
        "distinct_source_count": len(selected_sources),
    }


strategy_lab_runtime = SimpleNamespace(
    is_candidate_filter=_is_strategy_lab_candidate_filter,
    build_runtime_summary=_strategy_lab_runtime_selection_summary,
    reserve_review_candidates=_reserve_strategy_lab_review_candidates,
)


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


def run_once(settings: dict) -> dict:
    auxiliary_policy = _auxiliary_runtime_policy(settings)
    capabilities = settings["account_capabilities"]
    scan_cfg = settings["scanner"]
    risk_cfg = settings["risk"]
    allow_short_spot = bool(capabilities.get("spot_borrow", False))

    with connect() as conn:
        required = open_trade_instruments(conn)
        required_okx = set(required.get("perp_funding_basis", set()))
        required_okx.update(open_signal_trial_instruments(conn, "OKX|perp_funding_basis"))
        batches = []
        admission_observations = []
        okx_batch = build_okx_scan_batch(
            scan_cfg["scan_universe"],
            allow_short_spot=allow_short_spot,
            required_inst_ids=required_okx,
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
            candidates.extend(frontier_candidates)
            candidates.sort(key=lambda row: row["score"], reverse=True)
            if FRONTIER_CRYPTO_REPORT_JSON.exists():
                frontier_crypto_venues = json.loads(FRONTIER_CRYPTO_REPORT_JSON.read_text(encoding="utf-8"))
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
        self_improvement_open_pack = build_open_pack_report(
            conn,
            settings,
            candidates=candidates[:250],
            prediction_summary=prediction_summary,
            expansion_map=expansion_map,
        )
        write_open_pack_reports(self_improvement_open_pack)
        horizon_outcomes = record_due_horizon_outcomes(conn, price_observations, settings)
        closed = close_due_trades(
            conn,
            price_observations,
            int(scan_cfg["hold_minutes"]),
            settings=settings,
        )
        stats = update_signal_stats(conn, settings) if settings["learning"]["enabled"] else {}
        yahoo_counterfactual = run_yahoo_counterfactual_analysis(conn, settings)
        reliability_cards = cross_context_reliability(conn)
        strategy_lab_evaluation = evaluate_strategy_lab(conn, settings)
        strategy_lab_report = write_strategy_lab_reports(conn, strategy_lab_generation, strategy_lab_evaluation)
        adjustments = load_adjustments(conn)
        llm_recommendations_ingested = ingest_llm_recommendations(conn, settings)
        auto_improvement = run_auto_improvement(conn, settings, include_code_changes=False)
        signal_safety_governor = run_signal_safety_governor(conn, settings)
        contextual_failure_filters = run_contextual_failure_filters(conn, settings)
        policies = active_signal_policies(conn)
        review_limit = int(scan_cfg["review_top"])
        reserved_lab_candidates, strategy_lab_review_reserve = _reserve_strategy_lab_review_candidates(
            candidates,
            settings,
            review_limit,
        )
        non_lab_candidates = [candidate for candidate in candidates if not candidate.get("strategy_lab_id")]
        hunter_review_slots = max(0, review_limit - len(reserved_lab_candidates))
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
        review_candidates = reserved_lab_candidates + hunter_review_candidates
        hunter_allocation["selected_count"] = len(review_candidates)
        hunter_allocation["strategy_lab_review_reserve"] = strategy_lab_review_reserve
        hunter_allocation = write_hunter_allocation_report(hunter_allocation, review_candidates, candidates, settings)

        reviewed = []
        opened = []
        for candidate in review_candidates:
            review = review_candidate(candidate, settings, adjustments, policies=policies)
            save_opportunity(conn, candidate, review)
            record_review_policy_effects(conn, review)
            reviewed.append({"candidate": candidate, "review": review})

        for item in reviewed:
            candidate = item["candidate"]
            review = item["review"]
            if len(opened) >= int(scan_cfg["max_new_paper_trades"]):
                break
            if count_open_trades(conn) >= int(risk_cfg["max_open_paper_trades"]):
                break
            if review["decision"] not in {"approve_paper_trade", "approve_conditional_paper_trade"}:
                continue
            if has_open_trade(conn, candidate["inst_id"], candidate["direction"]):
                continue
            execution = execute_order(conn, candidate, review, settings)
            if not execution["paper_filled"]:
                continue
            trade_id = open_paper_trade(conn, candidate, review, execution=execution, settings=settings)
            record_open_policy_effects(conn, review)
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
                    "route_id": review.get("route_id"),
                    "effective_route_id": review.get("effective_route_id"),
                    "route_status": review.get("route_status"),
                    "route_alternative_used": review.get("route_alternative_used"),
                    "strategy_lab_id": candidate.get("strategy_lab_id"),
                }
            )

        signal_safety_governor = run_signal_safety_governor(conn, settings)
        market_admission = run_market_admission_monitor(
            conn,
            settings,
            candidates,
            reviewed,
            admission_observations,
        )
        market_admission_bridge = run_market_admission_bridge(conn, settings, market_admission)
        market_admission["bridge"] = market_admission_bridge
        expansion_map["market_admission"] = market_admission.get("summary", {})
        expansion_map["market_admission_bridge"] = market_admission_bridge.get("summary", {})
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
        auto_improvement = write_self_improvement_reports(conn, auto_improvement)
        summary = performance_summary(conn)
        maintenance = perform_maintenance(conn, settings)
        payload = {
            "mode": settings["mode"],
            "closed": closed,
            "horizon_outcomes": horizon_outcomes,
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
            "opened": opened,
            "summary": summary,
            "execution_summary": execution_summary(conn),
            "maintenance": maintenance,
            "top_reviewed": [
                {
                    "inst_id": item["candidate"]["inst_id"],
                    "direction": item["candidate"]["direction"],
                    "base_score": item["candidate"]["score"],
                    "learned_score": item["review"]["learned_score"],
                    "decision": item["review"]["decision"],
                    "confidence": item["review"]["confidence"],
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
        write_llm_state_packet(conn, payload, settings)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    latest_path = RUNS_DIR / "radar_state_latest.json"
    latest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
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
