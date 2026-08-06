"""Execution ticket engine.

This is the missing layer between "approved idea" and "trade." In paper mode it
creates order tickets and simulated fills. Live adapters can later implement the
same order schema for IBKR, Hummingbot/CCXT, Kalshi, Polymarket, or specialist
routes.
"""

from __future__ import annotations

import math
import sqlite3

from paper_order_router import apply_frontier_paper_guard
from paper_context_cost import enforce_paper_context_cost_gate
from paper_exploration import exploration_enabled, prepare_candidate_for_exploration
from storage import save_execution_fill, save_execution_order


NAV_REFERENCE_PAPER_ROUTE_ID = "synthetic_nav_reference_paper"
AUCTION_REFERENCE_PAPER_ROUTE_ID = "synthetic_auction_reference_paper"


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
        return str(candidate.get("route_id") or "synthetic_research_paper")
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
        # Route-requirement evidence is a paper-only sizing input.  It never
        # changes admission or makes a live route reachable.
        notional *= min(review_multiplier, registry_multiplier, report_multiplier)
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

    side = _side_for_direction(candidate["direction"])
    price = float(candidate["last"])
    quantity = 0.0 if price <= 0 else notional / price
    leg = {
        "leg_index": 0,
        "symbol": candidate["inst_id"],
        "venue": candidate.get("venue"),
        "side": side,
        "order_type": "market_paper" if mode == "paper" else "market",
        "quantity": round(quantity, 8),
        "reference_price": price,
        "notional_usd": notional,
        "estimated_slippage_bps": candidate.get("entry_slippage_bps_estimate"),
        "estimated_fee_bps": candidate.get("estimated_fee_bps_per_side"),
    }

    status = "ready_for_paper_execution"
    if side == "hold":
        status = "blocked_no_side"
    if proxy_route_requested and not proxy_not_live_equivalent:
        status = "blocked_invalid_paper_proxy_metadata"
    if mode == "live":
        status = "blocked_live_not_enabled"

    return {
        "mode": mode,
        "route_id": _route_for_candidate(candidate, review),
        "status": status,
        "notional_usd": notional,
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
        "signal_key": review.get("signal_key"),
        "direct_signal_key": candidate.get("direct_signal_key"),
        "direct_route_id": candidate.get("paper_proxy_source_route_id"),
        "legs": [leg],
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
        "symbol": leg["symbol"],
        "side": side,
        "quantity": leg["quantity"],
        "fill_price": round(fill_price, 10),
        "reference_price": reference,
        "notional_usd": leg["notional_usd"],
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
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


def execute_order(conn: sqlite3.Connection, candidate: dict, review: dict, settings: dict) -> dict:
    context_loss_quarantine = candidate.get("paper_context_loss_quarantine") or {}
    context_loss_quarantined = bool(
        isinstance(context_loss_quarantine, dict)
        and not context_loss_quarantine.get("paper_fill_allowed", True)
    )
    if exploration_enabled(settings):
        candidate = prepare_candidate_for_exploration(dict(candidate), settings)
        if context_loss_quarantined:
            candidate["shadow_filtered"] = True
            candidate["paper_fill_allowed"] = False
            candidate["paper_entry_blocked"] = True
            candidate["candidate_reject_reason"] = context_loss_quarantine.get(
                "reason", "paper_context_loss_quarantine"
            )
            candidate["candidate_reject_detail"] = dict(context_loss_quarantine)
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
    else:
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
    if candidate.get("paper_nav_reference"):
        return _nav_reference_execution(candidate, settings)
    if candidate.get("paper_auction_reference"):
        return _auction_reference_execution(candidate, settings)
    order = build_order_ticket(candidate, review, settings)
    if candidate.get("shadow_filtered"):
        order["status"] = "shadow_filtered"
        order["shadow_filter"] = candidate.get("candidate_reject_detail")
        order["notes"].append("Paper fill suppressed by a paper-only candidate guard.")
        order_id = save_execution_order(conn, order, candidate, review)
        return {
            "order_id": order_id,
            "order": order,
            "fills": [],
            "paper_filled": False,
            "candidate": candidate,
        }

    if settings.get("mode") == "live" or settings.get("allow_live_trading"):
        order["status"] = "blocked_live_trading_not_implemented"
        order_id = save_execution_order(conn, order, candidate, review)
        return {
            "order_id": order_id,
            "order": order,
            "fills": [],
            "paper_filled": False,
            "candidate": candidate,
        }

    if order["status"] != "ready_for_paper_execution":
        order_id = save_execution_order(conn, order, candidate, review)
        return {
            "order_id": order_id,
            "order": order,
            "fills": [],
            "paper_filled": False,
            "candidate": candidate,
        }

    fills = [_paper_fill_for_leg(leg, settings) for leg in order["legs"]]
    order["status"] = "paper_filled"
    order_id = save_execution_order(conn, order, candidate, review)
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
    }
