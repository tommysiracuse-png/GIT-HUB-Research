"""Convert precise market-admission states into one canonical next action."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from typing import Any

from recommendation_registry import bind_artifact, claim_topic, set_topic_status
from market_admission import STAGE_INDEX
from storage import RUNS_DIR, add_hunter_directive, add_improvement_task, add_route_probe_task
from strategy_lab import ingest_strategy_lab_recommendation


REPORT_JSON = RUNS_DIR / "market_admission_bridge.json"
REPORT_MD = RUNS_DIR / "market_admission_bridge.md"
RESOLVABLE_TABLES = {
    "improvement_tasks",
    "growth_experiments",
    "market_hunter_directives",
    "adapter_specs",
    "route_probe_tasks",
}
EEX_SECONDARY_SPOT_SURFACE = "eex_eu_ets_secondary_spot_trades"
EEX_SECONDARY_SPOT_LAB_ID = "eex_eu_ets_secondary_spot_reported_trade_v1"
ADX_DERIVATIVES_SURFACE = "adx_equity_and_index_futures_contract_catalog"
ADX_DERIVATIVES_LAB_ID = "adx_derivatives_companion_quote_v1"


def _strategy_lab_id(state: dict[str, Any]) -> str:
    return "admission_discovery_" + hashlib.sha256(str(state["admission_key"]).encode("utf-8")).hexdigest()[:16]


def _evidence(state: dict[str, Any]) -> dict[str, Any]:
    details = state.get("details") if isinstance(state.get("details"), dict) else {}
    return {
        "admission_key": state.get("admission_key"),
        "venue": state.get("venue"),
        "inst_id": state.get("inst_id"),
        "market_surface": state.get("market_surface"),
        "current_stage": state.get("current_stage"),
        "highest_stage": state.get("highest_stage"),
        "blocker_code": state.get("blocker_code"),
        "session_status": state.get("session_status"),
        "quality_status": details.get("quality_status"),
        "route_status": details.get("route_status"),
        "valid_labels": details.get("valid_labels", 0),
        "instrument_count": details.get("instrument_count", 1),
        "sample_instruments": details.get("sample_instruments", [state.get("inst_id")]),
    }


def _group_actionable_states(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for state in states:
        stage = str(state.get("current_stage") or "")
        if stage not in {"priceable", "quality_verified", "strategy_candidate", "route_feasible"}:
            continue
        if str(state.get("session_status") or "") == "closed":
            continue
        lineage = str(state.get("strategy_lineage") or "")
        if stage == "quality_verified" and lineage != "adapter_observation":
            continue
        key = (
            stage,
            str(state.get("venue") or "unknown"),
            str(state.get("market_surface") or "unknown"),
            str(state.get("blocker_code") or "none"),
            lineage,
        )
        groups.setdefault(key, []).append(state)
    output: list[dict[str, Any]] = []
    for key, members in groups.items():
        stage, venue, surface, blocker, lineage = key
        stable_identity = "|".join((venue, surface, lineage))
        digest = hashlib.sha256(stable_identity.encode("utf-8")).hexdigest()[:20]
        representative = dict(members[0])
        representative.update(
            {
                "admission_key": f"group-{digest}",
                "inst_id": f"{venue}:{surface}:GROUP",
                "venue": venue,
                "market_surface": surface,
                "strategy_lineage": lineage,
                "current_stage": stage,
                "blocker_code": None if blocker == "none" else blocker,
            }
        )
        details = dict(representative.get("details") or {})
        details.update(
            {
                "instrument_count": len(members),
                "sample_instruments": [str(item.get("inst_id")) for item in members[:20]],
                "member_admission_keys": [str(item.get("admission_key")) for item in members[:100]],
            }
        )
        representative["details"] = details
        output.append(representative)
    stage_priority = {"strategy_candidate": 0, "route_feasible": 1, "quality_verified": 2, "priceable": 3}
    return sorted(
        output,
        key=lambda item: (
            stage_priority.get(str(item.get("current_stage")), 9),
            -int((item.get("details") or {}).get("instrument_count") or 0),
            str(item.get("venue")),
        ),
    )


def _claim(conn: sqlite3.Connection, state: dict[str, Any], action: str, priority: int):
    evidence = _evidence(state)
    payload = {
        **evidence,
        "market_key": f"{state.get('venue')}|{state.get('inst_id')}",
        "strategy_lineage": state.get("strategy_lineage"),
        "admission_stage": state.get("current_stage"),
        "blocker_code": state.get("blocker_code"),
        "title": f"Advance {state.get('venue')} {state.get('inst_id')} from {state.get('current_stage')}",
        "proposed_change": action,
        "action": action,
    }
    return claim_topic(
        conn,
        payload=payload,
        topic_type="market_admission_action",
        priority=priority,
        evidence=evidence,
        source_ref=f"market_admission:{state.get('admission_key')}:{state.get('current_stage')}:{action}",
    )


def _resolve_prior_stage_topics(conn: sqlite3.Connection, state: dict[str, Any]) -> int:
    current_stage = str(state.get("current_stage") or "discovered")
    current_index = STAGE_INDEX.get(current_stage, 0)
    rows = conn.execute(
        """
        select distinct t.topic_key, t.status, t.canonical_table, t.canonical_row_id, t.descriptor_json
        from recommendation_topics t
        join recommendation_topic_sources s on s.topic_key = t.topic_key
        where s.source_ref like ? and t.status = 'open'
        """,
        (f"market_admission:{state.get('admission_key')}:%",),
    ).fetchall()
    resolved = 0
    for row in rows:
        descriptor = json.loads(row["descriptor_json"] or "{}")
        prior_stage = str(descriptor.get("admission_stage") or "discovered")
        if STAGE_INDEX.get(prior_stage, 0) >= current_index:
            continue
        set_topic_status(conn, row["topic_key"], "resolved_market_admission_advanced")
        table = str(row["canonical_table"] or "")
        row_id = str(row["canonical_row_id"] or "")
        if table in RESOLVABLE_TABLES and row_id.isdigit():
            conn.execute(
                f"update {table} set status = 'resolved_market_admission_advanced' where id = ? and status = 'open'",
                (int(row_id),),
            )
        resolved += 1
    return resolved


def _create_enrichment(conn: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
    claim = _claim(conn, state, "enrich_executable_quality", 85)
    if claim.duplicate and claim.canonical_row_id:
        return {"action": "enrich_executable_quality", "status": "deduplicated", "topic_key": claim.topic_key}
    market_key = f"market_admission|{state['admission_key']}"
    directive = "enrich_executable_quality"
    add_hunter_directive(
        conn,
        market_key,
        directive,
        85,
        "Collect fresh order-book, normalization, liquidity, and quality evidence before creating strategy entries.",
        _evidence(state),
    )
    row = conn.execute(
        "select id from market_hunter_directives where market_key = ? and directive = ? order by id desc limit 1",
        (market_key, directive),
    ).fetchone()
    if row:
        bind_artifact(conn, claim.topic_key, "market_hunter_directives", row["id"])
    return {"action": directive, "status": "created", "topic_key": claim.topic_key}


def _create_eex_secondary_spot_program(
    conn: sqlite3.Connection,
    settings: dict,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Create the canonical paper-only program for reported EEX spot trades.

    EEX's public DataSource row is a completed trade, not an executable
    quote. It is nevertheless a priceable official observation. The
    program preserves that distinction and lets the normal exploration layer
    select its existing synthetic research route rather than inventing a
    broker or venue route.
    """

    claim = _claim(conn, state, "activate_eex_secondary_spot_paper_research", 90)
    if claim.duplicate and claim.canonical_row_id:
        return {"action": "strategy_lab_eex_spot_program", "status": "deduplicated", "topic_key": claim.topic_key}
    evidence = _evidence(state)
    rec = {
        "recommendation_id": f"market_admission:{state['admission_key']}:eex_secondary_spot_program",
        "source_agent": "market_admission_bridge",
        "payload": {
            "agent_name": "market_admission_bridge",
            "title": "Paper-test EEX EU ETS reported secondary spot trades",
            "rationale": (
                "Fresh, validated official EEX secondary spot trades provide public price observations. "
                "They are tested only as synthetic research and never as executable EEX quotes."
            ),
            "strategy_lab_experiment": {
                "strategy_lab_id": EEX_SECONDARY_SPOT_LAB_ID,
                "experiment_type": "market_strategy",
                "hypothesis": (
                    "A fresh validated EEX EUA or EUAA reported secondary spot trade supports a "
                    "same-surface synthetic paper continuation measurement."
                ),
                "source_surface": EEX_SECONDARY_SPOT_SURFACE,
                "permitted_target_surface": [EEX_SECONDARY_SPOT_SURFACE],
                "strategy_logic": {
                    "type": "observation_program",
                    "universe": {
                        "venues": ["EEX"],
                        "asset_classes": ["emission_allowance"],
                        "market_types": ["spot_trade_reference"],
                        "market_surfaces": [EEX_SECONDARY_SPOT_SURFACE],
                    },
                    "calculated_features": {
                        "reported_trade_volume_signal": "log1p(reported_trade_volume)",
                        "reported_trade_validation_signal": "reported_trade_valid",
                    },
                    "entry_expression": (
                        "quality_status == 'official_reported_trade' "
                        "and candidate_reject_reason == 'reported_spot_trade_not_executable_quote' "
                        "and freshness_state == 'fresh' and reported_trade_price > 0"
                    ),
                    "invalidation_expression": (
                        "freshness_state != 'fresh' or reported_trade_price <= 0"
                    ),
                    "direction": "long",
                    "edge_expression": "return_5m_bps",
                    "score_expression": (
                        "clip(50 + return_5m_bps / 4 + reported_trade_volume_signal "
                        "+ reported_trade_validation_signal, 0, 100)"
                    ),
                    "route_surface": "proxy",
                },
                "data_requirements": {
                    "admission_key": state.get("admission_key"),
                    "market_surface": EEX_SECONDARY_SPOT_SURFACE,
                    "required_fields": [
                        "last",
                        "reported_trade_price",
                        "reported_trade_volume",
                        "reported_trade_valid",
                        "price_source",
                        "source_url",
                    ],
                    "requires_independent_signal_logic": True,
                    "paper_only_reference": True,
                },
                "risk_gates": {
                    "require_route_feasible": False,
                    "paper_allocation_multiplier": 0.25,
                    "synthetic_research_only": True,
                },
            },
            "evidence": evidence,
        },
    }
    result = ingest_strategy_lab_recommendation(conn, rec, settings)[0]
    if result.get("strategy_lab_id"):
        bind_artifact(conn, claim.topic_key, "strategy_lab_experiments", result["strategy_lab_id"])
    return {
        "action": "strategy_lab_eex_spot_program",
        "status": "created" if result.get("created") else "updated",
        "strategy_lab_id": result.get("strategy_lab_id"),
        "topic_key": claim.topic_key,
    }


