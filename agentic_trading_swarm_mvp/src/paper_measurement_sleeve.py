"""Narrow paper-only recovery probes for the bounded expansion campaign.

The durable admission queue may identify a directed, direct-route candidate
whose only remaining block is an under-sampled historical strategy overlay.
This module can reopen that exact paper episode.  It deliberately cannot
override data quality, route, cost, capability, proxy, synthetic, or live-mode
guards.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_APPROVALS = {"approve_paper_trade", "approve_conditional_paper_trade"}
_DIRECT_ROUTE_STATES = {"standard", "feasible"}
_QUALITY_STATES = {"verified", "normal"}
_ALLOWED_HISTORICAL_GUARDS = {
    "paper_strategy_family_quarantine",
    "paper_lineage_source_health",
    "strategy_reliability",
}
_ACTIVE_PHASES = {
    "measurement",
    "canary",
    "strategy_lab_canary",
    "research",
    "paid_research",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _route_status(candidate: Mapping[str, Any]) -> str:
    for container in (
        _mapping(candidate.get("direct_execution_feasibility")),
        _mapping(candidate.get("execution_feasibility")),
        _mapping(candidate.get("direct_execution_route")),
        _mapping(candidate.get("execution_route")),
        candidate,
    ):
        for key in ("route_status", "status", "paper_route_status"):
            value = str(container.get(key) or "").strip().lower()
            if value:
                return value
    return "unknown"


def _quality_status(candidate: Mapping[str, Any]) -> str:
    return str(
        candidate.get("quality_status")
        or candidate.get("proxy_quality_status")
        or ""
    ).strip().lower()


def _hard_guard_present(candidate: Mapping[str, Any]) -> bool:
    if candidate.get("paper_fill_gate_blocked"):
        return True
    fill_gate = _mapping(candidate.get("frontier_paper_fill_gate"))
    if fill_gate and not bool(fill_gate.get("paper_fill_allowed", True)):
        return True
    admission = _mapping(candidate.get("frontier_paper_admission"))
    if admission and not bool(admission.get("admitted", True)):
        return True
    registry = _mapping(candidate.get("paper_route_registry"))
    if str(registry.get("action") or "").lower() == "suppress":
        return True
    if candidate.get("paper_okx_basis_decay_quarantine"):
        record = _mapping(candidate.get("paper_okx_basis_decay_quarantine"))
        if record.get("active") or not bool(record.get("paper_fill_allowed", True)):
            return True
    context = _mapping(candidate.get("paper_context_loss_quarantine"))
    if context and not bool(context.get("paper_fill_allowed", True)):
        return True
    return False


def apply_bounded_measurement_probe(
    candidate: Mapping[str, Any],
    review: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a fillable copy only for an explicitly prequalified episode."""

    output = dict(candidate)
    cfg = _mapping(settings.get("paper_expansion"))
    phase = str(cfg.get("runtime_phase") or cfg.get("phase") or "").strip().lower()
    if not bool(cfg.get("measurement_probe_enabled", False)) or phase not in _ACTIVE_PHASES:
        return output
    if settings.get("mode") != "paper" or bool(settings.get("allow_live_trading")):
        return output
    if not bool(candidate.get("_paper_measurement_probe_allowed")):
        return output
    if not candidate.get("_paper_admission_queue_id") or candidate.get("_paper_admission_lane") != "discovery":
        return output
    if str(review.get("decision") or "") not in _APPROVALS or list(review.get("hard_blocks") or []):
        return output
    if int(candidate.get("_paper_admission_reliable_labels") or 0) >= int(
        cfg.get("discovery_retire_min_labels", 20)
    ):
        return output
    if _route_status(candidate) not in _DIRECT_ROUTE_STATES:
        return output
    if _quality_status(candidate) not in _QUALITY_STATES:
        return output
    if float(candidate.get("last") or 0.0) <= 0.0:
        return output
    if str(candidate.get("direction") or "").lower() in {"", "watch_only"}:
        return output
    if (
        candidate.get("synthetic_research_paper")
        or candidate.get("paper_proxy_activated")
        or candidate.get("paper_proxy_not_live_equivalent")
        or str(candidate.get("signal_stats_scope") or "").lower()
        in {"synthetic_research", "paper_proxy"}
    ):
        return output
    if _hard_guard_present(candidate):
        return output
    guard = str(
        candidate.get("_paper_measurement_probe_guard")
        or _mapping(candidate.get("candidate_reject_detail")).get("guard")
        or ""
    ).strip()
    configured_guards = {
        str(value)
        for value in cfg.get("measurement_probe_allowed_guards", _ALLOWED_HISTORICAL_GUARDS)
    }
    if guard not in configured_guards:
        return output
    if float(review.get("net_edge_bps_estimate") or review.get("net_edge_bps") or 0.0) < float(
        cfg.get("measurement_probe_min_net_edge_bps", 2.0)
    ):
        return output

    output["paper_measurement_probe"] = {
        "enabled": True,
        "paper_only": True,
        "guard_overridden": guard,
        "reliable_labels_before_probe": int(
            candidate.get("_paper_admission_reliable_labels") or 0
        ),
        "queue_id": candidate.get("_paper_admission_queue_id"),
        "episode_id": candidate.get("episode_id"),
    }
    output["shadow_filtered"] = False
    output["paper_fill_allowed"] = True
    output["paper_eligible"] = True
    output["paper_entry_blocked"] = False
    output["paper_observation_only"] = False
    output["paper_action"] = "bounded_measurement_probe"
    output["paper_status"] = "paper_eligible"
    output["paper_fill_status"] = "paper_eligible"
    output["paper_order_status"] = "paper_eligible"
    output["router_action"] = "bounded_measurement_probe"
    output["candidate_status"] = "bounded_measurement_probe"
    output["signal_stats_scope"] = "direct"
    output["paper_execution_semantics"] = "direct_paper_measurement_probe"
    output["promotion_eligible"] = False
    output["paper_allocation_multiplier"] = min(
        float(output.get("paper_allocation_multiplier") or 1.0),
        float(cfg.get("measurement_probe_allocation_multiplier", 1.0)),
    )
    output.pop("candidate_reject_reason", None)
    output.pop("candidate_reject_detail", None)
    return output
