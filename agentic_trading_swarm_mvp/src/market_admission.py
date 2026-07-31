"""Durable market-onboarding state and precise stall diagnostics.

Data-provider health, strategy evidence, and route feasibility are deliberately
separate here. A blocked endpoint must not become a losing-signal label, and a
losing strategy must not quarantine every strategy that shares its data feed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections import Counter
from typing import Any

from storage import RUNS_DIR, signal_key, utc_now


REPORT_JSON = RUNS_DIR / "market_admission_report.json"
REPORT_MD = RUNS_DIR / "market_admission_report.md"
STAGES = (
    "discovered",
    "reachable",
    "normalized",
    "priceable",
    "quality_verified",
    "strategy_candidate",
    "route_feasible",
    "paper_eligible",
    "paper_evaluated",
)
STAGE_INDEX = {stage: index for index, stage in enumerate(STAGES)}
ACTIVE_SESSION_STATES = {"open", "continuous", "unknown"}
ROUTE_OK_STATES = {"standard", "conditional", "feasible", "paper_proxy"}


def _text(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text or default


def _route_status(candidate: dict) -> str:
    for container in (
        candidate,
        candidate.get("execution_route"),
        candidate.get("execution_feasibility"),
    ):
        if not isinstance(container, dict):
            continue
        for key in ("route_status", "status", "paper_route_status"):
            value = str(container.get(key) or "").strip().lower()
            if value:
                return value
    return "unknown"


def _data_source(item: dict) -> str:
    source = item.get("data_source")
    if isinstance(source, dict):
        return _text(source.get("provider") or source.get("source_url") or source.get("url"))
    return _text(item.get("price_source") or item.get("source_url") or source)


def _surface(item: dict) -> str:
    return _text(
        item.get("proxy_surface")
        or item.get("market_surface")
        or item.get("surface_type_classified")
        or item.get("trade_type")
    )


def _lineage(item: dict) -> str:
    if item.get("signal_lineage_key"):
        return _text(item.get("signal_lineage_key"))
    if item.get("strategy_lab_id"):
        return f"STRATEGY_LAB|{item['strategy_lab_id']}|v{item.get('strategy_lab_version', 1)}"
    if item.get("signal_variant_id"):
        return _text(item.get("signal_variant_id"))
    if item.get("direction") and item.get("trade_type"):
        return f"{item.get('trade_type')}|{item.get('direction')}"
    return "adapter_observation"


def _admission_key(item: dict) -> str:
    identity = "|".join(
        (
            _text(item.get("venue")).upper(),
            _text(item.get("inst_id") or item.get("instrument_id")),
            _surface(item),
            _lineage(item),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:28]


def _session_status(item: dict) -> str:
    value = _text(item.get("session_status") or item.get("market_session_status"), "unknown").lower()
    return value if value in {"open", "closed", "continuous", "unknown"} else "unknown"


def _is_normalized(item: dict) -> bool:
    if item.get("proxy_symbol"):
        return bool(item.get("venue") and item.get("inst_id"))
    return bool(
        item.get("venue")
        and (item.get("inst_id") or item.get("instrument_id"))
        and (
            item.get("base")
            or item.get("base_asset")
            or item.get("underlying")
            or item.get("asset_class")
            or item.get("market_surface")
        )
    )


def _quality_verified(item: dict) -> bool:
    quality = str(item.get("quality_status") or item.get("proxy_quality_status") or "").lower()
    if quality in {"verified", "verified_proxy", "normal"}:
        return True
    if item.get("proxy_symbol"):
        return (
            _session_status(item) in ACTIVE_SESSION_STATES
            and float(item.get("stale_minutes") or 0.0) <= float(item.get("max_entry_stale_minutes") or 90.0)
            and float(item.get("liquidity_score") or 0.0) > 0.0
        )
    return False


def _blocker(item: dict, review: dict | None, stage: str) -> str | None:
    session = _session_status(item)
    if session == "closed":
        return "market_closed"
    data_status = str(item.get("data_status") or "reachable").lower()
    http_status = str(item.get("http_status") or "").lower()
    notes = " ".join(str(value) for value in item.get("notes") or []).lower()
    if item.get("access_blocker_code") == "network_region_blocked" or (
        data_status == "blocked" and ("403" in http_status or "451" in http_status or "probe_deferred" in http_status)
    ):
        return "network_region_blocked"
    if "parser failed" in notes or "parser" in str(item.get("candidate_reject_reason") or "").lower():
        return "parser_failure"
    if data_status in {"blocked", "unavailable", "degraded"}:
        return f"data_{data_status}"
    if stage == "reachable":
        return "normalization_missing"
    if stage == "normalized":
        return "price_missing"
    normalization = str(item.get("quote_normalization_status") or "").lower()
    if normalization.startswith("missing") or normalization == "unsupported_quote":
        return "missing_quote_normalization"
    if stage == "priceable" and not _quality_verified(item):
        return str(item.get("regional_candidate_gate_status") or item.get("quality_action") or "quality_unverified")
    reason = str(item.get("candidate_reject_reason") or "").strip()
    if stage == "quality_verified" and (item.get("direction") in {None, "", "watch_only"}):
        return reason or "no_candidate_logic"
    route_status = _route_status(item)
    if stage == "strategy_candidate" and route_status not in ROUTE_OK_STATES:
        return f"route_{route_status}"
    if review and stage == "route_feasible":
        blocks = review.get("hard_blocks") or []
        return str(blocks[0]) if blocks else "paper_review_not_approved"
    return reason or None


def _stage_for(item: dict, review: dict | None, stats: dict) -> str:
    stage = "discovered"
    data_status = str(item.get("data_status") or "reachable").lower()
    if data_status != "reachable":
        return stage
    stage = "reachable"
    if not _is_normalized(item):
        return stage
    stage = "normalized"
    if float(item.get("last") or 0.0) <= 0.0:
        return stage
    stage = "priceable"
    if not _quality_verified(item):
        return stage
    stage = "quality_verified"
    if item.get("direction") in {None, "", "watch_only"}:
        return stage
    stage = "strategy_candidate"
    if _route_status(item) not in ROUTE_OK_STATES:
        return stage
    stage = "route_feasible"
    if not review or review.get("decision") not in {"approve_paper_trade", "approve_conditional_paper_trade"}:
        return stage
    stage = "paper_eligible"
    if int(stats.get("valid_labels") or 0) > 0:
        return "paper_evaluated"
    return stage


def _paper_stats(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    output: dict[tuple[str, str], dict] = {}
    rows = conn.execute(
        """
        select inst_id, signal_key, count(*) as trades,
               sum(case when status = 'closed' then 1 else 0 end) as closed_trades,
               avg(case when status = 'closed' then pnl_bps end) as avg_pnl_bps
        from paper_trades
        group by inst_id, signal_key
        """
    ).fetchall()
    for row in rows:
        output[(str(row["inst_id"]), str(row["signal_key"]))] = dict(row)
    labels = conn.execute(
        """
        select p.inst_id, p.signal_key, count(*) as valid_labels
        from paper_trade_outcomes o
        join paper_trades p on p.id = o.trade_id
        where o.measurement_status = 'valid'
        group by p.inst_id, p.signal_key
        """
    ).fetchall()
    for row in labels:
        output.setdefault((str(row["inst_id"]), str(row["signal_key"])), {})["valid_labels"] = int(row["valid_labels"])
    return output


def _stats_for(item: dict, stats: dict[tuple[str, str], dict]) -> dict:
    inst_id = _text(item.get("inst_id") or item.get("instrument_id"))
    if item.get("direction"):
        key = signal_key(item)
        if (inst_id, key) in stats:
            return stats[(inst_id, key)]
    matching = [value for (stored_inst, _), value in stats.items() if stored_inst == inst_id]
    if not matching:
        return {"trades": 0, "closed_trades": 0, "valid_labels": 0, "avg_pnl_bps": None}
    return {
        "trades": sum(int(value.get("trades") or 0) for value in matching),
        "closed_trades": sum(int(value.get("closed_trades") or 0) for value in matching),
        "valid_labels": sum(int(value.get("valid_labels") or 0) for value in matching),
        "avg_pnl_bps": None,
    }


def _cohort_key(state: dict) -> str:
    identity = "|".join(
        (
            _text(state.get("venue")).upper(),
            _text(state.get("current_stage")),
            _text(state.get("blocker_code")),
            _text(state.get("market_surface")),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _cohort_members(conn: sqlite3.Connection, state: dict) -> list[sqlite3.Row]:
    return conn.execute(
        """
        select admission_key, inst_id, stalled_eligible_scans
        from market_admission_states
        where venue = ? and current_stage = ? and coalesce(blocker_code, '') = ?
          and market_surface = ?
        order by stalled_eligible_scans desc, inst_id asc
        """,
        (
            _text(state.get("venue")).upper(),
            _text(state.get("current_stage")),
            str(state.get("blocker_code") or ""),
            _text(state.get("market_surface")),
        ),
    ).fetchall()


def _upsert_diagnostic(conn: sqlite3.Connection, state: dict, priority: int) -> None:
    cohort_key = _cohort_key(state)
    members = _cohort_members(conn, state)
    market_key = f"market_admission_cohort|{cohort_key}"
    directive = (
        f"Diagnose {state['venue']} {state.get('market_surface')} cohort stalled at "
        f"{state['current_stage']} by {state.get('blocker_code')}"
    )
    evidence = json.dumps(
        {
            "cohort_key": cohort_key,
            "stage": state["current_stage"],
            "blocker": state.get("blocker_code"),
            "market_surface": state.get("market_surface"),
            "instrument_count": len(members),
            "sample_instruments": [str(row["inst_id"]) for row in members[:20]],
            "max_stalled_eligible_scans": max(
                [int(row["stalled_eligible_scans"] or 0) for row in members],
                default=int(state["stalled_eligible_scans"]),
            ),
        },
        sort_keys=True,
    )
    existing = conn.execute(
        "select id from market_hunter_directives where market_key = ? and directive = ? limit 1",
        (market_key, directive),
    ).fetchone()
    if existing:
        conn.execute(
            "update market_hunter_directives set priority = ?, evidence_json = ?, status = 'open' where id = ?",
            (priority, evidence, int(existing["id"])),
        )
    else:
        conn.execute(
            """
            insert into market_hunter_directives
                (created_at, market_key, directive, priority, rationale, evidence_json, status)
            values (?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                utc_now(),
                market_key,
                directive,
                priority,
                "Advance the exact blocked admission stage; do not infer strategy failure from provider or route health.",
                evidence,
            ),
        )


