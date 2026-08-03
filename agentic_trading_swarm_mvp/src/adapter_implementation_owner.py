"""Own adapter specs from discovery through an executable code proposal."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sqlite3
from typing import Any

from code_evolution import process_code_change_recommendation
from storage import RUNS_DIR


REPORT_JSON = RUNS_DIR / "adapter_implementation_owner.json"
REPORT_MD = RUNS_DIR / "adapter_implementation_owner.md"
MARKER = RUNS_DIR / "adapter_implementation_owner_last_attempt.txt"
RETRYABLE_PROPOSAL_STATUSES = {
    "patch_generation_unavailable_retry_later",
    "patch_generation_timeout",
    "patch_generation_failed",
}
SUCCESS_PROPOSAL_STATUSES = {"candidate_committed", "promoted", "workspace_applied_probation", "workspace_kept", "kept"}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return (slug or "global_public_market")[:64]


def _candidate(spec: dict) -> dict:
    candidate = spec.get("candidate")
    return candidate if isinstance(candidate, dict) else {}


def _eligible_score(row: dict) -> tuple[int, int, int] | None:
    candidate = _candidate(row["spec"])
    if candidate.get("data_access_type") != "public_no_key":
        return None
    if not candidate.get("public_docs_url") and not candidate.get("source_urls"):
        return None
    if candidate.get("source_validation_status") not in {None, "", "public_url_present", "validated_public_source"}:
        return None
    tradability = str(candidate.get("tradability_guess") or "unknown")
    tradability_rank = {
        "directly_tradable": 4,
        "route_needed": 3,
        "watch_only": 1,
        "unknown": 0,
    }.get(tradability, 0)
    if tradability_rank <= 0:
        return None
    confidence = int(round(float(candidate.get("confidence") or 0.0) * 100))
    return tradability_rank, int(row.get("priority") or 0), confidence


def _load_specs(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        select id, created_at, source_recommendation_id, market_key, priority,
               title, status, spec_json, evidence_json
        from adapter_specs
        where status in (
            'open', 'adapter_capability_gap', 'implementation_queued',
            'implementation_queued_retry'
        )
        order by priority desc, id asc
        limit ?
        """,
        (int(limit),),
    ).fetchall()
    output = []
    for raw in rows:
        row = dict(raw)
        row["spec"] = _json(row.pop("spec_json"))
        row["evidence"] = _json(row.pop("evidence_json"))
        output.append(row)
    return output


def _existing_proposal_status(conn: sqlite3.Connection, spec_id: int) -> str | None:
    needle = f'"adapter_spec_id": {int(spec_id)}'
    row = conn.execute(
        """
        select status
        from code_evolution_proposals
        where payload_json like ?
        order by updated_at desc
        limit 1
        """,
        (f"%{needle}%",),
    ).fetchone()
    return str(row["status"]) if row else None


def _select_spec(conn: sqlite3.Connection, settings: dict) -> dict | None:
    cfg = settings.get("adapter_implementation_owner", {})
    ranked = []
    for row in _load_specs(conn, int(cfg.get("scan_limit", 100))):
        prior_status = _existing_proposal_status(conn, int(row["id"]))
        if prior_status and prior_status not in RETRYABLE_PROPOSAL_STATUSES:
            continue
        score = _eligible_score(row)
        if score is not None:
            ranked.append((score, row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], -int(item[1]["id"])), reverse=True)
    return ranked[0][1]


