#!/usr/bin/env python3
"""Read-only external research worker skeleton with provenance capture."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from typing import Any

from storage import RUNS_DIR


REPORT_JSON = RUNS_DIR / "research_worker_latest.json"
REPORT_MD = RUNS_DIR / "research_worker_report.md"


def evidence_bundle(source_url: str, claim: str, market_relevance: str, suggested_validation: str) -> dict[str, Any]:
    return {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_url": source_url,
        "claim": claim,
        "market_relevance": market_relevance,
        "suggested_validation": suggested_validation,
        "allowed_use": "research_only_until_adapter_canary_passes",
    }


def run_once() -> dict[str, Any]:
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "idle",
        "tools": ["web_search", "official_docs", "public_news", "prediction_market_rules"],
        "evidence_bundles": [],
        "hard_rule": "No discovered endpoint becomes a runtime adapter until adapter/canary evaluation passes.",
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    REPORT_MD.write_text(
        "# Research Worker Report\n\n"
        f"- Generated: `{report['generated_at']}`\n"
        f"- Status: `{report['status']}`\n"
        "- Runtime adapter promotion: blocked until canary validation.\n",
        encoding="utf-8",
    )
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run read-only research worker once.")
    parser.parse_args(argv)
    report = run_once()
    print(f"Research worker status={report['status']}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