def _upsert_task(conn: sqlite3.Connection, state: dict) -> None:
    cohort_key = _cohort_key(state)
    members = _cohort_members(conn, state)
    samples = [str(row["inst_id"]) for row in members[:20]]
    title = f"Market admission cohort stalled [{cohort_key}]"
    rationale = (
        f"{state['venue']} {state.get('market_surface')} has {len(members)} instruments stalled at "
        f"{state['current_stage']}. Exact blocker: {state.get('blocker_code')}. "
        f"Sample instruments: {', '.join(samples[:10])}. "
        "Fix only the owning admission stage and preserve independent strategy/route evidence."
    )
    conn.execute(
        """
        insert into improvement_tasks (created_at, priority, title, rationale, status)
        values (?, 95, ?, ?, 'open')
        on conflict(title) do update set priority = 95, rationale = excluded.rationale, status = 'open'
        """,
        (utc_now(), title, rationale),
    )
    # Preserve history while removing the pre-cohort one-task-per-instrument flood.
    conn.execute(
        """
        update improvement_tasks
        set status = 'superseded_by_market_admission_cohort'
        where status = 'open' and title like 'Market admission stalled [%'
          and rationale like ? and rationale like ?
        """,
        (f"{state['venue']} %", f"%Exact blocker: {state.get('blocker_code')}%"),
    )


