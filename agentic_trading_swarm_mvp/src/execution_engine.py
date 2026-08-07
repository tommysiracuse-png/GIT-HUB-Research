"""Execution ticket engine.

This is the missing layer between "approved idea" and "trade." In paper mode it
creates order tickets and simulated fills. Live adapters can later implement the
same order schema for IBKR, Hummingbot/CCXT, Kalshi, Polymarket, or specialist
routes.
"""

from __future__ import annotations

import datetime as dt
import math
import sqlite3

from paper_order_router import (
    FRONTIER_SHADOW_REASON,
    PAPER_NET_EDGE_GUARD_REASON,
    apply_frontier_paper_admission_guard,
    apply_frontier_paper_fill_gate,
    apply_frontier_paper_guard,
    paper_route_feasibility_gate_review,
)
from paper_context_cost import enforce_paper_context_cost_gate
from paper_decay_quarantine import apply_quarantine as apply_okx_basis_decay_quarantine
from paper_exploration import exploration_enabled, prepare_candidate_for_exploration
from paper_measurement_sleeve import apply_bounded_measurement_probe
from paired_direct_contract import (
    ACCOUNTING_CONVENTION as PAIRED_DIRECT_ACCOUNTING_CONVENTION,
    CONTRACT_VERSION as PAIRED_DIRECT_CONTRACT_VERSION,
    DECLARED_GROSS_NOTIONAL_USD as PAIRED_DIRECT_GROSS_NOTIONAL_USD,
    STRATEGY_FAMILY as PAIRED_DIRECT_STRATEGY_FAMILY,
    is_paired_direction,
    validate_paired_direct_entry,
)
from storage import (
    bounded_paper_queue_claim_valid,
    consume_bounded_paper_queue_claim,
    open_paper_trade,
    paper_label_eligibility,
    paper_queue_claim_required,
    save_execution_fill,
    save_execution_order,
    save_frontier_paper_shadow_observation,
)


NAV_REFERENCE_PAPER_ROUTE_ID = "synthetic_nav_reference_paper"
AUCTION_REFERENCE_PAPER_ROUTE_ID = "synthetic_auction_reference_paper"
PAIRED_DIRECT_EXCLUSION_REASON = "paired_direct_contract_invalid_or_incomplete"


def _side_for_direction(direction: str) -> str:
    if direction in {"yes", "no"}:
        return "buy"
    if direction.startswith("buy_") or direction.startswith("long_"):
        return "buy"
    if direction.startswith("sell_") or direction.startswith("short_"):
        return "sell"
    if direction in {
        "long_perp_short_spot",
        "basis_mean_reversion_long_perp",
        "funding_capture_long_perp",
        "long_proxy",
        "buy_yes_event",
        "buy_no_event",
    }:
        return "buy"
    if direction in {"short_perp_long_spot", "basis_mean_reversion_short_perp", "funding_capture_short_perp", "short_proxy"}:
        return "sell"
    return "hold"


def _route_for_candidate(candidate: dict, review: dict) -> str:
    if candidate.get("synthetic_research_paper"):
        return str(
            candidate.get("synthetic_route_id")
            or candidate.get("route_id")
            or "synthetic_research_paper"
        )
    resolved_route = (
        review.get("effective_route_id")
        or review.get("route_id")
        or candidate.get("route_id")
        or (candidate.get("execution_route") or {}).get("route_id")
        or (candidate.get("execution_feasibility") or {}).get("route_id")
    )
    if resolved_route:
        return str(resolved_route)
    venue = candidate.get("venue", "unknown")
    trade_type = candidate.get("trade_type", "unknown")
    status = review.get("feasibility_status", candidate.get("execution_feasibility", {}).get("status", "unknown"))
    if venue == "OKX" and trade_type == "perp_funding_basis":
        return "okx_derivatives_paper" if status == "standard" else "conditional_crypto_route_paper"
    if venue == "YAHOO_PROXY":
        return "equity_proxy_paper" if status == "standard" else "conditional_equity_route_paper"
    if venue in {"POLYMARKET", "KALSHI"}:
        return "prediction_market_paper" if status == "standard" else "conditional_prediction_route_paper"
    return "generic_paper_route"