def _attempt_due(settings: dict) -> bool:
    minimum = float(settings.get("adapter_implementation_owner", {}).get("min_minutes_between_attempts", 15))
    if not MARKER.exists() or minimum <= 0:
        return True
    try:
        parsed = dt.datetime.fromisoformat(MARKER.read_text(encoding="utf-8").strip().replace("Z", "+00:00"))
    except (OSError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (_utc_now_datetime() - parsed).total_seconds() >= minimum * 60


def _utc_now_datetime() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _proposal_for_spec(row: dict) -> dict:
    spec_id = int(row["id"])
    candidate = _candidate(row["spec"])
    venue = str(candidate.get("venue_or_source") or candidate.get("venue") or row["market_key"])
    plugin_path = f"src/adapters/venues/{_slug(venue)}.py"
    source_urls = [
        str(value)
        for value in [candidate.get("public_docs_url"), *(candidate.get("source_urls") or [])]
        if value
    ]
    acceptance = [
        "register an AdapterInfo plugin that adapter_runtime auto-discovers",
        "fetch only public no-key data and normalize it into a ScanBatch",
        "preserve source URL, fetch status, freshness/session state, and parser failures",
        "emit real observations when the source is reachable and watch-only evidence when it is not",
        "add representative parser and runtime-discovery tests",
    ]
    proposed_change = (
        f"Implement adapter spec #{spec_id} for {venue}. Surface: "
        f"{candidate.get('asset_or_event') or candidate.get('surface_type_raw') or row['title']}. "
        f"Public sources: {source_urls}. Inefficiency hypothesis: "
        f"{candidate.get('inefficiency_hypothesis') or 'not provided'}. "
        f"The implementation must {', '.join(acceptance)}. Do not merely add another research seed or report row."
    )
    code_change = {
        "change_category": "public_data_adapter",
        "implementation_mode": "runtime_active",
        "expected_files": [
            plugin_path,
            "src/adapters/venues/common.py",
            "tests/test_public_market_adapters.py",
        ],
        "tests_to_run": ["python -m unittest tests.test_public_market_adapters"],
        "rollback_criteria": "Revert if adapter discovery, parser tests, full regression, or paper-only safety checks fail.",
        "frontier_escalation_reason": "A high-priority public market adapter spec needs a complete runtime plugin, parser tests, and acceptance path.",
        "adapter_spec_id": spec_id,
        "adapter_contract": {
            "venue": venue,
            "plugin_path": plugin_path,
            "data_access_type": candidate.get("data_access_type"),
            "tradability_guess": candidate.get("tradability_guess"),
            "surface_type": candidate.get("surface_type_classified") or candidate.get("surface_type_raw"),
            "source_urls": source_urls,
            "acceptance_criteria": acceptance,
        },
    }
    payload = {
        "action": "propose_code_change",
        "agent_name": "adapter_implementation_owner",
        "title": f"Implement public adapter #{spec_id}: {venue}"[:180],
        "priority": max(80, int(row.get("priority") or 80)),
        "market_key": row["market_key"],
        "rationale": proposed_change,
        "proposed_change": proposed_change,
        "evidence": {
            "source": "adapter_specs",
            "adapter_spec_id": spec_id,
            "candidate_id": candidate.get("candidate_id"),
            "confidence": candidate.get("confidence"),
            "public_docs_url": candidate.get("public_docs_url"),
        },
        "frontier_escalation_reason": code_change["frontier_escalation_reason"],
        "adapter_spec_id": spec_id,
        "code_change": code_change,
    }
    return {
        "recommendation_id": f"adapter-spec:{spec_id}:implementation",
        "title": payload["title"],
        "priority": payload["priority"],
        "payload": payload,
    }


def _update_spec_status(conn: sqlite3.Connection, row: dict, status: str, proposal_status: str | None) -> None:
    evidence = dict(row.get("evidence") or {})
    evidence["adapter_implementation_owner"] = {
        "checked_at": _utc_now(),
        "proposal_status": proposal_status,
        "owner_status": status,
    }
    conn.execute(
        "update adapter_specs set status = ?, evidence_json = ? where id = ?",
        (status, json.dumps(evidence, sort_keys=True), int(row["id"])),
    )
    conn.commit()


def _write_report(report: dict) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Adapter Implementation Owner",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Adapter spec: `{report.get('adapter_spec_id')}`",
        f"- Proposal status: `{report.get('proposal_status')}`",
    ]
    if report.get("title"):
        lines.append(f"- Target: {report['title']}")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run_once(conn: sqlite3.Connection, settings: dict) -> dict:
    cfg = settings.get("adapter_implementation_owner", {})
    if not cfg.get("enabled", True):
        return _write_report({"generated_at": _utc_now(), "status": "disabled"})
    if not _attempt_due(settings):
        return _write_report({"generated_at": _utc_now(), "status": "not_due"})
    row = _select_spec(conn, settings)
    if row is None:
        return _write_report({"generated_at": _utc_now(), "status": "no_eligible_adapter_spec"})

    proposal = _proposal_for_spec(row)
    _update_spec_status(conn, row, "implementation_queued", None)
    MARKER.write_text(_utc_now(), encoding="utf-8")
    artifacts = process_code_change_recommendation(conn, proposal, settings)
    artifact = artifacts[0] if artifacts else {}
    proposal_status = str(artifact.get("status") or "no_artifact")
    if proposal_status in SUCCESS_PROPOSAL_STATUSES:
        owner_status = "deployed_waiting_acceptance"
    elif proposal_status in RETRYABLE_PROPOSAL_STATUSES:
        owner_status = "implementation_queued_retry"
    else:
        owner_status = "implementation_failed_review"
    _update_spec_status(conn, row, owner_status, proposal_status)
    return _write_report(
        {
            "generated_at": _utc_now(),
            "status": owner_status,
            "adapter_spec_id": row["id"],
            "title": row["title"],
            "proposal_status": proposal_status,
            "artifacts": artifacts,
        }
    )