def _resolve_actions(conn: sqlite3.Connection, admission_key: str) -> None:
    conn.execute(
        "update improvement_tasks set status = 'resolved_market_admission_advanced' where title = ? and status = 'open'",
        (f"Market admission stalled [{admission_key}]",),
    )
    conn.execute(
        "update market_hunter_directives set status = 'resolved_market_admission_advanced' where market_key = ? and status = 'open'",
        (f"market_admission|{admission_key}",),
    )


def _reconcile_cohort_tasks(conn: sqlite3.Connection, diagnostic_after: int, task_after: int) -> dict:
    rows = conn.execute(
        """
        select venue, current_stage, blocker_code, market_surface, stalled_eligible_scans
        from market_admission_states
        where blocker_code is not null and stalled_eligible_scans >= ?
        """,
        (int(task_after),),
    ).fetchall()
    active_titles = {
        f"Market admission cohort stalled [{_cohort_key(dict(row))}]"
        for row in rows
    }
    directive_rows = conn.execute(
        """
        select venue, current_stage, blocker_code, market_surface, stalled_eligible_scans
        from market_admission_states
        where blocker_code is not null and stalled_eligible_scans >= ?
        """,
        (int(diagnostic_after),),
    ).fetchall()
    active_directive_keys = {
        f"market_admission_cohort|{_cohort_key(dict(row))}"
        for row in directive_rows
    }
    open_rows = conn.execute(
        "select id, title from improvement_tasks "
        "where status = 'open' and title like 'Market admission cohort stalled [%]%'"
    ).fetchall()
    resolved = 0
    for row in open_rows:
        if str(row["title"]) in active_titles:
            continue
        conn.execute(
            "update improvement_tasks set status = 'resolved_market_admission_advanced' where id = ?",
            (int(row["id"]),),
        )
        resolved += 1
    open_directives = conn.execute(
        "select id, market_key from market_hunter_directives "
        "where status = 'open' and market_key like 'market_admission_cohort|%'"
    ).fetchall()
    resolved_directives = 0
    for row in open_directives:
        if str(row["market_key"]) in active_directive_keys:
            continue
        conn.execute(
            "update market_hunter_directives set status = 'resolved_market_admission_advanced' where id = ?",
            (int(row["id"]),),
        )
        resolved_directives += 1
    legacy_tasks = conn.execute(
        """
        update improvement_tasks
        set status = 'superseded_by_market_admission_cohort'
        where status = 'open'
          and title like 'Market admission stalled [%]'
          and title not like 'Market admission cohort stalled [%]'
        """
    ).rowcount
    legacy_directives = conn.execute(
        """
        update market_hunter_directives
        set status = 'superseded_by_market_admission_cohort'
        where status = 'open' and market_key like 'market_admission|%'
        """
    ).rowcount
    return {
        "active_cohorts": len(active_titles),
        "active_diagnostic_cohorts": len(active_directive_keys),
        "resolved_cohort_tasks": resolved,
        "resolved_cohort_directives": resolved_directives,
        "legacy_instrument_tasks_superseded": int(legacy_tasks),
        "legacy_instrument_directives_superseded": int(legacy_directives),
    }