def _parse_execution_event_at(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _paired_direct_order_legs(
    candidate: dict,
    settings: dict,
    *,
    available_paper_notional_usd: float,
) -> tuple[list[dict], dict, list[str]]:
    """Return two canonical paper legs only for a still-fresh shared contract."""

    validation = validate_paired_direct_entry(
        candidate,
        settings=settings,
        now=dt.datetime.now(dt.timezone.utc),
    )
    contract = dict(validation.get("contract") or {})
    reasons = list(validation.get("reasons") or [])
    if candidate.get("paired_direct_contract_status") != "entry_complete":
        reasons.append("paired_direct_contract_status")
    if candidate.get("paper_fill_allowed") is False:
        reasons.append("paper_fill_not_allowed")

    try:
        declared_gross = float(contract.get("declared_gross_notional_usd"))
        denominator = float(contract.get("return_denominator_usd"))
    except (TypeError, ValueError):
        declared_gross = 0.0
        denominator = 0.0
    if not math.isclose(
        declared_gross,
        PAIRED_DIRECT_GROSS_NOTIONAL_USD,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        reasons.append("paired_gross_notional_must_equal_100_usd")
    if not math.isclose(
        denominator,
        PAIRED_DIRECT_GROSS_NOTIONAL_USD,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        reasons.append("paired_return_denominator_must_equal_100_usd")
    if available_paper_notional_usd + 1e-9 < PAIRED_DIRECT_GROSS_NOTIONAL_USD:
        reasons.append("paper_risk_notional_below_paired_contract")

    queue_cfg = (settings.get("market_admission") or {}).get("paper_queue") or {}
    try:
        configured_max_age = max(
            1.0,
            float(queue_cfg.get("max_freshness_age_seconds", 90.0)),
        )
    except (TypeError, ValueError):
        configured_max_age = 90.0
    try:
        declared_max_age = float(contract.get("max_entry_freshness_age_seconds"))
    except (TypeError, ValueError):
        declared_max_age = -1.0
    if not math.isclose(
        declared_max_age,
        configured_max_age,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        reasons.append("max_entry_freshness_age_seconds")

    now = dt.datetime.now(dt.timezone.utc)
    components = contract.get("entry_components") or {}
    canonical: list[tuple[str, str]] = [("perp", "sell"), ("spot", "buy")]
    legs: list[dict] = []
    for leg_index, (name, order_side) in enumerate(canonical):
        component = components.get(name) if isinstance(components, dict) else None
        component = dict(component) if isinstance(component, dict) else {}
        event_at = _parse_execution_event_at(component.get("event_at"))
        if event_at is None:
            reasons.append(f"entry_components.{name}.event_at")
        else:
            age_seconds = (now - event_at).total_seconds()
            if age_seconds < 0.0:
                reasons.append(f"entry_components.{name}.event_at_in_future")
            elif age_seconds > configured_max_age:
                reasons.append(f"entry_components.{name}.stale")
        try:
            price = float(component.get("price"))
            notional_usd = float(component.get("notional_usd"))
        except (TypeError, ValueError):
            price = 0.0
            notional_usd = 0.0
        if price <= 0.0 or notional_usd <= 0.0:
            continue
        source = component.get("source") or {}
        quantity = notional_usd / price
        legs.append(
            {
                "leg_index": leg_index,
                "leg_role": name,
                "strategy_family": PAIRED_DIRECT_STRATEGY_FAMILY,
                "contract_version": PAIRED_DIRECT_CONTRACT_VERSION,
                "symbol": component.get("inst_id"),
                "inst_id": component.get("inst_id"),
                "venue": component.get("venue"),
                "market_surface": component.get("market_surface"),
                "quote_asset": component.get("quote_asset"),
                "position_side": component.get("side"),
                "side": order_side,
                "order_type": "market_paper",
                "quantity": round(quantity, 12),
                "price": price,
                "reference_price": price,
                "event_at": component.get("event_at"),
                "notional_usd": notional_usd,
                "estimated_fee_bps": component.get("entry_fee_bps"),
                "estimated_slippage_bps": component.get("entry_slippage_bps"),
                "entry_fee_bps": component.get("entry_fee_bps"),
                "entry_slippage_bps": component.get("entry_slippage_bps"),
                "exit_fee_bps": component.get("exit_fee_bps"),
                "exit_slippage_bps": component.get("exit_slippage_bps"),
                "source": dict(source) if isinstance(source, dict) else {},
                "source_identity": (
                    source.get("event_id") if isinstance(source, dict) else None
                ),
            }
        )
    if len(legs) != 2:
        reasons.append("paired_direct_two_legs_required")
    unique_reasons = sorted(set(str(reason) for reason in reasons if reason))
    return ([] if unique_reasons else legs), contract, unique_reasons


def build_order_ticket(candidate: dict, review: dict, settings: dict) -> dict:
    risk = settings["risk"]
    mode = settings.get("mode", "paper")
    notional = float(risk.get("paper_notional_usd", 1000.0))
    if mode == "paper":
        route_requirement_report = candidate.get("paper_route_requirement_report") or {}
        report_multiplier = 1.0
        if isinstance(route_requirement_report, dict) and route_requirement_report.get("applies"):
            try:
                report_multiplier = max(
                    0.0,
                    min(1.0, float(route_requirement_report.get("paper_allocation_multiplier", 1.0))),
                )
            except (TypeError, ValueError):
                report_multiplier = 1.0
        review_multiplier = max(
            0.0,
            min(1.0, float(review.get("paper_allocation_multiplier", 1.0))),
        )
        registry_multiplier = max(
            0.0,
            min(
                1.0,
                float(candidate.get("paper_route_registry_allocation_multiplier", 1.0)),
            ),
        )
        if exploration_enabled(settings):
            registry_multiplier = max(registry_multiplier, review_multiplier)
        candidate_multiplier = max(0.0, min(1.0, float(candidate.get("paper_allocation_multiplier", 1.0))))
        # Route-requirement evidence is a paper-only sizing input.  It never
        # changes admission or makes a live route reachable.
        notional *= min(review_multiplier, registry_multiplier, report_multiplier, candidate_multiplier)
    if mode == "live":
        notional = min(notional, float(risk.get("max_live_notional_usd", 0.0)))

    route_alternative = review.get("route_alternative") or {}
    proxy_route_requested = bool(
        review.get("route_alternative_used")
        and route_alternative.get("status") == "paper_testable_proxy"
    )
    proxy_not_live_equivalent = bool(
        mode == "paper"
        and proxy_route_requested
        and candidate.get("paper_proxy_activated")
        and candidate.get("paper_proxy_not_live_equivalent")
        and review.get("proxy_not_live_equivalent")
        and review.get("effective_route_id") == "okx_derivatives_paper"
    )

    paired_direction = is_paired_direction(candidate.get("direction"))
    paired_contract: dict = {}
    paired_validation_reasons: list[str] = []
    if paired_direction:
        legs, paired_contract, paired_validation_reasons = _paired_direct_order_legs(
            candidate,
            settings,
            available_paper_notional_usd=notional,
        )
        side = "paired"
        if not paired_validation_reasons:
            notional = float(paired_contract["declared_gross_notional_usd"])
            status = "ready_for_paper_execution"
        else:
            notional = 0.0
            status = "blocked_invalid_or_incomplete_paired_direct_contract"
    else:
        side = _side_for_direction(candidate["direction"])
        price = float(candidate["last"])
        quantity = 0.0 if price <= 0 else notional / price
        legs = [
            {
                "leg_index": 0,
                "symbol": candidate["inst_id"],
                "inst_id": candidate["inst_id"],
                "venue": candidate.get("venue"),
                "side": side,
                "order_type": "market_paper" if mode == "paper" else "market",
                "quantity": round(quantity, 8),
                "price": price,
                "reference_price": price,
                "notional_usd": notional,
                "estimated_slippage_bps": candidate.get("entry_slippage_bps_estimate"),
                "estimated_fee_bps": candidate.get("estimated_fee_bps_per_side"),
            }
        ]
        status = "ready_for_paper_execution"
        if side == "hold":
            status = "blocked_no_side"
    if proxy_route_requested and not proxy_not_live_equivalent:
        status = "blocked_invalid_paper_proxy_metadata"
    if mode == "live":
        status = "blocked_live_not_enabled"
    label_candidate = dict(candidate)
    if (
        str(candidate.get("signal_stats_scope") or review.get("signal_stats_scope") or "").strip().lower() == "paper_proxy"
        or proxy_not_live_equivalent
    ):
        label_candidate["paper_label_eligible"] = True
    label_eligibility = paper_label_eligibility(candidate=label_candidate, review=review)
    if paired_direction and paired_validation_reasons:
        label_eligibility = {
            **label_eligibility,
            "paper_label_eligible": False,
            "paper_label_exclusion_reason": PAIRED_DIRECT_EXCLUSION_REASON,
            "paper_shadow_excluded_from_learning": True,
            "paper_shadow_exclusion_triggers": list(paired_validation_reasons),
        }

    return {
        "mode": mode,
        "route_id": _route_for_candidate(candidate, review),
        "status": status,
        "notional_usd": notional,
        "declared_gross_notional_usd": (
            paired_contract.get("declared_gross_notional_usd")
            if paired_direction
            else None
        ),
        "return_denominator_usd": (
            paired_contract.get("return_denominator_usd")
            if paired_direction
            else None
        ),
        "contract_version": (
            PAIRED_DIRECT_CONTRACT_VERSION if paired_direction else None
        ),
        "strategy_family": (
            PAIRED_DIRECT_STRATEGY_FAMILY if paired_direction else None
        ),
        "accounting_convention": (
            PAIRED_DIRECT_ACCOUNTING_CONVENTION if paired_direction else None
        ),
        "paired_direct_contract_status": (
            "entry_complete"
            if paired_direction and not paired_validation_reasons
            else "invalid_or_incomplete"
            if paired_direction
            else None
        ),
        "paired_direct_validation_reasons": list(paired_validation_reasons),
        PAIRED_DIRECT_CONTRACT_VERSION: paired_contract if paired_direction else None,
        "direction": candidate["direction"],
        "trade_type": candidate.get("trade_type", "unknown"),
        "feasibility_status": review.get("feasibility_status"),
        "route_status": review.get("route_status"),
        "missing_requirements": review.get("missing_requirements", []),
        "direct_missing_requirements": review.get("direct_missing_requirements", []),
        "route_alternative_used": bool(review.get("route_alternative_used")),
        "route_alternative": route_alternative,
        "proxy_route_requested": proxy_route_requested,
        "execution_semantics": (
            "proxy_not_live_equivalent"
            if proxy_not_live_equivalent
            else candidate.get("paper_execution_semantics") or review.get("execution_semantics") or "direct_live_equivalent"
        ),
        "proxy_not_live_equivalent": proxy_not_live_equivalent,
        "paper_proxy_not_live_equivalent": proxy_not_live_equivalent,
        "signal_stats_scope": candidate.get("signal_stats_scope") or review.get("signal_stats_scope") or (
            "paper_proxy" if proxy_not_live_equivalent else "direct"
        ),
        "paper_label_eligible": bool(label_eligibility.get("paper_label_eligible")),
        "paper_label_exclusion_reason": label_eligibility.get("paper_label_exclusion_reason"),
        "paper_label_route_blockers": list(label_eligibility.get("paper_label_route_blockers") or []),
        "paper_shadow_excluded_from_learning": bool(
            label_eligibility.get("paper_shadow_excluded_from_learning")
        ),
        "paper_shadow_exclusion_triggers": list(
            label_eligibility.get("paper_shadow_exclusion_triggers") or []
        ),
        "quality_action": candidate.get("quality_action"),
        "quality_status": candidate.get("quality_status"),
        "candidate_reject_reason": candidate.get("candidate_reject_reason") or candidate.get("shadow_reason"),
        "paper_context_gate_reason": candidate.get("paper_context_gate_reason"),
        "paper_context_gate_action": candidate.get("paper_context_gate_action"),
        "paper_context_gate_promotion_eligible": candidate.get("paper_context_gate_promotion_eligible"),
        "paper_context_gate_paper_fill_allowed": candidate.get("paper_context_gate_paper_fill_allowed"),
        "anomaly_flags": list(candidate.get("anomaly_flags") or []),
        "gross_edge_bps_estimate": candidate.get("gross_edge_bps_estimate"),
        "edge_bps_estimate": candidate.get("edge_bps_estimate"),
        "net_edge_bps_estimate": (
            candidate.get("frontier_net_edge_bps")
            if candidate.get("frontier_net_edge_bps") is not None
            else candidate.get("edge_bps_estimate")
            if candidate.get("edge_bps_estimate") is not None
            else review.get("net_edge_bps_estimate")
        ),
        "estimated_round_trip_cost_bps": candidate.get("estimated_round_trip_cost_bps"),
        "signal_key": review.get("signal_key"),
        "direct_signal_key": candidate.get("direct_signal_key"),
        "direct_route_id": candidate.get("paper_proxy_source_route_id"),
        "legs": legs,
        "risk": {
            "confidence": review.get("confidence"),
            "net_edge_bps_estimate": review.get("net_edge_bps_estimate"),
            "max_live_notional_usd": risk.get("max_live_notional_usd", 0.0),
        },
        "notes": [
            "Paper order ticket generated from approved opportunity.",
            "Live execution is blocked until explicit mode, route, credentials, and limits are configured.",
        ] + (
            [
                "paired_direct_v1: two directly quoted, matched-notional legs totaling $100 gross.",
                "Modeled fill prices are audit-only; paired PnL uses direct reference prices and deducts declared costs once.",
            ]
            if paired_direction and not paired_validation_reasons
            else [
                "Paired paper execution failed closed because the shared paired_direct_v1 entry contract is incomplete or invalid."
            ]
            if paired_direction
            else []
        ) + (
            [
                "proxy_not_live_equivalent: OKX derivatives paper exposure replaces a borrow-blocked direct short-spot attempt.",
                "Proxy outcomes use an isolated paper-proxy signal statistics scope.",
            ]
            if proxy_not_live_equivalent
            else []
        ) + (
            [
                "synthetic_research_paper: priceable research exposure is isolated from executable-strategy statistics.",
                "This fill is not evidence that the direct route, account, or jurisdiction is available.",
            ]
            if candidate.get("synthetic_research_paper")
            else []
        ),
    }


def _paper_fill_for_leg(leg: dict, settings: dict) -> dict:
    risk = settings["risk"]
    slippage_bps = float(
        leg.get("estimated_slippage_bps")
        if leg.get("estimated_slippage_bps") is not None
        else risk.get("slippage_bps_per_leg", 3.0)
    )
    fee_bps = float(
        leg.get("estimated_fee_bps")
        if leg.get("estimated_fee_bps") is not None
        else risk.get("taker_fee_bps_per_leg", 5.0)
    )
    reference = float(leg["reference_price"])
    side = leg["side"]
    sign = 1.0 if side == "buy" else -1.0
    fill_price = reference * (1.0 + sign * slippage_bps / 10_000.0)
    return {
        "leg_index": leg["leg_index"],
        "leg_role": leg.get("leg_role"),
        "strategy_family": leg.get("strategy_family"),
        "contract_version": leg.get("contract_version"),
        "symbol": leg["symbol"],
        "inst_id": leg.get("inst_id") or leg["symbol"],
        "venue": leg.get("venue"),
        "market_surface": leg.get("market_surface"),
        "quote_asset": leg.get("quote_asset"),
        "position_side": leg.get("position_side"),
        "side": side,
        "quantity": leg["quantity"],
        "price": reference,
        "fill_price": round(fill_price, 10),
        "reference_price": reference,
        "event_at": leg.get("event_at"),
        "notional_usd": leg["notional_usd"],
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "entry_fee_bps": fee_bps,
        "entry_slippage_bps": slippage_bps,
        "exit_fee_bps": leg.get("exit_fee_bps"),
        "exit_slippage_bps": leg.get("exit_slippage_bps"),
        "source": dict(leg.get("source") or {}),
        "source_identity": leg.get("source_identity"),
        "cost_accounting": "reference_prices_plus_explicit_costs_once",
        "modeled_fill_price_audit_only": bool(leg.get("contract_version")),
    }


def _nav_reference_execution(candidate: dict, settings: dict) -> dict:
    """Return a paper outcome label without creating an order or fill.

    Official factsheet NAV is a disclosed reference value, not an entry-quality
    quote.  It can anchor a synthetic research outcome, but must never become
    an execution ticket, including in paper mode.
    """
    valid = bool(candidate.get("paper_nav_reference_provenance_valid")) and not bool(
        candidate.get("shadow_filtered")
    )
    paper_mode = (
        settings.get("mode", "paper") == "paper"
        and not settings.get("allow_live_trading")
    )
    status = "paper_reference_labeled" if valid and paper_mode else "paper_reference_rejected"
    order = {
        "mode": "paper",
        "route_id": NAV_REFERENCE_PAPER_ROUTE_ID,
        "status": status,
        "notional_usd": 0.0,
        "direction": candidate.get("direction"),
        "trade_type": candidate.get("trade_type", "unknown"),
        "execution_semantics": "synthetic_nav_reference_not_live_equivalent",
        "signal_stats_scope": "synthetic_research",
        "proxy_not_live_equivalent": False,
        "paper_proxy_not_live_equivalent": False,
        "legs": [],
        "notes": [
            "Paper NAV-reference label recorded without an execution order or fill.",
            "Official factsheet NAV is not an executable quote and is never a broker route.",
        ],
    }
    return {
        "order_id": None,
        "order": order,
        "fills": [],
        "fill_ids": [],
        "paper_filled": status == "paper_reference_labeled",
        "candidate": candidate,
    }


def _auction_reference_execution(candidate: dict, settings: dict) -> dict:
    """Record an official auction research label without creating an order.

    Auction results are public event data, not a tradable Treasury-bill quote.
    The candidate is therefore eligible only for isolated synthetic-paper
    measurement and can never reach an order ticket in either paper or live
    mode.
    """
    valid = bool(candidate.get("paper_auction_reference_provenance_valid")) and not bool(
        candidate.get("shadow_filtered")
    )
    paper_mode = (
        settings.get("mode", "paper") == "paper"
        and not settings.get("allow_live_trading")
    )
    status = "paper_reference_labeled" if valid and paper_mode else "paper_reference_rejected"
    order = {
        "mode": "paper",
        "route_id": AUCTION_REFERENCE_PAPER_ROUTE_ID,
        "status": status,
        "notional_usd": 0.0,
        "direction": candidate.get("direction"),
        "trade_type": candidate.get("trade_type", "unknown"),
        "execution_semantics": "synthetic_auction_reference_not_live_equivalent",
        "signal_stats_scope": "synthetic_research",
        "proxy_not_live_equivalent": False,
        "paper_proxy_not_live_equivalent": False,
        "legs": [],
        "notes": [
            "Paper auction-reference label recorded without an execution order or fill.",
            "Official auction results are not executable quotes and are never broker routes.",
        ],
    }
    return {
        "order_id": None,
        "order": order,
        "fills": [],
        "fill_ids": [],
        "paper_filled": status == "paper_reference_labeled",
        "candidate": candidate,
    }


def _restore_yahoo_proxy_freshness_shadow(candidate: dict) -> dict:
    gate = candidate.get("paper_yahoo_proxy_freshness_gate")
    if not isinstance(gate, dict):
        return candidate
    if not gate.get("applies") or gate.get("paper_fill_allowed", True):
        return candidate
    restored = dict(candidate)
    restored["shadow_filtered"] = True
    restored["paper_fill_allowed"] = False
    restored["paper_entry_blocked"] = True
    restored["paper_observation_only"] = True
    restored["paper_observation_reason"] = gate.get("reason") or "proxy_freshness_degraded"
    restored["paper_execution_mode"] = "observe_only"
    restored["paper_execution_semantics"] = str(
        gate.get("paper_execution_semantics") or "synthetic_research_not_live_equivalent"
    )
    restored["signal_stats_scope"] = str(gate.get("signal_stats_scope") or "synthetic_research")
    restored["candidate_status"] = "shadow_only"
    restored["paper_action"] = "shadow_only"
    restored["paper_status"] = "shadow_only"
    restored["paper_fill_status"] = "shadow_only"
    restored["paper_order_status"] = "shadow_only"
    restored["shadow_reason"] = str(gate.get("reason") or "proxy_freshness_degraded")
    restored["candidate_reject_reason"] = restored["shadow_reason"]
    restored["candidate_reject_detail"] = dict(gate)
    restored["_hunter_bucket"] = "diagnose"
    return restored


def _restore_okx_basis_decay_shadow(candidate: dict) -> dict:
    record = candidate.get("paper_okx_basis_decay_quarantine")
    if not isinstance(record, dict):
        return candidate
    if not record.get("active") or record.get("paper_fill_allowed", True):
        return candidate
    restored = dict(candidate)
    restored["shadow_filtered"] = True
    restored["paper_fill_allowed"] = False
    restored["paper_entry_blocked"] = True
    restored["paper_observation_only"] = True
    restored["paper_observation_reason"] = str(
        record.get("reason") or "decayed_basis_mean_reversion_quarantine"
    )
    restored["paper_execution_mode"] = str(record.get("paper_execution_mode") or "observe_only")
    restored["paper_execution_semantics"] = str(
        record.get("paper_execution_semantics") or "counterfactual_okx_basis_decay_guard"
    )
    restored["signal_stats_scope"] = str(record.get("signal_stats_scope") or "synthetic_research")
    quarantine_action = str(record.get("quarantine_action") or "quarantined_basis_mr")
    restored["candidate_status"] = quarantine_action
    restored["paper_quarantine_status"] = quarantine_action
    restored["paper_action"] = "shadow_filtered"
    restored["paper_status"] = "shadow_filtered"
    restored["paper_fill_status"] = "shadow_filtered"
    restored["paper_order_status"] = "shadow_filtered"
    restored["router_action"] = quarantine_action
    restored["shadow_reason"] = str(record.get("reason") or "decayed_basis_mean_reversion_quarantine")
    restored["candidate_reject_reason"] = restored["shadow_reason"]
    restored["candidate_reject_detail"] = dict(record)
    restored["_hunter_bucket"] = "diagnose"
    return restored


def _route_feasibility_shadow_recoverable_for_exploration(candidate: dict, settings: dict) -> bool:
    detail = candidate.get("candidate_reject_detail")
    if isinstance(detail, dict) and detail.get("guard") == "paper_route_feasibility_score_gate":
        return bool(candidate.get("shadow_filtered"))
    gate = paper_route_feasibility_gate_review(candidate, settings)
    return bool(gate.get("applies") and not gate.get("eligible"))


def execute_order(
    conn: sqlite3.Connection,
    candidate: dict,
    review: dict,
    settings: dict,
    *,
    opportunity_id: int | None = None,
    record_shadow_observation: bool = True,
    allow_paper_fill: bool = True,
) -> dict:
    paper_mode = settings.get("mode", "paper") == "paper" and not settings.get(
        "allow_live_trading", False
    )
    bounded_queue_claim_required = paper_mode and paper_queue_claim_required(settings)
    if (
        paper_mode
        and is_paired_direction(candidate.get("direction"))
        and not bounded_queue_claim_required
    ):
        order = build_order_ticket(candidate, review, settings)
        order["status"] = "blocked_paired_direct_requires_bounded_queue"
        order["notes"].append(
            "paired_direct_v1 is executable only through the bounded queue transaction that atomically owns both fills and the paper trade."
        )
        return {
            "order_id": None,
            "order": order,
            "fills": [],
            "fill_ids": [],
            "paper_filled": False,
            "queue_claim_valid": False,
            "candidate": candidate,
            "opportunity_id": opportunity_id,
        }
    if bounded_queue_claim_required and not bounded_paper_queue_claim_valid(
        conn,
        candidate,
        settings,
    ):
        order = build_order_ticket(candidate, review, settings)
        order["status"] = "blocked_invalid_paper_queue_claim"
        order["notes"].append(
            "Bounded paper execution requires an exact, unexpired queue claim."
        )
        return {
            "order_id": None,
            "order": order,
            "fills": [],
            "fill_ids": [],
            "paper_filled": False,
            "queue_claim_valid": False,
            "candidate": candidate,
            "opportunity_id": opportunity_id,
        }
    if paper_mode:
        # Evaluate the direct candidate before exploration can substitute a
        # synthetic route and hide direct route blockers from the fill guard.
        candidate = apply_frontier_paper_admission_guard(candidate, settings)
        candidate = _restore_yahoo_proxy_freshness_shadow(candidate)
        candidate = _restore_okx_basis_decay_shadow(candidate)
    context_loss_quarantine = candidate.get("paper_context_loss_quarantine") or {}
    context_loss_quarantined = bool(
        isinstance(context_loss_quarantine, dict)
        and not context_loss_quarantine.get("paper_fill_allowed", True)
    )
    recoverable_route_shadow = _route_feasibility_shadow_recoverable_for_exploration(candidate, settings)
    if exploration_enabled(settings) and (
        not candidate.get("shadow_filtered") or recoverable_route_shadow
    ):
        candidate = prepare_candidate_for_exploration(dict(candidate), settings)
        # This explicit paper-only family exception survives exploration's
        # otherwise permissive synthetic-route preparation.
        candidate = apply_okx_basis_decay_quarantine(candidate, settings, conn=conn)
        candidate = _restore_okx_basis_decay_shadow(candidate)
        if context_loss_quarantined:
            candidate["shadow_filtered"] = True
            candidate["paper_fill_allowed"] = False
            candidate["paper_entry_blocked"] = True
            candidate["candidate_reject_reason"] = context_loss_quarantine.get(
                "reason", "paper_context_loss_quarantine"
            )
            candidate["candidate_reject_detail"] = dict(context_loss_quarantine)
        elif (
            (candidate.get("paper_okx_basis_decay_quarantine") or {}).get("active")
        ):
            # Preserve the paper-only shadow-only quarantine instead of
            # resetting it back into a fillable exploration candidate.
            pass
        elif candidate.get("paper_experiment_capacity_deferred"):
            # A scanner can defer an otherwise valid priceable idea when the
            # bounded paper window cannot produce a meaningful experiment.
            # Preserve that explicit capacity decision; exploration should not
            # turn it into a fill merely because its route is synthetic.
            candidate["shadow_filtered"] = True
            candidate["paper_fill_allowed"] = False
            candidate["paper_entry_blocked"] = True
        else:
            candidate["shadow_filtered"] = False
            candidate["paper_fill_allowed"] = True
            candidate["paper_entry_blocked"] = False
        candidate = _restore_yahoo_proxy_freshness_shadow(candidate)
        candidate = _restore_okx_basis_decay_shadow(candidate)
        candidate = apply_frontier_paper_admission_guard(candidate, settings)
        candidate = _restore_yahoo_proxy_freshness_shadow(candidate)
        candidate = _restore_okx_basis_decay_shadow(candidate)
        if not candidate.get("shadow_filtered") and not recoverable_route_shadow:
            # Exploration can substitute a synthetic route, but the fill-time
            # frontier net-edge guard still decides whether a paper order is
            # worth turning into a fill versus a shadow observation.
            candidate = apply_frontier_paper_guard(candidate, settings)
            candidate = _restore_yahoo_proxy_freshness_shadow(candidate)
            candidate = _restore_okx_basis_decay_shadow(candidate)
    else:
        # Apply the persisted paper-only state before the router recomputes
        # its guard.  This lets a released quarantine re-admit the exact
        # family in non-exploration paper configurations as well.
        candidate = apply_okx_basis_decay_quarantine(dict(candidate), settings, conn=conn)
        candidate = _restore_okx_basis_decay_shadow(candidate)
        existing_reject_detail = candidate.get("candidate_reject_detail") or {}
        scanner_shadow_preserved = (
            paper_mode
            and candidate.get("shadow_filtered")
            and isinstance(existing_reject_detail, dict)
            and existing_reject_detail.get("guard") == "frontier_paper_admission_guard"
        )
        if not scanner_shadow_preserved:
            candidate = apply_frontier_paper_guard(candidate, settings)
        recovery_probe = bool(
            settings.get("mode") == "paper"
            and not settings.get("allow_live_trading")
            and review.get("paper_context_recovery_probe")
            and 0.0 < float(review.get("paper_allocation_multiplier") or 0.0) <= 0.1
            and any(
                item.get("recovery_probe") and not item.get("blocks")
                for item in (review.get("applied_policies") or [])
                if isinstance(item, dict)
            )
        )
        if not candidate.get("shadow_filtered") and not recovery_probe:
            candidate = enforce_paper_context_cost_gate(candidate, settings)
        elif not candidate.get("shadow_filtered") and recovery_probe:
            candidate = dict(candidate)
            candidate["paper_context_recovery_probe"] = True
            candidate["gating_reason"] = "bounded_paper_recovery_probe_below_cost_floor"
        candidate = _restore_yahoo_proxy_freshness_shadow(candidate)
        candidate = _restore_okx_basis_decay_shadow(candidate)
    if candidate.get("paper_nav_reference"):
        result = _nav_reference_execution(candidate, settings)
        result["opportunity_id"] = opportunity_id
        return result
    if candidate.get("paper_auction_reference"):
        result = _auction_reference_execution(candidate, settings)
        result["opportunity_id"] = opportunity_id
        return result
    if paper_mode:
        candidate = apply_frontier_paper_fill_gate(candidate, settings)
        candidate = apply_bounded_measurement_probe(candidate, review, settings)
    order = build_order_ticket(candidate, review, settings)
    if candidate.get("shadow_filtered"):
        yahoo_proxy_freshness_shadow = (
            candidate.get("paper_observation_only")
            and isinstance(candidate.get("paper_yahoo_proxy_freshness_gate"), dict)
            and not candidate["paper_yahoo_proxy_freshness_gate"].get("paper_fill_allowed", True)
        )
        if yahoo_proxy_freshness_shadow:
            order["status"] = "shadow_only"
            order["shadow_filter"] = candidate.get("candidate_reject_detail")
            order["shadow_reason"] = candidate.get("shadow_reason") or candidate.get("candidate_reject_reason")
            order["signal_stats_scope"] = candidate.get("signal_stats_scope", "synthetic_research")
            order["execution_semantics"] = candidate.get(
                "paper_execution_semantics",
                "synthetic_research_not_live_equivalent",
            )
            order["notes"].append(
                "Yahoo proxy paper entry converted to a synthetic-research shadow observation; no paper fill was created."
            )
            order_id = save_execution_order(
                conn, order, candidate, review, opportunity_id=opportunity_id
            )
            return {
                "order_id": order_id,
                "order": order,
                "fills": [],
                "fill_ids": [],
                "paper_filled": False,
                "paper_observation_ready": True,
                "candidate": candidate,
                "opportunity_id": opportunity_id,
            }
        okx_basis_decay_shadow = (
            candidate.get("paper_observation_only")
            and isinstance(candidate.get("paper_okx_basis_decay_quarantine"), dict)
            and not candidate["paper_okx_basis_decay_quarantine"].get("paper_fill_allowed", True)
        )
        if okx_basis_decay_shadow:
            order["status"] = "shadow_filtered"
            order["router_action"] = (
                candidate.get("router_action")
                or (candidate.get("paper_okx_basis_decay_quarantine") or {}).get("quarantine_action")
                or "quarantined_basis_mr"
            )
            order["shadow_filter"] = candidate.get("candidate_reject_detail")
            order["shadow_reason"] = candidate.get("shadow_reason") or candidate.get("candidate_reject_reason")
            order["signal_stats_scope"] = candidate.get("signal_stats_scope", "synthetic_research")
            order["execution_semantics"] = candidate.get(
                "paper_execution_semantics",
                "counterfactual_okx_basis_decay_guard",
            )
            order["notes"].append(
                "OKX basis candidate recorded as a synthetic-research shadow observation due to decayed paper performance."
            )
            order_id = save_execution_order(
                conn, order, candidate, review, opportunity_id=opportunity_id
            )
            return {
                "order_id": order_id,
                "order": order,
                "fills": [],
                "fill_ids": [],
                "paper_filled": False,
                "paper_observation_ready": True,
                "candidate": candidate,
                "opportunity_id": opportunity_id,
            }
        reject_reason = candidate.get("candidate_reject_reason")
        reject_detail = candidate.get("candidate_reject_detail") or {}
        scanner_shadow_observation = (
            isinstance(reject_detail, dict)
            and reject_detail.get("guard") == "frontier_paper_admission_guard"
        )
        fill_gate_shadow_observation = (
            isinstance(reject_detail, dict)
            and reject_detail.get("guard") == "frontier_paper_fill_gate"
        )
        shadow_observation = reject_reason == FRONTIER_SHADOW_REASON or (
            paper_mode
            and (
                reject_reason == PAPER_NET_EDGE_GUARD_REASON
                or scanner_shadow_observation
                or fill_gate_shadow_observation
            )
        )
        if shadow_observation:
            observation_id = (
                save_frontier_paper_shadow_observation(
                    conn,
                    candidate,
                    review,
                    opportunity_id=opportunity_id,
                )
                if record_shadow_observation
                else None
            )
            order["status"] = (
                "shadow_only"
                if (
                    reject_reason == PAPER_NET_EDGE_GUARD_REASON
                    or scanner_shadow_observation
                    or fill_gate_shadow_observation
                )
                else "shadow_observed"
            )
            order["shadow_filter"] = candidate.get("candidate_reject_detail")
            order["shadow_reason"] = candidate.get("shadow_reason") or reject_reason
            order["notes"].append(
                "Frontier candidate recorded as a shadow observation; no paper order or fill was created."
                if record_shadow_observation
                else "Frontier shadow observation deferred by the per-cycle observation cap."
            )
            return {
                "order_id": None,
                "shadow_observation_id": observation_id,
                "shadow_observation_recorded": bool(observation_id),
                "shadow_observation_deferred": not record_shadow_observation,
                "order": order,
                "fills": [],
                "fill_ids": [],
                "paper_filled": False,
                "candidate": candidate,
                "opportunity_id": opportunity_id,
            }
        order["status"] = "shadow_filtered"
        order["shadow_filter"] = candidate.get("candidate_reject_detail")
        order["notes"].append("Paper fill suppressed by a paper-only candidate guard.")
        order_id = save_execution_order(
            conn, order, candidate, review, opportunity_id=opportunity_id
        )
        return {
            "order_id": order_id,
            "order": order,
            "fills": [],
            "paper_filled": False,
            "candidate": candidate,
            "opportunity_id": opportunity_id,
        }

    if settings.get("mode") == "live" or settings.get("allow_live_trading"):
        order["status"] = "blocked_live_trading_not_implemented"
        order_id = save_execution_order(
            conn, order, candidate, review, opportunity_id=opportunity_id
        )
        return {
            "order_id": order_id,
            "order": order,
            "fills": [],
            "paper_filled": False,
            "candidate": candidate,
            "opportunity_id": opportunity_id,
        }

    if order["status"] != "ready_for_paper_execution":
        order_id = save_execution_order(
            conn, order, candidate, review, opportunity_id=opportunity_id
        )
        return {
            "order_id": order_id,
            "order": order,
            "fills": [],
            "paper_filled": False,
            "candidate": candidate,
            "opportunity_id": opportunity_id,
        }

    if paper_mode and not allow_paper_fill:
        order["status"] = "deferred_capacity"
        order["capacity_reason"] = "paper_fill_capacity_unavailable"
        order["notes"].append(
            "Paper fill deferred before fill creation because the cycle or open-trade capacity is exhausted."
        )
        order_id = (
            None
            if bounded_queue_claim_required
            else save_execution_order(
                conn, order, candidate, review, opportunity_id=opportunity_id
            )
        )
        return {
            "order_id": order_id,
            "order": order,
            "fills": [],
            "fill_ids": [],
            "paper_filled": False,
            "paper_fill_deferred": True,
            "candidate": candidate,
            "opportunity_id": opportunity_id,
        }

    fills = [_paper_fill_for_leg(leg, settings) for leg in order["legs"]]
    order["status"] = "paper_filled"
    if bounded_queue_claim_required:
        savepoint = "bounded_paper_fill_bundle"
        conn.execute(f"savepoint {savepoint}")
        try:
            if not consume_bounded_paper_queue_claim(conn, candidate, settings):
                conn.execute(f"rollback to savepoint {savepoint}")
                conn.execute(f"release savepoint {savepoint}")
                order["status"] = "blocked_invalid_paper_queue_claim"
                order["notes"].append(
                    "The bounded paper queue claim was already consumed or expired."
                )
                return {
                    "order_id": None,
                    "order": order,
                    "fills": [],
                    "fill_ids": [],
                    "paper_filled": False,
                    "queue_claim_valid": False,
                    "candidate": candidate,
                    "opportunity_id": opportunity_id,
                }
            order_id = save_execution_order(
                conn,
                order,
                candidate,
                review,
                opportunity_id=opportunity_id,
                commit=False,
                settings=settings,
            )
            fill_ids = []
            for fill in fills:
                fill_id = save_execution_fill(
                    conn,
                    order_id,
                    fill,
                    commit=False,
                )
                fill["fill_id"] = fill_id
                fill_ids.append(fill_id)
            execution = {
                "order_id": order_id,
                "order": order,
                "fills": fills,
                "fill_ids": fill_ids,
                "paper_filled": True,
                "queue_claim_valid": True,
                "candidate": candidate,
                "opportunity_id": opportunity_id,
            }
            execution["paper_trade_id"] = open_paper_trade(
                conn,
                candidate,
                review,
                execution=execution,
                settings=settings,
                commit=False,
            )
            conn.execute(f"release savepoint {savepoint}")
            return execution
        except Exception:
            conn.execute(f"rollback to savepoint {savepoint}")
            conn.execute(f"release savepoint {savepoint}")
            raise

    order_id = save_execution_order(
        conn, order, candidate, review, opportunity_id=opportunity_id
    )
    fill_ids = []
    for fill in fills:
        fill_id = save_execution_fill(conn, order_id, fill)
        fill["fill_id"] = fill_id
        fill_ids.append(fill_id)

    return {
        "order_id": order_id,
        "order": order,
        "fills": fills,
        "fill_ids": fill_ids,
        "paper_filled": True,
        "candidate": candidate,
        "opportunity_id": opportunity_id,
    }
