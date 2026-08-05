#!/usr/bin/env python3
"""Asynchronous LLM research and code-evolution worker.

The radar loop should stay focused on market scanning, paper execution, and
outcome capture. This worker handles slow LLM/research/build work against the
latest state packet and existing recommendation backlog.
"""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sqlite3
import sys
import time
import uuid
from typing import Any

from adapter_implementation_owner import run_once as run_adapter_implementation_owner
from autonomous_builder import run_autonomous_builder
from evolution_owner_scheduler import lane_order, record_turn, scheduler_summary
from llm_bridge import STATE_JSON, ingest_llm_recommendations
from llm_swarm_runner import run_once as run_llm_swarm_once
from research_worker import run_once as run_research_worker_once
from self_improvement import run_auto_improvement
from settings import load_settings
from storage import RUNS_DIR, connect, llm_cost_summary, llm_inbox_summary
from strategy_implementation_owner import run_once as run_strategy_implementation_owner


REPORT_JSON = RUNS_DIR / "evolution_worker_report.json"
REPORT_MD = RUNS_DIR / "evolution_worker_report.md"


def _database_locked(exc: BaseException) -> bool:
    return "database is locked" in str(exc).lower()


def _run_db_stage(stage: str, callback: Any, *, attempts: int = 3) -> tuple[Any, dict | None]:
    """Keep transient radar writes from crashing the paid evolution cycle."""

    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        try:
            with connect() as conn:
                return callback(conn), None
        except sqlite3.OperationalError as exc:
            if not _database_locked(exc):
                raise
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(float(attempt * 2))
    return None, {
        "status": "database_busy_retry_later",
        "stage": stage,
        "attempts": attempts,
        "reason": last_error or "database is locked",
    }


def _worker_settings(settings: dict) -> dict:
    output = copy.deepcopy(settings)
    output.setdefault("self_improvement", {})["process_code_changes_in_radar_loop"] = True
    return output


def _run_research_stage(settings: dict) -> tuple[dict, dict | None]:
    """Treat a long radar write as a deferred research stage, not a worker crash."""

    try:
        return run_research_worker_once(settings=settings), None
    except sqlite3.OperationalError as exc:
        if not _database_locked(exc):
            raise
        return {
            "status": "database_busy_retry_later",
            "summary": {},
            "reason": str(exc),
        }, {
            "status": "database_busy_retry_later",
            "stage": "research_worker",
            "attempts": 1,
            "reason": str(exc),
        }


def _run_swarm_stage(settings: dict, *, force: bool = False) -> tuple[list[dict], dict | None]:
    """Defer a pre-model swarm lock without aborting the rest of the worker cycle."""

    try:
        return run_llm_swarm_once(settings=settings, force=force), None
    except sqlite3.OperationalError as exc:
        if not _database_locked(exc):
            raise
        return [], {
            "status": "database_busy_retry_later",
            "stage": "llm_swarm",
            "attempts": 1,
            "reason": str(exc),
        }


