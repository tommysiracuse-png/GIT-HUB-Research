"""Read-only paper route policy keyed by venue, trade type, and direction.

The registry is deliberately descriptive: it never checks credentials, calls a
venue, changes account state, or authorizes live trading.  It adds conservative
route requirements and cost assumptions to paper candidates so unsupported
direct routes remain visible as route context rather than being mistaken for
executable alpha.
"""

from __future__ import annotations

import copy
import json
import math
import pathlib
from typing import Iterable, Mapping


ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "paper_route_registry.json"
SUPPORTED_STATUSES = {"supported", "conditional", "unsupported"}
DEFAULT_UNSPECIFIED_SCORE_MULTIPLIER = 0.20
SCOPED_TRADE_TYPES = {"frontier_crypto_venue_map", "perp_funding_basis"}


def _token(value: object) -> str:
    return str(value or "").strip().upper()


def _surface_token(value: object) -> str:
    return str(value or "").strip().lower()


def _finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 6)


def load_paper_route_registry(path: pathlib.Path | None = None) -> dict:
    """Load and validate the maintained registry without ever writing it."""

    source = path or REGISTRY_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "version": 0,
            "paper_only": True,
            "unspecified_score_multiplier": DEFAULT_UNSPECIFIED_SCORE_MULTIPLIER,
            "routes": [],
        }
    routes: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in payload.get("routes") or []:
        if not isinstance(raw, dict):
            continue
        key = (
            _token(raw.get("venue")),
            _surface_token(raw.get("trade_type")),
            _surface_token(raw.get("direction")),
        )
        status = _surface_token(raw.get("support_status"))
        if not all(key) or key in seen or status not in SUPPORTED_STATUSES:
            continue
        item = copy.deepcopy(raw)
        item["venue"], item["trade_type"], item["direction"] = key
        item["support_status"] = status
        item["paper_only"] = True
        item["live_execution_allowed"] = False
        item["route_key"] = "|".join(key)
        routes.append(item)
        seen.add(key)
    multiplier = _finite_nonnegative(payload.get("unspecified_score_multiplier"))
    if multiplier is None:
        multiplier = DEFAULT_UNSPECIFIED_SCORE_MULTIPLIER
    return {
        "version": int(payload.get("version") or 0),
        "paper_only": True,
        "unspecified_score_multiplier": min(1.0, multiplier),
        "routes": routes,
    }


def _candidate_trade_type(candidate: Mapping[str, object]) -> str:
    direct = _surface_token(candidate.get("trade_type"))
    if direct in SCOPED_TRADE_TYPES:
        return direct
    for field in ("market_surface", "signal_family", "strategy_lineage_key", "signal_key"):
        value = _surface_token(candidate.get(field))
        for trade_type in SCOPED_TRADE_TYPES:
            if trade_type in value:
                return trade_type
    return direct


def candidate_route_key(candidate: Mapping[str, object]) -> tuple[str, str, str]:
    """Return the normalized registry identity for a candidate."""

    venue = _token(candidate.get("venue") or candidate.get("execution_venue"))
    trade_type = _candidate_trade_type(candidate)
    direction = _surface_token(candidate.get("direction") or candidate.get("signal_direction"))
    return venue, trade_type, direction


def _registry_enabled(settings: Mapping[str, object] | None) -> bool:
    if not isinstance(settings, Mapping):
        return True
    configured = settings.get("paper_route_registry")
    if isinstance(configured, Mapping):
        return bool(configured.get("enabled", True))
    return configured is not False


def _candidate_route_evidence_present(candidate: Mapping[str, object]) -> bool:
    capabilities = candidate.get("venue_capabilities")
    if isinstance(capabilities, Mapping) and bool(capabilities):
        return True
    for container_name in ("execution_feasibility", "execution_route"):
        container = candidate.get(container_name)
        if not isinstance(container, Mapping):
            continue
        capabilities = container.get("venue_capabilities")
        if isinstance(capabilities, Mapping) and bool(capabilities):
            return True
    return False


def _paper_proxy_present(candidate: Mapping[str, object]) -> bool:
    for container in (candidate, candidate.get("execution_feasibility"), candidate.get("execution_route")):
        if not isinstance(container, Mapping):
            continue
        alternative = (
            container.get("best_route_alternative")
            or container.get("paper_route_alternative")
            or container.get("route_alternative")
        )
        if not isinstance(alternative, Mapping):
            continue
        status = _surface_token(alternative.get("status") or alternative.get("route_status"))
        if status in {"paper_testable_proxy", "paper_testable_via_proxy"}:
            return True
    return False


def _cost_annotation(costs: object) -> dict[str, object]:
    source = costs if isinstance(costs, dict) else {}
    result = {
        "fee_round_trip": _finite_nonnegative(source.get("fee_round_trip")),
        "slippage_round_trip": _finite_nonnegative(source.get("slippage_round_trip")),
        "borrow": _finite_nonnegative(source.get("borrow")),
        "funding_drag": _finite_nonnegative(source.get("funding_drag")),
    }
    known = [value for value in result.values() if value is not None]
    result["estimated_total"] = round(sum(known), 6) if known else None
    # Borrow is the important unknown on short-spot routes; keep completeness
    # false when it has intentionally not been estimated.
    result["complete"] = all(result[name] is not None for name in ("fee_round_trip", "slippage_round_trip", "borrow"))
    result["source"] = "maintained_paper_route_registry"
    return result


def _fallback_requirements(key: tuple[str, str, str]) -> tuple[list[str], list[str]]:
    _, trade_type, direction = key
    if trade_type == "frontier_crypto_venue_map" and direction == "short_frontier_spot":
        return (
            ["crypto_spot", "margin_spot", "spot_short", "spot_borrow"],
            ["margin"],
        )
    if trade_type != "perp_funding_basis":
        return [], []
    permissions = ["crypto_derivatives"]
    modes = ["derivatives_margin"]
    if direction == "short_perp_long_spot":
        permissions.append("crypto_spot")
        modes.append("spot")
    elif direction == "long_perp_short_spot":
        permissions.extend(("crypto_spot", "spot_borrow", "margin_spot"))
        modes.append("spot_margin")
    return permissions, modes


