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
import sys
import time
from typing import Any

from autonomous_builder import run_autonomous_builder
from llm_bridge import STATE_JSON, ingest_llm_recommendations
from llm_swarm_runner import run_once as run_llm_swarm_once
from self_improvement import run_auto_improvement
from settings import load_settings
from storage import RUNS_DIR, connect, llm_cost_summary, llm_inbox_summary


REPORT_JSON = RUNS_DIR / "evolution_worker_report.json"
REPORT_MD = RUNS_DIR / "evolution_worker_report.md"


def _worker_settings(settings: dict) -> dict:
    output = copy.deepcopy(settings)
    output.setdefault("self_improvement", {})["process_code_changes_in_radar_loop"] = True
    return output


def _write_report(report: dict) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Evolution Worker Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- LLM swarm recommendations: `{len(report.get('llm_swarm_generated') or [])}`",
        f"- Inbox ingested: `{len(report.get('llm_recommendations_ingested') or [])}`",
        f"- Auto-improvement consumed: `{len((report.get('self_improvement') or {}).get('consumed') or [])}`",
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
    llm_swarm_generated = run_llm_swarm_once(settings=settings, force=force_swarm)
    with connect() as conn:
        ingested = ingest_llm_recommendations(conn, settings)
        self_improvement = run_auto_improvement(conn, worker_settings, include_code_changes=True)
        autonomous_builder = run_autonomous_builder(settings=settings, conn=conn, force=force_builder)
        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "ok",
            "mode": settings.get("mode"),
            "live_trading_allowed": bool(settings.get("allow_live_trading", False)),
            "llm_swarm_generated": llm_swarm_generated,
            "llm_recommendations_ingested": ingested,
            "self_improvement": self_improvement,
            "autonomous_builder": autonomous_builder,
            "llm_inbox": llm_inbox_summary(),
            "llm_cost_summary": llm_cost_summary(conn),
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