def _write_report(states: list[dict], settings: dict) -> dict:
    by_stage = Counter(item["current_stage"] for item in states)
    by_health = Counter(item["health_status"] for item in states)
    by_blocker = Counter(item.get("blocker_code") for item in states if item.get("blocker_code"))
    requested = set((settings.get("market_admission") or {}).get("requested_symbols") or [])
    requested_rows = [item for item in states if str(item.get("inst_id") or "").split(":")[-1] in requested]
    summary = {
        "generated_at": utc_now(),
        "state_count": len(states),
        "by_stage": dict(by_stage),
        "by_health": dict(by_health),
        "by_blocker": dict(by_blocker),
        "requested_symbol_count": len(requested),
        "requested_symbols_observed": len({str(item["inst_id"]).split(":")[-1] for item in requested_rows}),
        "paper_eligible_count": sum(STAGE_INDEX[item["highest_stage"]] >= STAGE_INDEX["paper_eligible"] for item in states),
        "paper_evaluated_count": sum(item["highest_stage"] == "paper_evaluated" for item in states),
    }
    payload = {"summary": summary, "requested_markets": requested_rows, "states": states}
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Market Admission Report",
        "",
        f"- Generated: `{summary['generated_at']}`",
        f"- States: `{summary['state_count']}`",
        f"- By stage: `{summary['by_stage']}`",
        f"- By health: `{summary['by_health']}`",
        f"- By blocker: `{summary['by_blocker']}`",
        f"- Requested symbols observed: `{summary['requested_symbols_observed']}/{summary['requested_symbol_count']}`",
        "",
        "## Requested Markets",
        "",
    ]
    for item in requested_rows:
        lines.append(
            f"- `{item['inst_id']}` stage=`{item['current_stage']}` highest=`{item['highest_stage']}` "
            f"health=`{item['health_status']}` blocker=`{item.get('blocker_code')}` labels=`{item['details'].get('valid_labels', 0)}`"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def run_market_admission_monitor(
    conn: sqlite3.Connection,
    settings: dict,
    candidates: list[dict],
    reviewed: list[dict],
    observations: list[dict] | None = None,
) -> dict:
    """Persist current onboarding states and create stage-specific diagnostics."""

    cfg = settings.get("market_admission", {})
    if not cfg.get("enabled", True):
        return {"summary": {"enabled": False}, "states": []}
    review_by_identity = {
        (str(item["candidate"].get("inst_id")), _lineage(item["candidate"])): item.get("review") or {}
        for item in reviewed
    }
    combined: dict[tuple[str, str], dict] = {}
    for item in observations or []:
        normalized = dict(item)
        if not normalized.get("inst_id"):
            normalized["inst_id"] = normalized.get("instrument_id")
        combined[(str(normalized.get("inst_id")), _lineage(normalized))] = normalized
    for item in candidates:
        combined[(str(item.get("inst_id")), _lineage(item))] = dict(item)

    now = utc_now()
    paper_stats = _paper_stats(conn)
    degraded_after = int(cfg.get("consecutive_failures_degraded", 5))
    diagnostic_after = int(cfg.get("diagnostic_after_eligible_scans", 30))
    task_after = int(cfg.get("implementation_task_after_eligible_scans", 120))
    touched: list[str] = []
    for item in combined.values():
        if not item.get("inst_id") or not item.get("venue"):
            continue
        admission_key = _admission_key(item)
        lineage = _lineage(item)
        review = review_by_identity.get((str(item.get("inst_id")), lineage))
        stats = _stats_for(item, paper_stats)
        current_stage = _stage_for(item, review, stats)
        session_status = _session_status(item)
        blocker = _blocker(item, review, current_stage)
        eligible = session_status in ACTIVE_SESSION_STATES
        previous = conn.execute(
            "select * from market_admission_states where admission_key = ?",
            (admission_key,),
        ).fetchone()
        previous_stage = str(previous["current_stage"]) if previous else "discovered"
        previous_highest = str(previous["highest_stage"]) if previous else "discovered"
        advanced = STAGE_INDEX[current_stage] > STAGE_INDEX[previous_highest]
        highest_stage = current_stage if advanced else previous_highest
        stalled = 0 if advanced else int(previous["stalled_eligible_scans"] or 0) if previous else 0
        if eligible and not advanced and current_stage != "paper_evaluated":
            stalled += 1
        consecutive_failures = 0
        if blocker and blocker != "market_closed":
            consecutive_failures = int(previous["consecutive_failures"] or 0) + 1 if previous else 1
        if blocker == "market_closed":
            health_status = "market_closed"
        elif consecutive_failures >= degraded_after:
            health_status = "blocked" if current_stage == "discovered" else "degraded"
        elif blocker:
            health_status = "waiting"
        else:
            health_status = "healthy"
        details = {
            **stats,
            "direction": item.get("direction"),
            "trade_type": item.get("trade_type"),
            "route_status": _route_status(item),
            "quality_status": item.get("quality_status") or item.get("proxy_quality_status"),
            "quote_normalization_status": item.get("quote_normalization_status"),
            "candidate_reject_reason": item.get("candidate_reject_reason"),
            "http_status": item.get("http_status"),
            "last": item.get("last"),
            "review_decision": (review or {}).get("decision"),
        }
        conn.execute(
            """
            insert into market_admission_states (
                admission_key, venue, inst_id, data_source, market_surface, strategy_lineage,
                current_stage, highest_stage, health_status, blocker_code, session_status,
                attempts, eligible_scans, stalled_eligible_scans, consecutive_failures,
                first_seen_at, last_seen_at, last_advanced_at, details_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            on conflict(admission_key) do update set
                current_stage = excluded.current_stage,
                highest_stage = excluded.highest_stage,
                health_status = excluded.health_status,
                blocker_code = excluded.blocker_code,
                session_status = excluded.session_status,
                attempts = market_admission_states.attempts + 1,
                eligible_scans = market_admission_states.eligible_scans + excluded.eligible_scans,
                stalled_eligible_scans = excluded.stalled_eligible_scans,
                consecutive_failures = excluded.consecutive_failures,
                last_seen_at = excluded.last_seen_at,
                last_advanced_at = excluded.last_advanced_at,
                data_source = excluded.data_source,
                details_json = excluded.details_json
            """,
            (
                admission_key,
                _text(item.get("venue")).upper(),
                _text(item.get("inst_id") or item.get("instrument_id")),
                _data_source(item),
                _surface(item),
                lineage,
                current_stage,
                highest_stage,
                health_status,
                blocker,
                session_status,
                1 if eligible else 0,
                stalled,
                consecutive_failures,
                str(previous["first_seen_at"]) if previous else now,
                now,
                now if advanced else str(previous["last_advanced_at"]) if previous else now,
                json.dumps(details, sort_keys=True),
            ),
        )
        if advanced and STAGE_INDEX[current_stage] > STAGE_INDEX[previous_stage]:
            _resolve_actions(conn, admission_key)
        state_for_action = {
            "admission_key": admission_key,
            "venue": _text(item.get("venue")).upper(),
            "inst_id": _text(item.get("inst_id") or item.get("instrument_id")),
            "current_stage": current_stage,
            "eligible_scans": (int(previous["eligible_scans"] or 0) if previous else 0) + (1 if eligible else 0),
            "stalled_eligible_scans": stalled,
            "blocker_code": blocker,
            "market_surface": _surface(item),
            "strategy_lineage": lineage,
        }
        if stalled >= task_after:
            _upsert_task(conn, state_for_action)
        elif stalled >= diagnostic_after:
            _upsert_diagnostic(conn, state_for_action, 85)
        touched.append(admission_key)
    cohort_tasks = _reconcile_cohort_tasks(conn, diagnostic_after, task_after)
    conn.commit()

    if touched:
        placeholders = ",".join("?" for _ in touched)
        rows = conn.execute(
            f"select * from market_admission_states where admission_key in ({placeholders}) order by venue, inst_id",
            touched,
        ).fetchall()
    else:
        rows = []
    states = []
    for row in rows:
        output = dict(row)
        output["details"] = json.loads(output.pop("details_json") or "{}")
        states.append(output)
    report = _write_report(states, settings)
    report["summary"]["task_cohorts"] = cohort_tasks
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