def _create_adx_derivatives_companion_program(
    conn: sqlite3.Connection,
    settings: dict,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Create the canonical paper-only program for ADX derivatives companion quotes.

    ADX's public derivatives surface identifies contracts but does not publish a
    futures quote or execution route. The adapter repairs this by attaching a
    defensible public companion quote for the SSF underlyings. This program
    turns those same-surface observations into paper proxy experiments on the
    existing equity proxy route without implying a live ADX futures order path.
    """

    claim = _claim(conn, state, "activate_adx_derivatives_companion_program", 90)
    if claim.duplicate and claim.canonical_row_id:
        return {
            "action": "strategy_lab_adx_derivatives_program",
            "status": "deduplicated",
            "topic_key": claim.topic_key,
        }
    evidence = _evidence(state)
    rec = {
        "recommendation_id": f"market_admission:{state['admission_key']}:adx_derivatives_program",
        "source_agent": "market_admission_bridge",
        "payload": {
            "agent_name": "market_admission_bridge",
            "title": "Paper-test ADX derivatives via public companion quotes",
            "rationale": (
                "ADX's public derivatives catalog is reference-only, but the repaired adapter now attaches "
                "fresh public companion prices for the listed SSF underlyings. Those prices can support "
                "same-surface paper proxy experiments without claiming a futures order route."
            ),
            "strategy_lab_experiment": {
                "strategy_lab_id": ADX_DERIVATIVES_LAB_ID,
                "experiment_type": "market_strategy",
                "hypothesis": (
                    "Fresh public companion quotes for ADX single-stock futures underlyings support "
                    "same-surface paper continuation experiments until a direct futures quote path exists."
                ),
                "source_surface": ADX_DERIVATIVES_SURFACE,
                "permitted_target_surface": [ADX_DERIVATIVES_SURFACE],
                "strategy_logic": {
                    "type": "observation_program",
                    "universe": {
                        "venues": ["ADX"],
                        "trade_types": ["official_derivatives_contract_reference"],
                        "market_surfaces": [ADX_DERIVATIVES_SURFACE],
                    },
                    "calculated_features": {
                        "companion_return_strength_bps": "abs(return_5m_bps)",
                    },
                    "entry_expression": (
                        "market_surface == 'adx_equity_and_index_futures_contract_catalog' "
                        "and price_basis == 'public_companion_underlying_spot_quote' "
                        "and quality_status == 'verified_proxy' and freshness_state == 'fresh' "
                        "and last > 0"
                    ),
                    "invalidation_expression": (
                        "freshness_state != 'fresh' or last <= 0"
                    ),
                    "long_expression": "return_5m_bps > 0",
                    "short_expression": "return_5m_bps < 0",
                    "edge_expression": "companion_return_strength_bps",
                    "score_expression": (
                        "clip(45 + companion_return_strength_bps / 4, 0, 100)"
                    ),
                    "route_surface": "proxy",
                },
                "data_requirements": {
                    "admission_key": state.get("admission_key"),
                    "market_surface": ADX_DERIVATIVES_SURFACE,
                    "required_fields": [
                        "last",
                        "price_basis",
                        "quality_status",
                        "freshness_state",
                        "price_source",
                        "source_url",
                        "source_contract_url",
                        "companion_quote_symbol",
                    ],
                    "requires_independent_signal_logic": True,
                    "paper_only_reference": True,
                },
                "risk_gates": {
                    "require_route_feasible": False,
                    "paper_allocation_multiplier": 0.25,
                },
            },
            "evidence": evidence,
        },
    }
    result = ingest_strategy_lab_recommendation(conn, rec, settings)[0]
    if result.get("strategy_lab_id"):
        bind_artifact(conn, claim.topic_key, "strategy_lab_experiments", result["strategy_lab_id"])
    return {
        "action": "strategy_lab_adx_derivatives_program",
        "status": "created" if result.get("created") else "updated",
        "strategy_lab_id": result.get("strategy_lab_id"),
        "topic_key": claim.topic_key,
    }


def _create_strategy_discovery(
    conn: sqlite3.Connection,
    settings: dict,
    state: dict[str, Any],
) -> dict[str, Any]:
    claim = _claim(conn, state, "discover_surface_specific_strategy", 90)
    if claim.duplicate and claim.canonical_row_id:
        return {"action": "strategy_lab_discovery", "status": "deduplicated", "topic_key": claim.topic_key}
    strategy_lab_id = _strategy_lab_id(state)
    evidence = _evidence(state)
    rec = {
        "recommendation_id": f"market_admission:{state['admission_key']}:strategy_lab",
        "source_agent": "market_admission_bridge",
        "payload": {
            "agent_name": "market_admission_bridge",
            "title": f"Discover strategy for {state.get('venue')} {state.get('inst_id')}",
            "rationale": "The market is priceable and quality-verified but has no independent strategy candidate lineage.",
            "strategy_lab_experiment": {
                "strategy_lab_id": strategy_lab_id,
                "experiment_type": "market_strategy",
                "hypothesis": (
                    f"Find a repeatable, surface-specific paper strategy for {state.get('venue')} "
                    f"{state.get('inst_id')} using its normalized market features and relative context."
                ),
                "source_surface": state.get("market_surface"),
                "permitted_target_surface": [state.get("market_surface")],
                "strategy_logic": {
                    "type": "candidate_selector",
                    "allowed_venues": [state.get("venue")],
                    "required_fields": ["last", "liquidity_score", "spread_bps"],
                    "max_candidates_per_loop": 2,
                },
                "data_requirements": {
                    "admission_key": state.get("admission_key"),
                    "inst_id": state.get("inst_id"),
                    "market_surface": state.get("market_surface"),
                    "source_surface": state.get("market_surface"),
                    "permitted_target_surface": [state.get("market_surface")],
                    "requires_independent_signal_logic": True,
                },
                "risk_gates": {"require_route_feasible": True},
            },
            "evidence": evidence,
        },
    }
    result = ingest_strategy_lab_recommendation(conn, rec, settings)[0]
    if result.get("strategy_lab_id"):
        bind_artifact(conn, claim.topic_key, "strategy_lab_experiments", result["strategy_lab_id"])
    return {
        "action": "strategy_lab_discovery",
        "status": "created" if result.get("created") else "updated",
        "strategy_lab_id": result.get("strategy_lab_id"),
        "topic_key": claim.topic_key,
    }


def _create_route_action(conn: sqlite3.Connection, settings: dict, state: dict[str, Any]) -> dict[str, Any]:
    blocker = str(state.get("blocker_code") or "route_unknown")
    claim = _claim(conn, state, "resolve_route_feasibility", 88)
    if blocker in {"route_spot_borrow", "spot_borrow"} and not settings.get("account_capabilities", {}).get("spot_borrow", False):
        set_topic_status(conn, claim.topic_key, "resolved_user_capability_no_spot_borrow", implemented_category="user_capability")
        return {"action": "route_decision", "status": "suppressed_user_capability", "topic_key": claim.topic_key}
    if claim.duplicate and claim.canonical_row_id:
        return {"action": "route_probe", "status": "deduplicated", "topic_key": claim.topic_key}
    created = add_route_probe_task(
        conn,
        f"market_admission:{state['admission_key']}",
        f"market_admission|{state['admission_key']}",
        blocker,
        88,
        "market_admission_route_probe",
        "Verify the exact read-only route requirement blocking paper admission.",
        _evidence(state),
    )
    row = conn.execute(
        "select id from route_probe_tasks where source_recommendation_id = ? order by id desc limit 1",
        (f"market_admission:{state['admission_key']}",),
    ).fetchone()
    if row:
        bind_artifact(conn, claim.topic_key, "route_probe_tasks", row["id"])
    return {"action": "route_probe", "status": "created" if created else "deduplicated", "topic_key": claim.topic_key}


def _create_review_diagnostic(conn: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
    claim = _claim(conn, state, "diagnose_paper_review_rejection", 90)
    if claim.duplicate and claim.canonical_row_id:
        return {"action": "paper_review_diagnostic", "status": "deduplicated", "topic_key": claim.topic_key}
    title = f"Paper admission review blocked [{state['admission_key']}]"
    add_improvement_task(
        conn,
        90,
        title,
        f"Diagnose the exact review blocker for {state.get('venue')} {state.get('inst_id')} without changing unrelated data or strategy health.",
    )
    row = conn.execute("select id from improvement_tasks where title = ?", (title,)).fetchone()
    if row:
        bind_artifact(conn, claim.topic_key, "improvement_tasks", row["id"])
    return {"action": "paper_review_diagnostic", "status": "created", "topic_key": claim.topic_key}


def run_market_admission_bridge(conn: sqlite3.Connection, settings: dict, admission_report: dict) -> dict:
    if not settings.get("market_admission", {}).get("enabled", True):
        return {"summary": {"enabled": False}, "actions": []}
    actions: list[dict[str, Any]] = []
    resolved_topics = 0
    states = list(admission_report.get("states") or [])
    for state in states:
        resolved_topics += _resolve_prior_stage_topics(conn, state)
    grouped_states = _group_actionable_states(states)
    max_actions = int(settings.get("market_admission", {}).get("bridge_max_actions_per_loop", 50))
    for state in grouped_states[:max_actions]:
        resolved_topics += _resolve_prior_stage_topics(conn, state)
        stage = str(state.get("current_stage") or "")
        if stage == "priceable":
            if (
                str(state.get("venue") or "").upper() == "EEX"
                and str(state.get("market_surface") or "") == EEX_SECONDARY_SPOT_SURFACE
            ):
                actions.append(_create_eex_secondary_spot_program(conn, settings, state))
            else:
                actions.append(_create_enrichment(conn, state))
        elif stage == "quality_verified" and str(state.get("strategy_lineage") or "") == "adapter_observation":
            if (
                str(state.get("venue") or "").upper() == "ADX"
                and str(state.get("market_surface") or "") == ADX_DERIVATIVES_SURFACE
            ):
                actions.append(_create_adx_derivatives_companion_program(conn, settings, state))
            else:
                actions.append(_create_strategy_discovery(conn, settings, state))
        elif stage == "strategy_candidate":
            actions.append(_create_route_action(conn, settings, state))
        elif stage == "route_feasible":
            actions.append(_create_review_diagnostic(conn, state))
    conn.commit()
    summary = {
        "enabled": True,
        "states_considered": len(admission_report.get("states") or []),
        "action_groups_considered": len(grouped_states),
        "action_groups_deferred": max(0, len(grouped_states) - max_actions),
        "actions_created": sum(item.get("status") == "created" for item in actions),
        "actions_updated": sum(item.get("status") == "updated" for item in actions),
        "duplicates_suppressed": sum(item.get("status") == "deduplicated" for item in actions),
        "user_constraints_suppressed": sum(item.get("status") == "suppressed_user_capability" for item in actions),
        "by_action": dict(Counter(item.get("action") for item in actions)),
        "prior_stage_topics_resolved": resolved_topics,
    }
    payload = {"summary": summary, "actions": actions}
    REPORT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Market Admission Bridge",
        "",
        f"- States considered: `{summary['states_considered']}`",
        f"- Actions created: `{summary['actions_created']}`",
        f"- Duplicates suppressed: `{summary['duplicates_suppressed']}`",
        f"- User constraints suppressed: `{summary['user_constraints_suppressed']}`",
        f"- By action: `{summary['by_action']}`",
    ]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload
