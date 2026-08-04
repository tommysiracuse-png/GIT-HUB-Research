"""Deterministic agent tribunal for candidate review.

This module is intentionally model-free for the MVP. Later, the same review
schema can be filled by LangGraph LLM agents, but deterministic checks keep the
fast paper loop auditable.
"""

from __future__ import annotations

from contextual_failure_filters import build_context_features, context_matches
from paper_context_cost import paper_context_cost_gate
from storage import signal_key


def _matching_policies(key: str, policies: list[dict] | None, context_features: dict | None = None) -> list[dict]:
    if not policies:
        return []
    matched = []
    for policy in policies:
        policy_signal = policy.get("signal_key")
        market_key = policy.get("market_key")
        payload = policy.get("policy") or {}
        context_filter = payload.get("context_filter")
        if policy_signal == key:
            if context_matches(context_features or {}, context_filter):
                matched.append(policy)
        elif not policy_signal and market_key and key.startswith(str(market_key) + "|"):
            if context_matches(context_features or {}, context_filter):
                matched.append(policy)
    return matched


def _recovery_probe_due(policy: dict) -> bool:
    payload = policy.get("policy") or {}
    if not payload.get("allow_recovery_probes"):
        return False
    try:
        every = int(payload.get("recovery_probe_every_n_reviews") or 0)
        applied = int(policy.get("applied_count") or 0)
    except (TypeError, ValueError):
        return False
    return every > 0 and (applied + 1) % every == 0


def estimate_net_edge_bps(candidate: dict, settings: dict) -> float:
    if "edge_bps_estimate" in candidate:
        return round(float(candidate["edge_bps_estimate"]), 3)

    risk = settings["risk"]
    funding_component = abs(candidate["funding_bps"])
    basis_component = min(abs(candidate["basis_bps"]) * 0.45, 30.0)
    gross = funding_component + basis_component
    leg_count = 2 if candidate["direction"] in {"short_perp_long_spot", "long_perp_short_spot"} else 1
    estimated_cost = leg_count * (risk["taker_fee_bps_per_leg"] + risk["slippage_bps_per_leg"])
    return round(gross - estimated_cost, 3)


def _best_route_alternative(feasibility: dict, route: dict) -> dict:
    alternative = feasibility.get("best_route_alternative") or route.get("best_route_alternative") or {}
    return alternative if isinstance(alternative, dict) else {}


def _route_alternative_covers_missing(missing_requirements: list[str], alternative: dict) -> bool:
    if not alternative:
        return False
    if alternative.get("status") not in {"paper_testable_proxy", "paper_testable_research"}:
        return False
    if alternative.get("missing_permissions"):
        return False
    missing = set(str(item) for item in missing_requirements or [])
    replaced = set(str(item) for item in alternative.get("replaces_blockers", []) or [])
    return bool(missing) and missing.issubset(replaced)