def assess_paper_route_registry(
    candidate: Mapping[str, object],
    settings: Mapping[str, object] | None = None,
    registry: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Assess one candidate against the exact tuple registry."""

    key = candidate_route_key(candidate)
    in_scope = key[1] in SCOPED_TRADE_TYPES and (
        key[1] == "perp_funding_basis" or key[2] == "short_frontier_spot"
    )
    mode = _surface_token((settings or {}).get("mode", "paper"))
    enabled = _registry_enabled(settings)
    data = dict(load_paper_route_registry() if registry is None else registry)
    route = next(
        (
            copy.deepcopy(item)
            for item in data.get("routes") or []
            if isinstance(item, dict)
            and (
                _token(item.get("venue")),
                _surface_token(item.get("trade_type")),
                _surface_token(item.get("direction")),
            ) == key
        ),
        None,
    )
    matched = route is not None
    candidate_evidence_present = _candidate_route_evidence_present(candidate)
    proxy_present = _paper_proxy_present(candidate)
    support_status = str(route.get("support_status")) if matched else "unspecified"
    fallback_permissions, fallback_modes = _fallback_requirements(key)
    multiplier = 1.0
    action = "tag"
    if enabled and mode == "paper" and in_scope:
        if support_status == "unsupported" and not proxy_present:
            # Conditional short route availability is route intelligence, not
            # evidence that the price or PnL is invalid.  Keep the candidate
            # in paper exploration and let the resolver attach a read-only
            # diagnostic/down-rank packet before ranking.
            action = "diagnose"
        elif support_status == "unspecified" and not candidate_evidence_present:
            multiplier = min(1.0, float(data.get("unspecified_score_multiplier") or DEFAULT_UNSPECIFIED_SCORE_MULTIPLIER))
            action = "penalize"
    elif not enabled:
        action = "disabled"
    elif mode != "paper":
        action = "observe_only"
    return {
        "paper_only": True,
        "live_execution_allowed": False,
        "enabled": enabled,
        "mode": mode,
        "in_scope": in_scope,
        "matched": matched,
        "candidate_route_evidence_present": candidate_evidence_present,
        "paper_proxy_present": proxy_present,
        "registry_version": int(data.get("version") or 0),
        "route_key": "|".join(key),
        "venue": key[0],
        "trade_type": key[1],
        "direction": key[2],
        "support_status": support_status,
        "action": action,
        "score_multiplier": multiplier,
        "allocation_multiplier": multiplier,
        "required_permissions": (
            list(route.get("required_permissions") or []) if matched else fallback_permissions
        ),
        "required_account_modes": (
            list(route.get("required_account_modes") or []) if matched else fallback_modes
        ),
        "estimated_cost_bps": _cost_annotation(route.get("estimated_cost_bps") if matched else {}),
        "reason": (
            (
                "The direct route is unsupported; an explicit paper proxy will be evaluated separately."
                if proxy_present and support_status == "unsupported"
                else route.get("reason")
            )
            if matched
            else "No exact venue/trade_type/direction paper route is maintained."
        ),
    }


def apply_paper_route_registry(
    candidate: Mapping[str, object],
    settings: Mapping[str, object] | None = None,
    registry: Mapping[str, object] | None = None,
) -> dict:
    """Tag and conservatively gate a paper candidate; return a new dictionary."""

    enriched = dict(candidate)
    assessment = assess_paper_route_registry(enriched, settings, registry)
    enriched["paper_route_registry"] = assessment
    enriched["paper_route_registry_key"] = assessment["route_key"]
    enriched["paper_route_registry_status"] = assessment["support_status"]
    enriched["paper_route_required_permissions"] = assessment["required_permissions"]
    enriched["paper_route_required_account_modes"] = assessment["required_account_modes"]
    enriched["paper_route_estimated_cost_bps"] = assessment["estimated_cost_bps"]
    enriched["paper_route_registry_allocation_multiplier"] = assessment["allocation_multiplier"]

    costs = assessment["estimated_cost_bps"]
    if assessment["matched"]:
        if enriched.get("estimated_round_trip_cost_bps") is None and costs.get("estimated_total") is not None:
            enriched["estimated_round_trip_cost_bps"] = costs["estimated_total"]
            enriched["paper_route_cost_fallback_applied"] = True
        enriched.setdefault("fee_model", "maintained_paper_route_registry")

    action = assessment["action"]
    multiplier = float(assessment["score_multiplier"])
    if action == "penalize":
        raw_score = enriched.get("pre_paper_route_registry_score", enriched.get("score"))
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            enriched.setdefault("pre_paper_route_registry_score", raw_score)
            enriched["score"] = round(max(0.0, float(raw_score)) * multiplier, 6)
        enriched["paper_route_registry_penalty_applied"] = True
    if action == "suppress":
        enriched["paper_entry_blocked"] = True
        enriched["promotion_eligible"] = False
        enriched["paper_allocation_multiplier"] = 0.0
        enriched["paper_route_registry_block_reason"] = "unsupported_paper_route"
    elif action == "penalize":
        existing_multiplier = enriched.get("paper_allocation_multiplier", 1.0)
        try:
            bounded_existing = max(0.0, min(1.0, float(existing_multiplier)))
        except (TypeError, ValueError):
            bounded_existing = 1.0
        enriched["paper_allocation_multiplier"] = min(bounded_existing, multiplier)
        enriched["paper_route_registry_block_reason"] = "paper_route_unspecified"
    return enriched


def apply_paper_route_registry_many(
    candidates: Iterable[Mapping[str, object]],
    settings: Mapping[str, object] | None = None,
    registry: Mapping[str, object] | None = None,
) -> list[dict]:
    data = load_paper_route_registry() if registry is None else registry
    return [apply_paper_route_registry(item, settings, data) for item in candidates]
