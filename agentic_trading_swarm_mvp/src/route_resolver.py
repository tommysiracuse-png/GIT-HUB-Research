"""Read-only execution route resolver.

This module turns a scanner candidate plus local account-capability settings
into a structured route decision. It does not call broker APIs, request
credentials, place orders, or enable live execution. Its job is to make route
uncertainty explicit so the radar can paper-test conditional ideas while
tracking what would be required to make them real.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import pathlib
from typing import Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]

try:
    from paper_context_cost import (
        annotate_paper_context_cost,
        paper_context_cost_report,
        rank_paper_candidates_by_context,
    )
    from paper_route_registry import apply_paper_route_registry
except ModuleNotFoundError:  # pragma: no cover - package import fallback
    from .paper_context_cost import (
        annotate_paper_context_cost,
        paper_context_cost_report,
        rank_paper_candidates_by_context,
    )
    from .paper_route_registry import apply_paper_route_registry

try:
    from route_intelligence import (
        build_conditional_short_route_diagnostics,
        build_route_requirements_report,
    )
except ModuleNotFoundError:  # pragma: no cover - package import fallback
    from .route_intelligence import (
        build_conditional_short_route_diagnostics,
        build_route_requirements_report,
    )

try:
    from storage import RUNS_DIR
except ModuleNotFoundError:  # pragma: no cover - fallback for isolated test imports
    # The resolver only needs the report directory.  Avoid importing the
    # persistence module here so package-style, read-only route diagnostics do
    # not load storage's runtime-only dependencies.
    RUNS_DIR = ROOT / "runs"


CONFIG_DIR = ROOT / "config"
CUSTOM_ROUTES_PATH = CONFIG_DIR / "execution_routes.json"
EXAMPLE_ROUTES_PATH = CONFIG_DIR / "execution_routes.example.json"
REPORT_JSON = RUNS_DIR / "route_resolver_report.json"
REPORT_MD = RUNS_DIR / "route_resolver_report.md"
ROUTE_INTELLIGENCE_JSON = RUNS_DIR / "route_intelligence_report.json"
ROUTE_INTELLIGENCE_MD = RUNS_DIR / "route_intelligence_report.md"
VENUE_CAPABILITIES_PATH = CONFIG_DIR / "paper_route_intelligence" / "crypto_venues.json"

REQUIREMENT_STATUSES = {"confirmed", "missing", "unknown", "not_applicable"}
HARD_BLOCKING_LEVELS = {"hard", "blocking"}
ROUTE_STATUSES = {
    "standard",
    "conditional",
    "route_unknown",
    "blocked",
    "unsupported_or_unknown",
    "paper_testable_via_proxy",
    "blocked_until_requirements_confirmed",
    "paper_observation_only",
}
PAPER_ROUTE_ASSUMPTION_SCORE_MULTIPLIER = 0.20
PAPER_ROUTE_UNKNOWN_RANK_CAP = 0.20
PAPER_PROXY_EXECUTION_SEMANTICS = "proxy_not_live_equivalent"
PAPER_PROXY_STATS_SCOPE = "paper_proxy"
OKX_DERIVATIVES_PAPER_ROUTE = "okx_derivatives_paper"
PAPER_PROXY_ROUTE_FEASIBILITY_SCORE = 0.75

VENUE_CAPABILITY_ALIASES = {
    "supports_spot_short": ("spot_short_supported", "supports_spot_short_margin"),
    "supports_margin_spot": ("margin_supported", "supports_margin"),
    "supports_borrow_check": ("borrow_supported", "borrow_inventory_supported"),
    "supports_basis_path": ("supports_basis_carry", "synthetic_carry_supported"),
    "supports_spot_long": ("spot_supported", "supports_spot"),
    "supports_perpetuals": ("perp_supported", "perp_available"),
    "supports_transfers": ("transfer_supported", "cross_venue_transfer_supported"),
    "supports_hedge_mode": ("hedge_mode_supported", "dual_side_position_supported"),
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_route_registry() -> dict:
    path = CUSTOM_ROUTES_PATH if CUSTOM_ROUTES_PATH.exists() else EXAMPLE_ROUTES_PATH
    if not path.exists():
        return {"routes": []}
    return json.loads(path.read_text(encoding="utf-8"))


def load_venue_capability_registry() -> dict[str, dict]:
    """Load read-only paper route capabilities keyed by venue.

    This registry describes modeled route support only. It is not account
    authorization and is never used to enable live execution.
    """

    if not VENUE_CAPABILITIES_PATH.exists():
        return {}
    try:
        payload = json.loads(VENUE_CAPABILITIES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    registry: dict[str, dict] = {}
    for venue, capabilities in payload.items():
        if not isinstance(capabilities, dict):
            continue
        registry[str(venue).strip().upper()] = _normalize_venue_capabilities(capabilities)
    return registry


def _normalize_venue_capabilities(capabilities: dict) -> dict:
    """Normalize aliases and fail closed on contradictory capability flags."""

    normalized = dict(capabilities)
    for canonical, legacy_keys in VENUE_CAPABILITY_ALIASES.items():
        values = [
            normalized[key]
            for key in (canonical, *legacy_keys)
            if key in normalized
        ]
        if not values:
            continue
        states = [_eligibility_bool(value) for value in values]
        if False in states:
            normalized[canonical] = False
        elif None in states:
            normalized[canonical] = None
        else:
            normalized[canonical] = True
    return normalized


def _merge_venue_capabilities(
    maintained: dict | None,
    observed: dict | None,
) -> dict:
    """Merge capability evidence without allowing a positive flag to erase a veto.

    Maintained metadata defines the paper route envelope. Candidate/instrument
    metadata may narrow that envelope, but cannot broaden a false or unknown
    maintained capability into a supported one.
    """

    maintained_normalized = _normalize_venue_capabilities(maintained or {})
    observed_normalized = _normalize_venue_capabilities(observed or {})
    merged = dict(maintained_normalized)
    for key, value in observed_normalized.items():
        if key not in VENUE_CAPABILITY_ALIASES or key not in maintained_normalized:
            merged[key] = value
            continue
        maintained_state = _eligibility_bool(maintained_normalized[key])
        observed_state = _eligibility_bool(value)
        if maintained_state is False or observed_state is False:
            merged[key] = False
        elif maintained_state is None or observed_state is None:
            merged[key] = None
        else:
            merged[key] = True
    return merged


def _configured_venue_capabilities(
    candidate: dict,
    capability_registry: dict[str, dict] | None = None,
) -> dict:
    registry = (
        load_venue_capability_registry()
        if capability_registry is None
        else capability_registry
    )
    venue = str(candidate.get("venue") or "").strip().upper()
    explicit_surface = str(
        candidate.get("market_key")
        or candidate.get("market_surface_key")
        or candidate.get("execution_venue")
        or ""
    ).strip().upper()
    lookup_keys: list[str] = []
    if explicit_surface in registry:
        lookup_keys.append(explicit_surface)
    if venue:
        descriptor = " ".join(
            _paper_gate_text(candidate.get(key))
            for key in (
                "surface",
                "market_type",
                "instrument_type",
                "instrument",
                "inst_id",
                "asset_class",
                "direction",
                "side",
                "strategy_tags",
            )
        )
        if "spot" in descriptor:
            lookup_keys.append(f"{venue}_SPOT")
        lookup_keys.append(venue)
    for key in lookup_keys:
        capabilities = registry.get(key)
        if isinstance(capabilities, dict):
            result = _normalize_venue_capabilities(capabilities)
            result.setdefault("capability_profile", key)
            return result
    return {}


def _route_lookup(registry: dict) -> dict[str, dict]:
    return {item.get("route_id"): item for item in registry.get("routes", []) if item.get("route_id")}


def _legacy_status(available: bool, *, missing: list[str] | None = None, blocked: bool = False) -> str:
    if blocked:
        return "blocked"
    return "standard" if available and not missing else "conditional"


def _coerce_requirement_status(value: object) -> str:
    status = str(value or "unknown")
    return status if status in REQUIREMENT_STATUSES else "unknown"


def _requirement_id(requirement: dict) -> str:
    return str(requirement.get("requirement_id") or requirement.get("id") or requirement.get("capability_key") or "")


def _synth_requirement(requirement_id: str, *, status: str = "unknown") -> dict:
    label = requirement_id.replace("_", " ")
    return {
        "requirement_id": requirement_id,
        "category": "account",
        "description": f"Confirm {label}.",
        "status": status,
        "blocking_level": "hard",
        "how_to_verify": f"Check account, venue, broker, or API settings for {label}.",
        "evidence_source": "legacy_route_permissions",
    }


def _route_requirements_template(route_meta: dict, required: set[str], missing: set[str]) -> list[dict]:
    configured = route_meta.get("requirements") or []
    if configured:
        return [dict(item) for item in configured]
    requirement_ids = sorted(required | missing)
    return [_synth_requirement(item, status="missing" if item in missing else "confirmed") for item in requirement_ids]


def _resolve_requirement_status(
    requirement: dict,
    required: set[str],
    missing: set[str],
    overrides: dict[str, str],
) -> str:
    rid = _requirement_id(requirement)
    capability_key = str(requirement.get("capability_key") or "")
    identifiers = {item for item in (rid, capability_key) if item}
    if rid in overrides:
        return _coerce_requirement_status(overrides[rid])
    if identifiers & missing:
        return "missing"
    if identifiers & required:
        return "confirmed"
    return _coerce_requirement_status(requirement.get("status", "unknown"))


def _build_requirements(
    route_meta: dict,
    *,
    required_permissions: list[str],
    missing_permissions: list[str],
    status_overrides: dict[str, str] | None = None,
) -> list[dict]:
    required = {str(item) for item in required_permissions}
    missing = {str(item) for item in missing_permissions}
    overrides = status_overrides or {}
    now = _utc_now()
    output = []
    for template in _route_requirements_template(route_meta, required, missing):
        rid = _requirement_id(template)
        if not rid:
            continue
        requirement = {
            "requirement_id": rid,
            "category": str(template.get("category") or "account"),
            "description": str(template.get("description") or f"Confirm {rid.replace('_', ' ')}."),
            "status": _resolve_requirement_status(template, required, missing, overrides),
            "blocking_level": str(template.get("blocking_level") or "hard"),
            "how_to_verify": str(template.get("how_to_verify") or f"Verify {rid.replace('_', ' ')}."),
            "evidence_source": str(template.get("evidence_source") or "route_registry"),
            "last_checked_at": str(template.get("last_checked_at") or now),
        }
        output.append(requirement)
    return output


def _hard_requirement_blockers(requirements: Iterable[dict]) -> list[dict]:
    blockers = []
    for requirement in requirements:
        if requirement.get("blocking_level") not in HARD_BLOCKING_LEVELS:
            continue
        if requirement.get("status") not in {"missing", "unknown"}:
            continue
        blockers.append(requirement)
    return blockers


def _derive_route_status(requested_status: str, requirements: list[dict]) -> str:
    if requested_status in {"blocked", "route_unknown"}:
        return requested_status
    blockers = _hard_requirement_blockers(requirements)
    return "conditional" if blockers else "standard"


def _route_next_actions(blockers: list[dict]) -> list[str]:
    actions = []
    seen = set()
    for blocker in blockers:
        action = blocker.get("how_to_verify") or blocker.get("description") or blocker.get("requirement_id")
        action = str(action)
        if action not in seen:
            actions.append(action)
            seen.add(action)
    return actions


def _route_blocker_labels(blockers: list[dict]) -> list[str]:
    return [
        f"{item.get('requirement_id')}: {item.get('description')}"
        for item in blockers
    ]


def _route_probe_priority(candidate: dict, route_status: str, blockers: list[dict]) -> int:
    try:
        score = float(candidate.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    base = {
        "standard": 20,
        "conditional": 70,
        "route_unknown": 80,
        "blocked": 35,
    }.get(route_status, 50)
    priority = base + min(20, int(score / 5)) + min(15, len(blockers) * 3)
    return max(0, min(100, priority))


def _route_unblocker_enabled(settings: dict) -> bool:
    cfg = settings.get("route_unblocker", {})
    return bool(cfg.get("enabled", True) and cfg.get("allow_paper_proxy_routes", True))


def _paper_gate_text(value: object) -> str:
    return str(value or "").strip().lower()


def _paper_gate_route(candidate: dict) -> dict:
    route = candidate.get("route")
    if isinstance(route, dict):
        return route
    return {}


def _paper_gate_direction(candidate: dict, route: dict) -> str:
    return _paper_gate_text(
        candidate.get("direction")
        or route.get("direction")
    )


def _paper_gate_surface(candidate: dict, route: dict) -> str:
    return _paper_gate_text(
        candidate.get("surface")
        or candidate.get("execution_surface")
        or candidate.get("strategy_surface")
        or candidate.get("trade_type")
        or route.get("surface")
        or route.get("execution_surface")
    )


def _paper_gate_missing_requirements(candidate: dict, route: dict) -> list[str]:
    missing: list[str] = []
    for requirement in route.get("requirements") or []:
        if not isinstance(requirement, dict):
            continue
        status = _paper_gate_text(requirement.get("status"))
        if status not in {"missing", "unknown"}:
            continue
        requirement_id = str(requirement.get("requirement_id") or "").strip()
        if requirement_id and requirement_id not in missing:
            missing.append(requirement_id)
    for item in route.get("missing_requirements") or candidate.get("missing_requirements") or []:
        requirement_id = str(item or "").strip()
        if requirement_id and requirement_id not in missing:
            missing.append(requirement_id)
    return missing


def _paper_gate_proxy_alternative(candidate: dict, route: dict) -> dict | None:
    alternatives = route.get("paper_route_alternatives") or candidate.get("paper_route_alternatives") or []
    for alternative in alternatives:
        if not isinstance(alternative, dict):
            continue
        if _paper_gate_text(alternative.get("status")) in {
            "paper_testable_proxy",
            "paper_testable_via_proxy",
        }:
            return dict(alternative)
    return None


def assess_paper_short_route_gate(candidate: dict) -> dict[str, object]:
    """Assess whether a paper candidate can use a direct spot-short route.

    The assessment is read-only and paper-only. It never places orders,
    changes account state, or enables live execution.
    """

    item = dict(candidate or {})
    route = _paper_gate_route(item)
    direction = _paper_gate_direction(item, route)
    surface = _paper_gate_surface(item, route)
    applies = direction == "short" and surface == "spot"
    route_status = str(route.get("route_status") or item.get("route_status") or route.get("status") or "").strip()
    missing_requirements = _paper_gate_missing_requirements(item, route)
    direct_route_id = str(route.get("route_id") or item.get("route_id") or "").strip() or None

    assessment: dict[str, object] = {
        "applies": applies,
        "direction": direction or None,
        "surface": surface or None,
        "route_status": route_status or None,
        "direct_route_id": direct_route_id,
        "missing_requirements": missing_requirements,
        "selected_route_id": direct_route_id,
        "paper_trade_allowed": True,
        "gate_status": "not_applicable",
        "execution_semantics": "direct_live_equivalent",
        "allocation_multiplier": 1.0,
        "suppression_reason": None,
        "proxy_route": None,
    }
    if not applies:
        return assessment

    if route_status == "standard":
        assessment["gate_status"] = "allowed_direct"
        return assessment

    proxy_route = _paper_gate_proxy_alternative(item, route)
    if proxy_route:
        assessment.update(
            {
                "gate_status": "rerouted_to_proxy",
                "selected_route_id": str(proxy_route.get("route_id") or "").strip() or direct_route_id,
                "execution_semantics": str(
                    proxy_route.get("execution_semantics") or "proxy_not_live_equivalent"
                ),
                "allocation_multiplier": float(proxy_route.get("paper_allocation_multiplier") or 1.0),
                "proxy_route": proxy_route,
            }
        )
        return assessment

    assessment.update(
        {
            "gate_status": "suppressed_no_proxy",
            "paper_trade_allowed": False,
            "execution_semantics": "paper_trade_suppressed",
            "suppression_reason": "spot_short_route_requirements_unconfirmed",
        }
    )
    return assessment


def summarize_paper_short_route_gates(candidates: Iterable[dict]) -> dict[str, object]:
    assessments = [assess_paper_short_route_gate(candidate) for candidate in candidates]
    applicable = [item for item in assessments if item.get("applies")]
    status_counts = collections.Counter(str(item.get("gate_status")) for item in applicable)
    execution_semantics_counts = collections.Counter(
        str(item.get("execution_semantics")) for item in applicable
    )
    return {
        "enabled": bool(applicable),
        "paper_only": True,
        "candidate_count": len(applicable),
        "status_counts": dict(status_counts),
        "execution_semantics_counts": dict(execution_semantics_counts),
        "gated_candidates": [item for item in applicable if str(item.get("gate_status")) != "allowed_direct"],
    }


def _eligibility_value(candidate: dict, *keys: str) -> object:
    """Return candidate-supplied route evidence without inferring it from account config."""

    containers = (
        candidate,
        candidate.get("paper_route_requirements"),
        candidate.get("route_requirements"),
    )
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            if key in container and container.get(key) is not None:
                return container.get(key)
    return None


def _venue_capability_metadata(candidate: dict) -> dict:
    """Return explicitly supplied venue route metadata, if any.

    Nested route packets are accepted because frontier adapters attach their
    capability evidence there. Candidate-level metadata wins over older nested
    snapshots.
    """

    observed: dict = {}
    containers = (
        candidate.get("execution_route"),
        candidate.get("execution_feasibility"),
        candidate.get("route_requirements_packet"),
        candidate.get("route_requirements"),
        candidate,
    )
    for container in containers:
        if not isinstance(container, dict):
            continue
        capabilities = container.get("venue_capabilities")
        if isinstance(capabilities, dict):
            observed = _merge_venue_capabilities(observed, capabilities)
        packet = container.get("route_requirements_packet")
        if isinstance(packet, dict) and isinstance(packet.get("venue_capabilities"), dict):
            observed = _merge_venue_capabilities(
                observed,
                packet["venue_capabilities"],
            )
    maintained = _configured_venue_capabilities(candidate)
    return _merge_venue_capabilities(maintained, observed)


def _capability_bool(capabilities: dict, *keys: str) -> bool | None:
    for key in keys:
        if key in capabilities:
            return _eligibility_bool(capabilities.get(key))
    return None


def _route_capability_bool(capabilities: dict, *keys: str) -> bool | None:
    """Resolve a route capability without letting a coarse flag hide a veto.

    Detailed venue metadata takes precedence whenever it is present.  A venue
    may instead publish one explicit route-level feasibility flag, but that
    aggregate flag cannot override a detailed ``False`` value.
    """

    detailed = _capability_bool(capabilities, *keys)
    if detailed is not None:
        return detailed
    return _capability_bool(
        capabilities,
        "paper_route_feasible",
        "route_feasible",
        "explicit_route_feasible",
    )


def _append_route_gap(missing: list[str], reasons: list[str], prerequisite: str, reason: str) -> None:
    if prerequisite not in missing:
        missing.append(prerequisite)
    if reason not in reasons:
        reasons.append(reason)


def _enforce_venue_capability_contract(
    capabilities: dict,
    *,
    spot_short_required: bool,
    hedged_structure_required: bool,
    margin_required: bool,
    transfer_required: bool,
    hedge_mode_required: bool,
    missing: list[str],
    reasons: list[str],
) -> list[dict[str, object]]:
    """Fail closed when present venue metadata does not confirm a route.

    Capability-dependent short and carry candidates fail closed when the
    packet is absent. Once a packet is present, loose candidate flags cannot
    override an unsupported or unknown short, margin, borrow, perp, spot-leg,
    or carry capability.
    """

    checks: list[dict[str, object]] = []
    capability_confirmation_required = any(
        (
            spot_short_required,
            hedged_structure_required,
            margin_required,
            transfer_required,
            hedge_mode_required,
        )
    )
    if not capability_confirmation_required:
        return checks

    if not capabilities:
        _append_route_gap(
            missing,
            reasons,
            "venue_capabilities",
            "venue_capability_metadata_missing",
        )
        expected_capabilities: list[tuple[str, str]] = []
        if spot_short_required:
            expected_capabilities.extend(
                (
                    ("venue_capabilities.spot_short", "supports_spot_short"),
                    ("venue_capabilities.margin", "supports_margin_spot"),
                    ("venue_capabilities.borrow_check", "supports_borrow_check"),
                )
            )
        elif margin_required:
            expected_capabilities.append(
                ("venue_capabilities.margin", "supports_margin_spot")
            )
        if hedged_structure_required:
            expected_capabilities.extend(
                (
                    ("venue_capabilities.basis_path", "supports_basis_path"),
                    ("venue_capabilities.perp", "supports_perpetuals"),
                    ("venue_capabilities.spot", "supports_spot_long"),
                )
            )
        if transfer_required:
            expected_capabilities.append(
                ("venue_capabilities.transfers", "supports_transfers")
            )
        if hedge_mode_required:
            expected_capabilities.append(
                ("venue_capabilities.hedge_mode", "supports_hedge_mode")
            )
        checks.extend(
            {
                "requirement": requirement,
                "capability": capability,
                "state": "unknown",
                "reason": "venue_capability_metadata_missing",
            }
            for requirement, capability in dict.fromkeys(expected_capabilities)
        )
        return checks

    if spot_short_required:
        short_supported = _route_capability_bool(
            capabilities,
            "supports_spot_short",
            "spot_short_supported",
            "supports_spot_short_margin",
            "shortability_indication",
        )
        margin_supported = _route_capability_bool(
            capabilities,
            "supports_margin_spot",
            "margin_supported",
            "margin_available",
            "supports_margin",
        )
        borrow_supported = _route_capability_bool(
            capabilities,
            "supports_borrow_check",
            "borrow_supported",
            "spot_borrow_supported",
            "borrow_inventory_supported",
            "borrow_hint_present",
        )
        for supported, prerequisite, capability, reason in (
            (short_supported, "venue_capabilities.spot_short", "supports_spot_short", "venue_spot_short_capability_unconfirmed"),
            (margin_supported, "venue_capabilities.margin", "supports_margin_spot", "venue_margin_capability_unconfirmed"),
            (borrow_supported, "venue_capabilities.borrow_check", "supports_borrow_check", "venue_borrow_capability_unconfirmed"),
        ):
            state = "supported" if supported is True else "unsupported" if supported is False else "unknown"
            checks.append(
                {
                    "requirement": prerequisite,
                    "capability": capability,
                    "state": state,
                    "reason": None if supported is True else reason,
                }
            )
            if supported is not True:
                _append_route_gap(missing, reasons, prerequisite, reason)

    if hedged_structure_required:
        carry_supported = _route_capability_bool(
            capabilities,
            "supports_basis_path",
            "synthetic_carry_supported",
            "supports_basis_carry",
            "basis_support",
            "carry_supported",
        )
        perp_supported = _route_capability_bool(
            capabilities,
            "perp_supported",
            "supports_perpetuals",
            "perp_available",
        )
        spot_supported = _route_capability_bool(
            capabilities,
            "spot_supported",
            "supports_spot",
            "supports_spot_long",
        )
        for supported, prerequisite, capability, reason in (
            (carry_supported, "venue_capabilities.basis_path", "supports_basis_path", "venue_synthetic_carry_capability_unconfirmed"),
            (perp_supported, "venue_capabilities.perp", "supports_perpetuals", "venue_perp_capability_unconfirmed"),
            (spot_supported, "venue_capabilities.spot", "supports_spot_long", "venue_spot_leg_capability_unconfirmed"),
        ):
            state = "supported" if supported is True else "unsupported" if supported is False else "unknown"
            checks.append(
                {
                    "requirement": prerequisite,
                    "capability": capability,
                    "state": state,
                    "reason": None if supported is True else reason,
                }
            )
            if supported is not True:
                _append_route_gap(missing, reasons, prerequisite, reason)

    extra_checks = (
        (
            margin_required and not spot_short_required,
            ("supports_margin_spot", "margin_supported", "supports_margin"),
            "venue_capabilities.margin",
            "supports_margin_spot",
            "venue_margin_capability_unconfirmed",
        ),
        (
            transfer_required,
            ("supports_transfers", "transfer_supported", "cross_venue_transfer_supported"),
            "venue_capabilities.transfers",
            "supports_transfers",
            "venue_transfer_capability_unconfirmed",
        ),
        (
            hedge_mode_required,
            ("supports_hedge_mode", "hedge_mode_supported", "dual_side_position_supported"),
            "venue_capabilities.hedge_mode",
            "supports_hedge_mode",
            "venue_hedge_mode_capability_unconfirmed",
        ),
    )
    for required, aliases, prerequisite, capability, reason in extra_checks:
        if not required:
            continue
        supported = _route_capability_bool(capabilities, *aliases)
        state = "supported" if supported is True else "unsupported" if supported is False else "unknown"
        checks.append(
            {
                "requirement": prerequisite,
                "capability": capability,
                "state": state,
                "reason": None if supported is True else reason,
            }
        )
        if supported is not True:
            _append_route_gap(missing, reasons, prerequisite, reason)
    return checks


def _eligibility_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {
        "true", "yes", "1", "confirmed", "supported", "available",
        "eligible", "valid", "allowed",
    }:
        return True
    if text in {
        "false", "no", "0", "missing", "unsupported", "unavailable",
        "ineligible", "invalid", "blocked",
    }:
        return False
    return None


def _eligibility_number(candidate: dict, *keys: str) -> float | None:
    value = _eligibility_value(candidate, *keys)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _eligibility_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value)
    return str(value).strip().lower() not in {
        "", "none", "unknown", "missing", "not_checked", "not_applicable",
    }


def _spot_short_dependency(candidate: dict) -> bool:
    direction = _paper_gate_text(
        _eligibility_value(candidate, "direction", "signal_direction", "side")
    )
    surface = " ".join(
        _paper_gate_text(_eligibility_value(candidate, key))
        for key in (
            "surface",
            "market_type",
            "instrument_type",
            "instrument",
            "inst_id",
            "symbol",
            "asset_class",
        )
    )
    route_id = _paper_gate_text(_eligibility_value(candidate, "route_id"))
    borrow_required = _eligibility_bool(
        _eligibility_value(candidate, "borrow_required", "requires_spot_borrow")
    )
    descriptor = " ".join(
        _paper_gate_text(_eligibility_value(candidate, key))
        for key in (
            "direction",
            "strategy",
            "strategy_id",
            "strategy_profile",
            "signal_key",
            "route_type",
            "tags",
            "strategy_tags",
        )
    )
    inventory_held = _eligibility_bool(
        _eligibility_value(
            candidate,
            "spot_inventory_held",
            "inventory_already_held",
            "inventory_available",
        )
    )
    explicit_short_spot = direction in {
        "short_frontier_spot", "long_perp_short_spot", "short_spot",
    }
    plain_spot_short = direction in {"short", "sell"} and (
        "spot" in surface or "spot" in route_id
    )
    tagged_short_spot = (
        "short_frontier_spot" in descriptor
        or ("short" in descriptor and "spot" in descriptor and "conditional" in descriptor)
        or "borrow_required" in descriptor
        or (
            "margin_required" in descriptor
            and (direction in {"short", "sell"} or "short" in descriptor)
            and ("spot" in surface or "spot" in descriptor)
        )
    )
    return inventory_held is not True and (
        explicit_short_spot or plain_spot_short or tagged_short_spot or borrow_required is True
    )


def _hedged_structure_dependency(candidate: dict) -> bool:
    requires_hedge = _eligibility_bool(
        _eligibility_value(candidate, "requires_hedge", "hedge_required")
    )
    if requires_hedge is not None:
        return requires_hedge
    structure = _paper_gate_text(
        _eligibility_value(candidate, "execution_structure", "position_structure", "leg_structure")
    )
    if structure in {"perpetual_spot_pair", "multi_leg", "two_leg", "hedged_pair"}:
        return True
    direction = _paper_gate_text(_eligibility_value(candidate, "direction"))
    if direction in {"short_perp_long_spot", "long_perp_short_spot"}:
        return True
    legs = candidate.get("paper_legs") or candidate.get("legs") or []
    return isinstance(legs, list) and len(legs) >= 2


def _route_dependency_flag(candidate: dict, *keys: str) -> bool:
    return _eligibility_bool(_eligibility_value(candidate, *keys)) is True


def _transfer_dependency(candidate: dict, *, hedged_structure_required: bool) -> bool:
    if _route_dependency_flag(
        candidate,
        "transfer_required",
        "requires_transfer",
        "cross_venue_transfer_required",
    ):
        return True
    descriptor = " ".join(
        _paper_gate_text(_eligibility_value(candidate, key))
        for key in ("route_type", "strategy", "strategy_profile", "signal_key", "tags")
    )
    prefunded = _eligibility_bool(
        _eligibility_value(candidate, "legs_prefunded", "prefunded_inventory")
    )
    return hedged_structure_required and "cross_venue" in descriptor and prefunded is not True


def _hedge_mode_dependency(candidate: dict) -> bool:
    return _route_dependency_flag(
        candidate,
        "hedge_mode_required",
        "requires_hedge_mode",
        "dual_side_position_required",
    )


def _simulation_assumption(candidate: dict, *keys: str) -> object:
    """Return an explicit paper simulation assumption, never an inferred default."""

    value = _eligibility_value(candidate, *keys)
    return value if _eligibility_present(value) else None


def _paper_route_costs(
    candidate: dict,
    *,
    spot_short_required: bool,
) -> tuple[dict[str, float], float]:
    explicit_total = _eligibility_number(
        candidate,
        "route_cost_bps_paper",
        "paper_route_cost_bps",
        "total_route_cost_bps",
        "total_cost_bps",
    )
    if explicit_total is not None:
        costs = {"route_total": max(0.0, explicit_total)}
        return costs, round(sum(costs.values()), 6)

    costs: dict[str, float] = {}
    round_trip = _eligibility_number(
        candidate, "estimated_round_trip_cost_bps", "round_trip_cost_bps"
    )
    if round_trip is not None:
        costs["round_trip"] = max(0.0, round_trip)
        fee = None
        slippage = None
    else:
        fee = _eligibility_number(
            candidate, "total_fee_bps", "estimated_fee_bps", "fee_bps", "route_fee_bps"
        )
        if fee is None:
            per_side_fee = _eligibility_number(
                candidate,
                "fee_bps_per_side",
                "fee_bps_per_side_or_unknown",
                "estimated_fee_bps_per_side",
            )
            fee = None if per_side_fee is None else per_side_fee * 2.0
        slippage = _eligibility_number(
            candidate, "total_slippage_bps", "estimated_slippage_bps", "slippage_bps"
        )
        if slippage is None:
            per_side_slippage = _eligibility_number(
                candidate, "slippage_bps_per_side", "slippage_bps_per_side_or_unknown"
            )
            slippage = None if per_side_slippage is None else per_side_slippage * 2.0
        if slippage is None:
            entry_slippage = _eligibility_number(candidate, "entry_slippage_bps_estimate")
            exit_slippage = _eligibility_number(candidate, "exit_slippage_bps_estimate")
            if entry_slippage is not None or exit_slippage is not None:
                slippage = (entry_slippage or 0.0) + (exit_slippage or 0.0)
    borrow = _eligibility_number(
        candidate,
        "borrow_cost_bps",
        "borrow_fee_bps",
        "borrow_fee_bps_estimate",
        "borrow_fee_bps_estimate_or_unknown",
    )
    if borrow is None:
        borrow_assumption = _eligibility_value(
            candidate,
            "borrow_cost_assumption",
            "borrow_cost_model",
        )
        if isinstance(borrow_assumption, dict):
            for key in ("bps", "cost_bps", "borrow_cost_bps", "value"):
                if key not in borrow_assumption:
                    continue
                value = borrow_assumption.get(key)
                if isinstance(value, bool):
                    continue
                try:
                    borrow = float(value)
                except (TypeError, ValueError):
                    borrow = None
                break
        elif not isinstance(borrow_assumption, bool):
            try:
                borrow = float(borrow_assumption)
            except (TypeError, ValueError):
                borrow = None
    funding_drag = _eligibility_number(
        candidate, "funding_drag_bps", "expected_funding_drag_bps"
    )
    for name, value in (
        ("fees", fee),
        ("slippage", slippage),
        ("funding_drag", funding_drag),
    ):
        if value is not None:
            costs[name] = max(0.0, value)
    if spot_short_required and borrow is not None:
        costs["borrow"] = max(0.0, borrow)
    return costs, round(sum(costs.values()), 6)


def evaluate_route_intelligence(candidate: dict) -> dict[str, object]:
    """Return a paper-only route verdict suitable for pre-review score gating.

    Instrument-level fields and configured venue metadata must explicitly
    establish routeability. Coarse account capabilities and inferred
    alternatives are deliberately not treated as proof of borrowability,
    hedge availability, or modeled costs.
    """

    item = dict(candidate or {})
    requirements = (
        item.get("route_requirements")
        if isinstance(item.get("route_requirements"), dict)
        else {}
    )
    legacy_proxy_allowed = _eligibility_bool(requirements.get("proxy_allowed")) is True
    legacy_proxy_id = requirements.get("paper_proxy_id")
    for container in (item, item.get("execution_feasibility"), item.get("execution_route")):
        if not isinstance(container, dict):
            continue
        alternative = (
            container.get("best_route_alternative")
            or container.get("paper_route_alternative")
            or container.get("route_alternative")
        )
        if not isinstance(alternative, dict):
            continue
        alternative_status = _paper_gate_text(
            alternative.get("status") or alternative.get("route_status")
        )
        alternative_missing = {
            str(value)
            for field in ("missing_permissions", "missing_requirements", "route_blockers")
            for value in (alternative.get(field) or [])
            if value
        }
        replaces = {
            str(value) for value in (alternative.get("replaces_blockers") or []) if value
        }
        if (
            alternative_status in {"paper_testable_proxy", "paper_testable_via_proxy"}
            and not alternative_missing
            and "spot_borrow" in replaces
        ):
            legacy_proxy_allowed = True
            legacy_proxy_id = alternative.get("route_id") or alternative.get("route")
            break
    spot_short_required = _spot_short_dependency(item)
    hedged_structure_required = _hedged_structure_dependency(item)
    margin_required = spot_short_required or _route_dependency_flag(
        item, "margin_required", "requires_margin", "margin_permission_required"
    )
    transfer_required = _transfer_dependency(
        item, hedged_structure_required=hedged_structure_required
    )
    hedge_mode_required = _hedge_mode_dependency(item)
    venue_capabilities = _venue_capability_metadata(item)
    missing: list[str] = []
    reasons: list[str] = []
    simulation_assumptions: dict[str, object] = {}

    if spot_short_required:
        paper_short_allowed = _eligibility_bool(
            _eligibility_value(
                item,
                "paper_short_simulation_allowed",
                "paper_spot_short_simulation_allowed",
                "paper_short_allowed",
                "synthetic_short_allowed",
                "supported_short_simulation",
                "short_simulation_supported",
            )
        )
        if paper_short_allowed is None:
            paper_short_allowed = _capability_bool(
                venue_capabilities,
                "supports_spot_short",
                "paper_short_simulation_allowed",
                "paper_route_feasible",
                "route_feasible",
            )
        borrowable = _eligibility_bool(
            _eligibility_value(
                item,
                "borrowable",
                "borrowable_status",
                "borrowable_confirmed",
                "borrow_supported",
                "spot_borrow_supported",
            )
        )
        borrow_inventory_assumption = _simulation_assumption(
            item,
            "borrow_inventory_assumption",
            "borrow_availability_assumption",
            "simulated_borrow_inventory",
        )
        if borrowable is None and borrow_inventory_assumption is not None:
            simulation_assumptions["borrow_inventory"] = borrow_inventory_assumption
        margin_eligible = _eligibility_bool(
            _eligibility_value(
                item,
                "margin_eligible",
                "margin_eligibility",
                "margin_eligibility_status",
                "margin_supported",
                "venue_supports_margin_or_equivalent",
            )
        )
        if margin_eligible is None:
            margin_eligible = _capability_bool(
                venue_capabilities,
                "supports_margin_spot",
                "margin_supported",
                "margin_available",
                "supports_margin",
            )
        borrow_cost = _eligibility_number(
            item,
            "borrow_cost_bps",
            "borrow_fee_bps",
            "borrow_fee_bps_estimate",
            "borrow_fee_bps_estimate_or_unknown",
        )
        borrow_cost_model = _eligibility_bool(
            _eligibility_value(item, "borrow_cost_model_present", "borrow_fee_modeled")
        )
        if borrow_cost_model is None:
            borrow_cost_model = _capability_bool(
                venue_capabilities,
                "borrow_fee_known",
                "borrow_cost_model_present",
            )
        borrow_cost_assumption = _eligibility_value(
            item, "borrow_cost_assumption", "borrow_cost_model"
        )
        if borrow_cost is None and (
            _eligibility_present(borrow_cost_assumption) or borrow_cost_model is True
        ):
            simulation_assumptions["borrow_cost"] = (
                borrow_cost_assumption
                if _eligibility_present(borrow_cost_assumption)
                else "explicit_borrow_cost_model"
            )
        short_checks = (
            (
                paper_short_allowed is True,
                "paper_short_simulation_allowed",
                "paper_short_simulation_permission_missing",
            ),
            (
                borrowable is True
                or (borrowable is None and borrow_inventory_assumption is not None),
                "borrowable",
                "spot_borrow_missing",
            ),
            (
                borrow_cost is not None
                or borrow_cost_model is True
                or _eligibility_present(borrow_cost_assumption),
                "borrow_cost_assumption",
                "borrow_cost_assumption_missing",
            ),
            (
                margin_eligible is True,
                "margin_eligible",
                "margin_eligibility_unconfirmed",
            ),
        )
        for satisfied, prerequisite, reason in short_checks:
            if not satisfied:
                missing.append(prerequisite)
                reasons.append(reason)

    if hedged_structure_required:
        hedge_venue = _eligibility_value(item, "hedge_venue", "route_hedge_venue")
        hedge_instrument = _eligibility_value(
            item,
            "hedge_instrument",
            "hedge_instrument_id",
            "hedge_symbol",
            "route_hedge_instrument_id",
            "route_hedge_symbol",
        )
        fee_model = _eligibility_value(
            item, "fee_model", "fee_model_status", "fee_assumptions"
        )
        fees_modeled = _eligibility_bool(
            _eligibility_value(item, "fees_modeled", "fee_model_available")
        )
        numeric_fee = _eligibility_number(
            item,
            "total_fee_bps",
            "estimated_fee_bps",
            "fee_bps",
            "route_fee_bps",
            "estimated_round_trip_cost_bps",
            "round_trip_cost_bps",
            "total_cost_bps",
        )
        leg_mapping_value = _eligibility_value(
            item,
            "paper_leg_mapping_valid",
            "leg_mapping_paper_valid",
            "paper_valid_leg_mapping",
            "paper_leg_mapping",
            "leg_mapping",
        )
        if isinstance(leg_mapping_value, dict):
            leg_mapping = _eligibility_bool(
                leg_mapping_value.get("paper_valid", leg_mapping_value.get("valid"))
            )
        else:
            leg_mapping = _eligibility_bool(leg_mapping_value)
        hedge_checks = (
            (_eligibility_present(hedge_venue), "hedge_venue", "hedge_venue_missing"),
            (
                _eligibility_present(hedge_instrument),
                "hedge_instrument",
                "hedge_instrument_missing",
            ),
            (
                _eligibility_present(fee_model)
                or fees_modeled is True
                or numeric_fee is not None,
                "fee_model",
                "fee_model_missing",
            ),
            (
                leg_mapping is True,
                "paper_leg_mapping_valid",
                "paper_leg_mapping_missing_or_invalid",
            ),
        )
        for satisfied, prerequisite, reason in hedge_checks:
            if not satisfied:
                missing.append(prerequisite)
                reasons.append(reason)

    capability_checks = _enforce_venue_capability_contract(
        venue_capabilities,
        spot_short_required=spot_short_required,
        hedged_structure_required=hedged_structure_required,
        margin_required=margin_required,
        transfer_required=transfer_required,
        hedge_mode_required=hedge_mode_required,
        missing=missing,
        reasons=reasons,
    )

    cost_breakdown, assumed_cost_bps = _paper_route_costs(
        item, spot_short_required=spot_short_required
    )
    expected_edge_bps = _eligibility_number(
        item,
        "expected_edge_bps",
        "edge_bps_estimate",
        "net_carry_edge_bps",
        "depth_adjusted_edge_bps",
    )
    if (
        expected_edge_bps is not None
        and cost_breakdown
        and expected_edge_bps < assumed_cost_bps
    ):
        missing.append("positive_edge_after_route_costs")
        reasons.append("expected_edge_below_route_costs")

    # An explicitly mapped paper proxy replaces the spot sale. This preserves
    # the older route-intelligence contract without treating inferred proxies
    # as proof that a direct short route exists.
    if (
        spot_short_required
        and not hedged_structure_required
        and legacy_proxy_allowed
        and _eligibility_present(legacy_proxy_id)
    ):
        direct_short_reasons = {
            "paper_short_simulation_permission_missing",
            "spot_borrow_missing",
            "borrow_cost_assumption_missing",
            "margin_eligibility_unconfirmed",
            "venue_spot_short_capability_unconfirmed",
            "venue_margin_capability_unconfirmed",
            "venue_borrow_capability_unconfirmed",
        }
        direct_short_prerequisites = {
            "paper_short_simulation_allowed",
            "borrowable",
            "borrow_cost_assumption",
            "margin_eligible",
            "venue_capabilities.spot_short",
            "venue_capabilities.margin",
            "venue_capabilities.borrow_check",
        }
        missing = [value for value in missing if value not in direct_short_prerequisites]
        reasons = [value for value in reasons if value not in direct_short_reasons]
        for check in capability_checks:
            if check.get("capability") in {
                "supports_spot_short",
                "supports_margin_spot",
                "supports_borrow_check",
            }:
                check["critical"] = False
                check["replaced_by_proxy"] = str(legacy_proxy_id)
        decision = "blocked_hard" if reasons else "executable_proxy"
        proxy_used = True
    else:
        decision = "blocked_hard" if reasons else "executable_standard"
        proxy_used = False

    suppressed = bool(reasons)
    assumption_penalty_applied = bool(simulation_assumptions) and not suppressed
    paper_score_multiplier = (
        0.0
        if suppressed
        else PAPER_ROUTE_ASSUMPTION_SCORE_MULTIPLIER
        if assumption_penalty_applied
        else 1.0
    )
    feasibility_status = (
        "infeasible_for_paper"
        if suppressed
        else "feasible_with_simulation_assumptions"
        if assumption_penalty_applied
        else "feasible_for_paper"
    )
    applies = bool(
        spot_short_required
        or hedged_structure_required
        or margin_required
        or transfer_required
        or hedge_mode_required
        or (expected_edge_bps is not None and cost_breakdown)
    )
    capability_states = {
        str(check.get("state") or "unknown")
        for check in capability_checks
        if check.get("critical", True)
    }
    if "unsupported" in capability_states:
        route_intelligence_status = "unsupported"
        candidate_status = "quarantined_route_unavailable"
    elif reasons or "unknown" in capability_states:
        route_intelligence_status = "unknown"
        candidate_status = "route_needs_confirmation"
    elif applies:
        route_intelligence_status = "supported"
        candidate_status = "route_supported"
    else:
        route_intelligence_status = "not_applicable"
        candidate_status = str(item.get("candidate_status") or "route_not_required")
    blocking_reasons = list(dict.fromkeys(reasons))
    unsupported_reasons = [
        str(check.get("reason"))
        for check in capability_checks
        if check.get("state") == "unsupported" and check.get("reason")
    ]
    blocking_reason = (
        unsupported_reasons[0]
        if unsupported_reasons
        else blocking_reasons[0]
        if blocking_reasons
        else None
    )
    if route_intelligence_status == "unsupported":
        paper_route_notes = [
            "Critical route capability is explicitly unsupported; candidate is quarantined from promotion.",
        ]
    elif route_intelligence_status == "unknown":
        paper_route_notes = [
            "Critical route evidence is unknown; keep the candidate paper-only and confirmation-gated.",
        ]
    elif route_intelligence_status == "supported":
        paper_route_notes = [
            "All inferred critical route capabilities are explicitly supported for paper scoring.",
        ]
    else:
        paper_route_notes = ["No route-dependent capability gate applies."]
    paper_route_notes.extend(
        f"{check['capability']}={check['state']}"
        for check in capability_checks
        if check.get("capability")
    )
    rank_contribution_cap = (
        PAPER_ROUTE_UNKNOWN_RANK_CAP
        if route_intelligence_status == "unknown"
        else 0.0
        if route_intelligence_status == "unsupported"
        else 1.0
    )
    raw_score = _eligibility_number(item, "score")
    rank_contribution = (
        None
        if raw_score is None
        else round(max(0.0, raw_score) * rank_contribution_cap, 6)
    )
    return {
        "paper_only": True,
        "applies": applies,
        "spot_short_required": spot_short_required,
        "hedged_structure_required": hedged_structure_required,
        "margin_required": margin_required,
        "transfer_required": transfer_required,
        "hedge_mode_required": hedge_mode_required,
        "venue_capability_metadata_present": bool(venue_capabilities),
        "venue_capabilities": venue_capabilities,
        "route_decision": decision,
        "feasibility_status": feasibility_status,
        "execution_eligibility": "blocked" if suppressed else "eligible",
        "route_eligible": not suppressed,
        "eligible_for_scoring": not suppressed,
        "suppressed": suppressed,
        "proxy_used": proxy_used,
        "selected_proxy_id": legacy_proxy_id if proxy_used else None,
        "missing_prerequisites": list(dict.fromkeys(missing)),
        "blocker_reasons": list(dict.fromkeys(reasons)),
        "blocking_reason": blocking_reason,
        "capability_checks": capability_checks,
        "required_capabilities": [
            str(check["capability"])
            for check in capability_checks
            if check.get("capability") and check.get("critical", True)
        ],
        "simulation_assumptions": simulation_assumptions,
        "route_status": route_intelligence_status,
        "candidate_status": candidate_status,
        "paper_route_notes": paper_route_notes,
        "rank_contribution_cap": rank_contribution_cap,
        "rank_contribution": rank_contribution,
        "assumption_penalty_applied": assumption_penalty_applied,
        "paper_score_multiplier": paper_score_multiplier,
        "expected_edge_bps": expected_edge_bps,
        "assumed_route_cost_bps": assumed_cost_bps,
        "cost_breakdown_bps": cost_breakdown,
    }


def _paper_route_alternatives(
    candidate: dict,
    missing_permissions: list[str],
    caps: dict,
    settings: dict,
    *,
    direct_route_id: str,
) -> list[dict]:
    if not _route_unblocker_enabled(settings):
        return []
    missing = set(missing_permissions or [])
    cfg = settings.get("route_unblocker", {})
    alternatives: list[dict] = []
    if "spot_borrow" in missing:
        derivatives_available = bool(caps.get("crypto_derivatives", False))
        alternatives.append(
            {
                "alternative_id": "crypto_perp_proxy_for_spot_borrow",
                "status": "paper_testable_proxy" if derivatives_available else "unavailable",
                "route_id": "okx_derivatives_paper"
                if candidate.get("venue") == "OKX"
                else "frontier_crypto_perp_proxy_paper",
                "direct_route_id": direct_route_id,
                "replaces_blockers": ["spot_borrow"],
                "required_permissions": ["crypto_derivatives"],
                "missing_permissions": [] if derivatives_available else ["crypto_derivatives"],
                "paper_allocation_multiplier": float(cfg.get("spot_borrow_proxy_allocation_multiplier", 0.25)),
                "execution_semantics": "proxy_not_live_equivalent",
                "notes": [
                    "Direct short-spot route still requires borrow or margin confirmation.",
                    "Paper proxy uses derivatives exposure to keep testing the edge direction where a perp route is available.",
                    "Do not treat proxy paper results as proof that the direct short-spot route is executable.",
                ],
            }
        )
    prediction_blockers = {"prediction_markets_account", "venue_api_access", "jurisdiction_eligibility"}
    if missing & prediction_blockers:
        alternatives.append(
            {
                "alternative_id": "prediction_public_probability_research",
                "status": "paper_testable_research",
                "route_id": "prediction_market_public_research_paper",
                "direct_route_id": direct_route_id,
                "replaces_blockers": sorted(missing & prediction_blockers),
                "required_permissions": [],
                "missing_permissions": [],
                "paper_allocation_multiplier": float(cfg.get("prediction_market_research_allocation_multiplier", 0.10)),
                "execution_semantics": "research_only_not_live_equivalent",
                "notes": [
                    "Public prediction-market prices can be paper-tracked for signal value.",
                    "Account, API, and jurisdiction requirements remain hard blockers for any real execution route.",
                    "No credentials, account changes, jurisdiction assumptions, or order APIs are enabled.",
                ],
            }
        )
    if "venue_api_access" in missing and direct_route_id == "frontier_crypto_blocked_public_data":
        alternatives.append(
            {
                "alternative_id": "public_data_route_probe",
                "status": "research_only",
                "route_id": "route_probe_only",
                "direct_route_id": direct_route_id,
                "replaces_blockers": ["venue_api_access"],
                "required_permissions": [],
                "missing_permissions": ["reachable_public_market_data"],
                "paper_allocation_multiplier": 0.0,
                "execution_semantics": "no_price_no_paper_trade",
                "notes": [
                    "No paper entry is allowed until public market data is reachable.",
                    "Keep this as a route probe and venue-health target.",
                ],
            }
        )
    return alternatives


def _best_route_alternative(alternatives: list[dict]) -> dict | None:
    priority = {
        "paper_testable_proxy": 0,
        "paper_testable_research": 1,
        "research_only": 2,
        "unavailable": 3,
    }
    usable = sorted(alternatives or [], key=lambda item: priority.get(str(item.get("status")), 99))
    return usable[0] if usable else None


def _compact_missing(requirements: list[dict]) -> list[str]:
    return [str(item["requirement_id"]) for item in _hard_requirement_blockers(requirements)]


def _requirement_status(requirements: list[dict], requirement_id: str) -> str:
    for item in requirements:
        if item.get("requirement_id") == requirement_id:
            return str(item.get("status"))
    return "not_applicable"


def _base_route(
    *,
    route_id: str,
    route_status: str,
    candidate: dict,
    required_permissions: list[str],
    missing_permissions: list[str],
    route_notes: list[str],
    confidence: float,
    registry: dict,
    borrow_required: bool = False,
    margin_required: bool = False,
    api_access_status: str = "not_checked",
    fee_model_status: str = "estimated",
    market_hours_status: str = "not_checked",
    jurisdiction_notes: list[str] | None = None,
    requirement_status_overrides: dict[str, str] | None = None,
    route_alternatives: list[dict] | None = None,
) -> dict:
    route_meta = _route_lookup(registry).get(route_id, {})
    requirements = _build_requirements(
        route_meta,
        required_permissions=required_permissions,
        missing_permissions=missing_permissions,
        status_overrides=requirement_status_overrides,
    )
    resolved_status = _derive_route_status(route_status, requirements)
    blockers = _hard_requirement_blockers(requirements)
    compact_missing = _compact_missing(requirements)
    venue = candidate.get("venue", "unknown")
    asset_class = candidate.get("asset_class") or route_meta.get("asset_class") or "unknown"
    instrument_type = candidate.get("trade_type", "unknown")
    if borrow_required:
        borrow_status = "configured" if _requirement_status(requirements, "spot_borrow") == "confirmed" else "required_unconfirmed"
    else:
        borrow_status = "not_required"
    alternatives = route_alternatives or []
    best_alternative = _best_route_alternative(alternatives)
    return {
        "route_id": route_id,
        "route_status": resolved_status,
        "asset_class": asset_class,
        "venue": venue,
        "instrument_type": instrument_type,
        "direction": candidate.get("direction", "unknown"),
        "required_permissions": required_permissions,
        "missing_permissions": compact_missing,
        "requirements": requirements,
        "route_next_actions": _route_next_actions(blockers),
        "route_blockers": _route_blocker_labels(blockers),
        "route_alternatives": alternatives,
        "best_route_alternative": best_alternative,
        "route_probe_priority": _route_probe_priority(candidate, resolved_status, blockers),
        "borrow_required": bool(borrow_required),
        "borrow_status": borrow_status,
        "margin_required": bool(margin_required),
        "fee_model_status": fee_model_status,
        "api_access_status": api_access_status,
        "market_hours_status": market_hours_status,
        "jurisdiction_notes": jurisdiction_notes or route_meta.get("jurisdiction_notes", []),
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "source": "local_account_capability_config",
        "last_checked_at": _utc_now(),
        "route_notes": [*route_notes, *([route_meta.get("notes")] if route_meta.get("notes") else [])],
    }


def _paper_route_sensitivity_reasons(candidate: dict, route: dict) -> list[str]:
    """Return the paper policy scopes that make a conditional route sensitive."""
    descriptor = " ".join(
        str(value or "")
        for value in (
            candidate.get("direction"),
            candidate.get("trade_type"),
            candidate.get("strategy"),
            candidate.get("strategy_profile"),
            candidate.get("signal_key"),
            candidate.get("market_key"),
            candidate.get("market_surface"),
            route.get("route_id"),
            route.get("instrument_type"),
        )
    ).lower().replace("-", "_")
    requirements = {
        str(item or "").strip().lower().replace("-", "_")
        for item in (
            list(route.get("required_permissions") or [])
            + list(route.get("missing_permissions") or [])
        )
        if str(item or "").strip()
    }
    reasons: list[str] = []
    if (
        "short_frontier_spot" in descriptor
        or "long_perp_short_spot" in descriptor
        or "short_spot" in descriptor
        or ("short" in descriptor and "spot" in descriptor)
    ):
        reasons.append("short_spot")
    if route.get("borrow_required") or any("borrow" in item for item in requirements):
        reasons.append("borrow_dependency")
    if (
        ("cross_venue" in descriptor or "cross venue" in descriptor)
        and ("basis" in descriptor or "multi_leg" in descriptor or "multi leg" in descriptor)
    ):
        reasons.append("cross_venue_basis")
    prerequisite_tokens = ("api", "margin", "permission", "venue_access", "jurisdiction")
    if route.get("margin_required") or any(
        any(token in item for token in prerequisite_tokens) for item in requirements
    ):
        reasons.append("venue_api_or_margin_prerequisite")
    return list(dict.fromkeys(reasons))


def _paper_route_feasibility_score(candidate: dict, route: dict, eligibility: dict) -> float:
    """Produce a bounded, auditable score from the resolved paper route."""
    explicit = _eligibility_number(
        candidate,
        "route_feasibility_score",
        "paper_route_feasibility_score",
    )
    if explicit is not None and 0.0 <= explicit <= 1.0:
        return round(explicit, 3)
    score = max(0.0, min(1.0, float(route.get("confidence") or 0.0)))
    status = str(route.get("route_status") or "").strip().lower()
    if eligibility.get("suppressed") or status == "blocked":
        score = 0.0
    elif eligibility.get("assumption_penalty_applied"):
        score = min(score, 0.5)
    elif route.get("missing_permissions") or status in {"route_unknown", "unknown"}:
        score = min(score, 0.5)
    return round(score, 3)


def resolve_candidate_route(candidate: dict, settings: dict, registry: dict | None = None) -> dict:
    registry = registry or load_route_registry()
    caps = settings.get("account_capabilities", {})
    venue = candidate.get("venue", "unknown")
    direction = candidate.get("direction", "unknown")
    trade_type = candidate.get("trade_type", "unknown")

    if trade_type == "frontier_crypto_venue_map":
        data_status = candidate.get("data_status", "unknown")
        market_type = "perp" if candidate.get("asset_class") == "crypto_derivatives" else "spot"
        if data_status != "reachable":
            return _base_route(
                route_id="frontier_crypto_blocked_public_data",
                route_status="blocked",
                candidate=candidate,
                required_permissions=["venue_api_access"],
                missing_permissions=["venue_api_access"],
                route_notes=[
                    f"Public data status is {data_status}; keep this as route-probe evidence only.",
                    "No paper execution is allowed without a reliable public or approved read-only data route.",
                ],
                confidence=0.9,
                registry=registry,
                api_access_status=data_status,
                fee_model_status="not_applicable",
                market_hours_status="24_7_unconfirmed",
            )
        if direction == "watch_only":
            return _base_route(
                route_id="watch_only",
                route_status="blocked",
                candidate=candidate,
                required_permissions=[],
                missing_permissions=[],
                route_notes=["Frontier crypto adapter saw the venue, but no actionable dislocation was present."],
                confidence=0.95,
                registry=registry,
                api_access_status="public_data_only",
                fee_model_status="not_applicable",
                market_hours_status="24_7",
            )
        if market_type == "spot" and direction == "long_frontier_spot":
            required = ["crypto_spot"]
            missing = [] if caps.get("crypto_spot", False) else ["crypto_spot"]
            return _base_route(
                route_id="frontier_crypto_spot_paper",
                route_status=_legacy_status(not missing, missing=missing),
                candidate=candidate,
                required_permissions=required,
                missing_permissions=missing,
                route_notes=["Reachable public spot venue data can be paper-tested long-only."],
                confidence=0.72 if not missing else 0.45,
                registry=registry,
                api_access_status="public_data_only",
                market_hours_status="24_7",
                requirement_status_overrides={"public_data_reachable": "confirmed"},
            )
        if market_type == "spot" and direction == "short_frontier_spot":
            required = ["crypto_spot", "spot_borrow"]
            missing = []
            if not caps.get("crypto_spot", False):
                missing.append("crypto_spot")
            if not caps.get("spot_borrow", False):
                missing.append("spot_borrow")
            direct_route_id = "conditional_crypto_route_paper"
            return _base_route(
                route_id=direct_route_id,
                route_status=_legacy_status(not missing, missing=missing),
                candidate=candidate,
                required_permissions=required,
                missing_permissions=missing,
                borrow_required=True,
                margin_required=True,
                route_notes=["Shorting a rich spot venue requires confirmed borrow, margin, or an equivalent hedge route."],
                confidence=0.66 if not missing else 0.42,
                registry=registry,
                api_access_status="public_data_only",
                market_hours_status="24_7",
                requirement_status_overrides={"crypto_derivatives": "not_applicable"},
                route_alternatives=_paper_route_alternatives(
                    candidate,
                    missing,
                    caps,
                    settings,
                    direct_route_id=direct_route_id,
                ),
            )
        if market_type == "perp" and direction in {"long_frontier_perp", "short_frontier_perp"}:
            required = ["crypto_derivatives"]
            missing = [] if caps.get("crypto_derivatives", False) else ["crypto_derivatives"]
            return _base_route(
                route_id="frontier_crypto_perp_paper",
                route_status=_legacy_status(not missing, missing=missing),
                candidate=candidate,
                required_permissions=required,
                missing_permissions=missing,
                margin_required=True,
                route_notes=["Reachable public perpetual venue data can be paper-tested through the derivatives paper route."],
                confidence=0.72 if not missing else 0.45,
                registry=registry,
                api_access_status="public_data_only",
                market_hours_status="24_7",
                requirement_status_overrides={"public_data_reachable": "confirmed"},
            )

    if direction == "watch_only" or trade_type == "scanner_error":
        return _base_route(
            route_id="watch_only",
            route_status="blocked",
            candidate=candidate,
            required_permissions=[],
            missing_permissions=[],
            route_notes=["Scanner did not produce an executable direction."],
            confidence=0.95,
            registry=registry,
            api_access_status="not_applicable",
            fee_model_status="not_applicable",
            market_hours_status="not_applicable",
        )

    if venue in {"OKX", "WHITEBIT", "DERIBIT"} and trade_type == "perp_funding_basis":
        needs_long_spot = direction == "short_perp_long_spot"
        needs_short_spot = direction == "long_perp_short_spot"
        required = ["crypto_derivatives"]
        missing = [] if caps.get("crypto_derivatives", False) else ["crypto_derivatives"]
        route_id = "okx_derivatives_paper" if venue == "OKX" else "public_crypto_derivatives_paper"
        borrow_required = False
        margin_required = True
        notes = [f"{venue} public perpetual data can be paper-tested through the derivatives route."]
        if needs_long_spot:
            required.append("crypto_spot")
            if not caps.get("crypto_spot", False):
                missing.append("crypto_spot")
            notes.append("Cash-and-carry requires a long spot hedge in addition to the perpetual leg.")
        elif needs_short_spot:
            route_id = "conditional_crypto_route_paper"
            borrow_required = True
            required.extend(["crypto_spot", "spot_borrow"])
            if not caps.get("crypto_spot", False):
                missing.append("crypto_spot")
            if not caps.get("spot_borrow", False):
                missing.append("spot_borrow")
            notes.append("Reverse hedge requires confirmed spot borrow or an equivalent margin route.")
        return _base_route(
            route_id=route_id,
            route_status=_legacy_status(not missing, missing=missing),
            candidate=candidate,
            required_permissions=required,
            missing_permissions=missing,
            borrow_required=borrow_required,
            margin_required=margin_required,
            route_notes=notes,
            confidence=0.9 if not missing else 0.68,
            registry=registry,
            api_access_status="public_data_only",
            market_hours_status="24_7",
            route_alternatives=_paper_route_alternatives(
                candidate,
                missing,
                caps,
                settings,
                direct_route_id=route_id,
            ),
        )

    if venue == "YAHOO_PROXY" or trade_type in {
        "global_proxy_momentum",
        "global_market_discovery_proxy",
        "global_proxy_shock_reversal",
    }:
        route_note_prefix = (
            "Global discovery proxy exposure uses a public/proxy instrument."
            if trade_type == "global_market_discovery_proxy"
            else "Long US-listed ETF/ADR proxy exposure needs an equity route."
        )
        if direction == "long_proxy":
            required = ["equity_long"]
            missing = [] if caps.get("equity_long", True) else ["equity_long"]
            return _base_route(
                route_id="equity_proxy_paper",
                route_status=_legacy_status(not missing, missing=missing),
                candidate=candidate,
                required_permissions=required,
                missing_permissions=missing,
                route_notes=[route_note_prefix],
                confidence=0.78 if not missing else 0.55,
                registry=registry,
                api_access_status="public_data_only",
                market_hours_status="exchange_hours_unconfirmed",
            )
        if direction == "short_proxy":
            required = ["equity_short_or_options"]
            allowed = bool(caps.get("equity_short", False) or caps.get("options", False))
            missing = [] if allowed else ["equity_short", "options_or_inverse_product"]
            overrides = {}
            if caps.get("equity_short", False):
                overrides["equity_short"] = "confirmed"
                overrides["options_or_inverse_product"] = "not_applicable"
            elif caps.get("options", False):
                overrides["equity_short"] = "not_applicable"
                overrides["options_or_inverse_product"] = "confirmed"
            return _base_route(
                route_id="conditional_equity_route_paper",
                route_status=_legacy_status(allowed, missing=missing),
                candidate=candidate,
                required_permissions=required,
                missing_permissions=missing,
                borrow_required=not caps.get("options", False),
                margin_required=True,
                route_notes=["Short proxy exposure needs borrow, margin, options, or inverse-product access."],
                confidence=0.7 if allowed else 0.45,
                registry=registry,
                api_access_status="public_data_only",
                market_hours_status="exchange_hours_unconfirmed",
                requirement_status_overrides=overrides,
            )

    if venue in {"KALSHI", "POLYMARKET"}:
        venue_key = "kalshi_events" if venue == "KALSHI" else "polymarket_events"
        feasibility = candidate.get("execution_feasibility") or {}
        prediction_market_research_only = bool(
            candidate.get("paper_only")
            or candidate.get("read_only")
            or candidate.get("execution_disabled")
            or candidate.get("order_routing_disabled")
            or feasibility.get("public_data_only")
            or feasibility.get("live_execution_supported") is False
        )
        # Scanner-created public prediction-market rows are deliberately not
        # promotable to a venue route. Account configuration must never turn
        # anonymous ingestion into order-routing authorization.
        allowed = bool(caps.get("prediction_markets", False)) and not prediction_market_research_only
        missing = [] if allowed else ["prediction_markets_account", "venue_api_access", "jurisdiction_eligibility"]
        overrides = {}
        if allowed:
            overrides = {
                "prediction_markets_account": "confirmed",
                "venue_api_access": "confirmed",
                "jurisdiction_eligibility": "confirmed",
            }
        return _base_route(
            route_id=venue_key,
            route_status=_legacy_status(allowed, missing=missing),
            candidate=candidate,
            required_permissions=["prediction_markets"],
            missing_permissions=missing,
            route_notes=[
                "Event-contract execution requires account, jurisdiction, contract eligibility, and API checks.",
                *(
                    [
                        f"This {venue.title()} candidate came from an anonymous public-data adapter; direct execution and order routing are disabled.",
                        "Only the prediction-market public research paper alternative may be used.",
                    ]
                    if prediction_market_research_only
                    else []
                ),
            ],
            confidence=0.72 if allowed else 0.42,
            registry=registry,
            api_access_status="public_data_only",
            market_hours_status="venue_hours_unconfirmed",
            jurisdiction_notes=["Check user eligibility and venue terms before any live route."],
            requirement_status_overrides=overrides,
            route_alternatives=_paper_route_alternatives(
                candidate,
                missing,
                caps,
                settings,
                direct_route_id=venue_key,
            ),
        )

    return _base_route(
        route_id="local_or_specialist_broker",
        route_status="route_unknown",
        candidate=candidate,
        required_permissions=["specialist_route_research"],
        missing_permissions=["broker_or_venue", "permissions", "fees", "market_hours", "api_or_manual_workflow"],
        route_notes=["No configured route matched this candidate; specialist/local route research is required."],
        confidence=0.25,
        registry=registry,
        api_access_status="unknown",
        fee_model_status="unknown",
        market_hours_status="unknown",
    )


def enrich_candidate_with_route(
    candidate: dict,
    settings: dict,
    registry: dict | None = None,
    venue_capability_registry: dict[str, dict] | None = None,
) -> dict:
    enriched = dict(candidate)
    configured_capabilities = _configured_venue_capabilities(
        enriched,
        capability_registry=venue_capability_registry,
    )
    supplied_capabilities = enriched.get("venue_capabilities")
    if configured_capabilities:
        merged_capabilities = _merge_venue_capabilities(
            configured_capabilities,
            supplied_capabilities if isinstance(supplied_capabilities, dict) else None,
        )
        enriched["venue_capabilities"] = merged_capabilities
        enriched["venue_capability_source"] = (
            "candidate_and_paper_route_registry"
            if isinstance(supplied_capabilities, dict)
            else "paper_route_intelligence.crypto_venues"
        )
    enriched = apply_paper_route_registry(enriched, settings)
    route = resolve_candidate_route(enriched, settings, registry=registry)
    eligibility_input = dict(enriched)
    eligibility_input.setdefault("route_id", route.get("route_id"))
    eligibility = evaluate_route_intelligence(eligibility_input)
    route_sensitivity_reasons = _paper_route_sensitivity_reasons(enriched, route)
    route_sensitive = bool(route_sensitivity_reasons)
    route_feasibility_score = _paper_route_feasibility_score(enriched, route, eligibility)
    diagnostic_input = dict(enriched)
    diagnostic_input.update(
        {
            "route_status": route.get("route_status"),
            "borrow_status": route.get("borrow_status"),
            "margin_required": route.get("margin_required"),
            "api_access_status": route.get("api_access_status"),
            "route_blockers": route.get("route_blockers", []),
        }
    )
    conditional_short_diagnostics = build_conditional_short_route_diagnostics(diagnostic_input)
    route["route_sensitive"] = route_sensitive
    route["route_sensitivity_reasons"] = route_sensitivity_reasons
    route["route_feasibility_score"] = route_feasibility_score
    route["conditional_short_route_diagnostics"] = conditional_short_diagnostics
    route["paper_route_eligibility"] = eligibility
    route["eligibility_missing_prerequisites"] = eligibility["missing_prerequisites"]
    route["paper_route_registry"] = enriched["paper_route_registry"]
    route["registry_required_permissions"] = enriched["paper_route_required_permissions"]
    route["registry_required_account_modes"] = enriched["paper_route_required_account_modes"]
    route["registry_estimated_cost_bps"] = enriched["paper_route_estimated_cost_bps"]
    existing = dict(enriched.get("execution_feasibility") or {})
    status = route["route_status"]
    if status == "route_unknown":
        feasibility_status = "route_unknown"
    elif status == "blocked":
        feasibility_status = "watch_only" if enriched.get("direction") == "watch_only" else "blocked"
    else:
        feasibility_status = status
    existing.update(
        {
            "status": feasibility_status,
            "route_status": route["route_status"],
            "route_id": route["route_id"],
            "missing_requirements": route["missing_permissions"],
            "required_permissions": route["required_permissions"],
            "requirements": route["requirements"],
            "route_next_actions": route["route_next_actions"],
            "route_blockers": route["route_blockers"],
            "route_alternatives": route.get("route_alternatives", []),
            "best_route_alternative": route.get("best_route_alternative"),
            "route_probe_priority": route["route_probe_priority"],
            "route_confidence": route["confidence"],
            "route_sensitive": route_sensitive,
            "route_sensitivity_reasons": route_sensitivity_reasons,
            "route_feasibility_score": route_feasibility_score,
            "route_notes": route["route_notes"],
            "borrow_required": route["borrow_required"],
            "borrow_status": route["borrow_status"],
            "margin_required": route["margin_required"],
            "api_access_status": route["api_access_status"],
            "fee_model_status": route["fee_model_status"],
            "market_hours_status": route["market_hours_status"],
            "paper_route_eligibility": eligibility,
            "paper_route_registry": enriched["paper_route_registry"],
            "registry_required_permissions": enriched["paper_route_required_permissions"],
            "registry_required_account_modes": enriched["paper_route_required_account_modes"],
            "registry_estimated_cost_bps": enriched["paper_route_estimated_cost_bps"],
            "eligibility_missing_prerequisites": eligibility["missing_prerequisites"],
            "paper_feasibility_status": eligibility["feasibility_status"],
            "execution_eligibility": eligibility["execution_eligibility"],
            "paper_score_multiplier": eligibility["paper_score_multiplier"],
            "route_intelligence_status": eligibility["route_status"],
            "candidate_status": eligibility["candidate_status"],
            "required_capabilities": eligibility["required_capabilities"],
            "capability_checks": eligibility["capability_checks"],
            "blocking_reason": eligibility["blocking_reason"],
            "paper_route_notes": eligibility["paper_route_notes"],
            "rank_contribution_cap": eligibility["rank_contribution_cap"],
            "rank_contribution": eligibility["rank_contribution"],
            "conditional_short_route_diagnostics": conditional_short_diagnostics,
        }
    )
    enriched["execution_feasibility"] = existing
    enriched["execution_route"] = route
    enriched["route_id"] = route["route_id"]
    enriched["route_status"] = route["route_status"]
    enriched["route_sensitive"] = route_sensitive
    enriched["route_sensitivity_reasons"] = route_sensitivity_reasons
    enriched["route_feasibility_score"] = route_feasibility_score
    enriched["paper_route_eligibility"] = eligibility
    enriched["paper_feasibility_status"] = eligibility["feasibility_status"]
    enriched["execution_eligibility"] = eligibility["execution_eligibility"]
    enriched["paper_route_score_multiplier"] = eligibility["paper_score_multiplier"]
    enriched["route_intelligence_status"] = eligibility["route_status"]
    enriched["candidate_status"] = eligibility["candidate_status"]
    enriched["required_capabilities"] = eligibility["required_capabilities"]
    enriched["route_capability_checks"] = eligibility["capability_checks"]
    enriched["blocking_reason"] = eligibility["blocking_reason"]
    enriched["paper_route_notes"] = eligibility["paper_route_notes"]
    enriched["rank_contribution_cap"] = eligibility["rank_contribution_cap"]
    enriched["rank_contribution"] = eligibility["rank_contribution"]
    enriched["conditional_short_route_diagnostics"] = conditional_short_diagnostics
    if eligibility["suppressed"]:
        if "score" in enriched:
            enriched.setdefault(
                "pre_route_eligibility_score",
                enriched.get("pre_paper_route_registry_score", enriched["score"]),
            )
            enriched["score"] = 0.0
        enriched["paper_entry_blocked"] = True
        enriched["promotion_eligible"] = False
        enriched["paper_route_allocation_multiplier"] = 0.0
        enriched["paper_route_score_clamped"] = True
        enriched["paper_route_block_reasons"] = eligibility["blocker_reasons"]
    elif eligibility["assumption_penalty_applied"]:
        numeric_score = _eligibility_number(enriched, "score")
        if numeric_score is not None:
            enriched.setdefault("pre_route_eligibility_score", enriched["score"])
            enriched["score"] = round(
                numeric_score * float(eligibility["paper_score_multiplier"]),
                6,
            )
        route_allocation = float(eligibility["paper_score_multiplier"])
        existing_allocation = _eligibility_number(enriched, "paper_allocation_multiplier")
        enriched["paper_route_allocation_multiplier"] = route_allocation
        enriched["paper_allocation_multiplier"] = (
            min(existing_allocation, route_allocation)
            if existing_allocation is not None
            else route_allocation
        )
        enriched["paper_route_assumption_penalty_applied"] = True
    if (
        conditional_short_diagnostics.get("applies")
        and not eligibility["suppressed"]
        and not eligibility["assumption_penalty_applied"]
        and not enriched.get("conditional_short_execution_risk_downrank_applied")
    ):
        risk_multiplier = float(conditional_short_diagnostics["paper_rank_multiplier"])
        if risk_multiplier < 1.0 and _eligibility_number(enriched, "score") is not None:
            enriched.setdefault("score_before_conditional_short_execution_risk", enriched["score"])
            enriched["score"] = round(float(enriched["score"]) * risk_multiplier, 6)
            enriched["conditional_short_execution_risk_downrank_applied"] = True
            enriched["conditional_short_execution_risk_multiplier"] = risk_multiplier
    prior_context_gate = enriched.get("paper_context_cost_gate") or {}
    prior_context_multiplier = float(prior_context_gate.get("score_multiplier", 1.0) or 1.0)
    refresh_context_cost = bool(prior_context_gate) or enriched.get("trade_type") in {
        "global_proxy_momentum",
        "global_market_discovery_proxy",
        "global_proxy_shock_reversal",
        "perp_funding_basis",
    }
    if refresh_context_cost:
        enriched = annotate_paper_context_cost(enriched, settings, adjust_score=False)
    refreshed_context_gate = enriched.get("paper_context_cost_gate") or {}
    if refresh_context_cost and refreshed_context_gate.get("applicable") and enriched.get("score") is not None:
        refreshed_multiplier = float(refreshed_context_gate.get("score_multiplier", 1.0) or 1.0)
        enriched["score_before_route_context_cost"] = round(float(enriched["score"]), 6)
        enriched["score"] = round(
            float(enriched["score"]) * refreshed_multiplier / max(prior_context_multiplier, 0.000001),
            6,
        )
    return enriched


def _activatable_okx_paper_proxy(candidate: dict, settings: dict) -> dict | None:
    """Return the explicit OKX paper proxy that fully replaces a borrow blocker."""

    if str(settings.get("mode") or "paper").strip().lower() != "paper":
        return None
    if not _route_unblocker_enabled(settings):
        return None
    if candidate.get("paper_proxy_activated"):
        return None

    route = candidate.get("execution_route") or {}
    if not isinstance(route, dict) or route.get("route_status") != "conditional":
        return None
    direction = str(candidate.get("direction") or "").strip().lower()
    if direction not in {"long_perp_short_spot", "short_frontier_spot", "short_spot"}:
        return None

    missing = {str(value) for value in route.get("missing_permissions", []) if value}
    alternative = route.get("best_route_alternative") or {}
    if not isinstance(alternative, dict):
        return None
    replaced = {str(value) for value in alternative.get("replaces_blockers", []) if value}
    alternative_missing = {
        str(value)
        for field in ("missing_permissions", "missing_requirements", "route_blockers")
        for value in (alternative.get(field) or [])
        if value
    }
    if (
        missing != {"spot_borrow"}
        or not missing.issubset(replaced)
        or alternative_missing
        or alternative.get("status") != "paper_testable_proxy"
        or alternative.get("route_id") != OKX_DERIVATIVES_PAPER_ROUTE
        or alternative.get("execution_semantics") != PAPER_PROXY_EXECUTION_SEMANTICS
        or not settings.get("account_capabilities", {}).get("crypto_derivatives", False)
    ):
        return None
    return dict(alternative)


def _direct_candidate_signal_key(candidate: dict, feasibility: dict) -> str:
    """Mirror the persisted direct signal identity without importing storage."""

    status = str(feasibility.get("status") or "unknown")
    if candidate.get("strategy_lab_id"):
        parts = (
            "STRATEGY_LAB",
            candidate.get("strategy_lab_id"),
            candidate.get("venue", "unknown"),
            candidate.get("direction", "unknown"),
            status,
        )
    elif candidate.get("signal_lineage_key"):
        parts = (
            candidate.get("signal_lineage_key"),
            candidate.get("venue", "unknown"),
            candidate.get("direction", "unknown"),
            status,
        )
    else:
        parts = (
            candidate.get("venue", "unknown"),
            candidate.get("trade_type", "unknown"),
            candidate.get("direction", "unknown"),
            status,
        )
    return "|".join(str(value) for value in parts)


def activate_paper_proxy_candidate(candidate: dict, settings: dict) -> dict:
    """Replace one borrow-blocked direct attempt with its labeled paper proxy.

    The direct route remains attached as route-intelligence evidence.  Runtime
    scoring, policies, orders, and outcomes use a separate proxy identity so the
    simulated derivative result cannot update the short-spot family.
    """

    activated = dict(candidate)
    alternative = _activatable_okx_paper_proxy(activated, settings)
    if alternative is None:
        return activated

    route = activated.get("execution_route") or {}
    feasibility = dict(activated.get("execution_feasibility") or {})
    direct_eligibility = dict(activated.get("paper_route_eligibility") or {})
    source_signal_key = activated.get("signal_key")
    direct_signal_key = _direct_candidate_signal_key(activated, feasibility)
    proxy_signal_key = "|".join(
        ("PAPER_PROXY", OKX_DERIVATIVES_PAPER_ROUTE, direct_signal_key)
    )
    allocation_multiplier = max(
        0.0,
        min(1.0, float(alternative.get("paper_allocation_multiplier") or 0.25)),
    )
    direct_score = activated.get("pre_route_eligibility_score")
    if direct_score is None:
        direct_score = activated.get("score", 0.0)

    proxy_eligibility = dict(direct_eligibility)
    proxy_eligibility.update(
        {
            "paper_only": True,
            "route_decision": "executable_proxy",
            "feasibility_status": "feasible_for_paper_proxy",
            "execution_eligibility": "eligible",
            "route_eligible": True,
            "eligible_for_scoring": True,
            "suppressed": False,
            "proxy_used": True,
            "selected_proxy_id": OKX_DERIVATIVES_PAPER_ROUTE,
            "missing_prerequisites": [],
            "blocker_reasons": [],
            "blocking_reason": None,
            "route_status": "paper_testable_proxy",
            "candidate_status": "paper_proxy_active",
            "rank_contribution_cap": 1.0,
            "rank_contribution": round(max(0.0, float(direct_score)), 6),
            "assumption_penalty_applied": False,
            "paper_score_multiplier": 1.0,
            "execution_semantics": PAPER_PROXY_EXECUTION_SEMANTICS,
            "direct_missing_prerequisites": direct_eligibility.get("missing_prerequisites", []),
            "direct_blocker_reasons": direct_eligibility.get("blocker_reasons", []),
        }
    )
    proxy_eligibility["capability_checks"] = [
        {
            **check,
            "critical": False,
            "replaced_by_proxy": OKX_DERIVATIVES_PAPER_ROUTE,
        }
        if isinstance(check, dict)
        else check
        for check in direct_eligibility.get("capability_checks", [])
    ]

    feasibility.update(
        {
            "paper_route_eligibility": proxy_eligibility,
            "paper_feasibility_status": "feasible_for_paper_proxy",
            "execution_eligibility": "eligible",
            "paper_score_multiplier": 1.0,
            "route_intelligence_status": "paper_testable_proxy",
            "candidate_status": "paper_proxy_active",
            "blocking_reason": None,
            "route_feasibility_score": max(
                PAPER_PROXY_ROUTE_FEASIBILITY_SCORE,
                float(feasibility.get("route_feasibility_score") or 0.0),
            ),
        }
    )

    activated.update(
        {
            "score": float(direct_score),
            "signal_key": proxy_signal_key,
            "direct_signal_key": direct_signal_key,
            "direct_source_signal_key": source_signal_key,
            "signal_stats_scope": PAPER_PROXY_STATS_SCOPE,
            "paper_proxy_stats_isolated": True,
            "paper_proxy_activated": True,
            "proxy_replaces_direct_candidate": True,
            "paper_only": True,
            "paper_proxy_used": True,
            "paper_proxy_route": alternative,
            "paper_proxy_source_route_id": route.get("route_id"),
            "paper_proxy_source_route_status": route.get("route_status"),
            "paper_proxy_direct_missing_requirements": list(route.get("missing_permissions", [])),
            "paper_route_status": "paper_testable_proxy",
            "paper_route_type": "proxy",
            "paper_execution_semantics": PAPER_PROXY_EXECUTION_SEMANTICS,
            "execution_semantics": PAPER_PROXY_EXECUTION_SEMANTICS,
            "proxy_not_live_equivalent": True,
            "paper_proxy_not_live_equivalent": True,
            "paper_fill_allowed_by_route": True,
            "paper_allocation_multiplier": allocation_multiplier,
            "paper_route_allocation_multiplier": allocation_multiplier,
            "route_feasibility_score": feasibility["route_feasibility_score"],
            "paper_route_eligibility": proxy_eligibility,
            "paper_feasibility_status": "feasible_for_paper_proxy",
            "execution_eligibility": "eligible",
            "route_intelligence_status": "paper_testable_proxy",
            "candidate_status": "paper_proxy_active",
            "blocking_reason": None,
            "paper_entry_blocked": False,
            "promotion_eligible": False,
            "direct_route_promotion_eligible": False,
            "execution_feasibility": feasibility,
            "direct_paper_route_eligibility": direct_eligibility,
        }
    )
    activated.pop("paper_route_score_clamped", None)
    activated.pop("paper_route_block_reasons", None)
    return activated


def activate_paper_proxy_candidates(candidates: Iterable[dict], settings: dict) -> list[dict]:
    """Activate eligible alternatives one-for-one without appending duplicates."""

    return [activate_paper_proxy_candidate(candidate, settings) for candidate in candidates]


def enrich_candidates(candidates: Iterable[dict], settings: dict) -> list[dict]:
    registry = load_route_registry()
    venue_capability_registry = load_venue_capability_registry()
    enriched = [
        enrich_candidate_with_route(
            candidate,
            settings,
            registry=registry,
            venue_capability_registry=venue_capability_registry,
        )
        for candidate in candidates
    ]
    activated = activate_paper_proxy_candidates(enriched, settings)
    # Context attribution is deliberately a paper-review ordering only.  It
    # keeps every candidate available for exploration while finite review
    # capacity favors transportable net edge over a gross-alpha illusion.
    if str(settings.get("mode") or "paper").strip().lower() == "paper":
        return rank_paper_candidates_by_context(activated, settings)
    return activated


def _requirement_counter_to_dict(counter: collections.Counter[tuple[str, str]]) -> dict:
    output: dict[str, dict[str, int]] = {}
    for (key, status), count in counter.items():
        output.setdefault(key, {})[status] = count
    return output


def _ranked_manual_actions(actions: dict[tuple[str, str], dict]) -> list[dict]:
    ranked = []
    for (_, action), item in actions.items():
        routes = sorted(item["routes"])
        statuses = sorted(item["statuses"])
        unlock_score = round(item["count"] * 5 + item["max_candidate_score"] + item["hard_count"] * 3, 3)
        ranked.append(
            {
                "requirement_id": item["requirement_id"],
                "category": item["category"],
                "suggested_action": action,
                "count": item["count"],
                "hard_count": item["hard_count"],
                "max_candidate_score": round(item["max_candidate_score"], 3),
                "affected_routes": routes,
                "route_statuses": statuses,
                "unlock_score": unlock_score,
            }
        )
    ranked.sort(key=lambda item: (item["unlock_score"], item["count"], item["max_candidate_score"]), reverse=True)
    return ranked[:20]


def summarize_routes(candidates: Iterable[dict]) -> dict:
    by_status: collections.Counter[str] = collections.Counter()
    by_route: collections.Counter[str] = collections.Counter()
    by_missing: collections.Counter[str] = collections.Counter()
    by_requirement_category: collections.Counter[tuple[str, str]] = collections.Counter()
    by_requirement_id: collections.Counter[tuple[str, str]] = collections.Counter()
    by_alternative_status: collections.Counter[str] = collections.Counter()
    by_eligibility_decision: collections.Counter[str] = collections.Counter()
    by_eligibility_blocker: collections.Counter[str] = collections.Counter()
    eligibility_blocked_samples: list[dict] = []
    manual_actions: dict[tuple[str, str], dict] = {}
    samples = {"conditional": [], "route_unknown": [], "blocked": [], "standard": []}
    total = 0
    for candidate in candidates:
        total += 1
        route = candidate.get("execution_route") or {}
        status = route.get("route_status") or candidate.get("route_status") or "unknown"
        route_id = route.get("route_id") or candidate.get("route_id") or "unknown"
        by_status[status] += 1
        by_route[route_id] += 1
        eligibility = candidate.get("paper_route_eligibility") or route.get("paper_route_eligibility") or {}
        if eligibility:
            by_eligibility_decision[str(eligibility.get("route_decision") or "unknown")] += 1
            for reason in eligibility.get("blocker_reasons", []) or []:
                by_eligibility_blocker[str(reason)] += 1
            if eligibility.get("suppressed") and len(eligibility_blocked_samples) < 20:
                eligibility_blocked_samples.append(
                    {
                        "inst_id": candidate.get("inst_id"),
                        "venue": candidate.get("venue"),
                        "direction": candidate.get("direction"),
                        "pre_gate_score": candidate.get("pre_route_eligibility_score"),
                        "missing_prerequisites": eligibility.get("missing_prerequisites", []),
                        "blocker_reasons": eligibility.get("blocker_reasons", []),
                    }
                )
        for requirement in route.get("requirements", []) or []:
            req_id = str(requirement.get("requirement_id") or "unknown")
            req_status = str(requirement.get("status") or "unknown")
            category = str(requirement.get("category") or "unknown")
            by_requirement_category[(category, req_status)] += 1
            by_requirement_id[(req_id, req_status)] += 1
        for missing in route.get("missing_permissions", []) or []:
            by_missing[missing] += 1
        for alternative in route.get("route_alternatives", []) or []:
            by_alternative_status[str(alternative.get("status") or "unknown")] += 1
        blockers = _hard_requirement_blockers(route.get("requirements", []) or [])
        for blocker in blockers:
            action = str(blocker.get("how_to_verify") or blocker.get("description") or blocker.get("requirement_id"))
            key = (str(blocker.get("requirement_id")), action)
            item = manual_actions.setdefault(
                key,
                {
                    "requirement_id": str(blocker.get("requirement_id")),
                    "category": str(blocker.get("category") or "unknown"),
                    "count": 0,
                    "hard_count": 0,
                    "max_candidate_score": 0.0,
                    "routes": set(),
                    "statuses": set(),
                },
            )
            item["count"] += 1
            item["hard_count"] += 1 if blocker.get("blocking_level") in HARD_BLOCKING_LEVELS else 0
            try:
                score = float(candidate.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            item["max_candidate_score"] = max(float(item["max_candidate_score"]), score)
            item["routes"].add(route_id)
            item["statuses"].add(status)
        if status in samples and len(samples[status]) < 10:
            samples[status].append(
                {
                    "inst_id": candidate.get("inst_id"),
                    "venue": candidate.get("venue"),
                    "direction": candidate.get("direction"),
                    "score": candidate.get("score"),
                    "route_id": route_id,
                    "missing_permissions": route.get("missing_permissions", []),
                    "route_next_actions": route.get("route_next_actions", [])[:3],
                    "route_blockers": route.get("route_blockers", [])[:3],
                    "best_route_alternative": route.get("best_route_alternative"),
                    "route_notes": route.get("route_notes", [])[:3],
                    "route_probe_priority": route.get("route_probe_priority"),
                }
            )
    return {
        "total_candidates": total,
        "by_route_status": dict(by_status),
        "by_route_id": dict(by_route),
        "by_missing_requirement": dict(by_missing),
        "by_requirement_category": _requirement_counter_to_dict(by_requirement_category),
        "by_requirement_id": _requirement_counter_to_dict(by_requirement_id),
        "by_route_alternative_status": dict(by_alternative_status),
        "by_paper_route_eligibility": dict(by_eligibility_decision),
        "by_paper_route_eligibility_blocker": dict(by_eligibility_blocker),
        "paper_route_eligibility_blocked_samples": eligibility_blocked_samples,
        "paper_proxy_available_count": int(by_alternative_status.get("paper_testable_proxy", 0)),
        "paper_research_available_count": int(by_alternative_status.get("paper_testable_research", 0)),
        "top_manual_actions": _ranked_manual_actions(manual_actions),
        "samples": samples,
    }


def summarize_route_intelligence(candidates: Iterable[dict], min_interesting_score: float = 35.0) -> dict:
    blocker_counts: collections.Counter[str] = collections.Counter()
    blocker_by_surface: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    spot_borrow_assets: collections.Counter[str] = collections.Counter()
    interesting_blocked = []
    potentially_executable_soon = []
    proxy_testable = []
    research_testable = []
    for candidate in candidates:
        route = candidate.get("execution_route") or {}
        score = float(
            candidate.get("pre_route_eligibility_score", candidate.get("score")) or 0.0
        )
        status = route.get("route_status") or candidate.get("route_status") or "unknown"
        route_id = route.get("route_id") or candidate.get("route_id") or "unknown"
        missing = list(route.get("missing_permissions", []) or [])
        best_alternative = route.get("best_route_alternative") or {}
        if best_alternative.get("status") == "paper_testable_proxy":
            proxy_testable.append(candidate)
        elif best_alternative.get("status") == "paper_testable_research":
            research_testable.append(candidate)
        surface = str(candidate.get("trade_type") or candidate.get("asset_class") or "unknown")
        for requirement in missing:
            blocker_counts[requirement] += 1
            blocker_by_surface[requirement][surface] += 1
        if "spot_borrow" in missing:
            asset = str(candidate.get("base") or candidate.get("inst_id") or "unknown")
            spot_borrow_assets[asset] += 1
        if missing and score >= min_interesting_score:
            row = {
                "inst_id": candidate.get("inst_id"),
                "venue": candidate.get("venue"),
                "direction": candidate.get("direction"),
                "trade_type": candidate.get("trade_type"),
                "score": round(score, 3),
                "paper_edge_bps": _candidate_edge_bps(candidate),
                "route_id": route_id,
                "route_status": status,
                "missing_requirements": missing,
                "best_route_alternative": best_alternative,
                "next_actions": route.get("route_next_actions", [])[:3],
            }
            interesting_blocked.append(row)
            if status == "conditional" and set(missing).issubset(
                {"spot_borrow", "prediction_markets_account", "venue_api_access", "jurisdiction_eligibility"}
            ):
                potentially_executable_soon.append(row)
    interesting_blocked.sort(key=lambda row: row["score"], reverse=True)
    potentially_executable_soon.sort(key=lambda row: row["score"], reverse=True)
    decision_pack = _build_route_decision_pack(interesting_blocked, blocker_counts)
    return {
        "generated_at": _utc_now(),
        "read_only": True,
        "min_interesting_score": min_interesting_score,
        "blocker_counts": dict(blocker_counts),
        "blocker_by_surface": {key: dict(value) for key, value in blocker_by_surface.items()},
        "spot_borrow_assets": dict(spot_borrow_assets.most_common(25)),
        "interesting_but_not_executable_count": len(interesting_blocked),
        "potentially_executable_soon_count": len(potentially_executable_soon),
        "paper_proxy_available_count": len(proxy_testable),
        "paper_research_available_count": len(research_testable),
        "interesting_but_not_executable": interesting_blocked[:30],
        "potentially_executable_soon": potentially_executable_soon[:30],
        "paper_proxy_available": [
            {
                "inst_id": item.get("inst_id"),
                "venue": item.get("venue"),
                "direction": item.get("direction"),
                "score": item.get("score"),
                "alternative": (item.get("execution_route") or {}).get("best_route_alternative"),
            }
            for item in proxy_testable[:30]
        ],
        "paper_research_available": [
            {
                "inst_id": item.get("inst_id"),
                "venue": item.get("venue"),
                "direction": item.get("direction"),
                "score": item.get("score"),
                "alternative": (item.get("execution_route") or {}).get("best_route_alternative"),
            }
            for item in research_testable[:30]
        ],
        "route_decision_pack": decision_pack,
        "hard_limits": [
            "Read-only route intelligence.",
            "No account capability flags are changed.",
            "No broker/API/order action is performed.",
            "Live trading remains controlled by global live-trading gates.",
        ],
    }


def _build_route_decision_pack(interesting_blocked: list[dict], blocker_counts: collections.Counter[str]) -> dict:
    decision_blockers = [
        "spot_borrow",
        "prediction_markets_account",
        "venue_api_access",
        "jurisdiction_eligibility",
    ]
    pack = {}
    for blocker in decision_blockers:
        affected = [row for row in interesting_blocked if blocker in set(row.get("missing_requirements") or [])]
        edges = [_safe_float(row.get("paper_edge_bps")) for row in affected if row.get("paper_edge_bps") is not None]
        scores = [_safe_float(row.get("score")) for row in affected]
        pack[blocker] = {
            "affected_opportunity_count": int(blocker_counts.get(blocker, 0)),
            "top_markets": affected[:10],
            "estimated_paper_edge_range": _edge_range(edges, scores),
            "route_feasibility": _route_feasibility_for_blocker(blocker, affected),
            "required_manual_action": _manual_action_for_blocker(blocker),
            "risk_constraint_notes": _risk_notes_for_blocker(blocker),
            "do_nothing_consequence": _do_nothing_consequence(blocker),
            "shadow_testing_can_continue": True,
            "hard_limits": [
                "No credentials are added.",
                "No account capability is changed.",
                "No broker/order API is called.",
                "No jurisdiction assumption is made.",
            ],
        }
    return pack


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _candidate_edge_bps(candidate: dict) -> float | None:
    for key in ("net_carry_edge_bps", "depth_adjusted_edge_bps", "edge_bps_estimate", "expected_edge_bps"):
        if candidate.get(key) is not None:
            return _safe_float(candidate.get(key))
    return None


def _score_range(values: list[float]) -> dict:
    if not values:
        return {"min_score": None, "max_score": None, "note": "No current affected high-score paper opportunities."}
    return {
        "min_score": round(min(values), 3),
        "max_score": round(max(values), 3),
        "note": "Score is a paper opportunity score, not verified live PnL.",
    }


def _edge_range(edges: list[float], fallback_scores: list[float]) -> dict:
    if edges:
        return {
            "min_edge_bps": round(min(edges), 3),
            "max_edge_bps": round(max(edges), 3),
            "note": "Paper edge estimate before unresolved route/account constraints.",
        }
    return _score_range(fallback_scores)


def _route_feasibility_for_blocker(blocker: str, affected: list[dict]) -> str:
    if not affected:
        return "no_current_high_score_surface"
    if blocker == "spot_borrow":
        return "potentially_executable_after_borrow_or_margin_route_confirmation"
    if blocker in {"prediction_markets_account", "venue_api_access", "jurisdiction_eligibility"}:
        return "potentially_executable_after_account_api_and_jurisdiction_review"
    return "manual_review_required"


def _manual_action_for_blocker(blocker: str) -> str:
    actions = {
        "spot_borrow": "Decide whether to open/verify a margin or borrow-capable route for the affected spot assets; then manually confirm supported instruments, borrow fees, limits, and route constraints.",
        "prediction_markets_account": "Decide whether to open/verify a prediction-market account route before any execution work.",
        "venue_api_access": "Decide whether to obtain read/trade API access for the venue after account and eligibility checks.",
        "jurisdiction_eligibility": "Verify legal/jurisdiction eligibility for the user and venue before any prediction-market execution route.",
    }
    return actions.get(blocker, "Manual route review required.")


def _risk_notes_for_blocker(blocker: str) -> list[str]:
    notes = {
        "spot_borrow": [
            "Borrow availability and fees can change intraday.",
            "Hard-to-borrow assets can erase apparent short-spot edge.",
            "The system must keep unresolved short-spot candidates shadow-only.",
        ],
        "prediction_markets_account": [
            "Event-contract rules and settlement mechanics can dominate apparent edge.",
            "Account approval and product eligibility are user-specific.",
        ],
        "venue_api_access": [
            "API permissions must be scoped and reviewed manually.",
            "Public-data research does not imply order-route availability.",
        ],
        "jurisdiction_eligibility": [
            "Prediction-market eligibility is jurisdiction-sensitive.",
            "The system cannot infer or self-certify user eligibility.",
        ],
    }
    return notes.get(blocker, ["Manual review required before any live route."])


def _do_nothing_consequence(blocker: str) -> str:
    consequences = {
        "spot_borrow": "Short-spot ideas remain research/shadow or conditional paper only; long-only and perp routes can continue where independently feasible.",
        "prediction_markets_account": "Prediction-market candidates remain conditional research items with no executable paper-to-live path.",
        "venue_api_access": "The system can keep observing public data but cannot validate execution-specific constraints.",
        "jurisdiction_eligibility": "Prediction-market ideas remain blocked from execution route activation.",
    }
    return consequences.get(blocker, "The system continues shadow testing without route activation.")


def _route_requirements_intel_markdown(requirements_intel: dict) -> list[str]:
    """Render the pre-promotion constraint block for paper reports only."""

    promotion_review = requirements_intel.get("promotion_review") or {}
    fields = (
        "venue",
        "inst_id",
        "direction",
        "required_permissions",
        "borrow_required",
        "borrow_fee_bps_estimate_or_unknown",
        "fee_bps_per_side_or_unknown",
        "margin_required",
        "endpoint_constraints",
        "venue_api_requirement",
        "paper_recommendation_action",
    )
    lines = [
        "## Pre-Promotion Route Requirements Intel",
        "",
        "Read-only paper-report block. It does not collect credentials, probe private endpoints, change route permissions, or promote a route.",
        (
            "- Review required before route promotion: "
            f"`{promotion_review.get('required_before_route_promotion', False)}`"
        ),
        f"- Rule: {promotion_review.get('rule', 'unknown')}",
        "",
        "| " + " | ".join(fields) + " |",
        "| " + " | ".join("---" for _ in fields) + " |",
    ]
    routes = requirements_intel.get("routes") or []
    if not routes:
        lines.append("| " + " | ".join("unknown" for _ in fields) + " |")
    for route in routes:
        lines.append(
            "| "
            + " | ".join(_report_markdown_value(route.get(field, "unknown")) for field in fields)
            + " |"
        )
    return lines


def _report_markdown_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        value = ", ".join(str(item) for item in value) if value else "unknown"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _route_intelligence_markdown(report: dict, requirements_intel: dict | None = None) -> str:
    lines = [
        "# Route Intelligence Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Read only: `{report.get('read_only')}`",
        f"- Interesting but not executable: `{report.get('interesting_but_not_executable_count', 0)}`",
        f"- Potentially executable soon: `{report.get('potentially_executable_soon_count', 0)}`",
        f"- Paper proxy available: `{report.get('paper_proxy_available_count', 0)}`",
        f"- Paper research available: `{report.get('paper_research_available_count', 0)}`",
        "",
        "## Blocker Counts",
        "",
    ]
    blockers = report.get("blocker_counts", {})
    if not blockers:
        lines.append("No route blockers in the considered candidate set.")
    for blocker, count in sorted(blockers.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- `{blocker}`: `{count}` surfaces={report.get('blocker_by_surface', {}).get(blocker, {})}")
    lines.extend(["", "## Spot Borrow Assets", ""])
    assets = report.get("spot_borrow_assets", {})
    if not assets:
        lines.append("No spot-borrow assets in this report.")
    for asset, count in list(assets.items())[:20]:
        lines.append(f"- `{asset}`: `{count}`")
    lines.extend(["", "## Potentially Executable Soon", ""])
    soon = report.get("potentially_executable_soon", [])
    if not soon:
        lines.append("No high-score conditional routes matched the soon-unlock criteria.")
    for row in soon[:20]:
        lines.append(
            f"- `{row.get('inst_id')}` {row.get('direction')} score=`{row.get('score')}` "
            f"missing={row.get('missing_requirements')} route=`{row.get('route_id')}` "
            f"alt=`{(row.get('best_route_alternative') or {}).get('alternative_id')}`"
        )
    lines.extend(["", "## Human Route Decision Pack", ""])
    for blocker, item in report.get("route_decision_pack", {}).items():
        lines.append(
            f"- `{blocker}` affected=`{item.get('affected_opportunity_count')}` "
            f"feasibility=`{item.get('route_feasibility')}` shadow_can_continue=`{item.get('shadow_testing_can_continue')}`"
        )
        lines.append(f"  - manual action: {item.get('required_manual_action')}")
        lines.append(f"  - do nothing: {item.get('do_nothing_consequence')}")
    if requirements_intel is not None:
        lines.extend(["", *_route_requirements_intel_markdown(requirements_intel)])
    return "\n".join(lines) + "\n"


def write_route_resolver_report(candidates: list[dict], settings: dict, limit: int = 250) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    considered = candidates[:limit]
    summary = summarize_routes(considered)
    requirements_intel = build_route_requirements_report(considered)
    paper_context_ranking = (
        paper_context_cost_report(considered)
        if str(settings.get("mode") or "paper").strip().lower() == "paper"
        else {"paper_only": True, "enabled": False, "candidate_count": 0, "candidates": []}
    )
    report = {
        "generated_at": _utc_now(),
        "mode": settings.get("mode"),
        "live_trading_allowed": bool(settings.get("allow_live_trading", False)),
        "summary": summary,
        "route_intelligence": summarize_route_intelligence(considered),
        "route_requirements_intel": requirements_intel,
        # Context evidence only orders paper review.  It is deliberately kept
        # separate from route status so weak transport assumptions remain
        # observable candidates rather than becoming a new route block.
        "paper_context_ranking": paper_context_ranking,
        "routes_registry": str(CUSTOM_ROUTES_PATH if CUSTOM_ROUTES_PATH.exists() else EXAMPLE_ROUTES_PATH),
        "hard_limits": [
            "Read-only resolver; no broker API actions are performed.",
            "Conditional and route_unknown ideas remain paper-only.",
            "Live execution is still blocked unless the global live-trading gates are explicitly enabled elsewhere.",
        ],
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")
    intelligence_sidecar = {
        **report["route_intelligence"],
        "route_requirements_intel": requirements_intel,
    }
    ROUTE_INTELLIGENCE_JSON.write_text(json.dumps(intelligence_sidecar, indent=2), encoding="utf-8")
    ROUTE_INTELLIGENCE_MD.write_text(
        _route_intelligence_markdown(report["route_intelligence"], requirements_intel),
        encoding="utf-8",
    )
    return report


def _markdown(report: dict) -> str:
    summary = report.get("summary", {})
    lines = [
        "# Route Resolver Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Live trading allowed: `{report.get('live_trading_allowed')}`",
        f"- Candidates considered: `{summary.get('total_candidates', 0)}`",
        "",
        "## Status Counts",
        "",
    ]
    for status, count in sorted(summary.get("by_route_status", {}).items(), key=lambda item: item[0]):
        lines.append(f"- `{status}`: `{count}`")
    lines.extend(["", "## Route Counts", ""])
    for route_id, count in sorted(summary.get("by_route_id", {}).items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- `{route_id}`: `{count}`")
    lines.extend(["", "## Missing Requirements", ""])
    missing = summary.get("by_missing_requirement", {})
    if not missing:
        lines.append("No missing requirements in the considered candidate set.")
    for item, count in sorted(missing.items(), key=lambda row: row[1], reverse=True):
        lines.append(f"- `{item}`: `{count}`")
    lines.extend(["", "## Paper Route Eligibility", ""])
    eligibility = summary.get("by_paper_route_eligibility", {})
    if not eligibility:
        lines.append("No candidate-level route eligibility checks were recorded.")
    for decision, count in sorted(eligibility.items()):
        lines.append(f"- `{decision}`: `{count}`")
    blockers = summary.get("by_paper_route_eligibility_blocker", {})
    for reason, count in sorted(blockers.items(), key=lambda row: row[1], reverse=True):
        lines.append(f"- blocker `{reason}`: `{count}`")
    lines.extend(["", "## Alternative Paper Routes", ""])
    alternatives = summary.get("by_route_alternative_status", {})
    if not alternatives:
        lines.append("No alternative paper routes attached.")
    for status, count in sorted(alternatives.items(), key=lambda row: row[0]):
        lines.append(f"- `{status}`: `{count}`")
    lines.extend(["", "## Requirement Categories", ""])
    categories = summary.get("by_requirement_category", {})
    if not categories:
        lines.append("No route requirements were attached.")
    for category, statuses in sorted(categories.items()):
        status_bits = ", ".join(f"{status}={count}" for status, count in sorted(statuses.items()))
        lines.append(f"- `{category}`: {status_bits}")
    lines.extend(["", "## Requirement IDs", ""])
    req_ids = summary.get("by_requirement_id", {})
    if not req_ids:
        lines.append("No route requirement IDs were attached.")
    for req_id, statuses in sorted(req_ids.items()):
        status_bits = ", ".join(f"{status}={count}" for status, count in sorted(statuses.items()))
        lines.append(f"- `{req_id}`: {status_bits}")
    lines.extend(["", "## Ranked Manual Actions", ""])
    manual_actions = summary.get("top_manual_actions", [])
    if not manual_actions:
        lines.append("No manual route actions required in the considered candidate set.")
    for action in manual_actions:
        lines.append(
            f"- score=`{action.get('unlock_score')}` count=`{action.get('count')}` "
            f"requirement=`{action.get('requirement_id')}` routes={action.get('affected_routes')}: "
            f"{action.get('suggested_action')}"
        )
    intelligence = report.get("route_intelligence", {})
    lines.extend(["", "## Route Intelligence", ""])
    lines.append(f"- Interesting but not executable: `{intelligence.get('interesting_but_not_executable_count', 0)}`")
    lines.append(f"- Potentially executable soon: `{intelligence.get('potentially_executable_soon_count', 0)}`")
    lines.append(f"- Paper proxy available: `{intelligence.get('paper_proxy_available_count', 0)}`")
    lines.append(f"- Paper research available: `{intelligence.get('paper_research_available_count', 0)}`")
    lines.append(f"- Blockers: `{intelligence.get('blocker_counts', {})}`")
    lines.append(f"- Spot-borrow assets: `{intelligence.get('spot_borrow_assets', {})}`")
    lines.append(f"- Human route decision pack: `{intelligence.get('route_decision_pack', {})}`")
    context_ranking = report.get("paper_context_ranking", {})
    lines.extend(["", "## Paper Context Ranking", ""])
    lines.append(f"- Paper candidates attributed: `{context_ranking.get('candidate_count', 0)}`")
    lines.append(
        f"- Ranking reasons: `{context_ranking.get('by_context_ranking_reason', {})}`"
    )
    for candidate in context_ranking.get("candidates", [])[:10]:
        reasons = candidate.get("context_ranking_reasons") or []
        if not reasons:
            continue
        lines.append(
            f"- `{candidate.get('inst_id')}` context-net="
            f"`{candidate.get('context_adjusted_expected_net_edge_bps')}` "
            f"reasons={reasons}"
        )
    lines.extend(["", *_route_requirements_intel_markdown(report.get("route_requirements_intel", {}))])
    for status in ("conditional", "route_unknown", "blocked", "standard"):
        lines.extend(["", f"## {status.replace('_', ' ').title()} Samples", ""])
        samples = summary.get("samples", {}).get(status, [])
        if not samples:
            lines.append("No samples.")
            continue
        for sample in samples:
            lines.append(
                f"- `{sample.get('inst_id')}` {sample.get('direction')} via `{sample.get('route_id')}` "
                f"missing={sample.get('missing_permissions')} priority=`{sample.get('route_probe_priority')}`"
            )
            for action in sample.get("route_next_actions", []):
                lines.append(f"  - next: {action}")
    return "\n".join(lines) + "\n"
