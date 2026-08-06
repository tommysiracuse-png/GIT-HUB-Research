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
ICDX_CPOTR_SURFACE = "icdx_cpotr"
ICDX_CPOTR_LAB_ID = "icdx_cpotr_price_card_reference_v1"
ICDX_MILESTONES_SURFACE = "icdx_exchange_milestones"
ICDX_MILESTONES_LAB_ID = "icdx_exchange_milestones_companion_v1"
CARB_ALLOWANCE_SURFACE = "california_quebec_cap_and_invest_joint_allowance_auctions"
CARB_ALLOWANCE_LAB_ID = "carb_joint_allowance_discount_tightness_v1"
ANP_OPC_SURFACE = "anp_oferta_permanente_de_concessao"
ANP_OPC_LAB_ID = "anp_opc_brazil_upstream_proxy_v1"
AOFM_SURFACE = "australian_treasury_bond_tenders_and_results"
AOFM_LAB_ID = "aofm_treasury_bond_tender_strength_v1"


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
        if (
            str(state.get("session_status") or "") == "closed"
            and not _is_carb_closed_allowance_reference_state(state)
        ):
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


def _is_carb_closed_allowance_reference_state(state: dict[str, Any]) -> bool:
    details = state.get("details") if isinstance(state.get("details"), dict) else {}
    return (
        str(state.get("current_stage") or "") == "priceable"
        and str(state.get("venue") or "").upper() == "CARB_CA_QC"
        and str(state.get("market_surface") or "") == CARB_ALLOWANCE_SURFACE
        and str(details.get("quality_status") or "") == "official_auction_result"
        and str(details.get("candidate_reject_reason") or "")
        == "official_allowance_auction_reference_not_order_routable"
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


def _create_icdx_cpotr_price_card_program(
    conn: sqlite3.Connection,
    settings: dict,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Create the canonical ICDX CPOTR synthetic paper program.

    ICDX publishes paired suggested-opening and previous-settlement values for
    CPOTR on its homepage price card. Those are defensible public prices, but
    they are still reference data rather than an executable order route. The
    program measures same-surface paper continuation through the existing
    synthetic research path.
    """

    claim = _claim(conn, state, "activate_icdx_cpotr_price_card_paper_research", 90)
    if claim.duplicate and claim.canonical_row_id:
        return {
            "action": "strategy_lab_icdx_cpotr_program",
            "status": "deduplicated",
            "topic_key": claim.topic_key,
        }
    evidence = _evidence(state)
    details = state.get("details") if isinstance(state.get("details"), dict) else {}
    rec = {
        "recommendation_id": f"market_admission:{state['admission_key']}:icdx_cpotr_program",
        "source_agent": "market_admission_bridge",
        "payload": {
            "agent_name": "market_admission_bridge",
            "title": "Paper-test ICDX CPOTR official price-card continuation",
            "rationale": (
                "ICDX's paired CPOTR suggested-opening and previous-settlement price-card values "
                "provide public same-surface price evidence. They remain synthetic paper research "
                "only and do not imply an executable ICDX route."
            ),
            "strategy_lab_experiment": {
                "strategy_lab_id": ICDX_CPOTR_LAB_ID,
                "experiment_type": "market_strategy",
                "hypothesis": (
                    "When ICDX publishes a paired CPOTR suggested opening and previous settlement, "
                    "the opening-gap direction supports a same-surface synthetic paper continuation test."
                ),
                "source_surface": ICDX_CPOTR_SURFACE,
                "permitted_target_surface": [ICDX_CPOTR_SURFACE],
                "strategy_logic": {
                    "type": "observation_program",
                    "universe": {
                        "venues": ["ICDX"],
                        "trade_types": ["official_price_card_reference"],
                        "market_surfaces": [ICDX_CPOTR_SURFACE],
                    },
                    "calculated_features": {
                        "cpotr_opening_gap_abs_bps": "abs(cpotr_opening_gap_bps)",
                    },
                    "entry_expression": (
                        "market_surface == 'icdx_cpotr' "
                        "and quality_status == 'official_price_card' "
                        "and candidate_reject_reason == 'public_price_card_not_execution_route' "
                        "and freshness_state == 'fresh' "
                        "and cpotr_price_card_pair_observed >= 1 "
                        "and price_type == 'previous_settlement' "
                        "and previous_settlement_price > 0 "
                        "and suggested_opening_price > 0"
                    ),
                    "invalidation_expression": (
                        "freshness_state != 'fresh' or cpotr_price_card_pair_observed < 1 "
                        "or previous_settlement_price <= 0 or suggested_opening_price <= 0"
                    ),
                    "long_expression": "cpotr_opening_gap_bps > 0",
                    "short_expression": "cpotr_opening_gap_bps < 0",
                    "edge_expression": "cpotr_opening_gap_abs_bps",
                    "score_expression": (
                        "clip(45 + min(cpotr_opening_gap_abs_bps, 80) / 2 "
                        "+ 5 * cpotr_price_card_pair_observed, 0, 100)"
                    ),
                    "route_surface": "proxy",
                },
                "data_requirements": {
                    "admission_key": state.get("admission_key"),
                    "adapter_id": details.get("adapter_id"),
                    "market_surface": ICDX_CPOTR_SURFACE,
                    "required_fields": [
                        "last",
                        "contract_month",
                        "price_type",
                        "price_basis",
                        "price_source",
                        "source_url",
                        "cpotr_price_card_pair_observed",
                        "suggested_opening_price",
                        "previous_settlement_price",
                        "cpotr_opening_gap_bps",
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
        "action": "strategy_lab_icdx_cpotr_program",
        "status": "created" if result.get("created") else "updated",
        "strategy_lab_id": result.get("strategy_lab_id"),
        "topic_key": claim.topic_key,
    }


def _create_icdx_milestones_companion_program(
    conn: sqlite3.Connection,
    settings: dict,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Create the canonical ICDX milestone companion program.

    The ICDX milestone page is reference-only, but the repaired adapter now
    preserves those launch-year facts on the same surface while attaching a
    defensible public CPOTR homepage companion price. The program turns that
    exact-surface context into synthetic paper research without claiming an
    executable ICDX order route.
    """

    claim = _claim(conn, state, "activate_icdx_exchange_milestones_companion_program", 90)
    if claim.duplicate and claim.canonical_row_id:
        return {
            "action": "strategy_lab_icdx_milestones_program",
            "status": "deduplicated",
            "topic_key": claim.topic_key,
        }
    evidence = _evidence(state)
    details = state.get("details") if isinstance(state.get("details"), dict) else {}
    rec = {
        "recommendation_id": f"market_admission:{state['admission_key']}:icdx_milestones_program",
        "source_agent": "market_admission_bridge",
        "payload": {
            "agent_name": "market_admission_bridge",
            "title": "Paper-test ICDX exchange milestones via CPOTR companion pricing",
            "rationale": (
                "ICDX's milestone timeline is reference-only, but the repaired adapter now keeps those launch "
                "facts on the same surface while attaching a public CPOTR price-card companion. That preserves "
                "exact provenance and enables same-surface synthetic paper research without implying an executable "
                "ICDX route."
            ),
            "strategy_lab_experiment": {
                "strategy_lab_id": ICDX_MILESTONES_LAB_ID,
                "experiment_type": "market_strategy",
                "hypothesis": (
                    "When ICDX's milestone surface carries a fresh CPOTR price-card companion, the signed CPOTR "
                    "opening gap can drive same-surface synthetic paper tests while the launch-year timeline "
                    "anchors the structural market context."
                ),
                "source_surface": ICDX_MILESTONES_SURFACE,
                "permitted_target_surface": [ICDX_MILESTONES_SURFACE],
                "strategy_logic": {
                    "type": "observation_program",
                    "universe": {
                        "venues": ["ICDX"],
                        "trade_types": ["official_market_milestone_reference"],
                        "market_surfaces": [ICDX_MILESTONES_SURFACE],
                    },
                    "calculated_features": {
                        "milestone_reference_depth_years": (
                            "years_since_cpotr_launch + years_since_gofx_launch + "
                            "years_since_crude_oil_contract_launch"
                        ),
                        "milestone_proxy_gap_abs_bps": "abs(cpotr_opening_gap_bps)",
                        "milestone_structural_signal": "min(milestone_reference_depth_years, 40)",
                    },
                    "entry_expression": (
                        "market_surface == 'icdx_exchange_milestones' "
                        "and price_basis == 'public_companion_cpotr_previous_settlement' "
                        "and quality_status == 'verified_proxy' "
                        "and candidate_reject_reason == 'public_companion_price_requires_strategy_logic' "
                        "and freshness_state == 'fresh' "
                        "and cpotr_price_card_pair_observed >= 1 "
                        "and last > 0"
                    ),
                    "invalidation_expression": (
                        "freshness_state != 'fresh' or cpotr_price_card_pair_observed < 1 or last <= 0"
                    ),
                    "long_expression": "cpotr_opening_gap_bps > 0",
                    "short_expression": "cpotr_opening_gap_bps < 0",
                    "edge_expression": "milestone_proxy_gap_abs_bps + milestone_structural_signal",
                    "score_expression": (
                        "clip(45 + min(milestone_proxy_gap_abs_bps, 80) / 2 + milestone_structural_signal / 4, 0, 100)"
                    ),
                    "route_surface": "proxy",
                },
                "data_requirements": {
                    "admission_key": state.get("admission_key"),
                    "adapter_id": details.get("adapter_id"),
                    "market_surface": ICDX_MILESTONES_SURFACE,
                    "required_fields": [
                        "last",
                        "price_basis",
                        "price_source",
                        "source_url",
                        "source_timeline_url",
                        "cpotr_price_card_pair_observed",
                        "suggested_opening_price",
                        "previous_settlement_price",
                        "cpotr_opening_gap_bps",
                        "exchange_established_year",
                        "cpotr_launch_year",
                        "gofx_launch_year",
                        "years_since_cpotr_launch",
                        "years_since_gofx_launch",
                        "years_since_crude_oil_contract_launch",
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
        "action": "strategy_lab_icdx_milestones_program",
        "status": "created" if result.get("created") else "updated",
        "strategy_lab_id": result.get("strategy_lab_id"),
        "topic_key": claim.topic_key,
    }


def _create_carb_allowance_paper_program(
    conn: sqlite3.Connection,
    settings: dict,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Create the canonical paper-only CARB joint-auction reference program."""

    claim = _claim(conn, state, "activate_carb_allowance_paper_research", 90)
    if claim.duplicate and claim.canonical_row_id:
        return {
            "action": "strategy_lab_carb_allowance_program",
            "status": "deduplicated",
            "topic_key": claim.topic_key,
        }
    evidence = _evidence(state)
    details = state.get("details") if isinstance(state.get("details"), dict) else {}
    rec = {
        "recommendation_id": f"market_admission:{state['admission_key']}:carb_allowance_program",
        "source_agent": "market_admission_bridge",
        "payload": {
            "agent_name": "market_admission_bridge",
            "title": "Paper-test CARB current-versus-advance auction discount tightness",
            "rationale": (
                "CARB publishes official current and advance settlement results with quantities, but no executable "
                "allowance order route. Those same-surface records should feed a synthetic paper auction-reference "
                "experiment instead of falling back to generic enrichment."
            ),
            "strategy_lab_experiment": {
                "strategy_lab_id": CARB_ALLOWANCE_LAB_ID,
                "version": 1,
                "experiment_type": "market_strategy",
                "hypothesis": (
                    "Across California-Quebec joint allowance auctions, when CURRENT and ADVANCE allowances both "
                    "fully clear and the CURRENT-versus-ADVANCE settlement discount is unusually tight, the next "
                    "CURRENT auction settlement tends to be firmer because bidders are pricing forward scarcity "
                    "rather than only spot compliance demand."
                ),
                "source_surface": CARB_ALLOWANCE_SURFACE,
                "permitted_target_surface": [CARB_ALLOWANCE_SURFACE],
                "strategy_logic": {
                    "type": "observation_program",
                    "universe": {
                        "venues": ["CARB_CA_QC"],
                        "asset_classes": ["greenhouse_gas_emission_allowance"],
                        "market_types": ["joint_allowance_auction_result"],
                        "market_surfaces": [CARB_ALLOWANCE_SURFACE],
                    },
                    "calculated_features": {
                        "paired_sellthrough_total": "current_sellthrough + advance_sellthrough",
                        "discount_quality_signal": "max(0,-term_discount_zscore)",
                        "tight_discount_edge_bps": "max(0,35 - term_discount_bps)",
                    },
                    "entry_expression": (
                        "market_surface == 'california_quebec_cap_and_invest_joint_allowance_auctions' "
                        "and quality_status == 'official_auction_result' "
                        "and candidate_reject_reason == 'official_allowance_auction_reference_not_order_routable' "
                        "and allowance_category == 'current' "
                        "and reserve_sale == False "
                        "and price_available == True "
                        "and paired_current_advance_observed >= 1 "
                        "and current_sellthrough >= 1 "
                        "and advance_sellthrough >= 1 "
                        "and term_discount_bps > 0 "
                        "and term_discount_bps <= 35"
                    ),
                    "invalidation_expression": (
                        "freshness_state != 'fresh' or reserve_sale == True or price_available == False "
                        "or paired_current_advance_observed < 1 or current_sellthrough < 1 "
                        "or advance_sellthrough < 1"
                    ),
                    "direction": "long",
                    "direction_logic": (
                        "Emit a long paper-reference observation only from CURRENT auction rows when a paired "
                        "ADVANCE result exists for the same auction_number, both categories fully clear, and the "
                        "settlement discount remains tight."
                    ),
                    "edge_expression": (
                        "tight_discount_edge_bps + 10 * discount_quality_signal + 5 * max(paired_sellthrough_total - 2,0)"
                    ),
                    "edge_formula": (
                        "tight_discount_edge_bps + 10 * discount_quality_signal + 5 * max(paired_sellthrough_total - 2,0)"
                    ),
                    "score_expression": (
                        "clip(50 + tight_discount_edge_bps / 2 + 10 * discount_quality_signal "
                        "+ 5 * max(paired_sellthrough_total - 2,0),0,100)"
                    ),
                    "score_formula": (
                        "clip(50 + tight_discount_edge_bps / 2 + 10 * discount_quality_signal "
                        "+ 5 * max(paired_sellthrough_total - 2,0),0,100)"
                    ),
                    "route_surface": "auction_reference",
                },
                "data_requirements": {
                    "admission_key": state.get("admission_key"),
                    "adapter_id": details.get("adapter_id"),
                    "paper_only": True,
                    "venue": "CARB_CA_QC",
                    "market_surface": CARB_ALLOWANCE_SURFACE,
                    "source_surface": CARB_ALLOWANCE_SURFACE,
                    "permitted_target_surface": [CARB_ALLOWANCE_SURFACE],
                    "required_fields": [
                        "allowance_category",
                        "allowances_offered",
                        "allowances_sold",
                        "auction_number",
                        "auction_settlement_price_usd",
                        "event_date",
                        "price_available",
                        "quality_status",
                        "reserve_sale",
                        "session_status",
                        "freshness_state",
                        "candidate_reject_reason",
                    ],
                    "supported_snapshot_features": [
                        "auction_settlement_price_usd",
                        "allowances_offered",
                        "allowances_sold",
                        "allowance_sellthrough_ratio",
                        "paired_current_advance_observed",
                        "current_price_usd_by_auction",
                        "advance_price_usd_by_auction",
                        "current_sellthrough",
                        "advance_sellthrough",
                        "term_discount_bps",
                        "term_discount_zscore",
                    ],
                    "label_requirement": "next later official CURRENT settlement result on the same market_surface",
                    "paper_only_reference": True,
                    "requires_independent_signal_logic": True,
                },
                "risk_gates": {
                    "require_route_feasible": False,
                    "paper_allocation_multiplier": 0.25,
                    "synthetic_research_only": True,
                    "data_quality": [
                        "Use only quality_status='official_auction_result' rows with candidate_reject_reason='official_allowance_auction_reference_not_order_routable'.",
                        "Use only CURRENT category entry rows with paired_current_advance_observed>=1.",
                        "Require price_available=true and reserve_sale=false.",
                        "Require current_sellthrough=1 and advance_sellthrough=1 before entry.",
                    ],
                    "exposure_limits": [
                        "Paper-only synthetic auction-reference measurement.",
                        "At most one active signal per auction_number.",
                        "No broker routing, no live trading, and no non-paper execution surface.",
                    ],
                    "robustness": [
                        "Judge fit only after at least 12 labeled CURRENT auction cycles.",
                        "Confirm the edge is not solely driven by a single auction cycle.",
                        "Retain exact same-surface routing and exact-surface outcome labeling only.",
                    ],
                },
                "promotion_rules": {
                    "minimum_sample_size": 12,
                    "holdout_requirements": [
                        "Positive median next-CURRENT settlement change for qualified entries.",
                        "Positive average labeled outcome after excluding the single strongest winner.",
                        "Same directional sign in at least 2 non-overlapping historical subperiods.",
                    ],
                    "robustness_checks": [
                        "Edge persists after removing one strongest auction outcome.",
                        "Edge is not dependent on reserve-sale rows, which are already excluded.",
                        "Edge remains same-surface and paper-only with no cross-surface leakage.",
                    ],
                    "route_constraints": [
                        "Remain on synthetic_auction_reference_paper semantics only.",
                        "Do not promote to any live or broker-connected route from this experiment.",
                    ],
                },
            },
            "evidence": evidence,
        },
    }
    result = ingest_strategy_lab_recommendation(conn, rec, settings)[0]
    if result.get("strategy_lab_id"):
        bind_artifact(conn, claim.topic_key, "strategy_lab_experiments", result["strategy_lab_id"])
    return {
        "action": "strategy_lab_carb_allowance_program",
        "status": "created" if result.get("created") else "updated",
        "strategy_lab_id": result.get("strategy_lab_id"),
        "topic_key": claim.topic_key,
    }


def _create_aofm_tender_paper_program(
    conn: sqlite3.Connection,
    settings: dict,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Create the canonical paper-only AOFM Treasury-bond tender program."""

    claim = _claim(conn, state, "activate_aofm_tender_paper_research", 90)
    if claim.duplicate and claim.canonical_row_id:
        return {
            "action": "strategy_lab_aofm_tender_program",
            "status": "deduplicated",
            "topic_key": claim.topic_key,
        }
    evidence = _evidence(state)
    details = state.get("details") if isinstance(state.get("details"), dict) else {}
    rec = {
        "recommendation_id": f"market_admission:{state['admission_key']}:aofm_tender_program",
        "source_agent": "market_admission_bridge",
        "payload": {
            "agent_name": "market_admission_bridge",
            "title": "Paper-test AOFM Treasury bond tender strength",
            "rationale": (
                "AOFM publishes official Treasury-bond tender results with weighted-average yields and coverage, "
                "but no executable secondary route. Those same-surface observations should feed the existing "
                "synthetic auction-reference paper path instead of stalling at generic enrichment."
            ),
            "strategy_lab_experiment": {
                "strategy_lab_id": AOFM_LAB_ID,
                "version": 1,
                "experiment_type": "market_strategy",
                "hypothesis": (
                    "Fresh AOFM bond tenders that clear with strong bid cover and moderate weighted-average yields "
                    "tend to be followed by firmer next same-ISIN tender outcomes, so the official result can seed "
                    "a same-surface synthetic auction-reference measurement."
                ),
                "source_surface": AOFM_SURFACE,
                "permitted_target_surface": [AOFM_SURFACE],
                "strategy_logic": {
                    "type": "observation_program",
                    "universe": {
                        "venues": ["AUSTRALIAN_OFFICE_OF_FINANCIAL_MANAGEMENT"],
                        "asset_classes": ["australian_government_treasury_bond"],
                        "market_surfaces": [AOFM_SURFACE],
                    },
                    "calculated_features": {
                        "aofm_term_years": "auction_term_days / 365",
                        "aofm_demand_pressure": "auction_coverage_ratio - max(auction_average_yield_pct - 4, 0)",
                    },
                    "entry_expression": (
                        "market_surface == 'australian_treasury_bond_tenders_and_results' "
                        "and quality_status == 'official_auction_result' "
                        "and candidate_reject_reason == 'official_auction_result_not_executable_quote' "
                        "and freshness_state == 'fresh' and auction_coverage_ratio >= 2 "
                        "and auction_average_yield_pct > 0 and auction_term_days >= 365 "
                        "and auction_result_published >= 1"
                    ),
                    "invalidation_expression": (
                        "freshness_state != 'fresh' or auction_coverage_ratio < 1.25 "
                        "or auction_average_yield_pct <= 0 or auction_term_days < 365"
                    ),
                    "direction": "long",
                    "edge_expression": "aofm_demand_pressure + min(aofm_term_years / 2, 5)",
                    "score_expression": (
                        "clip(45 + 10 * aofm_demand_pressure + min(aofm_term_years, 12), 0, 100)"
                    ),
                    "route_surface": "auction_reference",
                },
                "data_requirements": {
                    "admission_key": state.get("admission_key"),
                    "adapter_id": details.get("adapter_id"),
                    "paper_only": True,
                    "venue": "AUSTRALIAN_OFFICE_OF_FINANCIAL_MANAGEMENT",
                    "market_surface": AOFM_SURFACE,
                    "source_surface": AOFM_SURFACE,
                    "permitted_target_surface": [AOFM_SURFACE],
                    "required_fields": [
                        "isin",
                        "coupon_pct",
                        "maturity_date_iso",
                        "tender_date",
                        "settlement_date",
                        "term_days",
                        "average_yield_pct",
                        "weighted_average_yield_pct",
                        "coverage_ratio",
                        "bid_cover_ratio",
                        "quality_status",
                        "candidate_reject_reason",
                        "price_source",
                        "source_url",
                    ],
                    "supported_snapshot_features": [
                        "auction_coverage_ratio",
                        "auction_term_days",
                        "auction_average_yield_pct",
                    ],
                    "label_requirement": "next later official result on the same ISIN, or same maturity bucket when ISIN is unavailable",
                    "paper_only_reference": True,
                    "requires_independent_signal_logic": True,
                },
                "risk_gates": {
                    "require_route_feasible": False,
                    "paper_allocation_multiplier": 0.25,
                    "synthetic_research_only": True,
                    "data_quality": [
                        "Use only official AOFM result rows with candidate_reject_reason='official_auction_result_not_executable_quote'.",
                        "Require fresh weighted-average yield and coverage data from the public issuance workbook.",
                        "Preserve ISIN, maturity, and source_url provenance for every paper label.",
                    ],
                    "exposure_limits": [
                        "Paper-only synthetic auction-reference measurement.",
                        "At most one active Strategy Lab signal per AOFM ISIN.",
                        "No broker routing, no live trading, and no non-paper execution surface.",
                    ],
                },
                "promotion_rules": {
                    "minimum_sample_size": 8,
                    "holdout_requirements": [
                        "Positive median next same-ISIN tender yield move in the intended direction.",
                        "Positive average labeled outcome after excluding the single strongest winner.",
                    ],
                    "route_constraints": [
                        "Remain on synthetic_auction_reference_paper semantics only.",
                        "Do not promote to any live or broker-connected route from this experiment.",
                    ],
                },
            },
            "evidence": evidence,
        },
    }
    result = ingest_strategy_lab_recommendation(conn, rec, settings)[0]
    if result.get("strategy_lab_id"):
        bind_artifact(conn, claim.topic_key, "strategy_lab_experiments", result["strategy_lab_id"])
    return {
        "action": "strategy_lab_aofm_tender_program",
        "status": "created" if result.get("created") else "updated",
        "strategy_lab_id": result.get("strategy_lab_id"),
        "topic_key": claim.topic_key,
    }


def _create_anp_opc_companion_program(
    conn: sqlite3.Connection,
    settings: dict,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Create the canonical paper-only ANP OPC companion-price program."""

    claim = _claim(conn, state, "activate_anp_opc_companion_program", 90)
    if claim.duplicate and claim.canonical_row_id:
        return {
            "action": "strategy_lab_anp_opc_program",
            "status": "deduplicated",
            "topic_key": claim.topic_key,
        }
    evidence = _evidence(state)
    details = state.get("details") if isinstance(state.get("details"), dict) else {}
    rec = {
        "recommendation_id": f"market_admission:{state['admission_key']}:anp_opc_program",
        "source_agent": "market_admission_bridge",
        "payload": {
            "agent_name": "market_admission_bridge",
            "title": "Paper-test ANP OPC reference intensity with Petrobras ADR companion pricing",
            "rationale": (
                "ANP's Oferta Permanente de Concessao records are official exploration-programme references, not "
                "tradable quotes. The repaired adapter now preserves those reference fields while attaching a "
                "defensible public Petrobras ADR companion price so Strategy Lab can measure same-surface synthetic "
                "paper signals without implying a live ANP execution route."
            ),
            "strategy_lab_experiment": {
                "strategy_lab_id": ANP_OPC_LAB_ID,
                "version": 1,
                "experiment_type": "market_strategy",
                "hypothesis": (
                    "Fresh ANP OPC catalogue depth and exploratory-block amendment intensity, combined with a "
                    "public Petrobras ADR companion quote, can tag Brazilian upstream sentiment regimes for "
                    "synthetic paper continuation measurement."
                ),
                "source_surface": ANP_OPC_SURFACE,
                "permitted_target_surface": [ANP_OPC_SURFACE],
                "strategy_logic": {
                    "type": "observation_program",
                    "universe": {
                        "venues": ["ANP_BRAZIL_OPC"],
                        "trade_types": ["official_regulatory_programme_reference"],
                        "market_surfaces": [ANP_OPC_SURFACE],
                    },
                    "calculated_features": {
                        "opc_catalogue_depth_signal": "available_exploratory_blocks / 25",
                        "opc_new_block_signal": "new_exploratory_blocks * 2",
                        "opc_offshore_bias_pct": "offshore_new_blocks / max(new_exploratory_blocks, 1)",
                        "opc_reference_intensity": "max(opc_catalogue_depth_signal, opc_new_block_signal)",
                    },
                    "entry_expression": (
                        "market_surface == 'anp_oferta_permanente_de_concessao' "
                        "and price_basis == 'public_companion_petrobras_adr_quote' "
                        "and quality_status == 'verified_proxy' "
                        "and candidate_reject_reason == 'public_companion_price_requires_strategy_logic' "
                        "and freshness_state == 'fresh' and last > 0 and opc_reference_intensity > 0"
                    ),
                    "invalidation_expression": (
                        "freshness_state != 'fresh' or last <= 0 or opc_reference_intensity <= 0"
                    ),
                    "direction": "long",
                    "edge_expression": "opc_reference_intensity + 10 * opc_offshore_bias_pct",
                    "score_expression": (
                        "clip(45 + min(opc_reference_intensity, 80) / 2 + 10 * opc_offshore_bias_pct, 0, 100)"
                    ),
                    "route_surface": "proxy",
                },
                "data_requirements": {
                    "admission_key": state.get("admission_key"),
                    "adapter_id": details.get("adapter_id"),
                    "paper_only": True,
                    "venue": "ANP_BRAZIL_OPC",
                    "market_surface": ANP_OPC_SURFACE,
                    "source_surface": ANP_OPC_SURFACE,
                    "permitted_target_surface": [ANP_OPC_SURFACE],
                    "required_fields": [
                        "available_exploratory_blocks",
                        "new_exploratory_blocks",
                        "offshore_new_blocks",
                        "onshore_new_blocks",
                        "price_basis",
                        "quality_status",
                        "candidate_reject_reason",
                        "price_source",
                        "source_url",
                        "source_programme_url",
                        "companion_quote_symbol",
                        "last",
                    ],
                    "supported_snapshot_features": [
                        "available_exploratory_blocks",
                        "new_exploratory_blocks",
                        "offshore_new_blocks",
                        "onshore_new_blocks",
                    ],
                    "paper_only_reference": True,
                    "requires_independent_signal_logic": True,
                },
                "risk_gates": {
                    "require_route_feasible": False,
                    "paper_allocation_multiplier": 0.25,
                    "synthetic_research_only": True,
                    "data_quality": [
                        "Use only verified companion-price rows with price_basis='public_companion_petrobras_adr_quote'.",
                        "Retain the official ANP reference provenance via source_programme_url and source_url.",
                        "Never treat the Petrobras ADR quote as an ANP executable route.",
                    ],
                    "exposure_limits": [
                        "Paper-only synthetic research route.",
                        "At most one active Strategy Lab signal per inst_id.",
                        "No live trading, no broker writes, and no non-paper execution path.",
                    ],
                },
            },
            "evidence": evidence,
        },
    }
    result = ingest_strategy_lab_recommendation(conn, rec, settings)[0]
    if result.get("strategy_lab_id"):
        bind_artifact(conn, claim.topic_key, "strategy_lab_experiments", result["strategy_lab_id"])
    return {
        "action": "strategy_lab_anp_opc_program",
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
            elif (
                str(state.get("venue") or "").upper() == "AUSTRALIAN_OFFICE_OF_FINANCIAL_MANAGEMENT"
                and str(state.get("market_surface") or "") == AOFM_SURFACE
            ):
                actions.append(_create_aofm_tender_paper_program(conn, settings, state))
            elif (
                str(state.get("venue") or "").upper() == "CARB_CA_QC"
                and str(state.get("market_surface") or "") == CARB_ALLOWANCE_SURFACE
            ):
                actions.append(_create_carb_allowance_paper_program(conn, settings, state))
            elif (
                str(state.get("venue") or "").upper() == "ICDX"
                and str(state.get("market_surface") or "") == ICDX_CPOTR_SURFACE
            ):
                actions.append(_create_icdx_cpotr_price_card_program(conn, settings, state))
            else:
                actions.append(_create_enrichment(conn, state))
        elif stage == "quality_verified" and str(state.get("strategy_lineage") or "") == "adapter_observation":
            if (
                str(state.get("venue") or "").upper() == "ADX"
                and str(state.get("market_surface") or "") == ADX_DERIVATIVES_SURFACE
            ):
                actions.append(_create_adx_derivatives_companion_program(conn, settings, state))
            elif (
                str(state.get("venue") or "").upper() == "ANP_BRAZIL_OPC"
                and str(state.get("market_surface") or "") == ANP_OPC_SURFACE
            ):
                actions.append(_create_anp_opc_companion_program(conn, settings, state))
            elif (
                str(state.get("venue") or "").upper() == "ICDX"
                and str(state.get("market_surface") or "") == ICDX_MILESTONES_SURFACE
            ):
                actions.append(_create_icdx_milestones_companion_program(conn, settings, state))
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