def _write_report(report: dict) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Evolution Worker Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Research worker status: `{(report.get('research_worker') or {}).get('status')}`",
        f"- Research candidates: `{((report.get('research_worker') or {}).get('summary') or {}).get('candidate_count', 0)}`",
        f"- LLM swarm recommendations: `{len(report.get('llm_swarm_generated') or [])}`",
        f"- Inbox ingested: `{len(report.get('llm_recommendations_ingested') or [])}`",
        f"- Auto-improvement consumed: `{len((report.get('self_improvement') or {}).get('consumed') or [])}`",
        f"- Adapter owner status: `{(report.get('adapter_implementation_owner') or {}).get('status')}`",
        f"- Strategy owner status: `{((report.get('strategy_implementation_owner') or {}).get('last_cycle') or {}).get('status')}`",
        f"- Writer lane: `{(report.get('owner_scheduler') or {}).get('last_lane')}`",
        f"- Autonomous builder status: `{(report.get('autonomous_builder') or {}).get('status')}`",
    ]
    if report.get("reason"):
        lines.extend(["", "## Reason", "", str(report["reason"])])
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run_once(settings: dict, *, force_swarm: bool = False, force_builder: bool = False) -> dict:
    if settings.get("allow_live_trading"):
        return _write_report(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": "blocked_live_trading",
                "reason": "Evolution worker refuses to run when allow_live_trading is true.",
            }
        )

    if not STATE_JSON.exists():
        return _write_report(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": "missing_state_packet",
                "reason": f"Missing {STATE_JSON}; run one radar iteration first.",
            }
        )

    worker_settings = _worker_settings(settings)
    cycle_id = str(uuid.uuid4())
    # Existing executable work gets the first claim on model budget. Research
    # and new recommendations are generated afterward for the next cycle.
    ingested, ingest_error = _run_db_stage("ingest_llm_recommendations", lambda conn: ingest_llm_recommendations(conn, settings))
    ingested = ingested or []
    strategy_owner, strategy_error = _run_db_stage(
        "strategy_implementation_owner_sync",
        lambda conn: run_strategy_implementation_owner(
            conn, worker_settings, execute_turn=False, cycle_id=cycle_id,
            scheduler=scheduler_summary(conn),
        ),
    )
    strategy_owner = strategy_owner or {"status": "database_busy_retry_later", "last_cycle": {}}
    order_state, scheduler_error = _run_db_stage("owner_scheduler", lambda conn: lane_order(conn))
    if order_state:
        lanes, _initial_scheduler = order_state
    else:
        lanes, _initial_scheduler = ["strategy", "adapter", "general"], {}

    adapter_owner = {"status": "deferred_by_scheduler"}
    self_improvement = {"status": "deferred_by_scheduler", "consumed": []}
    autonomous_builder = {"status": "deferred_by_scheduler"}
    adapter_error = improvement_error = builder_error = None
    selected_lane = None
    lane_status = "no_lane_consumed"
    for lane in lanes:
        consumed_writer = False
        if lane == "strategy":
            strategy_owner, strategy_error = _run_db_stage(
                "strategy_implementation_owner",
                lambda conn: run_strategy_implementation_owner(
                    conn, worker_settings, execute_turn=True, cycle_id=cycle_id,
                    scheduler=scheduler_summary(conn),
                ),
            )
            strategy_owner = strategy_owner or {"status": "database_busy_retry_later", "last_cycle": {}}
            lane_status = str((strategy_owner.get("last_cycle") or {}).get("status") or strategy_owner.get("status") or "unknown")
            consumed_writer = lane_status not in {"disabled", "monitor_only", "no_eligible_strategy_task", "database_busy_retry_later"}
        elif lane == "adapter":
            adapter_owner, adapter_error = _run_db_stage(
                "adapter_implementation_owner",
                lambda conn: run_adapter_implementation_owner(conn, worker_settings),
            )
            adapter_owner = adapter_owner or {"status": "database_busy_retry_later"}
            lane_status = str(adapter_owner.get("status") or "unknown")
            consumed_writer = lane_status not in {"disabled", "not_due", "no_eligible_adapter_spec", "database_busy_retry_later"}
        else:
            self_improvement, improvement_error = _run_db_stage(
                "self_improvement",
                lambda conn: run_auto_improvement(conn, worker_settings, include_code_changes=True),
            )
            self_improvement = self_improvement or {"status": "database_busy_retry_later", "consumed": []}
            code_attempted = any(
                str(item.get("task_type") or "") == "code_change"
                for item in (self_improvement.get("consumed") or []) if isinstance(item, dict)
            )
            if code_attempted and not force_builder:
                autonomous_builder = {"status": "deferred_for_targeted_code_attempt"}
                lane_status = "targeted_code_attempt"
                consumed_writer = True
            else:
                autonomous_builder, builder_error = _run_db_stage(
                    "autonomous_builder",
                    lambda conn: run_autonomous_builder(settings=settings, conn=conn, force=force_builder),
                )
                autonomous_builder = autonomous_builder or {"status": "database_busy_retry_later"}
                lane_status = str(autonomous_builder.get("status") or "unknown")
                consumed_writer = lane_status not in {
                    "disabled", "not_due", "no_action", "no_actionable_plan", "database_busy_retry_later",
                }
        if consumed_writer:
            selected_lane = lane
            _turn, turn_error = _run_db_stage(
                "owner_scheduler_record",
                lambda conn, chosen=lane, status=lane_status: record_turn(
                    conn, chosen, cycle_id=cycle_id, status=status, consumed_writer=True,
                ),
            )
            scheduler_error = scheduler_error or turn_error
            break

    owner_scheduler, scheduler_summary_error = _run_db_stage("owner_scheduler_summary", scheduler_summary)
    scheduler_error = scheduler_error or scheduler_summary_error
    owner_scheduler = owner_scheduler or {"next_lane": lanes[0] if lanes else "strategy"}
    owner_scheduler["selected_lane"] = selected_lane
    owner_scheduler["cycle_lane_status"] = lane_status

    research_worker_report = {}
    research_error = None
    if settings.get("research_worker", {}).get("enabled", True) and settings.get("research_worker", {}).get(
        "run_every_evolution_cycle", True
    ):
        research_worker_report, research_error = _run_research_stage(worker_settings)
    llm_swarm_generated, swarm_error = _run_swarm_stage(settings, force=force_swarm)
    newly_ingested, new_ingest_error = _run_db_stage(
        "ingest_new_llm_recommendations",
        lambda conn: ingest_llm_recommendations(conn, settings),
    )
    if newly_ingested:
        known = {str(item.get("recommendation_id") or "") for item in ingested if isinstance(item, dict)}
        ingested.extend(
            item
            for item in newly_ingested
            if not isinstance(item, dict) or str(item.get("recommendation_id") or "") not in known
        )

    cost_summary, cost_error = _run_db_stage("cost_summary", lambda conn: llm_cost_summary(conn))
    inbox_summary, inbox_error = _run_db_stage("llm_inbox_summary", lambda _conn: llm_inbox_summary())
    database_errors = [
        error
        for error in (
            ingest_error,
            strategy_error,
            improvement_error,
            adapter_error,
            builder_error,
            scheduler_error,
            research_error,
            swarm_error,
            new_ingest_error,
            cost_error,
            inbox_error,
        )
        if error
    ]
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "degraded_database_busy" if database_errors else "ok",
        "mode": settings.get("mode"),
        "live_trading_allowed": bool(settings.get("allow_live_trading", False)),
        "research_worker": research_worker_report,
        "llm_swarm_generated": llm_swarm_generated,
        "llm_recommendations_ingested": ingested,
        "self_improvement": self_improvement,
        "strategy_implementation_owner": strategy_owner,
        "adapter_implementation_owner": adapter_owner,
        "autonomous_builder": autonomous_builder,
        "owner_scheduler": owner_scheduler,
        "llm_inbox": inbox_summary or {},
        "llm_cost_summary": cost_summary or {},
        "database_errors": database_errors,
    }
    return _write_report(report)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run asynchronous LLM/code evolution outside the radar loop.")
    parser.add_argument("--config", type=pathlib.Path, default=None)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--force-swarm", action="store_true")
    parser.add_argument("--force-builder", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    for index in range(args.iterations):
        report = run_once(settings, force_swarm=args.force_swarm, force_builder=args.force_builder)
        print(
            "Evolution worker "
            f"status={report.get('status')} "
            f"research={((report.get('research_worker') or {}).get('summary') or {}).get('candidate_count', 0)} "
            f"swarm={len(report.get('llm_swarm_generated') or [])} "
            f"ingested={len(report.get('llm_recommendations_ingested') or [])} "
            f"consumed={len((report.get('self_improvement') or {}).get('consumed') or [])} "
            f"builder={(report.get('autonomous_builder') or {}).get('status')}"
        )
        if index < args.iterations - 1:
            time.sleep(max(1, int(args.interval)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