def review_candidate(
    candidate: dict,
    settings: dict,
    adjustments: dict[str, float],
    policies: list[dict] | None = None,
) -> dict:
    risk = settings["risk"]
    key = signal_key(candidate)
    adjustment = adjustments.get(key, 0.0)
    learned_score = round(candidate["score"] + adjustment, 3)
    feasibility = candidate.get("execution_feasibility", {})
    feasibility_status = feasibility.get("status", "unknown")
    route = candidate.get("execution_route") or {}
    route_status = feasibility.get("route_status") or route.get("route_status") or feasibility_status
    route_id = feasibility.get("route_id") or route.get("route_id")
    missing_requirements = feasibility.get("missing_requirements") or route.get("missing_permissions") or []
    route_alternative = _best_route_alternative(feasibility, route)
    paper_route_eligibility = candidate.get("paper_route_eligibility") or {}
    route_alternative_usable = (
        _route_alternative_covers_missing(missing_requirements, route_alternative)
        and not paper_route_eligibility.get("suppressed", False)
    )
    net_edge_bps = estimate_net_edge_bps(candidate, settings)
    context_cost_gate = paper_context_cost_gate(candidate, settings)
    context_features = build_context_features(candidate, {}, net_edge_bps=net_edge_bps)
    matched_policies = _matching_policies(key, policies, context_features)

    evidence = []
    warnings = []
    hard_blocks = []
    applied_policies = []
    allocation_multiplier = max(
        0.0,
        min(1.0, float(candidate.get("quality_allocation_multiplier", 1.0))),
    )
    if context_cost_gate.get("applicable") and context_cost_gate.get("enabled"):
        allocation_multiplier = min(
            allocation_multiplier,
            max(0.0, min(1.0, float(context_cost_gate.get("score_multiplier", 1.0)))),
        )
    strategy_reliability = candidate.get("strategy_reliability") or {}
    if candidate.get("strategy_reliability_allocation_multiplier") is not None:
        allocation_multiplier = min(
            allocation_multiplier,
            max(0.0, min(1.0, float(candidate.get("strategy_reliability_allocation_multiplier", 1.0)))),
        )

    if abs(candidate["funding_bps"]) >= 3:
        evidence.append(f"funding magnitude {candidate['funding_bps']} bps")
    if abs(candidate["basis_bps"]) >= 15:
        evidence.append(f"basis magnitude {candidate['basis_bps']} bps")
    if candidate.get("edge_bps_estimate", 0) >= risk["min_net_edge_bps"]:
        evidence.append(f"scanner edge estimate {candidate['edge_bps_estimate']} bps")
    if candidate.get("region"):
        evidence.append(f"region exposure {candidate['region']}")
    if candidate["liquidity_score"] >= risk["min_liquidity_score"]:
        evidence.append(f"liquidity score {candidate['liquidity_score']}")
    else:
        hard_blocks.append(f"liquidity score below minimum: {candidate['liquidity_score']}")

    if candidate["spread_bps"] > risk["max_spread_bps"]:
        hard_blocks.append(f"spread too wide: {candidate['spread_bps']} bps")
    if abs(candidate["change_24h_pct"]) > risk["max_abs_24h_move_pct"]:
        warnings.append(f"large 24h move: {candidate['change_24h_pct']}%")
    if net_edge_bps < risk["min_net_edge_bps"]:
        hard_blocks.append(f"estimated net edge too small after costs: {net_edge_bps} bps")
    if context_cost_gate.get("applicable") and context_cost_gate.get("enabled"):
        if context_cost_gate.get("eligible"):
            evidence.append(
                "paper context gross edge "
                f"{context_cost_gate.get('gross_edge_bps')} bps clears required "
                f"{context_cost_gate.get('required_gross_edge_bps')} bps"
            )
        else:
            hard_blocks.append(
                "paper context cost floor not cleared: gross edge "
                f"{context_cost_gate.get('gross_edge_bps')} bps must exceed "
                f"{context_cost_gate.get('required_gross_edge_bps')} bps"
            )
    if feasibility_status == "conditional":
        if missing_requirements and route_alternative_usable:
            allocation_multiplier = min(
                allocation_multiplier,
                max(0.0, min(1.0, float(route_alternative.get("paper_allocation_multiplier", 1.0)))),
            )
            warnings.append(
                "direct route blocked by "
                f"{', '.join(missing_requirements)}; using paper-only alternative "
                f"{route_alternative.get('alternative_id')} at reduced allocation"
            )
            evidence.append(
                f"paper route alternative {route_alternative.get('alternative_id')} "
                f"status {route_alternative.get('status')}"
            )
        elif missing_requirements:
            hard_blocks.append(f"trade requires unconfirmed route capability: {', '.join(missing_requirements)}")
        else:
            hard_blocks.append("trade requires unconfirmed borrow, margin, or venue capability")
    if feasibility_status == "route_unknown" or route_status == "route_unknown":
        hard_blocks.append("trade route unknown; specialist, broker, permission, fee, or API route research required")
    if feasibility_status == "blocked" or route_status == "blocked":
        hard_blocks.append("execution route blocked or not paper-testable")
    if feasibility_status == "watch_only":
        hard_blocks.append("watch-only signal")
    if candidate["direction"] == "watch_only":
        hard_blocks.append("scanner classified direction as watch-only")
    if candidate.get("stale_minutes", 0) > 90:
        hard_blocks.append(f"market data stale: {candidate['stale_minutes']} minutes")
    if candidate.get("quality_score") is not None:
        evidence.append(
            f"frontier executable quality {candidate['quality_score']} "
            f"({candidate.get('quality_status', 'unknown')})"
        )
    if strategy_reliability:
        action = strategy_reliability.get("action")
        reasons = strategy_reliability.get("reasons") or []
        if strategy_reliability.get("protect_working_slice"):
            evidence.append(f"strategy reliability protected slice: {action}")
        if allocation_multiplier < 1.0 and not candidate.get("paper_entry_blocked"):
            warnings.append(f"strategy reliability allocation multiplier {allocation_multiplier}: {action}")
    if candidate.get("paper_entry_blocked"):
        if paper_route_eligibility.get("suppressed"):
            reasons = paper_route_eligibility.get("blocker_reasons") or []
            missing = paper_route_eligibility.get("missing_prerequisites") or []
            hard_blocks.append(
                "paper route eligibility blocked: "
                + ", ".join(str(item) for item in reasons)
                + (f"; missing prerequisites: {', '.join(str(item) for item in missing)}" if missing else "")
            )
        elif strategy_reliability:
            reason_text = ", ".join((strategy_reliability.get("reasons") or [])[:3])
            hard_blocks.append(
                f"strategy reliability pack moved this signal to shadow-only: "
                f"{strategy_reliability.get('action')} {reason_text}".strip()
            )
        else:
            hard_blocks.append(
                "frontier depth quality is shadow-only: "
                + ", ".join((candidate.get("anomaly_flags") or [])[:3])
            )
    elif candidate.get("quality_action") == "conditional":
        warnings.append(
            f"frontier depth quality requires conditional 25% paper allocation: {candidate.get('quality_score')}"
        )

    if learned_score < settings["scanner"]["min_base_score"]:
        hard_blocks.append(f"learned score below threshold: {learned_score}")

    for policy in matched_policies:
        policy_blocks = []
        policy_id = policy["policy_id"]
        policy_payload = policy.get("policy") or {}
        context_filter = policy_payload.get("context_filter") or {}
        allocation_value = policy.get("allocation_multiplier")
        policy_allocation = 1.0 if allocation_value is None else float(allocation_value)
        is_recovery_probe = _recovery_probe_due(policy)
        if is_recovery_probe and policy_payload.get("recovery_probe_allocation_multiplier") is not None:
            probe_allocation = float(policy_payload["recovery_probe_allocation_multiplier"])
            policy_allocation = probe_allocation if policy.get("pause_entries") else min(policy_allocation, probe_allocation)
        allocation_multiplier = min(allocation_multiplier, policy_allocation)
        policy_min_score = float(settings["scanner"]["min_base_score"]) + float(policy.get("min_score_delta") or 0.0)
        if policy.get("pause_entries") and not is_recovery_probe:
            policy_blocks.append("self-improvement policy paused entries for this signal")
        if learned_score < policy_min_score:
            policy_blocks.append(f"self-improvement min learned score {round(policy_min_score, 3)} not met")
        if policy.get("min_net_edge_bps") is not None and net_edge_bps < float(policy["min_net_edge_bps"]):
            policy_blocks.append(f"self-improvement min net edge {policy['min_net_edge_bps']} bps not met")
        if policy.get("max_spread_bps") is not None and candidate["spread_bps"] > float(policy["max_spread_bps"]):
            policy_blocks.append(f"self-improvement max spread {policy['max_spread_bps']} bps exceeded")
        if policy.get("pause_entries") and is_recovery_probe and not policy_blocks:
            warnings.append(
                "signal safety recovery probe allowed a tiny paper entry to test whether this family has recovered"
            )
        if allocation_multiplier < 1.0 and not policy_blocks:
            warnings.append(f"self-improvement allocation multiplier {allocation_multiplier}")
        for block in policy_blocks:
            hard_blocks.append(block)
        applied_policies.append(
            {
                "policy_id": policy_id,
                "policy_type": policy.get("policy_type"),
                "allocation_multiplier": allocation_multiplier,
                "filtered": bool(policy_blocks),
                "recovery_probe": bool(is_recovery_probe and not policy_blocks),
                "context_filter": context_filter,
                "matched_context": {key: context_features.get(key) for key in context_filter},
                "blocks": policy_blocks,
            }
        )

    route_research_blocks = (
        "trade route unknown",
        "trade requires unconfirmed",
    )
    non_feasibility_blocks = [block for block in hard_blocks if not block.startswith(route_research_blocks)]
    if hard_blocks:
        if (
            feasibility_status in {"conditional", "route_unknown"}
            and settings["scanner"].get("paper_trade_conditional", False)
            and not non_feasibility_blocks
        ):
            decision = "approve_conditional_paper_trade"
        else:
            decision = "conditional_review" if feasibility_status == "conditional" else "reject"
    else:
        decision = "approve_paper_trade"
    if decision == "approve_paper_trade" and candidate.get("quality_action") == "conditional":
        decision = "approve_conditional_paper_trade"
    if decision == "approve_paper_trade" and route_alternative_usable:
        decision = "approve_conditional_paper_trade"

    confidence = 0.5
    confidence += min(abs(candidate["funding_bps"]) / 40.0, 0.15)
    confidence += min(abs(candidate["basis_bps"]) / 250.0, 0.15)
    confidence += min(candidate["liquidity_score"] * 0.15, 0.15)
    confidence -= min(candidate["spread_bps"] / 60.0, 0.12)
    confidence += max(min(adjustment / 100.0, 0.08), -0.08)
    if hard_blocks:
        confidence -= 0.2
    confidence = round(max(0.0, min(0.95, confidence)), 3)

    return {
        "decision": decision,
        "signal_key": key,
        "base_score": candidate["score"],
        "score_adjustment": round(adjustment, 3),
        "learned_score": learned_score,
        "confidence": confidence,
        "net_edge_bps_estimate": net_edge_bps,
        "paper_context_cost_gate": context_cost_gate,
        "paper_allocation_multiplier": round(allocation_multiplier, 4),
        "applied_policies": applied_policies,
        "context_features": context_features,
        "feasibility_status": feasibility_status,
        "route_id": route_id,
        "effective_route_id": route_alternative.get("route_id") if route_alternative_usable else route_id,
        "route_status": route_status,
        "missing_requirements": missing_requirements,
        "direct_missing_requirements": missing_requirements if route_alternative_usable else [],
        "route_alternative": route_alternative if route_alternative_usable else {},
        "route_alternative_used": bool(route_alternative_usable),
        "route_confidence": feasibility.get("route_confidence") or route.get("confidence"),
        "route_notes": feasibility.get("route_notes") or route.get("route_notes", []),
        "evidence": evidence,
        "warnings": warnings,
        "hard_blocks": hard_blocks,
        "agent_votes": {
            "hunter": "pass" if evidence else "fail",
            "microstructure": "pass" if candidate["spread_bps"] <= risk["max_spread_bps"] else "fail",
            "feasibility": "pass" if feasibility_status == "standard" else "fail",
            "risk": "pass" if not hard_blocks else "fail",
            "judge": decision,
        },
    }
